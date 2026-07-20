"""Synthetic-fault detector-quality eval (Phase 7.5).

Builds the "did the detector actually fire on a known fault, and how often
does it cry wolf on clean data?" answer the plan asks for. For each fault
kind in :data:`DEFAULT_FAULT_KINDS` (the four real sensor-failure modes
plus the multiplicative *scale* fault), this:

* injects the fault into a copy of a clean *resampled* wide frame,
* re-runs the shared feature builder (so the rolling features pick up the
  injected values exactly like training does),
* scores with the trained Isolation Forest and the fused stream (the IF
  half of fusion is exercised even when no LSTM/behavior sources are
  available, so the regression guard sees the fusion code path),
* runs every row through a fresh ``PersistenceGate`` (config's 6.1 M-of-N —
  ``alerts.min_consecutive_ticks`` of ``alerts.persistence_window_ticks``,
  wall-clock-bound by ``alerts.persistence_window_seconds`` off the row's
  own timestamp) — a "hit" is the first row whose
  *sustained* verdict lands within ``[start, end + rolling_window_seconds)``
  (8.2: a fault whose feature signature only fully forms after the window
  closes, once the rolling mean/std/roc has "seen" it, still counts as a
  late-but-real detection with recorded latency, not a miss) — and counts
  alerts outside any window for the nominal false-positive rate.

The harness stays IF/fused-only (fast, torch-free) when no sequence-detector
artifact is supplied; passing a ``trained_sequence`` (2.3) additionally ticks
every row through the real ``SequenceInferenceEngine.observe`` windowed
contract, gated through its own fresh ``PersistenceGate`` key, and adds a
third ``detector="sequence"`` metrics entry per fault (plus a ``sequence``
block in ``nominal_fp``) — mirroring IF/fused exactly. The LSTM/TCN
training-completion + per-mode threshold-population is graded by a separate
dedicated test. The report is JSON-safe so the training ledger / dashboard
can consume it.

Task 2.6 (ml-correctness-hardening) trialed ``pyod``'s ECOD as an optional
fourth detector here, wired the same way ``trained_sequence`` is. ECOD lost
decisively on the seeded synthetic-fault benchmark (mean detection rate
~0.5-0.76 vs IF's ~0.97-0.99 across 3 seeds, and it missed "dropout" almost
entirely: 0.0-0.05 vs IF's 1.0) so the wiring was reverted per the task's own
instruction to drop a losing dependency rather than leave it half-adopted.
See the roadmap doc's landed-note for the full numbers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.alerts.fusion import FusionScorer, PersistenceGate, isolation_forest_source
from src.config import CanaryConfig
from src.models.inference import SequenceInferenceEngine
from src.models.isolation_forest import (
    CALIBRATED_ANOMALY_COLUMN,
    CALIBRATED_THRESHOLD_COLUMN,
    LABEL_COLUMN,
    MISSING_FEATURES_COLUMN,
    MODEL_MODE_COLUMN,
    SCORE_COLUMN,
    TrainedIsolationForest,
)
from src.models.sequence_protocol import TrainedSequenceModel
from src.preprocessing.anomaly_injection import AnomalyInjector, AnomalyKind
from src.preprocessing.feature_pipeline import featurize_wide_frame
from src.preprocessing.resampler import TIMESTAMP_COLUMN, ResampleConfig, is_slow_channel

DEFAULT_FAULT_KINDS: tuple[AnomalyKind, ...] = (
    "spike",
    "drift",
    "dropout",
    "stuck",
    "scale",
)


@dataclass(frozen=True)
class _DetectorMetrics:
    detector: str
    fault_kind: str
    runs: int
    detected: int
    #: Seconds from injection-window start to the first sustained
    #: (persistence-gated) detection, one entry per detected run — empty
    #: when ``detected == 0`` (2.4).
    latencies_seconds: tuple[float, ...] = ()
    #: Parallel to ``latencies_seconds`` — True where that hit landed in
    #: the post-window grace period (at/after the injection's ``end``,
    #: within ``end + window_seconds``) rather than strictly inside
    #: ``[start, end)`` (8.2). Kept separate from ``latencies_seconds`` so
    #: downstream latency/rate consumers can exclude or flag late hits
    #: instead of conflating them with in-window ones.
    late_flags: tuple[bool, ...] = ()

    @property
    def detection_rate(self) -> float:
        return self.detected / self.runs if self.runs else 0.0

    @property
    def late_hits(self) -> int:
        return sum(self.late_flags)

    @property
    def median_time_to_detect_seconds(self) -> float | None:
        return float(np.median(self.latencies_seconds)) if self.latencies_seconds else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "fault_kind": self.fault_kind,
            "runs": self.runs,
            "detected": self.detected,
            "detection_rate": self.detection_rate,
            "latencies_seconds": list(self.latencies_seconds),
            "late_flags": list(self.late_flags),
            "late_hits": self.late_hits,
            "median_time_to_detect_seconds": self.median_time_to_detect_seconds,
        }


def _if_anomalous(scored: pd.DataFrame) -> pd.Series:
    """Boolean per-row IF verdict mirroring ``emitter.emit_isolation_forest_row``:
    calibrated when present, legacy ``-1`` label fallback otherwise, with
    missing-features rows always anomalous (they aren't scoreable)."""
    missing = scored[MISSING_FEATURES_COLUMN].astype(bool)
    calib = scored[CALIBRATED_ANOMALY_COLUMN]
    if calib.notna().any():
        return missing | calib.fillna(False).astype(bool)
    return missing | (scored[LABEL_COLUMN].fillna(1).astype(int) == -1)


def _fused_anomalous(
    scored: pd.DataFrame,
    *,
    fusion: FusionScorer,
    scales: dict[str, float],
) -> pd.Series:
    """Boolean fused-anomaly per row using IF as the only fusion source.

    Surfacing it through the actual ``FusionScorer`` path exercises the
    production fusion code even with no LSTM/behavior sources available.
    6.2: fused ``is_anomaly`` now requires ``min_agreement`` (default 2)
    *independent* firing sources, so with IF as the sole source here fused
    structurally never fires — that's the intended agreement gate working,
    not a bug in this harness; the regression guard checks the code path
    runs cleanly (and that the FP floor is genuinely zero), not that it
    matches IF's own verdict any more."""
    out = np.zeros(len(scored), dtype=bool)
    for i in range(len(scored)):
        row = scored.iloc[i]
        if bool(row.get(MISSING_FEATURES_COLUMN, False)):
            out[i] = True
            continue
        mode = row.get(MODEL_MODE_COLUMN)
        threshold = row.get(CALIBRATED_THRESHOLD_COLUMN)
        if mode is None or pd.isna(mode) or pd.isna(threshold):
            continue
        score = row.get(SCORE_COLUMN)
        if pd.isna(score):
            continue
        ifsrc = isolation_forest_source(
            float(score),
            float(threshold),
            scale=scales.get(str(mode)),
        )
        out[i] = fusion.fuse([ifsrc], timestamp="", operating_mode=str(mode)).is_anomaly
    return pd.Series(out, index=scored.index)


def _sequence_anomalous(engine: SequenceInferenceEngine, featurized: pd.DataFrame) -> pd.Series:
    """Boolean per-row sequence-detector verdict from the real windowed
    ``observe`` contract (2.3) — ticks ``featurized`` row-by-row through
    ``engine`` exactly as the live daemon's ``run_live_inference_lstm`` does.

    ``engine``'s buffer is cleared first so a previous run's tail sequence
    never leaks into this one (fresh state per injection run, same reason
    ``persistence`` is rebuilt per run). A row before the buffer has filled
    (``observe`` returns ``None``, no verdict yet) reads as not-anomalous —
    the live daemon simply doesn't emit anything for it either."""
    engine.buffer.clear()
    out = np.zeros(len(featurized), dtype=bool)
    for i in range(len(featurized)):
        result = engine.observe(featurized.iloc[i])
        if result is not None:
            out[i] = result.is_anomaly
    return pd.Series(out, index=featurized.index)


def _first_sustained_detection(
    *,
    signals: Mapping[str, pd.Series],
    timestamps: pd.Series,
    persistence: PersistenceGate,
    match_start: pd.Timestamp,
    match_end: pd.Timestamp,
    grace_end: pd.Timestamp,
) -> dict[str, tuple[pd.Timestamp, bool] | None]:
    """First row within ``[match_start, grace_end)`` where each detector's
    persistence-gated verdict flips sustained (2.4/8.2).

    ``signals`` maps a detector name (its persistence-gate key, e.g.
    ``"isolation_forest"``, ``"fused"``, ``"sequence"`` — 2.3) to its
    per-row boolean verdict series; the return dict carries one entry per
    ``signals`` key. ``grace_end`` extends the match window
    ``window_seconds`` past ``match_end`` (the actual fault-injection end) so
    a fault whose feature signature only fully forms once the rolling
    window has "seen" it still counts as a (late) hit rather than a miss.
    Each return value pairs the detection timestamp with whether it landed
    in that post-``match_end`` grace period (``True``) vs strictly inside
    ``[match_start, match_end)`` (``False``), so callers can keep late hits
    distinguishable from in-window ones.

    Feeds every row of the frame through ``persistence`` in order — not just
    the match window — so a real consecutive-tick streak has to build
    exactly like the live daemon (a streak starting a couple of rows before
    the window, or continuing into the grace period, is handled correctly
    rather than resetting at an arbitrary slice boundary). ``persistence``
    must be fresh per injection run: sharing one gate across runs would leak
    streak state from a previous run's tail into the next run's start.
    """
    names = tuple(signals)
    detected: dict[str, tuple[pd.Timestamp, bool] | None] = dict.fromkeys(names)
    for row in zip(timestamps, *(signals[name] for name in names), strict=True):
        ts, fired_values = row[0], row[1:]
        in_match = match_start <= ts < grace_end
        # 6.1 — the gate's wall-clock bound is keyed off *data* time (the
        # row's own timestamp), not however fast this loop actually runs,
        # so a synthetic frame with a real multi-second data gap exercises
        # the same eviction the live daemon would apply, instead of every
        # row landing within milliseconds of real wall-clock time.
        now = ts.timestamp()
        for name, fired in zip(names, fired_values, strict=True):
            sustained = persistence.observe(name, bool(fired), now=now)
            if detected[name] is None and sustained and in_match:
                detected[name] = (ts, ts >= match_end)
    return detected


def _hours_covered(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    span = (timestamps.max() - timestamps.min()).total_seconds()
    return max(span / 3600.0, 1.0 / 3600.0)  # floor at 1 second to avoid div-by-zero


def _max_gap_seconds_for(channel: str, config: CanaryConfig) -> float:
    """The same fast/slow forward-fill ceiling ``resample_wide`` applies to
    ``channel``, so a synthetic dropout (8.1) holds its value for exactly as
    long as production would before the cell goes NaN."""
    resample_config = ResampleConfig(
        hz=config.preprocessing.resample_hz,
        slow_channel_max_gap_seconds=config.preprocessing.slow_channel_max_gap_seconds,
    )
    if is_slow_channel(channel):
        return resample_config.slow_channel_max_gap_seconds
    return resample_config.max_gap.total_seconds()


def _inject_one(
    resampled: pd.DataFrame,
    *,
    kind: AnomalyKind,
    channel: str,
    rng: np.random.Generator,
    config: CanaryConfig,
) -> AnomalyInjector:
    """Inject one fault of ``kind`` into a copy of ``resampled``.

    Window placement is deterministic-with-jitter from ``rng`` so multiple
    runs cover different stretches of the frame. Fault magnitudes are sized
    to be unambiguously anomalous given the channel's nominal spread (this
    is *detector quality* eval, not threshold-edge calibration — 7.5's harness
    grades "do the detectors see a clear fault at all", not "how close to
    the boundary can we get")."""
    frame = resampled.copy()
    inj = AnomalyInjector(frame)
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    n = len(timestamps)
    window_len = max(5, n // 10)
    # Place the window in the middle ± a jittered offset, bounded inside.
    centre = n // 2 + int(rng.integers(-n // 8, n // 8 + 1))
    centre = max(window_len, min(n - window_len - 1, centre))
    start = timestamps.iloc[centre - window_len // 2]
    end = timestamps.iloc[centre + window_len // 2]
    series = frame[channel].dropna()
    nominal_max = float(series.max()) if not series.empty else 1.0
    nominal_std = float(series.std()) if not series.empty else 1.0
    if kind == "spike":
        inj.spike(channel=channel, start=start, end=end, value=nominal_max * 5 + 1_000)
    elif kind == "drift":
        inj.drift(channel=channel, start=start, end=end, total_delta=nominal_std * 10 + 100)
    elif kind == "dropout":
        inj.dropout(
            channel=channel,
            start=start,
            end=end,
            max_gap_seconds=_max_gap_seconds_for(channel, config),
        )
    elif kind == "stuck":
        inj.stuck(channel=channel, start=start, end=end)
    elif kind == "scale":
        inj.scale(channel=channel, start=start, end=end, factor=5.0)
    else:
        raise ValueError(f"unsupported fault kind: {kind!r}")
    return inj


def evaluate_detectors(
    resampled: pd.DataFrame,
    config: CanaryConfig,
    trained_if: TrainedIsolationForest,
    *,
    fault_channel: str,
    fault_kinds: Sequence[AnomalyKind] = DEFAULT_FAULT_KINDS,
    n_injections: int = 5,
    seed: int = 42,
    trained_sequence: TrainedSequenceModel | None = None,
) -> dict[str, Any]:
    """Run the synthetic-fault harness and return a JSON-safe report.

    ``resampled`` is the clean wide frame *before* ``featurize_wide_frame``
    (the harness re-featurizes after each injection so rolling stats reflect
    the fault). ``trained_if`` is the model the harness grades. ``trained_sequence``
    (2.3) is optional — the LSTM/TCN artifact the daemon would actually load for
    its configured ``sequence_detector`` backbone; when ``None`` (no manifest
    configured/available, the common case in this test environment) the harness
    runs exactly as before, IF/fused only. Returns a dict with ``per_fault``
    metrics for each detector + a ``nominal_fp`` block measured on the
    unmodified frame.
    """
    rng = np.random.default_rng(seed)
    fusion = FusionScorer(config.models.fusion)
    scales = dict(trained_if.score_scales)
    sequence_engine = (
        SequenceInferenceEngine(trained_sequence) if trained_sequence is not None else None
    )
    detector_names = ["isolation_forest", "fused", *(["sequence"] if sequence_engine else [])]

    # Nominal: one featurize + score pass — this is the FP rate every other
    # injection run is measured against.
    nominal_features = featurize_wide_frame(resampled, config).features
    nominal_scored = trained_if.score(nominal_features)
    nominal_signals = {
        "isolation_forest": _if_anomalous(nominal_scored),
        "fused": _fused_anomalous(nominal_scored, fusion=fusion, scales=scales),
    }
    if sequence_engine is not None:
        nominal_signals["sequence"] = _sequence_anomalous(sequence_engine, nominal_features)
    hours = _hours_covered(nominal_features)
    nominal_fp = {
        name: {
            "alerts": int(signal.sum()),
            "rows": int(len(signal)),
            "hours": hours,
            "per_hour": round(float(signal.sum()) / hours, 4),
        }
        for name, signal in nominal_signals.items()
    }

    # 8.2's post-window grace: a fault whose rolling-feature signature only
    # fully forms after the injection window closes still counts as a late
    # (not missed) detection, up to one full feature window past `end`.
    grace = pd.Timedelta(seconds=config.preprocessing.rolling_window_seconds)

    per_fault: list[dict[str, Any]] = []
    for kind in fault_kinds:
        hit_counts = dict.fromkeys(detector_names, 0)
        latencies: dict[str, list[float]] = {name: [] for name in detector_names}
        late_flags: dict[str, list[bool]] = {name: [] for name in detector_names}
        for _ in range(n_injections):
            inj = _inject_one(resampled, kind=kind, channel=fault_channel, rng=rng, config=config)
            featurized = featurize_wide_frame(inj.df, config).features
            scored = trained_if.score(featurized)
            timestamps = pd.to_datetime(featurized[TIMESTAMP_COLUMN], utc=True)
            window = inj.windows[0]
            # Fresh gate per run — 2.4: run scoring through the production
            # persistence config so a hit reflects what would actually alert,
            # not any single over-threshold row.
            persistence = PersistenceGate(
                config.alerts.min_consecutive_ticks,
                window=config.alerts.persistence_window_ticks,
                window_seconds=config.alerts.persistence_window_seconds,
            )
            signals = {
                "isolation_forest": _if_anomalous(scored),
                "fused": _fused_anomalous(scored, fusion=fusion, scales=scales),
            }
            if sequence_engine is not None:
                signals["sequence"] = _sequence_anomalous(sequence_engine, featurized)
            hits = _first_sustained_detection(
                signals=signals,
                timestamps=timestamps,
                persistence=persistence,
                match_start=window.start,
                match_end=window.end,
                grace_end=window.end + grace,
            )
            for name in detector_names:
                hit = hits[name]
                if hit is not None:
                    ts, late = hit
                    hit_counts[name] += 1
                    latencies[name].append((ts - window.start).total_seconds())
                    late_flags[name].append(late)
        for name in detector_names:
            per_fault.append(
                _DetectorMetrics(
                    detector=name,
                    fault_kind=kind,
                    runs=n_injections,
                    detected=hit_counts[name],
                    latencies_seconds=tuple(latencies[name]),
                    late_flags=tuple(late_flags[name]),
                ).to_dict()
            )

    return {
        "fault_kinds": list(fault_kinds),
        "n_injections": n_injections,
        "seed": seed,
        "nominal_fp": nominal_fp,
        "per_fault": per_fault,
    }


__all__ = ["DEFAULT_FAULT_KINDS", "evaluate_detectors"]
