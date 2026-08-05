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

This module is the single place that knows all four, so callers ask for
``obd:RPM`` or ``did:ECM_AF_SENSOR_V_B1`` and get numbers they can compare.

Scaling always routes through :func:`src.decode.bindings.apply_scaling` and
:func:`src.decode.dids.apply_scaling` rather than re-implementing the
arithmetic — an unpinned ``scale: null`` binding must keep yielding raw
counts with ``unit="counts"``, not a silently invented engineering value.

See ``docs/roadmap/tuning-analysis-toolkit.md`` Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

from src.analysis.afr import AfrCalibration
from src.decode import bindings as bindings_decode
from src.decode import dids as dids_decode
from src.preprocessing.loader import read_session
from src.telemetry import DECODED_SOURCE, SOURCE_SEP

#: Namespaces a spec may name. ``did``/``can`` are decode routes rather than
#: literal ``source`` column values — a DID reading is stored with
#: ``source="obd"`` and channel ``DID_1225``.
SpecSource = Literal["obd", "did", "decoded", "can"]

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
    if source not in ("obd", "did", "decoded", "can"):
        raise ChannelSpecError(
            f"unknown namespace {source!r} in {text!r}; expected obd/did/decoded/can"
        )
    return ChannelSpec(source=source, name=name, text=raw)  # type: ignore[arg-type]


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


def _long_frame(session: Path | pd.DataFrame) -> pd.DataFrame:
    if isinstance(session, pd.DataFrame):
        return session
    return read_session(Path(session))


def _obd_series(frame: pd.DataFrame, spec: ChannelSpec) -> pd.DataFrame:
    rows = frame[(frame["source"] == "obd") & (frame["channel"] == spec.name.upper())]
    out = rows.loc[:, ["timestamp"]].copy()
    out["value"] = pd.to_numeric(rows["value_number"], errors="coerce")
    out["unit"] = rows["unit"].astype("string").fillna("")
    return out.dropna(subset=["value"])


def _did_series(frame: pd.DataFrame, spec: ChannelSpec, catalogs: _Catalogs) -> pd.DataFrame:
    entry = catalogs.did(spec.name)
    channel = f"DID_{entry.did:04X}"
    rows = frame[frame["channel"] == channel]
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
    if signal.scale is None:
        out["value"] = raw
        out["unit"] = _RAW_UNIT
    else:
        offset = signal.offset if signal.offset is not None else 0.0
        out["value"] = raw * signal.scale + offset
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
    afr_stoich: float,
) -> pd.DataFrame:
    if calibration is None:
        raise ChannelSpecError(
            f"{COMMANDED_AFR_SPEC} needs an AFR calibration; pass calibration= "
            "(the vehicle profile's afr_calibration.yaml)"
        )
    rows = frame[(frame["source"] == "obd") & (frame["channel"] == _EQUIV_RATIO_CHANNEL)]
    out = rows.loc[:, ["timestamp"]].copy()
    ratio = pd.to_numeric(rows["value_number"], errors="coerce")
    # Raises for an unverified convention — deliberately loud, and once per
    # call rather than per row.
    probe = float(ratio.dropna().iloc[0]) if not ratio.dropna().empty else 1.0
    calibration.commanded_afr(probe, afr_stoich=afr_stoich)
    if calibration.commanded_equiv_ratio_convention == "phi":
        values = afr_stoich / ratio.where(ratio > 0)
    else:
        values = afr_stoich * ratio.where(ratio > 0)
    out["value"] = values
    out["unit"] = "AFR"
    return out.dropna(subset=["value"])


def read_channels(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
    calibration: AfrCalibration | None = None,
    afr_stoich: float = 14.7,
) -> pd.DataFrame:
    """Read ``specs`` from one session as a long frame in engineering units.

    Returns columns ``timestamp``, ``spec``, ``value``, ``unit`` — long rather
    than wide because each source has its own arrival cadence; putting them on
    a shared clock is :func:`read_aligned`'s job, and doing it implicitly here
    is exactly the stale-join trap (G.4) this module exists to close.

    ``unit`` is ``"counts"`` wherever the catalog's ``scale`` is unpinned, so a
    caller can tell "raw" from "engineering" without consulting the YAML.
    """
    frame = _long_frame(session)
    catalogs = _Catalogs(bindings_path=bindings_path, dids_path=dids_path)
    parsed = [parse_spec(text) for text in specs]

    readers = {
        "obd": lambda s: _obd_series(frame, s),
        "did": lambda s: _did_series(frame, s, catalogs),
        "decoded": lambda s: _decoded_series(frame, s, catalogs),
        "can": lambda s: _can_series(frame, s, catalogs),
    }

    parts: list[pd.DataFrame] = []
    for spec in parsed:
        if spec.source == "obd" and spec.name.upper() == "COMMANDED_AFR":
            part = _commanded_afr_series(frame, calibration, afr_stoich)
        else:
            part = readers[spec.source](spec)
        part = part.assign(spec=spec.text)
        parts.append(part.loc[:, ["timestamp", "spec", "value", "unit"]])

    if not parts:
        return pd.DataFrame(columns=["timestamp", "spec", "value", "unit"])
    out = pd.concat(parts, ignore_index=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    return out.sort_values(["timestamp", "spec"], kind="stable").reset_index(drop=True)


def read_aligned(
    session: Path | pd.DataFrame,
    specs: list[str],
    *,
    grid: str = "1s",
    require_all: bool = True,
    max_staleness: pd.Timedelta | str | None = None,
    with_age: bool = False,
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
    calibration: AfrCalibration | None = None,
    afr_stoich: float = 14.7,
) -> pd.DataFrame:
    """Put ``specs`` on one clock, refusing stale joins by default.

    A row exists only where **every** requested channel has its own sample
    inside the grid cell (``require_all=True``). Nothing is forward-filled:
    the 2026-07 campaign recorded four "WOT AFR" samples that were one stale
    reading aged 24–35 s, paired in by an ASOF join and indistinguishable from
    measurements. Freshness is the default here so that cannot recur silently.

    ``max_staleness`` opts into carrying a value forward, and is explicit on
    purpose. When it is set — or ``with_age=True`` — the frame also carries a
    ``<spec>__age_s`` column per channel, so a caller can always see how old
    the number it is using actually is.
    """
    long = read_channels(
        session,
        specs,
        bindings_path=bindings_path,
        dids_path=dids_path,
        calibration=calibration,
        afr_stoich=afr_stoich,
    )
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
        # Last sample within each cell — a cell wider than the sample interval
        # would otherwise average across a transient.
        cell = rows.groupby("bucket", sort=True).agg(value=("value", "last"))
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
    bindings_path: Path = Path("data/bindings.yaml"),
    dids_path: Path = Path("data/dids.yaml"),
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
                factors[key] = (
                    None
                    if signal.scale is None
                    else (signal.scale, signal.offset if signal.offset is not None else 0.0)
                )
        factor = factors[key]
        if factor is None:
            continue
        raw = row.get(value_field)
        if isinstance(raw, (int, float)):
            row[value_field] = float(raw) * factor[0] + factor[1]
    return rows
