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
  own namespace because it is a *different instrument* reading the same
  vehicle: the 3.8 % measured-AFR disagreement between UpRev's conversion and
  the ECM's own λ is exactly the kind of thing that must not be blurred by
  filing both under one name.
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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

import numpy as np
import pandas as pd

from src.analysis.afr import AfrCalibration
from src.control.capture_manifest import read_capture_manifest
from src.decode import bindings as bindings_decode
from src.decode import dids as dids_decode
from src.paths import BINDINGS_YAML, DIDS_YAML
from src.preprocessing.loader import read_session
from src.telemetry import DECODED_SOURCE, SOURCE_SEP

#: Namespaces a spec may name. ``did``/``can`` are decode routes rather than
#: literal ``source`` column values — a DID reading is stored with
#: ``source="obd"`` and channel ``DID_1225``.
SpecSource = Literal[
    "obd", "did", "decoded", "can", "uprev", "gps", "imu", "mem", "sensor", "kline"
]
#: The same names as a tuple, for validation and messages. Derived from the
#: Literal so the two cannot drift (they had: ``imu`` was a captured source
#: for months before a spec could name it).
SPEC_SOURCES: tuple[SpecSource, ...] = get_args(SpecSource)

_RAW_UNIT = "counts"


class ChannelSpecError(ValueError):
    """A spec that cannot be resolved against the loaded catalogs."""


@dataclass(frozen=True)
class ChannelSpec:
    """A parsed ``source:NAME`` request."""

    source: SpecSource
    name: str
    text: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.text


def parse_spec(text: str) -> ChannelSpec:
    """Parse ``"obd:RPM"`` / ``"did:0x1225"`` / ``"can:ECM_RPM"``.

    A bare name with no namespace is read as ``obd:`` — that is the namespace
    the rest of the codebase already defaults to (``_channel_satisfied`` in
    capture_profiles does the same), so the shorthand does not surprise.
    """
    raw = text.strip()
    if not raw:
        raise ChannelSpecError("empty channel spec")
    source, sep, name = raw.partition(":")
    if not sep:
        source, name = "obd", raw
    source = source.strip().lower()
    name = name.strip()
    if not name:
        raise ChannelSpecError(f"channel spec {text!r} has no channel name")
    if source not in SPEC_SOURCES:
        raise ChannelSpecError(
            f"unknown namespace {source!r} in {text!r}; expected {'/'.join(SPEC_SOURCES)}"
        )
    return ChannelSpec(source=source, name=name, text=raw)


@dataclass
class _Catalogs:
    """Lazily-loaded binding/DID catalogs, indexed for lookup by spec."""

    bindings_path: Path
    dids_path: Path
    _bindings: bindings_decode.Bindings | None = None
    _did_map: dids_decode.DidMap | None = None

    @property
    def bindings(self) -> bindings_decode.Bindings:
        if self._bindings is None:
            if not self.bindings_path.exists():
                raise ChannelSpecError(f"bindings file not found: {self.bindings_path}")
            self._bindings = bindings_decode.load_bindings(self.bindings_path)
        return self._bindings

    @property
    def did_map(self) -> dids_decode.DidMap:
        if self._did_map is None:
            if not self.dids_path.exists():
                raise ChannelSpecError(f"DID file not found: {self.dids_path}")
            self._did_map = dids_decode.load_did_map(self.dids_path)
        return self._did_map

    def signal(self, name: str) -> bindings_decode.Signal:
        wanted = name.upper()
        for signal in self.bindings.signals:
            if signal.name.upper() == wanted:
                return signal
        raise ChannelSpecError(f"no binding named {name!r} in {self.bindings_path}")

    def did(self, name: str) -> dids_decode.DidEntry:
        """Resolve a DID by name (``ECM_AF_SENSOR_V_B1``) or id (``0x1225``)."""
        wanted = name.upper().removeprefix("DID_")
        for entry in self.did_map.dids:
            if entry.name.upper() == wanted:
                return entry
        try:
            did_id = int(wanted, 16)
        except ValueError as exc:
            raise ChannelSpecError(f"no DID named {name!r} in {self.dids_path}") from exc
        for entry in self.did_map.dids:
            if entry.did == did_id:
                return entry
        raise ChannelSpecError(f"no DID 0x{did_id:04X} in {self.dids_path}")


#: Column added by :func:`read_channels` when ``acquired_time=True``; values
#: are :data:`src.analysis.pairing.TimestampBasis`.
TIMESTAMP_BASIS_COLUMN = "timestamp_basis"
#: Round identity columns :func:`read_acquisition_frame` adds from
#: ``metadata_json`` (``docs/capture-rounds.md``). Nullable: legacy rows and
#: broadcast frames carry neither.
GENERATION_COLUMN = "generation"
ROUND_ID_COLUMN = "round_id"
ACQUIRED_AT_COLUMN = "acquired_at"
#: Per-round skew columns from :func:`round_skew`.
ROUND_SKEW_COLUMNS = ("generation", "round_id", "n", "first_acquired_at", "skew_s")


#: The ``metadata_json`` keys this module reads. Parsed together, once.
_ROUND_IDENTITY_KEYS = ("acquired_at", "round_id", "generation")


def _metadata_payload(raw: object) -> dict[str, object] | None:
    """One row's ``metadata_json`` as a mapping, or ``None``.

    Every "no usable object here" case collapses to ``None``: a missing value
    (``None``/``NaN``), a non-string, text that is not JSON, and JSON that is
    not an object (``"3"``, ``"[1]"``, ``"null"`` — those used to arrive as an
    ``AttributeError`` from ``dict.get`` and are still not readings).
    """
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _payload_number(payload: dict[str, object], key: str) -> float | None:
    """``payload[key]`` as a float, or ``None`` unless it is a real number.

    ``bool`` is rejected deliberately: it is an ``int`` subclass, so a stray
    ``"acquired_at": true`` would otherwise read as 1.0 — 1970, and a plausible
    enough number to survive.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True)
class _MetadataFields:
    """``metadata_json``'s round identity as columns, parsed once per row.

    Six ``Series.map`` passes used to re-run ``json.loads`` over the whole
    column — 21.1 M parses for a 3.5 M-row capture, 115 s of a 122 s
    ``recompute_capture_quality`` (measured 2026-09-17). One parse fills all
    three columns and every consumer reads them from here.

    Float, not ``Int64``, because that is what the JSON gave: the public
    ``generation``/``round_id`` columns do the integer cast themselves.
    """

    acquired_at: pd.Series
    round_id: pd.Series
    generation: pd.Series


def _parse_candidates(raw: pd.Series) -> np.ndarray:
    """Row positions whose text could possibly carry a round-identity key.

    A JSON object can name ``acquired_at`` without containing that literal
    substring only by escaping a character (``{"acquired_a\\u0074": 1}``), and
    every JSON escape starts with a backslash. So "no key substring **and** no
    backslash" is a proof the keys are absent rather than a guess about how the
    writer formats things, and anything that fails the proof — including
    non-strings and unparseable text — is handed to the parser as before.

    This is the whole of the win: 94 % of a capture's rows are broadcast and
    decoded frames whose metadata is ``{"can_id": …, "dlc": …}``, and parsing
    them yielded nothing three times over.
    """
    text = raw.astype("string")
    candidate = text.str.contains("\\", regex=False)
    for key in _ROUND_IDENTITY_KEYS:
        candidate |= text.str.contains(key, regex=False)
    return candidate.fillna(True).to_numpy(dtype=bool)


def _metadata_fields(frame: pd.DataFrame) -> _MetadataFields | None:
    """Parse ``metadata_json`` once, into one column per round-identity key.

    ``None`` when the frame carries no ``metadata_json`` column at all — the
    single place that fact is decided, so "unknown" timestamp basis and
    all-null identity columns cannot disagree about it.
    """
    if "metadata_json" not in frame:
        return None
    raw = frame["metadata_json"]
    values = raw.to_numpy()
    columns = {key: np.full(len(values), np.nan) for key in _ROUND_IDENTITY_KEYS}
    for position in np.flatnonzero(_parse_candidates(raw)):
        payload = _metadata_payload(values[position])
        if payload is None:
            continue
        for key, column in columns.items():
            number = _payload_number(payload, key)
            if number is not None:
                column[position] = number
    return _MetadataFields(
        acquired_at=pd.Series(columns["acquired_at"], index=frame.index, dtype="float64"),
        round_id=pd.Series(columns["round_id"], index=frame.index, dtype="float64"),
        generation=pd.Series(columns["generation"], index=frame.index, dtype="float64"),
    )


def _round_stamped_sessions(frame: pd.DataFrame, session: Path | pd.DataFrame) -> set[str]:
    """Session paths whose capture manifest says OBD rows carry round stamps.

    Only manifest evidence counts. A stamp shared by several OBD channels is
    *not* evidence on its own: a batched Mode 01 response legitimately stamps
    every PID it carried with the one decode instant, and that is
    indistinguishable from a round stamp without ``acquired_at``.
    """
    candidates: set[str] = set()
    if isinstance(session, Path):
        candidates.add(str(session))
    if "session_path" in frame:
        candidates.update(str(path) for path in frame["session_path"].dropna().unique())
    round_stamped: set[str] = set()
    for path in candidates:
        manifest = read_capture_manifest(Path(path))
        if manifest is not None and bool(manifest.profile_snapshot.get("round_timestamps")):
            round_stamped.add(path)
    return round_stamped


def _timestamp_basis(
    frame: pd.DataFrame, session: Path | pd.DataFrame, fields: _MetadataFields | None
) -> pd.Series:
    """Per-row :data:`~src.analysis.pairing.TimestampBasis`.

    ``acquired`` whenever ``acquired_at`` is present, whatever else the row
    or manifest says. ``round`` needs either the manifest's
    ``round_timestamps`` or a ``round_id`` on the row itself (C02.6) — with
    no ``acquired_at`` beside it, a round-tagged stamp is a round stamp.

    ``fields`` is :func:`_metadata_fields`' already-parsed columns; ``None``
    means the frame has no ``metadata_json`` to judge by.
    """
    if fields is None:
        return pd.Series("unknown", index=frame.index, dtype="string")
    acquired = fields.acquired_at.notna()
    basis = pd.Series("stored", index=frame.index, dtype="string")
    basis = basis.mask(fields.round_id.notna(), "round")
    round_stamped = _round_stamped_sessions(frame, session)
    if round_stamped:
        # Only polled (OBD/DID) rows are ever round-stamped; broadcast frames
        # keep their arrival stamp whatever the profile says.
        in_round_session = (
            frame["session_path"].astype("string").isin(round_stamped)
            if "session_path" in frame
            else pd.Series(True, index=frame.index)
        )
        basis = basis.mask((frame["source"] == "obd") & in_round_session, "round")
    return basis.mask(acquired, "acquired")


def _long_frame(session: Path | pd.DataFrame, *, acquired_time: bool = False) -> pd.DataFrame:
    return _long_frame_with_metadata(session, acquired_time=acquired_time)[0]


def _long_frame_with_metadata(
    session: Path | pd.DataFrame, *, acquired_time: bool = False
) -> tuple[pd.DataFrame, _MetadataFields | None]:
    """:func:`_long_frame` plus the metadata columns it had to parse.

    Handed back rather than recomputed so :func:`read_acquisition_frame` reuses
    the one parse. ``None`` covers both "nothing was parsed" cases: without
    ``acquired_time`` no metadata is wanted (and no caller should pay the parse
    for it), and a frame with no ``metadata_json`` column has none to parse.
    """
    frame = session.copy() if isinstance(session, pd.DataFrame) else read_session(Path(session))
    if not acquired_time:
        return frame, None
    fields = _metadata_fields(frame)
    frame[TIMESTAMP_BASIS_COLUMN] = _timestamp_basis(frame, session, fields)
    if fields is None:
        return frame, None
    acquired = pd.to_datetime(fields.acquired_at, unit="s", utc=True)
    # Broadcast and legacy rows have no acquisition field; retain their stored
    # timestamp rather than dropping an otherwise valid cross-source series.
    frame["timestamp"] = acquired.fillna(pd.to_datetime(frame["timestamp"], utc=True))
    return frame, fields


def read_acquisition_frame(session: Path | pd.DataFrame) -> pd.DataFrame:
    """The raw session on its acquisition clock, with ``timestamp_basis`` per row.

    For consumers that decode rows themselves (the broadcast byte sweep) but
    must still pair on the same clock, and with the same provenance rules, as
    :func:`read_channels`. Also carries ``acquired_at`` (float Unix seconds),
    ``generation`` and ``round_id`` (nullable) so round membership is a column
    rather than a JSON parse; :func:`round_skew` summarises them.
    """
    frame, fields = _long_frame_with_metadata(session, acquired_time=True)
    if fields is None:
        for column in (ACQUIRED_AT_COLUMN, GENERATION_COLUMN, ROUND_ID_COLUMN):
            frame[column] = pd.Series(pd.NA, index=frame.index, dtype="Float64")
        return frame
    frame[ACQUIRED_AT_COLUMN] = fields.acquired_at.astype("Float64")
    frame[GENERATION_COLUMN] = fields.generation.astype("Float64").astype("Int64")
    frame[ROUND_ID_COLUMN] = fields.round_id.astype("Float64").astype("Int64")
    return frame


def round_skew(frame: pd.DataFrame) -> pd.DataFrame:
    """Measured acquisition spread per ``(generation, round_id)``.

    One row per round that has at least one round-tagged reading with an
    ``acquired_at``: ``n`` readings, ``first_acquired_at``, and ``skew_s`` =
    last minus first acquisition. This is the evidence a tuple check must
    consult (C03) — sharing a ``round_id`` says the scheduler *asked* for
    these together; ``skew_s`` says how far apart they actually arrived.
    Rows without round identity contribute nothing; an empty result means no
    round evidence was persisted, not that skew was zero.
    """
    frame = frame if ROUND_ID_COLUMN in frame else read_acquisition_frame(frame)
    tagged = frame.dropna(subset=[GENERATION_COLUMN, ROUND_ID_COLUMN, ACQUIRED_AT_COLUMN])
    if tagged.empty:
        return pd.DataFrame(columns=list(ROUND_SKEW_COLUMNS))
    grouped = tagged.groupby([GENERATION_COLUMN, ROUND_ID_COLUMN], sort=True)[ACQUIRED_AT_COLUMN]
    out = pd.DataFrame(
        {
            "n": grouped.size(),
            "first_acquired_at": grouped.min(),
            "skew_s": grouped.max() - grouped.min(),
        }
    ).reset_index()
    return out.loc[:, list(ROUND_SKEW_COLUMNS)]


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
    from src.collection.kline.names import is_legacy

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
_EQUIV_RATIO_CHANNEL = "COMMANDED_EQUIV_RATIO"


def _commanded_afr_series(
    frame: pd.DataFrame,
    calibration: AfrCalibration | None,
    afr_stoich: float | None,
) -> pd.DataFrame:
    if calibration is None:
        raise ChannelSpecError(
            f"{COMMANDED_AFR_SPEC} needs an AFR calibration; pass calibration= "
            "(the vehicle profile's afr_calibration.yaml)"
        )
    if afr_stoich is None:
        # No gasoline default (R05.1): a literal 14.7 here made the workbench
        # disagree with the serve path for any non-gasoline profile.
        raise ChannelSpecError(
            f"{COMMANDED_AFR_SPEC} needs the vehicle's stoichiometric AFR; pass "
            "afr_stoich= (TuningContext.afr_stoich, from derived.fuel_properties)"
        )
    rows = frame[(frame["source"] == "obd") & (frame["channel"] == _EQUIV_RATIO_CHANNEL)]
    out = rows.loc[:, ["timestamp"]].copy()
    ratio = pd.to_numeric(rows["value_number"], errors="coerce")
    # Raises for an unverified convention — deliberately loud, once per call.
    # A ratio of 0.0 is decel fuel cut (no target at all) and converts to NaN,
    # which is dropped below; see AfrCalibration.commanded_afr_series.
    out["value"] = calibration.commanded_afr_series(ratio, afr_stoich=afr_stoich)
    out["unit"] = "AFR"
    return out.dropna(subset=["value"])


def read_channels(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    bindings_path: Path = BINDINGS_YAML,
    dids_path: Path = DIDS_YAML,
    calibration: AfrCalibration | None = None,
    afr_stoich: float | None = None,
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
    (:data:`src.analysis.pairing.TimestampBasis`) saying which of those
    happened, so a comparison can refuse a tolerance the clock cannot carry.
    """
    frame = _long_frame(session, acquired_time=acquired_time)
    catalogs = _Catalogs(bindings_path=bindings_path, dids_path=dids_path)
    parsed = [parse_spec(text) for text in specs]

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
            part = _commanded_afr_series(frame, calibration, afr_stoich)
        else:
            part = readers[spec.source](spec)
        part = part.assign(spec=spec.text)
        if acquired_time:
            part = part.assign(
                **{TIMESTAMP_BASIS_COLUMN: frame.loc[part.index, TIMESTAMP_BASIS_COLUMN]}
            )
        parts.append(part.loc[:, columns])

    if not parts:
        return pd.DataFrame(columns=columns)
    out = pd.concat(parts, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out.sort_values(["timestamp", "spec"], kind="stable").reset_index(drop=True)


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
    afr_stoich: float | None = None,
) -> pd.DataFrame:
    """Put ``specs`` on one clock, refusing stale joins by default.

    This is the cell-based *aligned frame* API — one row per ``grid`` cell,
    for band averages, gating and plotting. It is **not** the comparison
    contract: a cell holding a ``"last"`` sample from each channel does not
    say how far apart those samples were acquired, and a ``"mean"`` cell is
    a synthetic value that was never observed. Its output is therefore not
    pairing evidence; a claim that two channels agree goes through
    :func:`src.analysis.compare.compare_channels`, which pairs individual
    observations under an explicit :class:`~src.analysis.pairing.PairingPolicy`.

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
        afr_stoich=afr_stoich,
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


def align_long(
    long: pd.DataFrame,
    specs: list[str],
    *,
    grid: str = "1s",
    require_all: bool = True,
    cell_agg: Literal["last", "mean"] = "last",
    max_staleness: pd.Timedelta | str | None = None,
    with_age: bool = False,
) -> pd.DataFrame:
    """:func:`read_aligned`'s alignment step over an already-read long frame.

    Split out so a caller that also needs :func:`read_channels`' per-row
    ``unit`` (the workbench log frame) reads the session once rather than
    twice. Same freshness contract as :func:`read_aligned`.
    """
    names = [parse_spec(text).text for text in specs]
    if long.empty:
        return pd.DataFrame(columns=["timestamp", *names])

    tolerance = pd.Timedelta(max_staleness) if max_staleness is not None else None
    emit_age = with_age or tolerance is not None

    long = long.copy()
    long["bucket"] = long["timestamp"].dt.floor(grid)
    buckets = pd.DatetimeIndex(sorted(long["bucket"].unique()), name="timestamp")
    out = pd.DataFrame(index=buckets)

    for name in names:
        rows = long[long["spec"] == name]
        cell = rows.groupby("bucket", sort=True).agg(value=("value", cell_agg))
        series = cell["value"].reindex(buckets)
        seen = pd.Series(cell.index, index=cell.index).reindex(buckets)
        if tolerance is not None:
            series = series.ffill()
            seen = seen.ffill()
        age = (
            buckets.to_series().reset_index(drop=True).to_numpy() - seen.to_numpy()
        ) / pd.Timedelta(seconds=1)
        age_series = pd.Series(age, index=buckets, dtype="float64")
        if tolerance is not None:
            too_old = age_series > tolerance.total_seconds()
            series = series.mask(too_old)
            age_series = age_series.mask(too_old)
        out[name] = series
        if emit_age:
            out[f"{name}__age_s"] = age_series

    if require_all:
        out = out.dropna(subset=names)
    else:
        out = out.dropna(subset=names, how="all")
    return out.reset_index()


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
