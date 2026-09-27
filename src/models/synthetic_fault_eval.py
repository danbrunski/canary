"""Synthetic-fault detector-quality eval (Phase 7.5).

Builds the "did the detector actually fire on a known fault, and how often
does it cry wolf on clean data?" answer the plan asks for. For each fault
kind in :data:`DEFAULT_FAULT_KINDS` (the four real sensor-failure modes
plus the multiplicative *scale* fault), this:

* injects the fault into a copy of a clean *resampled* wide frame,
* re-runs the shared feature builder (so the rolling features pick up the
  injected values exactly like training does),
* scores with the trained Isolation Forest, the sequence detector when an
  artifact is supplied, and the fused stream built from those sources the
  way the daemon builds it (each source persistence-gated, then
  ``FusionScorer.fuse``). With fewer sources than
  ``models.fusion.min_agreement`` fused cannot fire by construction, so it
  is *withheld* with the reason rather than graded as recall 0 (A02.9),
* runs every row through a fresh ``PersistenceGate`` (config's 6.1 M-of-N —
  ``alerts.min_consecutive_ticks`` of ``alerts.persistence_window_ticks``,
  wall-clock-bound by ``alerts.persistence_window_seconds`` off the row's
  own timestamp) — a "hit" is the first row whose
  *sustained* verdict lands within ``[start, end + rolling_window_seconds)``
  (8.2: a fault whose feature signature only fully forms after the window
  closes, once the rolling mean/std/roc has "seen" it, still counts as a
  late-but-real detection with recorded latency, not a miss) — and counts
  alerts outside any window for the nominal false-positive rate.

The harness stays IF-only (fast, torch-free; fused withheld) when no
sequence-detector artifact is supplied; passing a ``trained_sequence`` (2.3) additionally ticks
every row through the real ``SequenceInferenceEngine.observe`` windowed
contract, gated through its own fresh ``PersistenceGate`` key, and adds a
third ``detector="sequence"`` metrics entry per fault (plus a ``sequence``
block in ``nominal_fp``) — mirroring IF/fused exactly. The LSTM/TCN
training-completion + per-mode threshold-population is graded by a separate
dedicated test. The report is JSON-safe so the training ledger / dashboard
can consume it.

6.5 added a sixth kind, ``"intermittent"`` (fire-gap-fire, the pattern 6.1's
M-of-N ``PersistenceGate`` was built to catch) — kept out of
``DEFAULT_FAULT_KINDS`` so it doesn't change the CI regression-guard's
acceptance floor, but fully wired through ``_inject_one``; pass
``fault_kinds=(INTERMITTENT_FAULT_KIND,)`` explicitly to exercise it.

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

from src.alerts.fusion import (
    FusionScorer,
    FusionSource,
    PersistenceGate,
    downgrade_unsustained,
    isolation_forest_source,
    lstm_source,
)
from src.columns import TIMESTAMP_COLUMN
from src.config import CanaryConfig
from src.models.inference import SequenceInferenceEngine, SequenceInferenceResult
from src.models.isolation_forest import (
    CALIBRATED_THRESHOLD_COLUMN,
    MISSING_FEATURES_COLUMN,
    MODEL_MODE_COLUMN,
    SCORE_COLUMN,
    TrainedIsolationForest,
    served_verdicts,
)
from src.models.sequence_protocol import TrainedSequenceModel
from src.preprocessing.anomaly_injection import AnomalyInjector, AnomalyKind
from src.preprocessing.feature_pipeline import featurize_wide_frame
from src.preprocessing.resampler import resample_config_for

DEFAULT_FAULT_KINDS: tuple[AnomalyKind, ...] = (
    "spike",
    "drift",
    "dropout",
    "stuck",
    "scale",
)

#: Not part of DEFAULT_FAULT_KINDS: adding it there would change the CI
#: regression-guard's acceptance floor (`test_evaluate_detectors_grades_
#: each_fault_kind_and_keeps_nominal_fp_low`), which was tuned against the
#: five kinds above. It's fully wired through `_inject_one`/`evaluate_detectors`
#: — pass `fault_kinds=("intermittent",)` (or with others) explicitly for the
#: 6.5 persistence-gate benchmark.
INTERMITTENT_FAULT_KIND: AnomalyKind = "intermittent"


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
    """Boolean per-row IF verdict as served — one definition,
    :func:`src.models.isolation_forest.served_verdicts` (A04.9): the
    calibrated verdict, uncalibrated modes non-emitting, missing-feature rows
    ``None`` (no trustworthy verdict, matching the daemon's fail-safe)."""
    return served_verdicts(scored)


#: Persistence-gate key the daemon uses for the sequence detector's fusion
#: source (``daemon.inference``) — the harness gates the same key the same way.
SEQUENCE_FUSION_KEY = "lstm_autoencoder"


def _persistence_gate(config: CanaryConfig) -> PersistenceGate:
    """A fresh production M-of-N gate (``alerts.*``), one per scoring pass."""
    return PersistenceGate(
        config.alerts.min_consecutive_ticks,
        window=config.alerts.persistence_window_ticks,
        window_seconds=config.alerts.persistence_window_seconds,
    )


def fusion_withheld_reason(available_sources: Sequence[str], config: CanaryConfig) -> str | None:
    """Why the fused detector cannot be graded on these sources, or ``None``.

    A02.9: the 2026-09-20 benchmark reported fused recall 0 of 250. The fused
    verdict needs ``models.fusion.min_agreement`` independent firing sources
    (6.2); that run had the Isolation Forest alone (``canary.dev.example.yaml``
    disables the sequence detector, and this harness never simulates the
    behavior residuals), so fusion could not fire *by construction*. Reporting
    that as recall 0.0 read as a detector that looked and missed. It is now
    withheld and says why.
    """
    required = int(config.models.fusion.min_agreement)
    if len(available_sources) >= required:
        return None
    return (
        f"fusion requires {required} independent sources (models.fusion.min_agreement) "
        f"and only {len(available_sources)} ran in this harness "
        f"({', '.join(available_sources) or 'none'}); behavior residuals are not simulated"
    )


def _fused_anomalous(
    scored: pd.DataFrame,
    *,
    fusion: FusionScorer,
    scales: dict[str, float],
    config: CanaryConfig,
    sequence_results: Sequence[SequenceInferenceResult | None] | None = None,
) -> pd.Series:
    """Boolean fused verdict per row, built the way the daemon builds it.

    Each source passes its own persistence gate first and an unsustained one
    is downgraded to non-firing (``downgrade_unsustained``) before
    ``FusionScorer.fuse`` sees it, exactly as ``daemon.inference`` does; the
    fused verdict itself is then *not* gated again (the daemon emits it
    directly), so callers pass ``"fused"`` in ``pre_gated``.

    ``sequence_results`` (aligned with ``scored``) adds the sequence detector
    as the second source via ``lstm_source(global_score, global_threshold)``
    — the daemon's fusion input, which is the global score only, never the
    per-family path. Without it fusion has one source and can never reach
    ``min_agreement`` (6.2); ``evaluate_detectors`` withholds the fused
    metrics in that case instead of reporting a structural zero (A02.9).
    """
    out = np.full(len(scored), None, dtype=object)
    stamps = pd.to_datetime(scored[TIMESTAMP_COLUMN], utc=True)
    seconds = (stamps - pd.Timestamp(0, tz="UTC")).dt.total_seconds().to_numpy()
    modes = scored[MODEL_MODE_COLUMN].to_numpy(dtype=object)
    thresholds = pd.to_numeric(scored[CALIBRATED_THRESHOLD_COLUMN], errors="coerce").to_numpy(
        dtype=float
    )
    score_values = pd.to_numeric(scored[SCORE_COLUMN], errors="coerce").to_numpy(dtype=float)
    missing = (
        scored[MISSING_FEATURES_COLUMN].fillna(False).astype(bool).to_numpy()
        if MISSING_FEATURES_COLUMN in scored.columns
        else np.zeros(len(scored), dtype=bool)
    )
    gate = _persistence_gate(config)
    for i in range(len(scored)):
        now = float(seconds[i])
        sources: list[FusionSource] = []
        if_verdict: bool | None = None
        mode = modes[i]
        threshold = thresholds[i]
        score = score_values[i]
        if not (
            bool(missing[i])
            or mode is None
            or pd.isna(mode)
            or np.isnan(threshold)
            or np.isnan(score)
        ):
            ifsrc = isolation_forest_source(
                float(score),
                float(threshold),
                scale=scales.get(str(mode)),
            )
            if_verdict = ifsrc.fired
            sources.append(
                downgrade_unsustained(ifsrc, gate.observe("isolation_forest", ifsrc.fired, now=now))
            )
        else:
            gate.observe("isolation_forest", None, now=now)
        if sequence_results is not None:
            result = sequence_results[i]
            if result is None:
                gate.observe(SEQUENCE_FUSION_KEY, None, now=now)
            else:
                seqsrc = lstm_source(result.global_score, result.global_threshold)
                sustained = gate.observe(SEQUENCE_FUSION_KEY, result.is_anomaly, now=now)
                sources.append(downgrade_unsustained(seqsrc, sustained))
        if not sources or (if_verdict is None and sequence_results is None):
            continue
        mode_label = None if mode is None or pd.isna(mode) else str(mode)
        out[i] = fusion.fuse(sources, timestamp="", operating_mode=mode_label).is_anomaly
    return pd.Series(out, index=scored.index)


def _sequence_results(
    engine: SequenceInferenceEngine, featurized: pd.DataFrame
) -> list[SequenceInferenceResult | None]:
    """Tick ``featurized`` row by row through the real ``observe`` contract.

    ``engine`` is reset first so a previous run's tail never leaks into this
    one. Rows before the window fills are ``None`` (no verdict yet).
    """
    engine.reset()
    return [engine.observe(featurized.iloc[i]) for i in range(len(featurized))]


def _sequence_anomalous(engine: SequenceInferenceEngine, featurized: pd.DataFrame) -> pd.Series:
    """Boolean per-row sequence-detector verdict from the real windowed
    ``observe`` contract (2.3) — ticks ``featurized`` row-by-row through
    ``engine`` exactly as the live daemon's ``SequenceDetector`` (Task 3.1)
    does.

    ``engine``'s buffer is cleared first so a previous run's tail sequence
    never leaks into this one (fresh state per injection run, same reason
    ``persistence`` is rebuilt per run). A row before the buffer has filled
    (``observe`` returns ``None``, no verdict yet) is ``None`` — the live
    daemon simply doesn't emit anything for it either."""
    return _sequence_verdicts(_sequence_results(engine, featurized), featurized.index)


def _sequence_verdicts(
    results: Sequence[SequenceInferenceResult | None], index: pd.Index
) -> pd.Series:
    out = np.full(len(results), None, dtype=object)
    for i, result in enumerate(results):
        if result is not None:
            out[i] = bool(result.is_anomaly)
    return pd.Series(out, index=index)


def _first_sustained_detection(
    *,
    signals: Mapping[str, pd.Series],
    timestamps: pd.Series,
    persistence: PersistenceGate,
    match_start: pd.Timestamp,
    match_end: pd.Timestamp,
    grace_end: pd.Timestamp,
    pre_gated: frozenset[str] = frozenset(),
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

    ``pre_gated`` names signals that already went through persistence
    upstream (the fused verdict, whose sources are gated individually the
    way the daemon gates them) — gating them twice would delay them past
    what the daemon emits.
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
            verdict = None if fired is None or pd.isna(fired) else bool(fired)
            if name in pre_gated:
                sustained = verdict is True
            else:
                sustained = persistence.observe(name, verdict, now=now)
            if detected[name] is None and verdict is True and sustained and in_match:
                detected[name] = (ts, ts >= match_end)
    return detected


def _persistence_gated_nominal(
    signals: Mapping[str, pd.Series],
    timestamps: pd.Series,
    config: CanaryConfig,
    *,
    pre_gated: frozenset[str] = frozenset(),
) -> dict[str, pd.Series]:
    """Run nominal per-tick verdicts through the production M-of-N gate
    (signals named in ``pre_gated`` were gated upstream and pass through)."""
    persistence = _persistence_gate(config)
    gated = {name: np.zeros(len(timestamps), dtype=bool) for name in signals}
    for index, row in enumerate(zip(timestamps, *(signals[name] for name in signals), strict=True)):
        timestamp, verdicts = row[0], row[1:]
        now = pd.Timestamp(timestamp).timestamp()
        for name, verdict in zip(signals, verdicts, strict=True):
            firing = None if verdict is None or pd.isna(verdict) else bool(verdict)
            sustained = (
                firing is True if name in pre_gated else persistence.observe(name, firing, now=now)
            )
            gated[name][index] = firing is True and sustained
    return {name: pd.Series(values, index=timestamps.index) for name, values in gated.items()}


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
    return resample_config_for(config).max_gap_seconds_for(channel)


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
    elif kind == "intermittent":
        # 2s on / 1s off: each "on" burst is shorter than a strict
        # 3-consecutive persistence gate's requirement (so that gate can
        # never latch), while the 1s gap is well inside the M-of-N gate's
        # trailing window (default 5 ticks / 8s) so 6.1 recovers it. Reuses
        # `spike`'s exact magnitude for the "on" value — same fault
        # physics, different timing (6.5).
        inj.intermittent(
            channel=channel,
            start=start,
            end=end,
            value=nominal_max * 5 + 1_000,
            on_seconds=2.0,
            off_seconds=1.0,
        )
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
    runs IF only and withholds fused (one source cannot meet
    ``min_agreement``). Returns a dict with ``per_fault`` metrics for each
    graded detector, a ``nominal_fp`` block measured on the unmodified frame,
    and ``withheld`` naming each detector that could not be graded and why.
    """
    rng = np.random.default_rng(seed)
    fusion = FusionScorer(config.models.fusion, roles=config.models.roles.by_source())
    scales = dict(trained_if.score_scales)
    sequence_engine = (
        SequenceInferenceEngine(trained_sequence) if trained_sequence is not None else None
    )
    fusion_sources = ["isolation_forest", *(["sequence"] if sequence_engine else [])]
    withheld: dict[str, str] = {}
    fused_reason = fusion_withheld_reason(fusion_sources, config)
    if fused_reason is not None:
        withheld["fused"] = fused_reason
    detector_names = [
        "isolation_forest",
        *([] if "fused" in withheld else ["fused"]),
        *(["sequence"] if sequence_engine else []),
    ]
    pre_gated = frozenset({"fused"})

    def _signals(scored: pd.DataFrame, featurized: pd.DataFrame) -> dict[str, pd.Series]:
        results = (
            _sequence_results(sequence_engine, featurized) if sequence_engine is not None else None
        )
        signals = {"isolation_forest": _if_anomalous(scored)}
        if "fused" not in withheld:
            signals["fused"] = _fused_anomalous(
                scored,
                fusion=fusion,
                scales=scales,
                config=config,
                sequence_results=results,
            )
        if results is not None:
            signals["sequence"] = _sequence_verdicts(results, featurized.index)
        return signals

    # Nominal: one featurize + score pass — this is the FP rate every other
    # injection run is measured against.
    nominal_features = featurize_wide_frame(resampled, config).features
    nominal_scored = trained_if.score(nominal_features)
    nominal_signals = _persistence_gated_nominal(
        _signals(nominal_scored, nominal_features),
        pd.to_datetime(nominal_features[TIMESTAMP_COLUMN], utc=True),
        config,
        pre_gated=pre_gated,
    )
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
            hits = _first_sustained_detection(
                signals=_signals(scored, featurized),
                timestamps=timestamps,
                persistence=_persistence_gate(config),
                match_start=window.start,
                match_end=window.end,
                grace_end=window.end + grace,
                pre_gated=pre_gated,
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
        # A02.9: a detector that could not be graded is named with the reason,
        # never reported as a zero recall.
        "withheld": withheld,
    }


__all__ = [
    "DEFAULT_FAULT_KINDS",
    "INTERMITTENT_FAULT_KIND",
    "evaluate_detectors",
    "fusion_withheld_reason",
]
