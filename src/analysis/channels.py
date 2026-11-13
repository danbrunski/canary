"""One reader over the three storage conventions a session uses.

The same physical quantity is stored three different ways, and each one bit at
least once during the 2026-08 tuning campaign:

* ``obd:`` — ``value_number``, already in engineering units.
* ``did:`` — the Mode-22 response payload as **hex text in ``value_text``**,
  with ``value_number`` empty. Analysis that reached for ``value_number``
  silently got nothing.
* ``decoded:`` — ``value_number`` holding the **raw integer**, deliberately
  unscaled so training's scaler stays comparable across sessions (see
  :mod:`src.decode.frame_decoder`). ``decoded:ECM_RPM`` reads ~9700 at
  1200 rpm; taking it at face value looks like a factor-of-8 bug and was
  first diagnosed as one.
* ``can:`` — raw frame bytes in ``payload``, needing a binding to slice.
* ``uprev:`` — imported UpRev K-line logs (``tools/import_uprev_log.py``),
  stored numeric and already in engineering units like ``obd:``. Kept as its
  own namespace because it is a *different reader and conversion* (another
  logger, another clock), not necessarily a different sensor: UpRev's AFR and
  the ECM's own λ (PID 0x24) are one A/F sensor converted two ways -- UpRev
  skips the ECM's barometric gain (B06.7) -- and their 3.8 % disagreement is
  exactly what must not be blurred by filing both under one name.
* ``mem:`` — service-0x23 (ReadMemoryByAddress) channels, numeric and
  already in engineering units. Its own namespace rather than ``obd:``
  because the trust differs: a DID number is a contract the ECU keeps across
  calibrations, while a RAM address is an artifact of one ROM build, and a
  reader has to be able to tell which kind of channel it is holding. The
  companion ``mem:<NAME>_SENTINEL`` channel carries the declared
  no-measurement states (fuel cut, for the commanded-phi word) as a sparse
  indicator: a row exists exactly where the ECU reported one, so "commanding
  nothing" is a value you can pair against rather than a gap in the data.
* ``gps:`` — the GPS receiver's own fields (``speed``, ``heading``,
  ``altitude``, …), numeric and in engineering units. Also a separate
  instrument, and the only road-speed reference in a Mode 22-only tuning
  capture, where no Mode 01 PID is being polled at all.

This module is the single place that knows all four, so callers ask for
``obd:RPM`` or ``did:ECM_AF_SENSOR_V_B1`` and get numbers they can compare.

Scaling always routes through :func:`src.decode.bindings.apply_scaling` and
:func:`src.decode.dids.apply_scaling` rather than re-implementing the
arithmetic — an unpinned ``scale: null`` binding must keep yielding raw
counts with ``unit="counts"``, not a silently invented engineering value.

See ``docs/roadmap/archive/tuning-analysis-toolkit.md`` Phase 1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pandas as pd

from src.analysis.channels_align import (
    AS_OF_ATTR,
    _as_of_validity,
    align_long,
    as_of_refusal,
    as_of_validity,
    concat_channel_frames,
    refuse_as_of,
)
from src.analysis.channels_plan import (
    _EQUIV_RATIO_CHANNEL,
    ACQUIRED_AT_COLUMN,
    GENERATION_COLUMN,
    ROUND_ID_COLUMN,
    ROUND_SKEW_COLUMNS,
    SPEC_SOURCES,
    TIMESTAMP_BASIS_COLUMN,
    ChannelSpec,
    ChannelSpecError,
    SharedSessionRead,
    SpecSource,
    _Catalogs,
    _long_frame,
    _narrowing,
    _raw_read_filters,
    _read_columns,
    channel_read_columns,
    parse_spec,
    raw_read_filters,
    read_acquisition_frame,
    read_spec_rows,
    round_skew,
    shared_session_read,
)
from src.collection.kline.names import is_legacy
from src.decode import bindings as bindings_decode
from src.decode import dids as dids_decode
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.signals.afr import AfrCalibration
from src.telemetry import DECODED_SOURCE, SOURCE_SEP

#: Moved to :mod:`~src.analysis.channels_plan` and :mod:`~src.analysis.channels_align`
#: (R26.8) and re-exported: every caller still imports them from here.
__all__ = [
    "ACQUIRED_AT_COLUMN",
    "AS_OF_ATTR",
    "GENERATION_COLUMN",
    "ROUND_ID_COLUMN",
    "ROUND_SKEW_COLUMNS",
    "SPEC_SOURCES",
    "TIMESTAMP_BASIS_COLUMN",
    "ChannelSpec",
    "ChannelSpecError",
    "SharedSessionRead",
    "SpecSource",
    "align_long",
    "as_of_refusal",
    "as_of_validity",
    "channel_read_columns",
    "concat_channel_frames",
    "parse_spec",
    "raw_read_filters",
    "read_acquisition_frame",
    "read_spec_rows",
    "refuse_as_of",
    "round_skew",
    "shared_session_read",
]


_RAW_UNIT = "counts"


def _numeric_series(frame: pd.DataFrame, spec: ChannelSpec, source: str) -> pd.DataFrame:
    """``obd:``/``uprev:``/``gps:`` — engineering units already, select and coerce.

    Matched case-insensitively rather than upper-cased: OBD and UpRev channels
    are stored upper-case but the GPS receiver's are lower-case (``speed``),
    and forcing one convention silently returned an empty frame for the other.
    """
    channel = frame["channel"].astype("string").str.casefold()
    rows = frame[(frame["source"] == source) & (channel == spec.name.casefold())]
    out = rows.loc[:, ["timestamp"]].copy()
    out["value"] = pd.to_numeric(rows["value_number"], errors="coerce")
    out["unit"] = rows["unit"].astype("string").fillna("")
    return out.dropna(subset=["value"])


def _kline_series(frame: pd.DataFrame, spec: ChannelSpec) -> pd.DataFrame:
    """``kline:<module>:<item>`` — K-line values, as raw counts.

    Stored like DID rows: the value is hex in ``value_text`` and
    ``value_number`` is empty, so a numeric select returned nothing. Decoded
    here as the unsigned big-endian integer the bytes hold, unit ``counts``:
    engineering units are the vehicle's K-line profile bindings, applied by
    ``src.analysis.kline_exchanges``, not guessed here.

    A legacy name (``DID_1202``, stored before K01.1) is refused: it names no
    module, and the ABS's 0x1202 is not the ECM's.
    """

    if is_legacy(spec.name):
        raise ChannelSpecError(
            f"{spec.text!r} is a pre-K01.1 K-line name with no module; recover the module "
            "with src.analysis.kline_exchanges.attribute_modules and ask for "
            "kline:<MODULE>:<hex>"
        )
    channel = frame["channel"].astype("string").str.casefold()
    rows = frame[(frame["source"] == "kline") & (channel == spec.name.casefold())]
    out = rows.loc[:, ["timestamp"]].copy()
    numeric = pd.to_numeric(rows["value_number"], errors="coerce")
    text = rows["value_text"].astype("string")
    counts = text.map(_hex_counts, na_action="ignore")
    out["value"] = numeric.where(numeric.notna(), pd.to_numeric(counts, errors="coerce"))
    out["unit"] = rows["unit"].astype("string").fillna("").where(numeric.notna(), _RAW_UNIT)
    return out.dropna(subset=["value"])


def _hex_counts(text: str) -> float | None:
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    return float(int.from_bytes(raw, "big")) if raw else None


def _memory_series(frame: pd.DataFrame, spec: ChannelSpec) -> pd.DataFrame:
    """``mem:`` — service-0x23 channels, scaled at collection.

    The collector applies the catalog's scale before the reading is stored
    (unlike ``did:``, whose payload is kept as hex), so this is a select and
    a coerce. The stored channel keeps its ``mem:`` prefix, which is what
    distinguishes it from a Mode 01 channel of the same name.

    A ``_SENTINEL`` row has no numeric value — it is a declared *state*, not
    a measurement, and scaling the fuel-cut word would put a phi of 0.0 into
    every mean that followed. It is surfaced as a 1.0 indicator with unit
    ``"flag"`` so the instants stay pairable while remaining impossible to
    mistake for a reading.
    """
    channel = frame["channel"].astype("string")
    rows = frame[(frame["source"] == "obd") & (channel == f"mem:{spec.name}")]
    out = rows.loc[:, ["timestamp"]].copy()
    numeric = pd.to_numeric(rows["value_number"], errors="coerce")
    text = rows["value_text"].astype("string")
    out["value"] = numeric.where(numeric.notna(), text.notna().map({True: 1.0, False: None}))
    out["unit"] = rows["unit"].astype("string").fillna("").where(numeric.notna(), "flag")
    return out.dropna(subset=["value"])


def _did_series(frame: pd.DataFrame, spec: ChannelSpec, catalogs: _Catalogs) -> pd.DataFrame:
    entry = catalogs.did(spec.name)
    channel = f"DID_{entry.did:04X}"
    # A legacy ``kline:DID_<hex>`` row is some K-line module's, not this
    # catalog's (the ABS's 0x1210 is yaw rate, the ECM's is TPS2).
    named = frame["channel"] == channel
    foreign = frame["source"].isin(dids_decode.FOREIGN_DID_SOURCES)
    rows = frame[named & ~foreign]
    if rows.empty and (named & foreign).any():
        # Say which kind of empty: the hex was read, but by some K-line module.
        raise ChannelSpecError(
            f"{spec.text!r}: this session's only {channel} rows are pre-K01.1 K-line rows, "
            "which name no module and are not this catalog's; recover their module with "
            "src.analysis.kline_exchanges.attribute_modules and ask for kline:<MODULE>:<hex>"
        )
    out = rows.loc[:, ["timestamp"]].copy()
    text = rows["value_text"].astype("string")

    def decode(hex_text: object) -> float | None:
        if not isinstance(hex_text, str) or not hex_text:
            return None
        try:
            payload = bytes.fromhex(hex_text)
        except ValueError:
            return None
        raw = dids_decode.decode_did_raw(entry, payload)
        if raw is None:
            return None
        scaled = dids_decode.apply_scaling(entry, raw)
        return float(raw) if scaled is None else float(scaled)

    out["value"] = [decode(v) for v in text]
    out["unit"] = entry.unit if entry.scale is not None else _RAW_UNIT
    return out.dropna(subset=["value"])


def _decoded_series(frame: pd.DataFrame, spec: ChannelSpec, catalogs: _Catalogs) -> pd.DataFrame:
    """``decoded:`` rows carry the RAW integer — scale it here, once."""
    signal = catalogs.signal(spec.name)
    rows = frame[(frame["source"] == "decoded") & (frame["channel"] == signal.name)]
    out = rows.loc[:, ["timestamp"]].copy()
    raw = pd.to_numeric(rows["value_number"], errors="coerce")
    factors = bindings_decode.scaling_factors(signal)
    if factors is None:
        out["value"] = raw
        out["unit"] = _RAW_UNIT
    else:
        scale, offset = factors
        out["value"] = raw * scale + offset
        out["unit"] = signal.unit
    return out.dropna(subset=["value"])


def _can_series(frame: pd.DataFrame, spec: ChannelSpec, catalogs: _Catalogs) -> pd.DataFrame:
    """Decode a bound signal straight from stored frame bytes.

    Named by *signal*, not by CAN id: one frame carries several signals, so
    ``can:0x1F9`` would not say which. Unbound payload archaeology stays in
    DuckDB (``hex(payload)``), deliberately — this reader's contract is
    engineering units, and an unbound byte range has none.
    """
    signal = catalogs.signal(spec.name)
    channel = f"0x{signal.can_id:03X}"
    rows = frame[(frame["source"] == "can") & (frame["channel"].str.upper() == channel.upper())]
    out = rows.loc[:, ["timestamp"]].copy()

    def decode(payload: object) -> float | None:
        if not isinstance(payload, (bytes, bytearray)):
            return None
        raw = bindings_decode.decode_raw(signal, bytes(payload))
        if raw is None:
            return None
        scaled = bindings_decode.apply_scaling(signal, raw)
        return float(raw) if scaled is None else float(scaled)

    out["value"] = [decode(p) for p in rows["payload"]]
    out["unit"] = signal.unit if signal.scale is not None else _RAW_UNIT
    return out.dropna(subset=["value"])


#: Derived spec: commanded AFR, reconstructed from PID 0x44 through the
#: vehicle's recorded phi/lambda convention (roadmap 1.3). Not a stored
#: channel — it exists only when the convention has been verified, because
#: guessing it wrong is a reciprocal error that hides at stoich.
COMMANDED_AFR_SPEC = "obd:COMMANDED_AFR"


def _commanded_afr_series(
    frame: pd.DataFrame,
    calibration: AfrCalibration | None,
) -> pd.DataFrame:
    if calibration is None:
        raise ChannelSpecError(
            f"{COMMANDED_AFR_SPEC} needs an AFR calibration; pass calibration= "
            "(the vehicle profile's afr_calibration.yaml): its convention says phi or "
            "lambda, and its afr_scale is the AFR the ECM states at lambda = 1 (M01.3)"
        )
    rows = frame[(frame["source"] == "obd") & (frame["channel"] == _EQUIV_RATIO_CHANNEL)]
    out = rows.loc[:, ["timestamp"]].copy()
    ratio = pd.to_numeric(rows["value_number"], errors="coerce")
    # Raises for an unverified convention — deliberately loud, once per call.
    # A ratio of 0.0 is decel fuel cut (no target at all) and converts to NaN,
    # which is dropped below; see AfrCalibration.commanded_afr_series.
    out["value"] = calibration.commanded_afr_series(ratio)
    out["unit"] = "AFR"
    return out.dropna(subset=["value"])


def read_channels(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    calibration: AfrCalibration | None = None,
    acquired_time: bool = False,
) -> pd.DataFrame:
    """Read ``specs`` from one session as a long frame in engineering units.

    Returns columns ``timestamp``, ``spec``, ``value``, ``unit`` — long rather
    than wide because each source has its own arrival cadence; putting them on
    a shared clock is :func:`read_aligned`'s job, and doing it implicitly here
    is exactly the stale-join trap (G.4) this module exists to close.

    ``unit`` is ``"counts"`` wherever the catalog's ``scale`` is unpinned, so a
    caller can tell "raw" from "engineering" without consulting the YAML.
    Set ``acquired_time=True`` for lag/cross-source work: OBD/DID rows then
    use their persisted per-reading arrival time rather than a logical round
    timestamp, while sources without that field retain their stored time. The
    frame then also carries a ``timestamp_basis`` column per row
    (:data:`src.signals.pairing.TimestampBasis`) saying which of those
    happened, so a comparison can refuse a tolerance the clock cannot carry.

    A broadcast spec read from a session that stores CAN frames change-only
    (P02.10) returns its stored rows — the changes and the re-writes — and
    names itself in ``attrs[AS_OF_ATTR]`` with how long each row stays valid;
    :func:`align_long` and ``compare.align_pairs`` apply that. A caller that
    counts these rows is counting payload changes, not frames.
    """
    catalogs = _Catalogs(bindings_path=bindings_path, dids_path=dids_path)
    parsed = [parse_spec(text) for text in specs]
    frame = _long_frame(
        session,
        acquired_time=acquired_time,
        filters=_raw_read_filters(parsed, catalogs) if parsed else None,
        columns=_read_columns(parsed, acquired_time=acquired_time),
        narrow=_narrowing(parsed, catalogs) if parsed else None,
    )

    readers = {
        "obd": lambda s: _numeric_series(frame, s, "obd"),
        "uprev": lambda s: _numeric_series(frame, s, "uprev"),
        "gps": lambda s: _numeric_series(frame, s, "gps"),
        "imu": lambda s: _numeric_series(frame, s, "imu"),
        "sensor": lambda s: _numeric_series(frame, s, "sensor"),
        "kline": lambda s: _kline_series(frame, s),
        "mem": lambda s: _memory_series(frame, s),
        "did": lambda s: _did_series(frame, s, catalogs),
        "decoded": lambda s: _decoded_series(frame, s, catalogs),
        "can": lambda s: _can_series(frame, s, catalogs),
    }

    columns = ["timestamp", "spec", "value", "unit"]
    if acquired_time:
        columns.append(TIMESTAMP_BASIS_COLUMN)
    parts: list[pd.DataFrame] = []
    for spec in parsed:
        if spec.source == "obd" and spec.name.upper() == "COMMANDED_AFR":
            part = _commanded_afr_series(frame, calibration)
        else:
            part = readers[spec.source](spec)
        part = part.assign(spec=spec.text)
        if acquired_time:
            part = part.assign(
                **{TIMESTAMP_BASIS_COLUMN: frame.loc[part.index, TIMESTAMP_BASIS_COLUMN]}
            )
        parts.append(part.loc[:, columns])

    as_of = _as_of_validity(frame, parsed)
    if not parts:
        empty = pd.DataFrame(columns=columns)
        if as_of:
            empty.attrs[AS_OF_ATTR] = as_of
        return empty
    out = pd.concat(parts, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.sort_values(["timestamp", "spec"], kind="stable").reset_index(drop=True)
    out.attrs.pop(AS_OF_ATTR, None)
    if as_of:
        out.attrs[AS_OF_ATTR] = as_of
    return out


def read_aligned(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    grid: str = "1s",
    require_all: bool = True,
    cell_agg: Literal["last", "mean"] = "last",
    max_staleness: pd.Timedelta | str | None = None,
    with_age: bool = False,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    calibration: AfrCalibration | None = None,
) -> pd.DataFrame:
    """Put ``specs`` on one clock, refusing stale joins by default.

    This is the cell-based *aligned frame* API — one row per ``grid`` cell,
    for band averages, gating and plotting. It is **not** the comparison
    contract: a cell holding a ``"last"`` sample from each channel does not
    say how far apart those samples were acquired, and a ``"mean"`` cell is
    a synthetic value that was never observed. Its output is therefore not
    pairing evidence; a claim that two channels agree goes through
    :func:`src.analysis.compare.compare_channels`, which pairs individual
    observations under an explicit :class:`~src.signals.pairing.PairingPolicy`.

    A row exists only where **every** requested channel has its own sample
    inside the grid cell (``require_all=True``). Nothing is forward-filled:
    the 2026-07 campaign recorded four "WOT AFR" samples that were one stale
    reading aged 24–35 s, paired in by an ASOF join and indistinguishable from
    measurements. Freshness is the default here so that cannot recur silently.

    ``max_staleness`` opts into carrying a value forward, and is explicit on
    purpose. When it is set — or ``with_age=True`` — the frame also carries a
    ``<spec>__age_s`` column per channel, so a caller can always see how old
    the number it is using actually is.

    ``cell_agg`` decides what a cell holding several samples reports.
    ``"last"`` (default) keeps a real sample taken at a known instant;
    ``"mean"`` averages, which is smoother but synthetic and can smear a
    transient. This is not cosmetic: on the 2026-08-05 capture the choice
    moved band-averaged delivery error from +0.58 % to +0.72 %, purely by
    reassigning boundary samples between rpm bands. It is a parameter rather
    than a hidden default for exactly that reason.
    """
    long = read_channels(
        session,
        specs,
        bindings_path=bindings_path,
        dids_path=dids_path,
        calibration=calibration,
    )
    return align_long(
        long,
        specs,
        grid=grid,
        require_all=require_all,
        cell_agg=cell_agg,
        max_staleness=max_staleness,
        with_age=with_age,
    )


def rescale_channel_rows(
    rows: list[dict[str, object]],
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    key_field: str = "channel_key",
    value_field: str = "value_number",
) -> list[dict[str, object]]:
    """Convert long ``source:channel`` rows in place to engineering units.

    The query layer reads ``value_number`` straight out of Parquet, which is
    correct for ``obd:`` and **wrong for ``decoded:``** — those rows hold raw
    integers by design, so a binned "trims by rpm" over ``decoded:ECM_RPM``
    silently bins on counts (×8 for rpm). This is the smallest fix that closes
    that on the remote/agent path without standing up a second query engine.

    ``did:`` channels are deliberately *not* handled here: their values live in
    ``value_text`` and never survive the query layer's numeric filter, so there
    is nothing to rescale. Use :func:`read_channels` for those.
    """
    catalogs = _Catalogs(bindings_path=bindings_path, dids_path=dids_path)
    factors: dict[str, tuple[float, float] | None] = {}

    for row in rows:
        key = str(row.get(key_field, ""))
        source, sep, name = key.partition(SOURCE_SEP)
        if not sep or source != DECODED_SOURCE:
            continue
        if key not in factors:
            try:
                signal = catalogs.signal(name)
            except ChannelSpecError:
                factors[key] = None
            else:
                factors[key] = bindings_decode.scaling_factors(signal)
        factor = factors[key]
        if factor is None:
            continue
        raw = row.get(value_field)
        if isinstance(raw, (int, float)):
            row[value_field] = float(raw) * factor[0] + factor[1]
    return rows
