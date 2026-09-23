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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
import yaml

from src.alerts.fusion import (
    FusionScorer,
    FusionSource,
    PersistenceGate,
    downgrade_unsustained,
    isolation_forest_source,
    lstm_source,
)
from src.analysis.refit import ENGINE_RUNNING_MIN_RPM
from src.config import CanaryConfig
from src.decode.behavior_models import CLOSED_LOOP_FUEL_STATES
from src.decode.bindings import Bindings
from src.decode.engineering import (
    UnscaledDecodedChannelError,
    decoded_factors,
    is_decoded_column,
)
from src.models.inference import SequenceInferenceEngine, SequenceInferenceResult
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
from src.preprocessing.resampler import TIMESTAMP_COLUMN, is_slow_channel, resample_config_for

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
    """Boolean per-row IF verdict mirroring ``emitter.emit_isolation_forest_row``:
    calibrated when present and legacy ``-1`` fallback otherwise. Missing-
    feature rows are ``None`` (no trustworthy verdict), matching the daemon's
    fail-safe suppression rather than manufacturing a dropout anomaly."""
    missing = scored[MISSING_FEATURES_COLUMN].astype(bool)
    calib = scored[CALIBRATED_ANOMALY_COLUMN]
    if calib.notna().any():
        verdict = calib.fillna(False).astype(bool).astype(object)
    else:
        verdict = (scored[LABEL_COLUMN].fillna(1).astype(int) == -1).astype(object)
    verdict.loc[missing] = None
    return verdict


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
    resample_config = resample_config_for(config)
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


# ---------------------------------------------------------------------------
# A02.6 — realistic faults with severity sweeps
# ---------------------------------------------------------------------------
#
# ``_inject_one``'s kinds (spike = 5 x max + 1,000, scale x 5, drift = 10
# sigma + 100) are plumbing tests: a range check catches every one of them by
# construction. The faults below are the ones a range check cannot catch —
# small, physically consistent, and applied to every channel that measures the
# same quantity — swept over severity so the report reads as recall against
# severity rather than one rate.

RealisticFaultKind = Literal["maf_gain", "lazy_o2", "thermostat", "bank_imbalance", "idle_air_leak"]

#: The vehicle-agnostic core of each channel role: SAE J1979 Mode 01 PIDs that
#: any OBD-II vehicle defines. A profile adds the channels that mirror the same
#: quantity on its own bus (Mode 22 DIDs, broadcast bindings) through its
#: ``fault_injection.yaml`` (:func:`load_fault_channel_roles`), so a fault is
#: applied consistently across related channels without naming one vehicle's
#: catalog in code.
DEFAULT_FAULT_CHANNEL_ROLES: dict[str, tuple[str, ...]] = {
    "airflow": ("obd:MAF",),
    "calculated_load": ("obd:ENGINE_LOAD", "obd:ABSOLUTE_LOAD"),
    "stft_b1": ("obd:SHORT_FUEL_TRIM_1",),
    "stft_b2": ("obd:SHORT_FUEL_TRIM_2",),
    "af_sensor_b1": ("obd:O2_WR_LAMBDA_S1",),
    "coolant": ("obd:COOLANT_TEMP",),
}

MAF_CHANNEL = "obd:MAF"
RPM_CHANNEL = "obd:RPM"
SPEED_CHANNEL = "obd:SPEED"
FUEL_STATUS_CHANNEL = "obd:FUEL_STATUS"

#: Seconds a stuck-open thermostat takes to pull coolant to its new plateau.
THERMOSTAT_RAMP_SECONDS = 120.0
#: Short-term trim authority. A leak that needs more than this is a code-setting
#: lean condition, not the subtle signature this sweep is for.
TRIM_AUTHORITY_PERCENT = 25.0
#: Floor on the airflow denominator of the leak signature (g/s), so a MAF
#: reading of ~0 during a stall cannot turn a small leak into an infinite trim.
MIN_LEAK_AIRFLOW_G_S = 0.5


@dataclass(frozen=True)
class RealisticFault:
    """One realistic fault and the severities it is swept over."""

    kind: RealisticFaultKind
    severity_unit: str
    #: Swept values, least detectable last is NOT implied — each kind states
    #: its own direction in ``description``.
    severities: tuple[float, ...]
    description: str


#: The channel roles each realistic fault writes (``inject_realistic_fault``
#: edits exactly these), for callers that must know which channels a fault
#: can touch — e.g. restricting a per-channel detector to them.
REALISTIC_FAULT_ROLES: dict[str, tuple[str, ...]] = {
    "maf_gain": ("airflow", "calculated_load", "stft_b1", "stft_b2"),
    "lazy_o2": ("af_sensor_b1",),
    "thermostat": ("coolant",),
    "bank_imbalance": ("stft_b1",),
    "idle_air_leak": ("stft_b1", "stft_b2"),
}

REALISTIC_FAULTS: dict[str, RealisticFault] = {
    "maf_gain": RealisticFault(
        "maf_gain",
        "MAF gain",
        (0.90, 0.93, 0.95, 0.97),
        "MAF under-reads by the gain; airflow and calculated-load channels scale with it, "
        "and closed-loop short-term trims add (1/g - 1) x 100 % of fuel. 0.97 is subtlest.",
    ),
    "lazy_o2": RealisticFault(
        "lazy_o2",
        "time constant s",
        (2.0, 5.0, 10.0, 20.0),
        "The bank-1 A/F sensor responds through a first-order lag of this time constant, "
        "on every channel carrying that sensor. 2 s is subtlest.",
    ),
    "thermostat": RealisticFault(
        "thermostat",
        "coolant plateau drop degC",
        (3.0, 6.0, 10.0, 15.0),
        "A stuck-open thermostat: coolant falls this far below its reading over "
        f"{THERMOSTAT_RAMP_SECONDS:.0f} s and stays there, on every coolant channel. 3 degC "
        "is subtlest.",
    ),
    "bank_imbalance": RealisticFault(
        "bank_imbalance",
        "bank-1 lean %",
        (2.0, 4.0, 6.0, 10.0),
        "Bank 1 runs lean by this much; its closed-loop short-term trim adds it back while "
        "bank 2 is unchanged. 2 % is subtlest.",
    ),
    "idle_air_leak": RealisticFault(
        "idle_air_leak",
        "unmetered air g/s",
        (0.2, 0.4, 0.6, 1.0),
        "Unmetered air enters after the MAF: closed-loop short-term trims on both banks add "
        "100 x leak / MAF %, so the signature is largest at idle and fades with load. "
        "0.2 g/s is subtlest.",
    ),
}


@dataclass(frozen=True)
class RealisticInjection:
    """What one realistic injection actually changed — and what it could not."""

    kind: str
    severity: float
    start: pd.Timestamp
    end: pd.Timestamp
    #: Channels whose values changed inside the window.
    perturbed: tuple[str, ...]
    #: Role channels left untouched, each with the reason: ``absent`` from the
    #: frame, ``not observed`` in the window, or ``unscaled`` (a ``decoded:``
    #: channel with no pinned engineering scale to convert the fault into).
    not_perturbed: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "perturbed": list(self.perturbed),
            "not_perturbed": dict(self.not_perturbed),
        }


def load_fault_channel_roles(path: Path | None) -> dict[str, tuple[str, ...]]:
    """The default J1979 roles plus a profile's own mirrors of each quantity.

    ``path`` is a YAML file of ``roles: {<role>: [<channel>, ...]}``; unknown
    roles are refused (a typo would silently apply nothing). ``None`` or a
    missing file returns the defaults.
    """
    roles = {role: tuple(channels) for role, channels in DEFAULT_FAULT_CHANNEL_ROLES.items()}
    if path is None or not path.exists():
        return roles
    raw = yaml.safe_load(path.read_text()) or {}
    extra = raw.get("roles", {})
    if not isinstance(extra, Mapping):
        raise ValueError(f"{path}: 'roles' must be a mapping of role -> channel list")
    unknown = sorted(set(extra) - set(roles))
    if unknown:
        raise ValueError(f"{path}: unknown fault-channel roles {unknown}; known: {sorted(roles)}")
    for role, channels in extra.items():
        roles[role] = tuple(dict.fromkeys([*roles[role], *(str(c) for c in channels)]))
    return roles


def _running_mask(frame: pd.DataFrame) -> pd.Series:
    if RPM_CHANNEL not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[RPM_CHANNEL].astype("float64") >= ENGINE_RUNNING_MIN_RPM


def _idle_mask(frame: pd.DataFrame) -> pd.Series:
    if SPEED_CHANNEL not in frame.columns:
        return pd.Series(False, index=frame.index)
    return _running_mask(frame) & (frame[SPEED_CHANNEL].astype("float64") <= 0.0)


def _closed_loop_mask(frame: pd.DataFrame) -> pd.Series:
    """Rows where the ECM trims from O2 feedback. Without a fuel-status
    channel every running row is assumed closed-loop (the trims only move when
    it is, so this over-applies at worst to open-loop rows). Fuel status is a
    slowly polled state: it holds until the next reading, so it is carried
    forward past the resampler's staleness ceiling."""
    running = _running_mask(frame)
    if FUEL_STATUS_CHANNEL not in frame.columns:
        return running
    status = frame[FUEL_STATUS_CHANNEL].astype("float64").ffill()
    return running & status.isin([float(s) for s in CLOSED_LOOP_FUEL_STATES])


def _window_eligible(frame: pd.DataFrame, mask: pd.Series, kind: str) -> bool:
    rows = frame.loc[mask]
    if rows.empty:
        return False
    if kind == "idle_air_leak":
        return bool(_idle_mask(rows).mean() >= 0.25) and _observed(rows, MAF_CHANNEL)
    if kind == "thermostat":
        return _observed(rows, "obd:COOLANT_TEMP")
    if kind == "lazy_o2":
        return bool(_running_mask(rows).mean() >= 0.5) and _observed(rows, "obd:O2_WR_LAMBDA_S1")
    if kind in {"maf_gain", "bank_imbalance"}:
        return bool(_closed_loop_mask(rows).mean() >= 0.5)
    raise ValueError(f"unsupported realistic fault kind: {kind!r}")


def _observed(frame: pd.DataFrame, column: str) -> bool:
    return column in frame.columns and bool(frame[column].notna().any())


def choose_fault_window(
    resampled: pd.DataFrame,
    *,
    kind: str,
    length_seconds: float,
    lead_seconds: float,
    rng: np.random.Generator,
    attempts: int = 64,
) -> tuple[pd.Timestamp, pd.Timestamp] | str:
    """A window where ``kind`` can physically show, or the reason there is none.

    ``lead_seconds`` of clean rows must precede the window (the scorers'
    warm-up). The window must hold the fault's operating condition — idle for
    a leak, closed loop for a trim fault, the sensor observed for a sensor
    fault. A drive with no such stretch returns a string naming why, which the
    harness reports as *skipped*, not as a miss.
    """
    timestamps = pd.to_datetime(resampled[TIMESTAMP_COLUMN], utc=True).reset_index(drop=True)
    if timestamps.empty:
        return "no rows"
    first = timestamps.iloc[0] + pd.Timedelta(seconds=lead_seconds)
    last_start = timestamps.iloc[-1] - pd.Timedelta(seconds=length_seconds)
    candidates = np.flatnonzero(((timestamps >= first) & (timestamps <= last_start)).to_numpy())
    if candidates.size == 0:
        return f"drive shorter than {lead_seconds:.0f} s lead + {length_seconds:.0f} s window"
    frame = resampled.reset_index(drop=True)
    for position in rng.permutation(candidates)[:attempts]:
        start = timestamps.iloc[int(position)]
        end = start + pd.Timedelta(seconds=length_seconds)
        if _window_eligible(frame, (timestamps >= start) & (timestamps < end), kind):
            return start, end
    return f"no {length_seconds:.0f} s stretch meets the {kind} condition"


def _physical_apply(
    frame: pd.DataFrame,
    column: str,
    mask: pd.Series,
    fn: Callable[[pd.Series], pd.Series],
    bindings: Bindings | None,
) -> str | None:
    """Apply ``fn`` (physical units in, physical units out) to ``column`` on
    ``mask``; return the reason when the column cannot be perturbed."""
    if column not in frame.columns:
        return "absent"
    window = mask & frame[column].notna()
    if not bool(window.any()):
        return "not observed"
    values = frame.loc[window, column].astype("float64")
    if is_decoded_column(column):
        try:
            scale, offset = decoded_factors(column, bindings)
        except UnscaledDecodedChannelError:
            return "unscaled"
        changed = (fn(values * scale + offset) - offset) / scale
    else:
        changed = fn(values)
    # Keep the column's float width (the resampler emits float32 channels);
    # a non-float column is widened rather than truncating the fault.
    if pd.api.types.is_float_dtype(frame[column].dtype):
        frame.loc[window, column] = changed.astype(frame[column].dtype)
    else:
        frame[column] = frame[column].astype("float64")
        frame.loc[window, column] = changed
    return None


def _scaled_by(gain: float) -> Callable[[pd.Series], pd.Series]:
    def apply(values: pd.Series) -> pd.Series:
        return values * gain

    return apply


def _offset_by(delta: float | pd.Series) -> Callable[[pd.Series], pd.Series]:
    """Add a constant, or a per-row series aligned on the frame index."""

    def apply(values: pd.Series) -> pd.Series:
        if isinstance(delta, pd.Series):
            return values + delta.loc[values.index]
        return values + delta

    return apply


def inject_realistic_fault(
    resampled: pd.DataFrame,
    *,
    kind: str,
    severity: float,
    start: pd.Timestamp,
    end: pd.Timestamp,
    roles: Mapping[str, Sequence[str]] = DEFAULT_FAULT_CHANNEL_ROLES,
    bindings: Bindings | None = None,
) -> tuple[pd.DataFrame, RealisticInjection]:
    """Inject one realistic fault into a copy of a clean resampled frame.

    Every channel in the fault's roles is changed consistently, in physical
    units (``decoded:`` counts go through the binding's scale). The derived
    channels — air mass, total trim, bank balances, VE — are recomputed from
    these by ``featurize_wide_frame``, so they follow without being touched
    here. Returns the frame and what was, and was not, perturbed.
    """
    if kind not in REALISTIC_FAULTS:
        raise ValueError(f"unsupported realistic fault kind: {kind!r}")
    frame = resampled.copy()
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    in_window = (timestamps >= start) & (timestamps < end)
    closed_loop = in_window & _closed_loop_mask(frame)
    edits: list[tuple[str, pd.Series, Callable[[pd.Series], pd.Series]]] = []

    def role(name: str) -> Sequence[str]:
        return roles.get(name, ())

    if kind == "maf_gain":
        gain = float(severity)
        compensation = (1.0 / gain - 1.0) * 100.0
        for column in (*role("airflow"), *role("calculated_load")):
            edits.append((column, in_window, _scaled_by(gain)))
        for column in (*role("stft_b1"), *role("stft_b2")):
            edits.append((column, closed_loop, _offset_by(compensation)))
    elif kind == "bank_imbalance":
        for column in role("stft_b1"):
            edits.append((column, closed_loop, _offset_by(float(severity))))
    elif kind == "thermostat":
        elapsed = (timestamps - start).dt.total_seconds()
        ramp = (elapsed / THERMOSTAT_RAMP_SECONDS).clip(lower=0.0, upper=1.0)
        drop = float(severity) * ramp
        for column in role("coolant"):
            edits.append((column, in_window, _offset_by(-drop)))
    elif kind == "lazy_o2":
        tau = float(severity)

        def lag(values: pd.Series) -> pd.Series:
            times = timestamps.loc[values.index]
            steps = times.diff().dt.total_seconds().fillna(0.0).to_numpy()
            alpha = 1.0 - np.exp(-steps / tau)
            raw = values.to_numpy(dtype="float64")
            out = np.empty_like(raw)
            out[0] = raw[0]
            for i in range(1, raw.size):
                out[i] = out[i - 1] + alpha[i] * (raw[i] - out[i - 1])
            return pd.Series(out, index=values.index)

        for column in role("af_sensor_b1"):
            edits.append((column, in_window, lag))
    elif kind == "idle_air_leak":
        if MAF_CHANNEL in frame.columns:
            airflow = frame[MAF_CHANNEL].astype("float64").clip(lower=MIN_LEAK_AIRFLOW_G_S)
            added = (100.0 * float(severity) / airflow).clip(upper=TRIM_AUTHORITY_PERCENT)
            leak_rows = closed_loop & added.notna()
        else:
            added = pd.Series(np.nan, index=frame.index)
            leak_rows = pd.Series(False, index=frame.index)
        for column in (*role("stft_b1"), *role("stft_b2")):
            edits.append((column, leak_rows, _offset_by(added)))

    perturbed: list[str] = []
    not_perturbed: dict[str, str] = {}
    for column, mask, fn in edits:
        reason = _physical_apply(frame, column, mask, fn, bindings)
        if reason is None:
            perturbed.append(column)
        else:
            not_perturbed[column] = reason
    return frame, RealisticInjection(
        kind=kind,
        severity=float(severity),
        start=pd.Timestamp(start),
        end=pd.Timestamp(end),
        perturbed=tuple(dict.fromkeys(perturbed)),
        not_perturbed=not_perturbed,
    )


__all__ = [
    "DEFAULT_FAULT_CHANNEL_ROLES",
    "DEFAULT_FAULT_KINDS",
    "INTERMITTENT_FAULT_KIND",
    "REALISTIC_FAULTS",
    "REALISTIC_FAULT_ROLES",
    "RealisticFault",
    "RealisticInjection",
    "choose_fault_window",
    "evaluate_detectors",
    "fusion_withheld_reason",
    "inject_realistic_fault",
    "load_fault_channel_roles",
]
