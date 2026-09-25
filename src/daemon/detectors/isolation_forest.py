"""The IsolationForest live detector: prepare on the loop, compute on the worker, apply
on the loop (R03.9).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.fusion import (
    FusionSource,
    PersistenceGate,
    downgrade_unsustained,
    isolation_forest_source,
)
from src.alerts.tuning import AlertTuner
from src.daemon.components import DaemonComponents
from src.daemon.detectors.common import (
    TickContext,
    _log_schema_reconciliation_once,
    _PreparedDetector,
    _publish_score,
    _with_effective_role,
    gate_and_collect,
    log,
)
from src.daemon.health import HealthMonitor
from src.dashboard.broadcast import Broadcaster
from src.models.inference import IsolationForestInferenceEngine
from src.preprocessing.feature_pipeline import (
    FeatureSpecReconciliation,
    reconcile_feature_frame_to_spec,
)
from src.timefmt import format_timestamp


@dataclass(frozen=True)
class _IFComputeJob:
    """Loop-snapshotted inputs for one IF scoring pass (C08.4): the pinned
    model bundle and the one-row feature frame."""

    loaded: Any
    row: Any


@dataclass(frozen=True)
class _IFComputed:
    """Everything the IF worker pass produced for one tick (C08.4) — pure
    data, applied on the loop by :func:`_if_apply`. Carries the reconciliation
    report so the once-per-version schema warning is logged from the loop."""

    #: The version that actually scored. Was the whole engine until the
    #: scoring pass moved out-of-process: shipping a model back across a
    #: pipe every tick to read one string defeats the point, and this
    #: dataclass already documented itself as "pure data".
    model_version: str
    report: FeatureSpecReconciliation
    scored: Any  # pd.Series
    latency_ms: float
    timestamp: Any
    missing: bool
    mode_str: str | None
    calibrated_threshold: float | None
    scale: float | None
    score_val: Any
    feature_contributors: dict[str, float]
    affected_channels: tuple[str, ...]
    #: A04.2 — ``mode_median`` / ``pooled_mean`` (see _rank_feature_contributors).
    feature_contributors_reference: str | None = None


class IsolationForestDetector(_PreparedDetector[_IFComputeJob, _IFComputed]):
    """IF per-tick scoring → per-source event → IF fusion source (Task 3.1).

    Thin ``Detector`` wrapper around ``_score_and_emit``, which already owns
    the scoring/health/missing-feature-fail-safe/contributor-ranking/
    persistence/emit shape — this class adds only what ``_score_and_emit``
    can't build itself: the calibrated-threshold ``FusionSource``, downgraded
    by the persistence verdict ``_score_and_emit`` already computed (a second
    ``persistence.observe`` call here would double-drive the M-of-N streak).
    """

    def __init__(self, components: DaemonComponents) -> None:
        self._components = components
        self._manager = components.model_managers.get("isolation_forest")
        #: Last-scored model version, read by ``run_live_inference`` for the
        #: fused event's ``if=...`` version tag — ``None`` before the first
        #: tick or while no artifact is loaded.
        self.model_version: str | None = None

    def prepare(self, row: Any, ctx: TickContext) -> _IFComputeJob | None:
        """Loop side (C08.4): pin the model generation and decide whether
        this tick scores at all. ``None`` means nothing to compute."""
        loaded = self._manager.current if self._manager is not None else None
        # serving_version, not the sidecar manifest's claim — on a partial
        # deploy (stale binary + fresh manifest) the fused event's version
        # tag must name the model actually scoring.
        self.model_version = loaded.serving_version if loaded is not None else None
        if loaded is None or row is None or not ctx.scoring_allowed:
            return None
        return _IFComputeJob(loaded=loaded, row=row)

    @staticmethod
    def compute(job: _IFComputeJob) -> _IFComputed:
        """Worker side: the sklearn forward pass + contributor ranking, over
        inputs pinned by :meth:`prepare`. Touches no shared state."""
        return _if_compute(job.loaded, job.row)

    def apply(self, computed: _IFComputed, ctx: TickContext) -> list[FusionSource]:
        """Loop side: health, ``/ws/scores``, persistence, per-source emit,
        and the IF fusion source."""
        if_input = _if_apply(
            computed,
            self._components.emitter,
            self._components.health_monitor,
            scores=self._components.scores_broadcaster,
            persistence=self._components.persistence_gate,
            tuner=self._components.alert_tuner,
            now=ctx.now.timestamp(),
        )
        if if_input is None:
            return []
        return [
            _with_effective_role(
                self._components,
                downgrade_unsustained(
                    isolation_forest_source(
                        if_input.score, if_input.threshold, scale=if_input.scale
                    ),
                    if_input.sustained,
                ),
            )
        ]


def _schema_for_loaded(loaded: Any) -> dict[str, Any]:
    artifact_manifest = getattr(getattr(loaded, "artifact", None), "manifest", None)
    schema = getattr(artifact_manifest, "schema", None)
    if isinstance(schema, dict) and schema.get("feature_columns"):
        return schema
    manager_schema = getattr(getattr(loaded, "manifest", None), "schema_", None)
    if isinstance(manager_schema, dict) and manager_schema.get("feature_columns"):
        return manager_schema
    artifact = getattr(loaded, "artifact", None)
    return {
        "feature_columns": list(getattr(artifact, "feature_columns", ())),
        "mode_column": getattr(artifact, "mode_column", "operating_mode"),
    }


#: Phase 7.2a — how many top IF feature contributors (by score effect, A04.2) the
#: emitter attaches as ``detail.feature_contributors``. Keeps the per-event
#: payload bounded for IF models with dozens of features.
_IF_TOP_FEATURES = 5


@dataclass(frozen=True)
class _IFFusionInput:
    """The IF half of one fused tick (Phase 7.2). ``threshold``/``scale`` are
    ``None`` for an uncalibrated mode, so the IF source can't fire in fusion —
    nor does it emit its own per-source event any more (2.3: uncalibrated
    modes are non-emitting, score-only). ``sustained`` (6.3) is this tick's
    already-computed persistence verdict — carried through instead of the
    caller re-deriving it via ``persistence.active`` so a tuner-bypassed key
    (see ``_persistence_sustained``) downgrades the fusion source correctly
    even though the gate's own streak state was never touched for it."""

    score: float
    threshold: float | None
    scale: float | None
    sustained: bool = True


def _record_if_health(
    health: HealthMonitor | None,
    *,
    latency_ms: float,
    missing: bool,
    score: Any,
    timestamp: Any,
    mode_str: str | None,
    model_version: str,
) -> None:
    """Task 3.1: the health-bookkeeping slice of ``_score_and_emit``, split
    out because it's a pure side effect (no bearing on the returned
    ``_IFFusionInput``) — a few dict reads + a perf-counter delta against a
    wired ``HealthMonitor``, silently skipped when ``health`` is ``None``.

    2.1: per-mode blindness is edge-triggered off the same tick health
    already records, so a mode transitioning into it logs a fresh warning
    instead of the old once-per-process one keyed on model version.
    ``blind_modes``/``missing_feature_rate_by_mode`` are then visible on
    ``/status`` for every tick, not just the transition.
    """
    import pandas as pd

    if health is None:
        return
    try:
        ts_epoch = pd.Timestamp(timestamp).timestamp()
    except (ValueError, TypeError):
        ts_epoch = None
    newly_blind = health.observe_inference(
        latency_ms=latency_ms,
        missing_features=missing,
        score=float(score) if score is not None and pd.notna(score) else None,
        data_timestamp_epoch=ts_epoch,
        operating_mode=mode_str,
    )
    if newly_blind:
        log.warning(
            "isolation_forest mode %r transitioned into blindness — a "
            "trained feature column has gone permanently missing from "
            "the live stream; anomaly emits are suppressed for this mode "
            "until it recovers. See /status "
            "detectors.isolation_forest.blind_modes.",
            mode_str,
            extra={"model_version": model_version, "operating_mode": mode_str},
        )


def _rank_feature_contributors(
    engine: IsolationForestInferenceEngine, aligned: Any
) -> tuple[dict[str, float], tuple[str, ...], str | None]:
    """A04.2 — IF attribution over the *scoring mode model's own* features, by
    their effect on the score (occlusion, one batched ``decision_function``).
    The earlier |z| ranking over the artifact's feature union named a feature
    the scoring model did not have on 60 of 62 city rows. Best-effort and
    side-effect-free; keeps the top :data:`_IF_TOP_FEATURES`. The third value
    says what each feature was replaced with: ``mode_median``, or
    ``pooled_mean`` on an artifact that recorded no per-mode medians.
    """
    try:
        row = aligned.iloc[0]
        contributions = engine.feature_contributions(row, top_k=_IF_TOP_FEATURES)
        reference = engine.contribution_reference(row)
    except Exception:  # noqa: BLE001 — attribution is best-effort, never blocks an emit
        log.exception("isolation_forest feature_contributors ranking failed")
        return {}, (), None
    return contributions, tuple(contributions), reference


def _if_compute(loaded: Any, row: Any) -> _IFComputed:
    """The worker half of :func:`_score_and_emit`: schema alignment, the
    sklearn forward pass, threshold/scale lookup, and contributor ranking.
    No shared state is read or written — safe off the loop (C08.4)."""
    import pandas as pd

    from src.models.isolation_forest import (
        CALIBRATED_THRESHOLD_COLUMN,
        MISSING_FEATURES_COLUMN,
        MODEL_MODE_COLUMN,
        SCORE_COLUMN,
    )

    engine = IsolationForestInferenceEngine(loaded.artifact)
    aligned, report = reconcile_feature_frame_to_spec(row, _schema_for_loaded(loaded))
    started = time.perf_counter()
    scored = engine.observe(aligned.iloc[0])
    latency_ms = (time.perf_counter() - started) * 1000.0
    timestamp = row["timestamp"].iloc[0]
    missing = bool(scored.get(MISSING_FEATURES_COLUMN, False))
    mode_val = row["operating_mode"].iloc[0] if "operating_mode" in row else None
    mode_str = None if mode_val is None or pd.isna(mode_val) else str(mode_val)
    raw_threshold = scored.get(CALIBRATED_THRESHOLD_COLUMN)
    calibrated_threshold = (
        float(raw_threshold) if raw_threshold is not None and pd.notna(raw_threshold) else None
    )
    model_mode = scored.get(MODEL_MODE_COLUMN)
    scale = (
        getattr(loaded.artifact, "score_scales", {}).get(str(model_mode))
        if model_mode is not None and pd.notna(model_mode)
        else None
    )
    # A04.2 — occlusion attribution, only for a row that can emit (calibrated
    # and at/over its threshold): the persistence gate and emitter only ever
    # read it there, and it costs one batched forward pass. Skipped for a
    # missing-feature row: its verdict is suppressed below.
    score_now = scored.get(SCORE_COLUMN)
    over_threshold = (
        calibrated_threshold is not None
        and score_now is not None
        and pd.notna(score_now)
        and float(score_now) >= calibrated_threshold
    )
    reference: str | None = None
    if missing or not over_threshold:
        feature_contributors: dict[str, float] = {}
        affected_channels: tuple[str, ...] = ()
    else:
        feature_contributors, affected_channels, reference = _rank_feature_contributors(
            engine, aligned
        )
    return _IFComputed(
        model_version=engine.model_version,
        report=report,
        scored=scored,
        latency_ms=latency_ms,
        timestamp=timestamp,
        missing=missing,
        mode_str=mode_str,
        calibrated_threshold=calibrated_threshold,
        scale=scale if scale is None else float(scale),
        score_val=scored.get(SCORE_COLUMN),
        feature_contributors=feature_contributors,
        affected_channels=affected_channels,
        feature_contributors_reference=reference,
    )


def _if_apply(
    computed: _IFComputed,
    emitter: AnomalyEventEmitter,
    health: HealthMonitor | None = None,
    *,
    scores: Broadcaster | None = None,
    persistence: PersistenceGate | None = None,
    tuner: AlertTuner | None = None,
    now: float | None = None,
) -> _IFFusionInput | None:
    """The loop half of :func:`_score_and_emit`: health bookkeeping,
    ``/ws/scores``, the persistence gate and the per-source emit."""
    import pandas as pd

    model_version = computed.model_version
    _log_schema_reconciliation_once("isolation_forest", model_version, computed.report)
    _record_if_health(
        health,
        latency_ms=computed.latency_ms,
        missing=computed.missing,
        score=computed.score_val,
        timestamp=computed.timestamp,
        mode_str=computed.mode_str,
        model_version=model_version,
    )
    calibrated_threshold = computed.calibrated_threshold
    _publish_score(
        scores,
        source="isolation_forest",
        score=computed.score_val,
        threshold=calibrated_threshold,  # 7.2 per-mode calibrated boundary
        operating_mode=computed.mode_str,
        model_version=model_version,
        timestamp=computed.timestamp,
        missing_features=computed.missing,
    )
    # Layer 3 fail-safe: an IF verdict computed from a feature vector with
    # missing trained columns is untrustworthy. Suppress the emit (symmetric
    # with the LSTM path, which already withholds output until a clean
    # window) rather than alerting off a half-blind model. Health still
    # recorded above, so the degraded state is visible on /status — silent
    # but observable beats confidently wrong. The raw score still went onto
    # /ws/scores above so the live readout shows the degraded state.
    if computed.missing:
        return None
    score_val = computed.score_val
    calibrated = calibrated_threshold is not None
    suppress_emit = False
    sustained = True
    if persistence is not None:
        # Same firing rule the fusion source uses: calibrated over-threshold.
        # Uncalibrated rows (no threshold) can never raise this — they're
        # non-emitting entirely now (2.3, below) — but still flow through so
        # the gate's internal streak state and the emitter's
        # suppressed_not_anomaly accounting stay truthful.
        raw_fired = (
            score_val is not None
            and pd.notna(score_val)
            and calibrated_threshold is not None
            and float(score_val) >= calibrated_threshold
        )
        # 6.3/3.1: a tuner debounce override for (isolation_forest, mode_str),
        # if any, fully owns persistence for this key instead of stacking —
        # see `gate_and_collect`, shared with the sequence + behavior detectors.
        sustained = gate_and_collect(
            persistence,
            "isolation_forest",
            raw_fired,
            tuner=tuner,
            source="isolation_forest",
            mode=computed.mode_str,
            now=now if now is not None else time.monotonic(),
            counters=emitter.counters,
        )
        suppress_emit = raw_fired and not sustained
    # 2.2/2.3: a mode with no calibrated threshold is either too sparse to
    # trust (skipped at training, 2.2) or hasn't accumulated enough holdout
    # yet — either way its raw sklearn contamination label no longer reaches
    # the emitter. This retires the legacy path that used to bypass the
    # persistence gate entirely and fire straight off that label; the score
    # is still recorded above (health + /ws/scores) so the degraded mode
    # stays observable, just non-emitting.
    if calibrated and not suppress_emit:
        emitter.emit_isolation_forest_row(
            computed.scored,
            model_version=model_version,
            timestamp=format_timestamp(computed.timestamp),
            affected_channels=computed.affected_channels,
            feature_contributors=computed.feature_contributors or None,
            feature_contributors_reference=computed.feature_contributors_reference,
        )
    if score_val is None or pd.isna(score_val):
        return None
    return _IFFusionInput(
        score=float(score_val),
        threshold=calibrated_threshold,
        scale=computed.scale,
        sustained=sustained,
    )


def _score_and_emit(
    loaded: Any,
    row: Any,  # pd.DataFrame, one row
    emitter: AnomalyEventEmitter,
    health: HealthMonitor | None = None,
    *,
    scores: Broadcaster | None = None,
    persistence: PersistenceGate | None = None,
    tuner: AlertTuner | None = None,
    now: float | None = None,
) -> _IFFusionInput | None:
    """Run one feature row through IF.score → emitter, synchronously on the
    caller's thread. Best-effort.

    Composition of :func:`_if_compute` (pure scoring, what the live pipeline
    runs in its worker) and :func:`_if_apply` (every side effect, what it
    runs on the loop) — kept as one call for offline callers and tests.

    Returns the IF fusion input on a trustworthy tick, or ``None`` when the
    verdict was suppressed (missing-feature schema warmup) and so must not
    contribute to fusion either — the per-source emit policy is unchanged.

    ``persistence`` (when given) is advanced once per trustworthy tick with
    the calibrated over-threshold verdict (6.3: through ``tuner``'s
    debounce-ownership rule, see ``_persistence_sustained``); the per-source
    event is withheld until the streak is sustained. The caller reads the
    returned ``sustained`` flag to downgrade the fusion source consistently.

    The model's expected ``feature_columns`` are a subset of what the
    live source produces; missing columns surface as NaN, which
    ``TrainedIsolationForest.score`` flags as ``missing_features`` rather
    than crashing. That behavior is exactly what we want during the
    schema-warmup window immediately after startup.

    When a ``HealthMonitor`` is wired, the per-tick latency / missing-feature
    flag / score / data freshness are recorded for ``/status`` (Task 6.27) —
    a few dict reads + a perf-counter delta, negligible against scoring.
    """
    return _if_apply(
        _if_compute(loaded, row),
        emitter,
        health,
        scores=scores,
        persistence=persistence,
        tuner=tuner,
        now=now,
    )
