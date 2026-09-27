#!/usr/bin/env python3
"""Feature-only microbenchmark for ``LiveFeatureSource`` (roadmap task C05).

**Scope.** This times the *feature stage* of the live pipeline and nothing
else: per tick, the ``observe()`` calls that apply one tick's worth of
synthetic OBD + CAN records, then the ``tick()`` that emits a wide row and
runs the shared wide→features builder over the ring buffer. Detector
scoring, the collectors, the network and disk paths are all **untimed** —
the full-pipeline harness is task C10. The ``1 / p95`` figure printed at
the end is therefore a microbenchmark estimate of this stage alone, not a
demonstrated sustainable daemon collection rate.

**Why this exists in this form.** The previous version of this script
observed records at Unix seconds 0, 1, 2, … and ticked at a 2026
datetime, so ``LiveFeatureSource``'s freshness rule rejected every
observation and the "benchmark" reported ~27,600 Hz over an empty buffer
with zero feature rows. Everything below is arranged so that result is
impossible to accept again:

* one scenario clock — observations and ticks share ``SCENARIO_EPOCH`` and
  advance monotonically through warmup *and* measurement, one configured
  resampling period (``1 / preprocessing.resample_hz``) per tick;
* per-tick verification — each measured tick records whether ``tick()``
  returned feature rows; a run whose warm buffer is empty or that produced
  no feature rows is reported ``valid: false`` with a named reason and the
  CLI exits non-zero;
* truthful workload — ``--channels`` and ``--can-frames-per-tick`` are
  honoured exactly (validated against the finite supported channel list;
  CAN frames cycle through the bound IDs so repeated IDs follow a per-ID
  cadence spread across the tick period);
* physically meaningful values — a deterministic synthetic drive cycle
  (idle → acceleration → highway → deceleration → idle, coolant warming
  from cold) so operating-mode classification and the downstream feature
  work actually execute.

Workload scope: **synthetic only.** Replaying a recorded session's records
through ``observe()`` is not implemented here; recorded-workload timing is
part of the C10 end-to-end harness.

Usage::

    .venv/bin/python scripts/benchmark_live_pipeline.py --profile smoke
    .venv/bin/python scripts/benchmark_live_pipeline.py --profile steady \\
        --report data/_benchmarks/steady.json
    .venv/bin/python scripts/benchmark_live_pipeline.py --ticks 200 --channels 20

The JSON report (``--report``, default under ``data/_benchmarks/`` — a
``_``-prefixed directory the training-corpus discovery ignores) carries
median/p95/p99/max per stage, completed vs empty tick counts, buffer size,
host identity, git revision, config/bindings hashes, and the validity
verdict. Human-readable output goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import socket
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

try:
    from _cli import (
        add_bindings_arg,
        load_scoped_config,
    )
except ImportError:
    from scripts._cli import (  # type: ignore[no-redef]
        add_bindings_arg,
        load_scoped_config,
    )

from src.artifact_hash import sha256_file_or_none
from src.collection.can_obd_client import PID_DECODERS
from src.config import CanaryConfig
from src.dashboard.version import _git_sha
from src.decode.bindings import Bindings, load_bindings
from src.preprocessing.live import LiveFeatureSource
from src.telemetry import TelemetryRecord

#: Start of scenario time. Every observation timestamp and every tick is
#: derived from this one instant, so the two can never sit on different
#: epochs again. Fixed (not ``now``) so a seeded run is byte-reproducible.
SCENARIO_EPOCH = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)

#: Default report directory — ``_``-prefixed so nothing under it is ever
#: discovered as a training session or a live session.
DEFAULT_REPORT_DIR = Path("data/_benchmarks")

#: CAN IDs used when no bindings are loaded. None of them decode, which
#: exercises the "unknown ID → skip" path of ``observe()`` deliberately.
FALLBACK_CAN_IDS: tuple[int, ...] = (0x1F9, 0x551, 0x23D)

#: ``--profile`` presets: (warmup ticks, measured ticks).
WORKLOAD_PROFILES: dict[str, tuple[int, int]] = {
    "smoke": (30, 60),
    "steady": (120, 600),
}

#: Exit code for a run whose validity check failed (the result must not be
#: quoted as a measurement).
EXIT_INVALID = 2

BENCHMARK_NAME = "live_feature_pipeline_microbenchmark"
BENCHMARK_SCOPE = (
    "feature-only microbenchmark: LiveFeatureSource.observe() + tick(); detector "
    "scoring, collectors, network and disk paths are untimed (full pipeline: C10)"
)
HZ_ESTIMATE_NOTE = (
    "1000 / p95(total_ms): microbenchmark estimate for the feature stage alone; "
    "excludes untimed components and is not a sustainable daemon collection rate"
)

# ---------------------------------------------------------------------------
# synthetic drive cycle
# ---------------------------------------------------------------------------

_CYCLE_SECONDS = 240.0


@dataclass(frozen=True)
class DriveState:
    """The four segmenter inputs at one instant of the synthetic drive."""

    rpm: float
    speed: float
    throttle: float
    coolant: float


def _cycle_point(phase: float) -> tuple[float, float, float]:
    """Nominal (rpm, speed, throttle) for ``phase`` seconds into the cycle."""
    if phase < 30.0:  # standing idle
        return 700.0, 0.0, 12.0
    if phase < 90.0:  # pull away and accelerate through city speeds
        f = (phase - 30.0) / 60.0
        return 1500.0 + 1700.0 * f, 60.0 * f, 45.0
    if phase < 150.0:  # highway cruise
        return 2600.0, 100.0 + 10.0 * math.sin((phase - 90.0) / 10.0), 35.0
    if phase < 200.0:  # decelerate back to a stop
        f = (phase - 150.0) / 50.0
        return 2600.0 - 1800.0 * f, 100.0 * (1.0 - f), 8.0
    return 700.0, 0.0, 12.0  # standing idle


def drive_state(scenario_seconds: float, rng: np.random.Generator) -> DriveState:
    """Physically plausible operating point ``scenario_seconds`` into the run.

    Ranges: rpm 650–4500, speed 0–110 km/h, throttle 0–100 %, coolant
    20→95 °C (warming from cold with a 120 s time constant, so warmup gives
    way to warm modes a couple of minutes in). Speed is jittered only while
    moving: the idle modes gate on ``speed_max: 0`` exactly.
    """
    rpm, speed, throttle = _cycle_point(scenario_seconds % _CYCLE_SECONDS)
    rpm_j, speed_j, throttle_j = rng.normal(0.0, 1.0, size=3)
    rpm = float(np.clip(rpm + 25.0 * rpm_j, 650.0, 4500.0))
    if speed > 0.0:
        speed = float(np.clip(speed + 1.0 * speed_j, 1.0, 110.0))
    throttle = float(np.clip(throttle + 1.5 * throttle_j, 0.0, 100.0))
    coolant = min(95.0, 20.0 + 75.0 * (1.0 - math.exp(-scenario_seconds / 120.0)))
    return DriveState(rpm=rpm, speed=speed, throttle=throttle, coolant=coolant)


def _load(s: DriveState) -> float:
    return 15.0 + 0.7 * s.throttle


#: Supported OBD channels, in the order ``--channels N`` takes them, each with
#: a unit and a generator from the drive state + scenario seconds. The four
#: segmenter inputs come first so any ``--channels >= 4`` run classifies real
#: operating modes. Every name is a real Mode-01 PID from ``PID_DECODERS`` and
#: every generated value satisfies ``quality_gate.is_physically_valid``
#: (asserted in tests/test_benchmark_live_pipeline.py).
_CHANNEL_MODELS: dict[str, tuple[str, Callable[[DriveState, float], float]]] = {
    "RPM": ("rpm", lambda s, t: s.rpm),
    "SPEED": ("km/h", lambda s, t: s.speed),
    "THROTTLE_POS": ("%", lambda s, t: s.throttle),
    "COOLANT_TEMP": ("degC", lambda s, t: s.coolant),
    "ENGINE_LOAD": ("%", lambda s, t: _load(s)),
    "INTAKE_TEMP": ("degC", lambda s, t: 28.0 + 0.1 * s.coolant),
    "INTAKE_PRESSURE": ("kPa", lambda s, t: 30.0 + 0.65 * s.throttle),
    "MAF": ("g/s", lambda s, t: 2.0 + s.rpm / 1000.0 * (0.5 + s.throttle / 100.0) * 10.0),
    "TIMING_ADVANCE": ("deg", lambda s, t: 10.0 + 25.0 * (s.rpm - 650.0) / 3850.0),
    "SHORT_FUEL_TRIM_1": ("%", lambda s, t: 3.0 * math.sin(t / 7.0)),
    "LONG_FUEL_TRIM_1": ("%", lambda s, t: -2.5),
    "SHORT_FUEL_TRIM_2": ("%", lambda s, t: 3.0 * math.cos(t / 9.0)),
    "LONG_FUEL_TRIM_2": ("%", lambda s, t: -1.5),
    "O2_B1S1": ("V", lambda s, t: 0.45 + 0.4 * math.sin(2.0 * t)),
    "O2_B2S1": ("V", lambda s, t: 0.45 + 0.4 * math.cos(2.0 * t)),
    "BAROMETRIC_PRESSURE": ("kPa", lambda s, t: 99.0),
    "CONTROL_MODULE_VOLTAGE": ("V", lambda s, t: 14.1 + 0.1 * math.sin(t / 3.0)),
    "RUN_TIME": ("s", lambda s, t: t),
    "FUEL_LEVEL": ("%", lambda s, t: 62.0 - t / 3600.0),
    "AMBIANT_AIR_TEMP": ("degC", lambda s, t: 24.0),
    "RELATIVE_THROTTLE_POS": ("%", lambda s, t: 0.9 * s.throttle),
    "ABSOLUTE_LOAD": ("%", lambda s, t: 0.9 * _load(s)),
    "COMMANDED_EQUIV_RATIO": ("ratio", lambda s, t: 1.0),
    "CATALYST_TEMP_B1S1": ("degC", lambda s, t: 300.0 + 4.0 * _load(s)),
    "OIL_TEMP": ("degC", lambda s, t: s.coolant + 5.0),
    "ACCEL_PEDAL_POS_D": ("%", lambda s, t: 0.8 * s.throttle),
    "FUEL_STATUS": ("", lambda s, t: 2.0),
}

SUPPORTED_OBD_CHANNELS: tuple[str, ...] = tuple(_CHANNEL_MODELS)
MAX_CHANNELS = len(SUPPORTED_OBD_CHANNELS)
_KNOWN_PIDS = frozenset(name for name, _ in PID_DECODERS.values())


def channel_unit(channel: str) -> str:
    return _CHANNEL_MODELS[channel][0]


def channel_value(channel: str, state: DriveState, scenario_seconds: float) -> float:
    return _CHANNEL_MODELS[channel][1](state, scenario_seconds)


# ---------------------------------------------------------------------------
# scenario clock and per-tick workload
# ---------------------------------------------------------------------------


def tick_period_seconds(config: CanaryConfig) -> float:
    """One tick per configured resampling period — never a hard-coded 1 s."""
    return 1.0 / float(config.preprocessing.resample_hz)


def tick_time(index: int, period_seconds: float) -> datetime:
    """Scenario time of tick ``index`` (0-based, continuous across phases)."""
    return SCENARIO_EPOCH + timedelta(seconds=index * period_seconds)


@dataclass(frozen=True)
class TickWorkload:
    """Everything one tick observes, plus the instant it ticks at."""

    index: int
    tick_at: datetime
    records: tuple[TelemetryRecord, ...]


def _spread(tick_at: datetime, period_seconds: float, position: int, count: int) -> float:
    """Unix time of observation ``position`` of ``count`` inside the tick
    period ``(tick_at - period, tick_at]`` — the last one lands on the tick."""
    return tick_at.timestamp() - period_seconds + period_seconds * (position + 1) / count


def build_tick_workload(
    index: int,
    *,
    period_seconds: float,
    channels: Sequence[str],
    can_ids: Sequence[int],
    can_frames_per_tick: int,
    rng: np.random.Generator,
    observation_epoch_offset_seconds: float = 0.0,
) -> TickWorkload:
    """Synthesize tick ``index``'s observations on the shared scenario clock.

    OBD: one record per requested channel, spread through the tick period.
    CAN: exactly ``can_frames_per_tick`` frames, IDs taken round-robin from
    ``can_ids`` with the rotation carried across ticks, so a 20-frame tick
    over 3 IDs emits 6–7 frames per ID at an even per-ID cadence.

    ``observation_epoch_offset_seconds`` is a **test-only knob**: it shifts
    observation timestamps relative to the tick clock to reproduce the
    original mismatched-epoch defect and prove the validity check rejects
    it. Leave it at 0 for a measurement.
    """
    tick_at = tick_time(index, period_seconds)
    scenario_seconds = index * period_seconds
    state = drive_state(scenario_seconds, rng)
    offset = observation_epoch_offset_seconds
    obd = tuple(
        TelemetryRecord.obd(
            _spread(tick_at, period_seconds, j, len(channels)) + offset,
            channel,
            channel_value(channel, state, scenario_seconds),
            channel_unit(channel),
        )
        for j, channel in enumerate(channels)
    )
    payloads = rng.integers(0, 256, size=(can_frames_per_tick, 8), dtype=np.uint8)
    can = tuple(
        TelemetryRecord.can(
            _spread(tick_at, period_seconds, k, can_frames_per_tick) + offset,
            can_ids[(index * can_frames_per_tick + k) % len(can_ids)],
            bytes(payloads[k]),
        )
        for k in range(can_frames_per_tick)
    )
    return TickWorkload(index=index, tick_at=tick_at, records=obd + can)


# ---------------------------------------------------------------------------
# measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TickResult:
    """One tick's timings and what the feature stage actually produced."""

    index: int
    observe_ms: float
    tick_ms: float
    total_ms: float
    observations: int
    feature_rows: int
    feature_columns: int
    operating_mode: str | None
    segmenter_fell_back: bool

    @property
    def completed(self) -> bool:
        return self.feature_rows > 0


def run_tick(source: LiveFeatureSource, workload: TickWorkload) -> TickResult:
    """Apply one tick's observations, tick, and time each stage separately."""
    started = time.perf_counter()
    for record in workload.records:
        source.observe(record)
    observed = time.perf_counter()
    frame = source.tick(workload.tick_at)
    finished = time.perf_counter()
    point = source.latest_operating_point or {}
    mode = point.get("operating_mode")
    return TickResult(
        index=workload.index,
        observe_ms=(observed - started) * 1000.0,
        tick_ms=(finished - observed) * 1000.0,
        total_ms=(finished - started) * 1000.0,
        observations=len(workload.records),
        feature_rows=0 if frame is None else len(frame),
        feature_columns=0 if frame is None else len(frame.columns),
        operating_mode=None if mode is None else str(mode),
        segmenter_fell_back=source.last_segmenter_fallback,
    )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageTiming:
    count: int
    mean_ms: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float


def stage_timing(samples: Sequence[float]) -> StageTiming:
    if not samples:
        return StageTiming(0, math.nan, math.nan, math.nan, math.nan, math.nan)
    values = np.asarray(samples, dtype=float)
    return StageTiming(
        count=len(samples),
        mean_ms=float(values.mean()),
        median_ms=float(statistics.median(samples)),
        p95_ms=float(np.percentile(values, 95)),
        p99_ms=float(np.percentile(values, 99)),
        max_ms=float(values.max()),
    )


@dataclass(frozen=True)
class HostIdentity:
    hostname: str
    platform: str
    python: str
    git_revision: str | None


def host_identity() -> HostIdentity:
    return HostIdentity(
        hostname=socket.gethostname(),
        platform=platform.platform(),
        python=platform.python_version(),
        git_revision=_git_sha(),
    )


@dataclass(frozen=True)
class Provenance:
    """What the benchmark ran against — paths plus content hashes, so two
    reports are comparable only when these agree."""

    config_path: str | None
    config_sha256: str | None
    bindings_path: str | None
    bindings_sha256: str | None
    models_dir: str
    #: ``{path, model_type, model_version}`` per manifest under ``models_dir``.
    #: Informational only — no model is scored by this microbenchmark.
    model_manifests: tuple[dict[str, str | None], ...]
    behavior_models: str | None = None


#: R01.6: streamed, not ``read_bytes()`` (the old copy read the whole file).
_sha256 = sha256_file_or_none


def _manifest_summary(path: Path) -> dict[str, str | None]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return {"path": str(path), "model_type": None, "model_version": f"unreadable: {exc}"}
    return {
        "path": str(path),
        "model_type": str(payload.get("model_type")) if "model_type" in payload else None,
        "model_version": (
            str(payload.get("model_version")) if "model_version" in payload else None
        ),
    }


def collect_provenance(
    config: CanaryConfig, config_path: Path | None, bindings_path: Path | None
) -> Provenance:
    models_dir = config.daemon.models_dir
    manifests = tuple(
        _manifest_summary(path) for path in sorted(models_dir.glob("**/*.manifest.json"))
    )
    return Provenance(
        config_path=None if config_path is None else str(config_path),
        config_sha256=_sha256(config_path),
        bindings_path=None if bindings_path is None else str(bindings_path),
        bindings_sha256=_sha256(bindings_path),
        models_dir=str(models_dir),
        model_manifests=manifests,
    )


@dataclass(frozen=True)
class WorkloadSummary:
    kind: str
    profile: str | None
    seed: int
    warmup_ticks: int
    measured_ticks: int
    resample_hz: float
    tick_period_seconds: float
    channels: tuple[str, ...]
    can_ids: tuple[str, ...]
    can_frames_per_tick: int
    observations_per_tick: int
    observations_measured: int
    scenario_start: str
    scenario_end: str
    scenario_seconds: float
    observation_epoch_offset_seconds: float


@dataclass(frozen=True)
class Results:
    buffer_size_after_warmup: int
    buffer_size_at_end: int
    warmup_completed_ticks: int
    completed_ticks: int
    empty_ticks: int
    feature_rows: int
    feature_columns: int
    operating_modes: dict[str, int]
    segmenter_fallback_ticks: int
    quality_gate_rejects: dict[str, int]
    timings_ms: dict[str, StageTiming]
    microbenchmark_hz_at_p95: float | None
    microbenchmark_hz_note: str = HZ_ESTIMATE_NOTE


@dataclass(frozen=True)
class BenchmarkReport:
    valid: bool
    invalid_reasons: tuple[str, ...]
    host: HostIdentity
    provenance: Provenance
    workload: WorkloadSummary
    results: Results
    generated_at: str
    benchmark: str = BENCHMARK_NAME
    scope: str = BENCHMARK_SCOPE
    ticks: tuple[TickResult, ...] = field(default_factory=tuple, repr=False)

    def to_dict(self, *, include_ticks: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        if not include_ticks:
            payload.pop("ticks")
        return payload


def validity_reasons(
    *,
    buffer_size_after_warmup: int,
    completed_ticks: int,
    measured_ticks: int,
    observations_measured: int,
    observations_expected: int,
) -> tuple[str, ...]:
    """Why a run must not be quoted as a measurement (empty when it may)."""
    reasons: list[str] = []
    if buffer_size_after_warmup == 0:
        reasons.append(
            "warm buffer empty after warmup (buffer_size == 0): every observation "
            "was rejected as stale or never applied, so ticks measured nothing"
        )
    if completed_ticks == 0:
        reasons.append(
            f"no measured tick produced feature rows (0 of {measured_ticks} completed): "
            "the feature transformation never executed"
        )
    elif completed_ticks != measured_ticks:
        # Every measured tick must exercise the intended workload. A run that
        # timed mostly empty ticks would otherwise quote the empty-tick latency
        # as the feature stage's cost -- the 27,600 Hz shape with one real row.
        reasons.append(
            f"{measured_ticks - completed_ticks} of {measured_ticks} measured ticks "
            "produced no feature rows: the intended workload was not exercised on "
            "every tick, so the timing distribution mixes empty and real ticks"
        )
    if observations_measured != observations_expected:
        reasons.append(
            f"workload mismatch: observed {observations_measured} records over the "
            f"measured phase, requested {observations_expected}"
        )
    return tuple(reasons)


# ---------------------------------------------------------------------------
# the benchmark
# ---------------------------------------------------------------------------


def select_channels(count: int) -> tuple[str, ...]:
    """The first ``count`` supported channels, or a clear error."""
    if count < 1 or count > MAX_CHANNELS:
        raise ValueError(
            f"--channels must be between 1 and {MAX_CHANNELS} (the supported synthetic "
            f"OBD channel list: {', '.join(SUPPORTED_OBD_CHANNELS)}); got {count}"
        )
    return SUPPORTED_OBD_CHANNELS[:count]


def select_can_ids(bindings: Bindings | None) -> tuple[int, ...]:
    """Bound CAN IDs (sorted) or the non-decoding fallback set."""
    if bindings is None:
        return FALLBACK_CAN_IDS
    bound = tuple(sorted({sig.can_id for sig in bindings.signals}))
    return bound or FALLBACK_CAN_IDS


def _validate_counts(warmup_ticks: int, ticks: int, can_frames_per_tick: int) -> None:
    if warmup_ticks < 0:
        raise ValueError(f"--warmup-ticks must be >= 0; got {warmup_ticks}")
    if ticks < 1:
        raise ValueError(f"--ticks must be >= 1; got {ticks}")
    if can_frames_per_tick < 0:
        raise ValueError(f"--can-frames-per-tick must be >= 0; got {can_frames_per_tick}")


def _mode_counts(results: Sequence[TickResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        key = result.operating_mode or "<none>"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def run_benchmark(
    config: CanaryConfig,
    *,
    bindings: Bindings | None = None,
    warmup_ticks: int,
    ticks: int,
    channels: int,
    can_frames_per_tick: int,
    seed: int = 0,
    profile: str | None = None,
    provenance: Provenance | None = None,
    observation_epoch_offset_seconds: float = 0.0,
) -> BenchmarkReport:
    """Run warmup + measured ticks on one continuous scenario clock.

    ``observation_epoch_offset_seconds`` is test-only (see
    :func:`build_tick_workload`); it exists so the all-stale failure mode is
    reproducible and provably rejected.
    """
    _validate_counts(warmup_ticks, ticks, can_frames_per_tick)
    channel_names = select_channels(channels)
    can_ids = select_can_ids(bindings)
    period = tick_period_seconds(config)
    rng = np.random.default_rng(seed)
    source = LiveFeatureSource(config=config, bindings=bindings, session="benchmark")

    def workload(index: int) -> TickWorkload:
        return build_tick_workload(
            index,
            period_seconds=period,
            channels=channel_names,
            can_ids=can_ids,
            can_frames_per_tick=can_frames_per_tick,
            rng=rng,
            observation_epoch_offset_seconds=observation_epoch_offset_seconds,
        )

    warm = tuple(run_tick(source, workload(i)) for i in range(warmup_ticks))
    buffer_after_warmup = source.buffer_size
    measured = tuple(
        run_tick(source, workload(i)) for i in range(warmup_ticks, warmup_ticks + ticks)
    )
    total_ticks = warmup_ticks + ticks

    observations_per_tick = len(channel_names) + can_frames_per_tick
    observations_measured = sum(r.observations for r in measured)
    completed = sum(1 for r in measured if r.completed)
    reasons = validity_reasons(
        buffer_size_after_warmup=buffer_after_warmup,
        completed_ticks=completed,
        measured_ticks=ticks,
        observations_measured=observations_measured,
        observations_expected=observations_per_tick * ticks,
    )
    # Time only ticks that actually produced feature rows: an empty tick
    # measures nothing, and mixing it in is how a zero-row run once read as
    # 27,600 Hz. The report stays invalid whenever any tick was empty.
    timed = tuple(r for r in measured if r.completed)
    timings = {
        "observe": stage_timing([r.observe_ms for r in timed]),
        "tick": stage_timing([r.tick_ms for r in timed]),
        "total": stage_timing([r.total_ms for r in timed]),
    }
    p95_total = timings["total"].p95_ms
    return BenchmarkReport(
        valid=not reasons,
        invalid_reasons=reasons,
        host=host_identity(),
        provenance=provenance or collect_provenance(config, None, None),
        workload=WorkloadSummary(
            kind="synthetic",
            profile=profile,
            seed=seed,
            warmup_ticks=warmup_ticks,
            measured_ticks=ticks,
            resample_hz=float(config.preprocessing.resample_hz),
            tick_period_seconds=period,
            channels=channel_names,
            can_ids=tuple(f"0x{can_id:X}" for can_id in can_ids),
            can_frames_per_tick=can_frames_per_tick,
            observations_per_tick=observations_per_tick,
            observations_measured=observations_measured,
            scenario_start=tick_time(0, period).isoformat(),
            scenario_end=tick_time(total_ticks - 1, period).isoformat(),
            scenario_seconds=(total_ticks - 1) * period,
            observation_epoch_offset_seconds=observation_epoch_offset_seconds,
        ),
        results=Results(
            buffer_size_after_warmup=buffer_after_warmup,
            buffer_size_at_end=source.buffer_size,
            warmup_completed_ticks=sum(1 for r in warm if r.completed),
            completed_ticks=completed,
            empty_ticks=ticks - completed,
            feature_rows=sum(r.feature_rows for r in measured),
            feature_columns=max((r.feature_columns for r in measured), default=0),
            operating_modes=_mode_counts(measured),
            segmenter_fallback_ticks=sum(1 for r in measured if r.segmenter_fell_back),
            quality_gate_rejects=source.reject_counts,
            timings_ms=timings,
            microbenchmark_hz_at_p95=(
                1000.0 / p95_total if not reasons and p95_total > 0 else None
            ),
        ),
        generated_at=datetime.now(UTC).isoformat(),
        ticks=measured,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="benchmark_live_pipeline",
        description=(
            "Feature-only microbenchmark of LiveFeatureSource.observe()+tick(). "
            "Detector scoring is not timed (see task C10)."
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("canary.example.yaml"), help="Path to canary.yaml."
    )
    add_bindings_arg(parser)
    parser.add_argument(
        "--profile",
        choices=sorted(WORKLOAD_PROFILES),
        default="smoke",
        help=(
            "Workload preset (not a vehicle profile): "
            + "; ".join(
                f"{k} = {w} warmup / {m} measured ticks" for k, (w, m) in WORKLOAD_PROFILES.items()
            )
            + ". --ticks/--warmup-ticks override the preset."
        ),
    )
    parser.add_argument(
        "--ticks", type=int, default=None, help="Measured ticks (overrides preset)."
    )
    parser.add_argument(
        "--warmup-ticks", type=int, default=None, help="Warmup ticks (overrides preset)."
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=16,
        help=f"OBD channels to simulate, 1..{MAX_CHANNELS} (segmenter inputs first). Default 16.",
    )
    parser.add_argument(
        "--can-frames-per-tick",
        type=int,
        default=20,
        help="Exact CAN frames observed per tick, round-robin over the bound IDs. Default 20.",
    )
    parser.add_argument("--seed", type=int, default=0, help="RNG seed for the synthetic drive.")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help=f"JSON report path. Default: {DEFAULT_REPORT_DIR}/<profile>-<utc stamp>.json.",
    )
    parser.add_argument(
        "--include-ticks", action="store_true", help="Embed per-tick results in the JSON report."
    )
    # Test-only: reproduce the mismatched-epoch defect (observations on a
    # different epoch than the ticks) to prove the validity check rejects it.
    parser.add_argument(
        "--observation-epoch-offset-seconds", type=float, default=0.0, help=argparse.SUPPRESS
    )
    return parser.parse_args(argv)


def resolve_tick_counts(args: argparse.Namespace) -> tuple[int, int]:
    """(warmup, measured) from the preset, with explicit flags winning."""
    warmup, measured = WORKLOAD_PROFILES[args.profile]
    if args.warmup_ticks is not None:
        warmup = args.warmup_ticks
    if args.ticks is not None:
        measured = args.ticks
    return warmup, measured


def default_report_path(profile: str, now: datetime) -> Path:
    return DEFAULT_REPORT_DIR / f"{profile}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"


def write_report(report: BenchmarkReport, path: Path, *, include_ticks: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(include_ticks=include_ticks), indent=2) + "\n")


def _fmt_stage(name: str, timing: StageTiming) -> str:
    return (
        f"  {name:<8} median {timing.median_ms:8.3f}  p95 {timing.p95_ms:8.3f}  "
        f"p99 {timing.p99_ms:8.3f}  max {timing.max_ms:8.3f}  (ms, n={timing.count})"
    )


def print_report(report: BenchmarkReport, report_path: Path | None = None) -> None:
    w, r = report.workload, report.results
    print("=" * 72)
    print(f"{report.benchmark}  [{'VALID' if report.valid else 'INVALID'}]")
    print("=" * 72)
    print(f"  scope:                    {report.scope}")
    print(f"  host:                     {report.host.hostname} ({report.host.platform})")
    print(f"  python / git:             {report.host.python} / {report.host.git_revision}")
    print(f"  config:                   {report.provenance.config_path}")
    print(f"  bindings:                 {report.provenance.bindings_path}")
    print(f"  profile / seed:           {w.profile} / {w.seed}")
    print(f"  resample_hz / period:     {w.resample_hz} Hz / {w.tick_period_seconds:.4f} s")
    print(f"  warmup / measured ticks:  {w.warmup_ticks} / {w.measured_ticks}")
    span = f"{w.scenario_start} -> {w.scenario_end}"
    print(f"  scenario span:            {span} ({w.scenario_seconds:.1f} s)")
    print(f"  channels ({len(w.channels)}):            {', '.join(w.channels)}")
    print(f"  CAN frames/tick over IDs: {w.can_frames_per_tick} over {len(w.can_ids)} IDs")
    print(f"  observations measured:    {w.observations_measured} ({w.observations_per_tick}/tick)")
    print()
    print(f"  buffer after warmup/end:  {r.buffer_size_after_warmup} / {r.buffer_size_at_end}")
    print(f"  completed / empty ticks:  {r.completed_ticks} / {r.empty_ticks}")
    print(f"  feature rows / columns:   {r.feature_rows} / {r.feature_columns}")
    print(f"  operating modes:          {r.operating_modes}")
    print(f"  segmenter fallback ticks: {r.segmenter_fallback_ticks}")
    print(f"  quality-gate rejects:     {r.quality_gate_rejects or '{}'}")
    print()
    for name in ("observe", "tick", "total"):
        print(_fmt_stage(name, r.timings_ms[name]))
    print()
    hz = r.microbenchmark_hz_at_p95
    print(f"  1/p95(total):             {'n/a' if hz is None else f'{hz:.1f} Hz'}")
    print(f"    ({r.microbenchmark_hz_note})")
    if report.invalid_reasons:
        print()
        print("  INVALID — do not quote this run:")
        for reason in report.invalid_reasons:
            print(f"    - {reason}")
    if report_path is not None:
        print()
        print(f"  report: {report_path}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    warmup, measured = resolve_tick_counts(args)
    # The vehicle's default profile (``--profile`` here is the workload
    # preset): the live source refuses an unscoped config (P02.11).
    config = load_scoped_config(args, None)
    bindings_path = args.bindings if args.bindings.is_file() else None
    bindings = load_bindings(bindings_path) if bindings_path is not None else None
    print(f"running {args.profile}: {warmup} warmup + {measured} measured ticks…", file=sys.stderr)
    try:
        report = run_benchmark(
            config,
            bindings=bindings,
            warmup_ticks=warmup,
            ticks=measured,
            channels=args.channels,
            can_frames_per_tick=args.can_frames_per_tick,
            seed=args.seed,
            profile=args.profile,
            provenance=collect_provenance(config, args.config, bindings_path),
            observation_epoch_offset_seconds=args.observation_epoch_offset_seconds,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INVALID
    report_path = args.report or default_report_path(args.profile, datetime.now(UTC))
    write_report(report, report_path, include_ticks=args.include_ticks)
    print_report(report, report_path)
    return 0 if report.valid else EXIT_INVALID


if __name__ == "__main__":
    sys.exit(main())
