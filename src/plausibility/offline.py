"""Plausibility over stored sessions: raw samples, modes, fault injection (A05.8).

The live evaluator sees every arrived sample before resampling; this module
reconstructs exactly that stream from a session's Parquet so the same
:class:`~src.plausibility.evaluator.PlausibilityEvaluator` can be run on the
desk:

* :func:`session_samples` — one row per arrived numeric sample
  (``t`` epoch seconds, ``channel`` wide key, ``value``). ``decoded:`` rows are
  decoded from the raw CAN frames (stored decoded rows win, as in training),
  ``did:`` rows from the raw ``DID_<hex>`` polls; nothing is resampled or
  forward-filled.
* :func:`sample_modes` — the operating mode in force at each sample, from the
  same segmenter the daemon runs over the 1 Hz grid (the live evaluator uses
  the latest tick's mode the same way).
* :func:`run_evaluator` — feeds samples in time order, with a freshness check
  on every whole second (the live tick).
* :func:`inject_plumbing_fault` — the 2026-09-20 benchmark's five plumbing
  faults at ``synthetic_fault_eval._inject_one``'s magnitudes (spike
  5 x max + 1,000; drift 10 sigma + 100; scale x 5; stuck at the pre-window
  value; dropout), applied to *arrived samples* rather than a resampled grid:
  a dropout removes samples instead of forward-filling them.
* :func:`inject_realistic_samples` — A02.6's realistic faults, through
  ``synthetic_fault_eval.inject_realistic_fault`` itself, applied only to
  cells that actually arrived.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from src.plausibility.evaluator import EvidenceRecord, PlausibilityEvaluator
from src.plausibility.limits import LimitsCatalog

if TYPE_CHECKING:
    from src.config import CanaryConfig
    from src.decode.bindings import Bindings
    from src.decode.dids import DidMap


def segmenter_channels() -> tuple[str, ...]:
    """Every column the live segmenter reads
    (``feature_pipeline.segmenter_input_columns``), so offline mode labels
    cannot diverge from it. Imported here, not at module load: the feature
    pipeline stays off every path that only needs the evaluator."""
    from src.preprocessing.feature_pipeline import segmenter_input_columns

    return segmenter_input_columns()


def segmenter_decoded() -> tuple[str, ...]:
    """The broadcast signals among :func:`segmenter_channels` (the segmenter's
    fallback when Mode 01 is absent), by binding name; decoded for mode labels
    even when no check covers them."""
    prefix = "decoded:"
    return tuple(c[len(prefix) :] for c in segmenter_channels() if c.startswith(prefix))


PLUMBING_FAULT_KINDS: tuple[str, ...] = ("spike", "drift", "dropout", "stuck", "scale")


def epoch_seconds(timestamps: pd.Series) -> np.ndarray[Any, np.dtype[np.float64]]:
    """Epoch seconds, whatever the column's datetime resolution (us or ns)."""
    stamps = pd.to_datetime(timestamps, utc=True)
    seconds = (stamps - pd.Timestamp(0, tz="UTC")).dt.total_seconds()
    return np.asarray(seconds.to_numpy(dtype="float64"), dtype=np.float64)


def _checked_decoded(catalog: LimitsCatalog) -> set[str]:
    return {
        channel.partition(":")[2]
        for channel, limits in catalog.channels.items()
        if channel.startswith("decoded:") and limits.is_checked()
    }


def session_long(
    session_dir: Path,
    *,
    bindings: Bindings | None,
    did_map: DidMap | None,
    decoded: Iterable[str],
) -> pd.DataFrame:
    """The session's long frame with the named broadcast signals and every
    DID decoded, raw CAN rows dropped.

    Reads through :func:`src.preprocessing.decode.read_session_decoded_subset`,
    the one bounded reader: raw CAN is read only for the ids the named
    bindings decode, under every era's channel spelling (R04.14).
    """
    from src.decode.dids import decode_did_rows
    from src.preprocessing.decode import decode_can_rows, read_session_decoded_subset

    wanted = set(decoded)
    long, subset = read_session_decoded_subset(session_dir, bindings=bindings, decoded=wanted)
    is_can = long["source"] == "can"
    rest = long.loc[~is_can]
    if did_map is not None:
        rest = decode_did_rows(rest, did_map)
    pieces = [rest.loc[(rest["source"] != "decoded") | rest["channel"].isin(wanted)]]
    if subset is not None and bool(is_can.any()):
        # Stored decoded rows win (decode_can_rows would skip them anyway).
        stored = set(rest.loc[rest["source"] == "decoded", "channel"].unique())
        signals = [s for s in subset.signals if s.name not in stored]
        if signals:
            decoded_rows = decode_can_rows(
                long.loc[is_can], subset.model_copy(update={"signals": signals})
            )
            pieces.append(decoded_rows.loc[decoded_rows["source"] == "decoded"])
    out = pd.concat(pieces, ignore_index=True) if len(pieces) > 1 else pieces[0]
    return out.sort_values("timestamp", kind="stable").reset_index(drop=True)


def samples_from_long(long: pd.DataFrame) -> pd.DataFrame:
    """Arrived numeric samples: ``t`` (epoch s), ``channel``, ``value``.

    One row per (channel, timestamp) — a stored decoded row and a re-decode
    of the same frame are one arrival.
    """
    if long.empty:
        return pd.DataFrame({"t": [], "channel": [], "value": []})
    values = pd.to_numeric(long["value_number"], errors="coerce")
    keep = values.notna()
    frame = pd.DataFrame(
        {
            "t": epoch_seconds(long.loc[keep, "timestamp"]),
            "channel": (
                long.loc[keep, "source"].astype(str) + ":" + long.loc[keep, "channel"].astype(str)
            ),
            "value": values.loc[keep].astype("float64"),
        }
    )
    frame = frame.drop_duplicates(subset=["channel", "t"], keep="first")
    return frame.sort_values("t", kind="stable").reset_index(drop=True)


def sample_modes(
    long: pd.DataFrame,
    samples: pd.DataFrame,
    config: CanaryConfig,
    *,
    bindings: Bindings | None,
) -> np.ndarray[Any, np.dtype[np.object_]]:
    """Operating mode in force at each sample (``None`` before the first tick)."""
    from src.preprocessing.feature_pipeline import resample_long_session, segment_or_constant
    from src.preprocessing.resampler import TIMESTAMP_COLUMN, resample_config_for
    from src.preprocessing.segmenter import MODE_COLUMN

    modes = np.full(len(samples), None, dtype=object)
    keys = long["source"].astype(str) + ":" + long["channel"].astype(str)
    subset = long.loc[keys.isin(segmenter_channels()).to_numpy()]
    if subset.empty or samples.empty:
        return modes
    resampled = resample_long_session(
        subset, None, None, None, resample_config_for(config), session_name="plausibility"
    )
    if resampled.empty:
        return modes
    segmented, _ = segment_or_constant(resampled, config, bindings=bindings)
    grid = epoch_seconds(segmented[TIMESTAMP_COLUMN])
    labels = segmented[MODE_COLUMN].astype(object).to_numpy()
    index = np.searchsorted(grid, samples["t"].to_numpy(), side="right") - 1
    valid = index >= 0
    modes[valid] = labels[index[valid]]
    return modes


@dataclass(frozen=True)
class SessionStream:
    """A session's arrived samples and the mode at each."""

    name: str
    samples: pd.DataFrame
    modes: np.ndarray[Any, np.dtype[np.object_]]

    @property
    def span(self) -> tuple[float, float] | None:
        if self.samples.empty:
            return None
        return float(self.samples["t"].iloc[0]), float(self.samples["t"].iloc[-1])

    def with_samples(
        self, samples: pd.DataFrame, modes: np.ndarray[Any, np.dtype[np.object_]]
    ) -> SessionStream:
        return SessionStream(self.name, samples, modes)


def load_stream(
    session_dir: Path,
    config: CanaryConfig,
    catalog: LimitsCatalog,
    *,
    bindings: Bindings | None,
    did_map: DidMap | None,
) -> SessionStream:
    decoded = _checked_decoded(catalog) | set(segmenter_decoded())
    long = session_long(session_dir, bindings=bindings, did_map=did_map, decoded=decoded)
    samples = samples_from_long(long)
    modes = sample_modes(long, samples, config, bindings=bindings)
    return SessionStream(session_dir.name, samples, modes)


def run_evaluator(
    evaluator: PlausibilityEvaluator,
    stream: SessionStream,
    *,
    tick_seconds: float = 1.0,
) -> list[EvidenceRecord]:
    """Feed ``stream`` through ``evaluator``; return the records it wrote."""
    written: list[EvidenceRecord] = []
    previous_sink = evaluator.sink

    def collect(record: EvidenceRecord) -> None:
        written.append(record)
        if previous_sink is not None:
            previous_sink(record)

    evaluator.sink = collect
    try:
        times = stream.samples["t"].to_numpy(dtype="float64")
        channels = stream.samples["channel"].to_numpy(dtype=object)
        values = stream.samples["value"].to_numpy(dtype="float64")
        modes = stream.modes
        if times.size == 0:
            return written
        next_tick = math.floor(times[0]) + tick_seconds
        observe = evaluator.observe
        for i in range(times.size):
            at = times[i]
            mode = modes[i]
            while at >= next_tick:
                evaluator.check_freshness(next_tick, mode)
                next_tick += tick_seconds
            observe(channels[i], values[i], at, mode)
        evaluator.check_freshness(next_tick, modes[-1])
    finally:
        evaluator.sink = previous_sink
    return written


# ---------------------------------------------------------------------------
# Fault injection on arrived samples
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Injection:
    kind: str
    channel: str
    start: float
    end: float
    #: Samples changed or removed. Zero means the fault was a no-op on this
    #: drive (channel not observed in the window, or stuck at the value it
    #: already held) — reported as such, never graded as a miss.
    changed: int
    parameter: float | None = None


def plumbing_window(span: tuple[float, float], rng: np.random.Generator) -> tuple[float, float]:
    """``_inject_one``'s window rule over the session's 1 Hz span: a tenth of
    the drive (at least 5 s), centred mid-drive ± an eighth, jittered."""
    first, last = span
    n = int(last - first) + 1
    window_len = max(5, n // 10)
    centre = n // 2 + int(rng.integers(-n // 8, n // 8 + 1))
    centre = max(window_len, min(n - window_len - 1, centre))
    return first + (centre - window_len // 2), first + (centre + window_len // 2)


def inject_plumbing_fault(
    stream: SessionStream,
    *,
    channel: str,
    kind: str,
    rng: np.random.Generator,
) -> tuple[SessionStream, Injection]:
    """One plumbing fault on ``channel``'s arrived samples; see module doc."""
    span = stream.span
    if span is None:
        raise ValueError("empty session")
    start, end = plumbing_window(span, rng)
    samples = stream.samples
    is_channel = (samples["channel"] == channel).to_numpy()
    series = samples.loc[is_channel, "value"]
    nominal_max = float(series.max()) if not series.empty else 1.0
    nominal_std = float(series.std()) if len(series) > 1 else 1.0
    if not math.isfinite(nominal_std):
        nominal_std = 1.0
    times = samples["t"].to_numpy()
    in_window = is_channel & (times >= start) & (times < end)
    values = samples["value"].to_numpy(dtype="float64").copy()
    parameter: float | None = None
    keep = np.ones(len(samples), dtype=bool)
    if kind == "spike":
        parameter = nominal_max * 5 + 1_000
        values[in_window] = parameter
    elif kind == "drift":
        parameter = nominal_std * 10 + 100
        fractions = np.clip((times[in_window] - start) / (end - start), 0.0, 1.0)
        values[in_window] = values[in_window] + fractions * parameter
    elif kind == "scale":
        parameter = 5.0
        values[in_window] = values[in_window] * parameter
    elif kind == "stuck":
        before = is_channel & (times < start)
        if before.any():
            parameter = float(values[np.flatnonzero(before)[-1]])
        elif is_channel.any():
            parameter = float(values[np.flatnonzero(is_channel)[0]])
        if parameter is not None:
            values[in_window] = parameter
    elif kind == "dropout":
        keep = ~in_window
    else:
        raise ValueError(f"unsupported plumbing fault kind: {kind!r}")
    original = samples["value"].to_numpy(dtype="float64")
    if kind == "dropout":
        changed = int(in_window.sum())
    else:
        changed = int((values[in_window] != original[in_window]).sum())
    out = samples.assign(value=values).loc[keep].reset_index(drop=True)
    modes = stream.modes[keep]
    return stream.with_samples(out, modes), Injection(
        kind, channel, float(start), float(end), changed, parameter
    )


def inject_realistic_samples(
    stream: SessionStream,
    *,
    kind: str,
    severity: float,
    start: float,
    end: float,
    roles: Mapping[str, Sequence[str]],
    bindings: Bindings | None,
) -> tuple[SessionStream, dict[str, Any]]:
    """Apply one A02.6 realistic fault to the arrived samples.

    The samples are pivoted to a wide frame at raw timestamps (a cell exists
    only where a sample arrived), with the fault's context channels (rpm,
    speed, fuel status) carried forward so its operating condition can be
    read on every row; the fault is applied by
    ``synthetic_fault_eval.inject_realistic_fault``; only role-channel cells
    that arrived are written back.
    """
    from src.models.synthetic_fault_eval import (
        FUEL_STATUS_CHANNEL,
        RPM_CHANNEL,
        SPEED_CHANNEL,
        inject_realistic_fault,
    )

    role_channels = {c for channels in roles.values() for c in channels}
    context = {RPM_CHANNEL, SPEED_CHANNEL, FUEL_STATUS_CHANNEL, "obd:MAF"}
    samples = stream.samples
    wanted = samples["channel"].isin(role_channels | context).to_numpy()
    part = samples.loc[wanted]
    wide = part.pivot_table(index="t", columns="channel", values="value", aggfunc="last")
    arrived = wide.notna()
    for column in context & set(wide.columns):
        wide[column] = wide[column].ffill()
    wide = wide.reset_index()
    wide["timestamp"] = pd.to_datetime(wide["t"], unit="s", utc=True)
    faulted, injection = inject_realistic_fault(
        wide,
        kind=kind,
        severity=severity,
        start=pd.Timestamp(start, unit="s", tz="UTC"),
        end=pd.Timestamp(end, unit="s", tz="UTC"),
        roles=roles,
        bindings=bindings,
    )
    faulted = faulted.set_index("t")
    changed = 0
    values = samples["value"].to_numpy(dtype="float64").copy()
    index_of = {
        (c, t): i for i, (c, t) in enumerate(zip(samples["channel"], samples["t"], strict=True))
    }
    for column in injection.perturbed:
        if column not in arrived.columns:
            continue
        mask = arrived[column].to_numpy()
        stamps = arrived.index.to_numpy()[mask]
        new = faulted[column].to_numpy(dtype="float64")[mask]
        for stamp, value in zip(stamps, new, strict=True):
            i = index_of.get((column, stamp))
            if i is not None and values[i] != value:
                values[i] = value
                changed += 1
    report = injection.to_dict()
    report["changed_samples"] = changed
    return stream.with_samples(samples.assign(value=values), stream.modes), report


def detections_in(
    records: Iterable[EvidenceRecord],
    *,
    channels: Iterable[str],
    start: float,
    end: float,
) -> list[EvidenceRecord]:
    """Records on ``channels`` whose detection time falls in ``[start, end)``."""
    from datetime import datetime

    names = set(channels)
    out: list[EvidenceRecord] = []
    for record in records:
        if record.channel not in names:
            continue
        at = datetime.fromisoformat(record.detected_at.replace("Z", "+00:00")).timestamp()
        if start <= at < end:
            out.append(record)
    return out


__all__ = [
    "PLUMBING_FAULT_KINDS",
    "Injection",
    "SessionStream",
    "detections_in",
    "inject_plumbing_fault",
    "inject_realistic_samples",
    "load_stream",
    "plumbing_window",
    "run_evaluator",
    "sample_modes",
    "samples_from_long",
    "segmenter_channels",
    "segmenter_decoded",
    "session_long",
]
