from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.alerts.emitter import AnomalyEventEmitter, IsolationForestInferenceEngine
from src.alerts.fusion import (
    FusionScorer,
    FusionSource,
    PersistenceGate,
    downgrade_unsustained,
    isolation_forest_source,
    lstm_source,
)
from src.dashboard.behavior_live import BehaviorLiveState, BehaviorObservation
from src.dashboard.broadcast import Broadcaster
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.dashboard.status import VehicleState, classify_vehicle_state
from src.decode.behavior_decoder import deviation
from src.decode.behavior_models import (
    BehaviorModel1D,
    load_behavior_models,
    unknown_operating_modes,
)
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.decode.dids import load_did_map
from src.models.health import HealthMonitor
from src.preprocessing.feature_pipeline import (
    DEFAULT_DIDS_PATH,
    FeatureSpecReconciliation,
    reconcile_feature_frame_to_spec,
)
from src.preprocessing.live import LiveFeatureSource
from src.preprocessing.segmenter import MODE_COLUMN
from src.telemetry import DECODED_SOURCE, SOURCE_SEP, channel_key

from .components import DaemonComponents

log = logging.getLogger(__name__)

#: Vehicle states IF/LSTM are allowed to score in (Phase 1.5). ``replay``
#: re-emits real running-vehicle data through the live pipeline, so it scores
#: like ``engine_running``. Everything else (ignition_only, engine_off,
#: no_data, unknown) is skipped — no koeo-trained model exists yet to make
#: ignition_only meaningful, so it stays gated out with the others.
_SCORABLE_VEHICLE_STATES = frozenset({"engine_running", "replay"})


def _live_vehicle_state(
    source: LiveFeatureSource, components: DaemonComponents, now: datetime
) -> VehicleState:
    """No-I/O ``classify_vehicle_state`` for the inference hot loop.

    Mirrors ``dashboard.status._derive_vehicle_state`` but reads
    ``source.vehicle_state_inputs`` (in-memory) instead of re-reading the
    latest session's Parquet — safe to call every tick.
    """
    replay_id: str | None = None
    if components.replay_controller is not None:
        replay_state = components.replay_controller.status()
        if replay_state.running:
            replay_id = replay_state.replay_id
    inputs = source.vehicle_state_inputs(now)
    return classify_vehicle_state(
        rpm=inputs["rpm"],
        coolant_temp=inputs["coolant_temp"],
        run_time=inputs["run_time"],
        obd_freshness_seconds=inputs["obd_freshness_seconds"],
        can_freshness_seconds=inputs["can_freshness_seconds"],
        replay_id=replay_id,
    )


async def run_live_inference(
    source: LiveFeatureSource,
    components: DaemonComponents,
    stop_event: asyncio.Event,
    *,
    tick_seconds: float | None = None,
) -> None:
    """Unified per-tick inference: IF + LSTM + behavior → fusion (Phase 7.2).

    Caller wires record-stream → ``source.observe(record)`` somewhere else.
    Each tick (``tick_seconds``, default ``1 / preprocessing.resample_hz``)
    pulls *one* feature row and runs every available detector against it:

    * **IF** → ``_score_and_emit`` (per-source ``isolation_forest`` event,
      unchanged) + an IF fusion source from the 7.2 calibrated threshold;
    * **LSTM** → engine built/hot-swapped here, per-source
      ``lstm_autoencoder`` event (unchanged) + an LSTM fusion source;
    * **behavior residuals** → the live overlay (unchanged) + a fusion
      source per graded model.

    The fusion sources are combined into one agreement-aware calibrated
    ``fused`` event (deduped/tuned through the same emitter), emitted
    *alongside* the per-source events. A single unified task ticks the
    source once per interval (the old split IF/LSTM tasks double-ticked the
    buffer); ``run_live_inference_lstm`` is retained standalone for its
    direct tests but is no longer spawned by ``run_daemon``.

    **Vehicle-state gating (Phase 1.5).** IF and the sequence detector only
    score ticks where ``_live_vehicle_state`` classifies as
    ``engine_running`` or ``replay`` — ``_SCORABLE_VEHICLE_STATES``. A
    key-out / KOEO / no-data tick is counted in
    ``emitter.counters.skipped_vehicle_state`` instead of scored, since
    neither detector was trained on that regime and scoring it just adds
    noise. The behavior overlay is unaffected — it keeps its own
    per-model ``operating_mode`` gating and still observes every tick.

    Best-effort: every error inside the loop is caught + logged.
    """
    import pandas as pd

    interval = tick_seconds or 1.0 / max(components.config.preprocessing.resample_hz, 1.0)
    if_manager = components.model_managers.get("isolation_forest")
    sequence_manager = (
        components.model_managers.get(components.sequence_model_type)
        if components.sequence_model_type is not None
        else None
    )
    fusion = FusionScorer(components.config.models.fusion)
    severity_threshold = components.config.models.fusion.severity_threshold
    # Phase 7.5: every detector must be over threshold for K consecutive
    # ticks before it emits or counts as a firing fusion source.
    persistence = PersistenceGate(components.config.alerts.min_consecutive_ticks)
    if if_manager is None:
        log.info(
            "isolation_forest model manager absent; IF scoring disabled — the "
            "behavior overlay + fusion still run off the live feature source"
        )
    sequence_engine: Any = None
    sequence_engine_version: str | None = None

    while not stop_event.is_set():
        try:
            now = datetime.now(UTC)
            row = source.tick(now)
            sources: list[FusionSource] = []
            timestamp_str: str | None = None
            operating_mode: str | None = None
            if row is not None:
                timestamp_str = str(row["timestamp"].iloc[0])
                mode_val = row["operating_mode"].iloc[0] if "operating_mode" in row else None
                operating_mode = None if mode_val is None or pd.isna(mode_val) else str(mode_val)

            scoring_allowed = True
            if row is not None:
                vehicle_state = _live_vehicle_state(source, components, now)
                scoring_allowed = vehicle_state.state in _SCORABLE_VEHICLE_STATES
                if not scoring_allowed:
                    components.emitter.counters.skipped_vehicle_state += 1

            loaded = if_manager.current if if_manager is not None else None
            if_version = loaded.manifest.model_version if loaded is not None else None
            if loaded is not None and row is not None and scoring_allowed:
                if_input = _score_and_emit(
                    loaded,
                    row,
                    components.emitter,
                    components.health_monitor,
                    scores=components.scores_broadcaster,
                    persistence=persistence,
                )
                if if_input is not None:
                    sources.append(
                        downgrade_unsustained(
                            isolation_forest_source(
                                if_input.score, if_input.threshold, scale=if_input.scale
                            ),
                            persistence.active("isolation_forest"),
                        )
                    )

            sequence_version: str | None = None
            sequence_loaded = sequence_manager.current if sequence_manager is not None else None
            if sequence_loaded is not None and row is not None and scoring_allowed:
                sequence_version = sequence_loaded.manifest.model_version
                if sequence_engine_version != sequence_version:
                    sequence_engine = _build_sequence_engine(
                        sequence_loaded,
                        suppress_channels=_suppress_channels_for(components, sequence_loaded),
                    )
                    sequence_engine_version = (
                        sequence_version if sequence_engine is not None else None
                    )
                if sequence_engine is not None:
                    result = _sequence_observe(sequence_engine, row)
                    if result is not None:
                        _publish_score(
                            components.scores_broadcaster,
                            source="lstm_autoencoder",
                            score=result.global_score,
                            threshold=result.global_threshold,
                            operating_mode=operating_mode,
                            model_version=result.model_version,
                            timestamp=result.timestamp,
                            missing_features=False,
                        )
                        seq_sustained = persistence.observe("lstm_autoencoder", result.is_anomaly)
                        if result.is_anomaly and not seq_sustained:
                            components.emitter.counters.suppressed_persistence += 1
                        else:
                            components.emitter.emit_lstm(result, operating_mode=operating_mode)
                        sources.append(
                            downgrade_unsustained(
                                lstm_source(result.global_score, result.global_threshold),
                                seq_sustained,
                            )
                        )

            # Behavior overlay (unchanged) + first-class residual detector
            # (Phase 7.4): each fired model emits its own behavior event and
            # publishes a /ws/scores frame, *and* feeds the fusion path.
            warn_severity = components.config.models.fusion.behavior_warning_severity
            for residual in _observe_behavior(source, components.behavior_live):
                # Sibling surfaces grading the same output channel share a
                # fusion group so they count as one agreeing voice, not two.
                behavior = fusion.behavior_source(
                    f"behavior:{residual.name}",
                    residual.severity,
                    group=f"behavior:{residual.channel}",
                )
                raw_fired = behavior.fired and residual.mode_match
                sustained = persistence.observe(f"behavior:{residual.name}", raw_fired)
                if not raw_fired:
                    continue
                if not sustained:
                    components.emitter.counters.suppressed_persistence += 1
                    continue
                sources.append(behavior)
                ev_ts = timestamp_str or residual.timestamp
                if ev_ts is None:
                    continue
                _publish_score(
                    components.scores_broadcaster,
                    source="behavior",
                    score=behavior.severity,
                    threshold=warn_severity,
                    operating_mode=residual.operating_mode,
                    model_version=f"behavior/{residual.name}",
                    timestamp=ev_ts,
                    missing_features=False,
                )
                components.emitter.emit_behavior(
                    model=residual.name,
                    kind=residual.kind,
                    channel=residual.channel,
                    timestamp=ev_ts,
                    severity_band=residual.severity,
                    score=behavior.severity,
                    threshold=warn_severity,
                    actual=residual.actual,
                    expected=residual.expected,
                    deviation=residual.deviation,
                    operating_mode=residual.operating_mode,
                )

            if fusion.enabled and sources and timestamp_str is not None:
                fused_version = f"if={if_version or '-'}|lstm={sequence_version or '-'}"
                fused = fusion.fuse(sources, timestamp=timestamp_str, operating_mode=operating_mode)
                _publish_score(
                    components.scores_broadcaster,
                    source="fused",
                    score=fused.severity,
                    threshold=severity_threshold,
                    operating_mode=operating_mode,
                    model_version=fused_version,
                    timestamp=timestamp_str,
                    missing_features=False,
                )
                components.emitter.emit_fused(
                    fused,
                    model_version=fused_version,
                    severity_threshold=severity_threshold,
                )
        except Exception:  # noqa: BLE001 — live inference is best-effort
            log.exception("live inference tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # stop_event fired
        except TimeoutError:
            continue


def _build_sequence_engine(loaded: Any, *, suppress_channels: tuple[str, ...] = ()) -> Any:
    """Build a ``SequenceInferenceEngine`` for a freshly-swapped model, or
    ``None`` on a bad checkpoint (retried next tick). Backbone-agnostic: the
    engine drives whichever sequence detector the manager loaded.

    ``suppress_channels`` is forwarded so config-listed noise channels (fuel
    level, HVAC, etc.) get their mask zeroed at every window — keeps the
    runtime mask knob honoured from *every* engine-construction path.
    """
    try:
        from src.models.inference import SequenceInferenceEngine

        return SequenceInferenceEngine(loaded.artifact, suppress_channels=suppress_channels)
    except Exception:  # noqa: BLE001 — bad checkpoint shouldn't kill the task
        log.exception("failed to build SequenceInferenceEngine; will retry next tick")
        return None


def _suppress_channels_for(components: Any, loaded: Any) -> tuple[str, ...]:
    """Pick the right backbone's ``suppress_channels`` for the loaded
    artifact's ``model_type``, so an LSTM↔TCN switch honours its own knob."""
    artifact_manifest = getattr(loaded.artifact, "manifest", None)
    model_type = getattr(artifact_manifest, "model_type", None)
    if model_type == "tcn_autoencoder":
        seq_cfg = components.config.models.tcn_autoencoder
    else:
        seq_cfg = components.config.models.lstm_autoencoder
    return tuple(getattr(seq_cfg, "suppress_channels", ()))


async def run_live_inference_lstm(
    source: LiveFeatureSource,
    components: DaemonComponents,
    stop_event: asyncio.Event,
    *,
    tick_seconds: float | None = None,
) -> None:
    """Drive live LSTM autoencoder inference from a ``LiveFeatureSource``.

    Symmetric to ``run_live_inference`` but for the LSTM. Picks up the
    LSTM-manager's current loaded model each tick (so hot-swap takes
    effect immediately), pulls a one-row feature frame from ``source``,
    and feeds the model-specific ``feature_columns`` through
    ``SequenceInferenceEngine.observe``. Schema-alignment policy:

    * **Missing columns are NaN → masked.** The LSTM buffer treats a
      non-finite / absent feature cell as unobserved (masked + imputed),
      exactly as the offline dataset does, so a channel still warming up —
      or a whole sensor family absent on this drive (e.g. no GPS fix) —
      doesn't stall the detector. The buffer fills over ``sequence_length``
      ticks and scores on whatever channels it has; the model was trained on
      GPS-less sessions this same way.
    * **Extra columns are dropped.** The source naturally carries
      whatever's been observed; the engine only reads its trained set.

    Best-effort: every error caught + logged, stream survives bad rows.
    """
    if "lstm_autoencoder" not in components.model_managers:
        log.info("lstm_autoencoder model manager absent; LSTM live inference skipped")
        await stop_event.wait()
        return

    interval = tick_seconds or 1.0 / max(components.config.preprocessing.resample_hz, 1.0)
    lstm_manager = components.model_managers["lstm_autoencoder"]
    engine: Any = None
    engine_version: str | None = None

    while not stop_event.is_set():
        loaded = lstm_manager.current
        if loaded is not None:
            # Rebuild the engine only when the manager has swapped — same
            # idiom as the IF path; an SequenceInferenceEngine is cheap to
            # construct, expensive to load (torch tensors). Hot-swap takes
            # effect on the very next tick after ModelManager.poll() reports
            # ``swapped``.
            new_version = loaded.manifest.model_version
            if engine_version != new_version:
                engine = _build_sequence_engine(
                    loaded,
                    suppress_channels=_suppress_channels_for(components, loaded),
                )
                engine_version = new_version if engine is not None else None

        if engine is not None:
            try:
                row = source.tick(datetime.now(UTC))
                if row is not None:
                    result = _sequence_observe(engine, row)
                    if result is not None:
                        # Every scored window → /ws/scores (live readout),
                        # then the emitter still gates the anomaly feed on
                        # is_anomaly as before.
                        _publish_score(
                            components.scores_broadcaster,
                            source="lstm_autoencoder",
                            score=result.global_score,
                            threshold=result.global_threshold,
                            operating_mode=None,
                            model_version=result.model_version,
                            timestamp=result.timestamp,
                            missing_features=False,
                        )
                        components.emitter.emit_lstm(result)
            except Exception:  # noqa: BLE001 — live inference is best-effort
                log.exception("live LSTM inference tick failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # stop_event fired
        except TimeoutError:
            continue


def _sequence_observe(engine: Any, row: Any) -> Any:
    """Align ``row`` to the sequence engine's expected feature columns and observe.

    ``SequenceWindowBuffer.append`` already handles missing-column / non-finite
    cases by clearing the buffer (return False, no result), so we don't need
    to pre-validate — we just present the columns the engine asks for. The
    return value is ``SequenceInferenceResult`` once a full window is ready,
    ``None`` otherwise.

    The GRU-D backbone needs each value column's ``<channel>__gap`` flag to
    derive the observed-mask. ``reconcile_feature_frame_to_spec`` only keeps
    structural + value columns (it's shared with the IF path), so re-attach the
    gap columns from the original live row after reconciling.
    """
    schema = _schema_for_engine(engine)
    aligned, report = reconcile_feature_frame_to_spec(row, schema)
    _log_schema_reconciliation_once(
        _engine_model_type(engine),
        _engine_version(engine),
        report,
    )
    observed = aligned.iloc[0].to_dict()
    for column in row.columns:
        name = str(column)
        if name.endswith("__gap") and name not in observed:
            observed[name] = row.iloc[0][column]
    return engine.observe(observed)


def _schema_for_engine(engine: Any) -> dict[str, Any]:
    trained = getattr(engine, "trained", None)
    manifest = getattr(trained, "manifest", None)
    schema = getattr(manifest, "schema", None)
    if isinstance(schema, dict):
        return schema
    return {"feature_columns": list(getattr(engine, "feature_columns", ()))}


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


def _engine_version(engine: Any) -> str:
    trained = getattr(engine, "trained", None)
    manifest = getattr(trained, "manifest", None)
    version = getattr(manifest, "model_version", None) or getattr(engine, "model_version", None)
    return str(version or "unknown")


def _engine_model_type(engine: Any) -> str:
    """``model_type`` of the engine's loaded model, for log attribution. Falls
    back to a generic label for test doubles without a manifest."""
    trained = getattr(engine, "trained", None)
    manifest = getattr(trained, "manifest", None)
    return str(getattr(manifest, "model_type", None) or "sequence_autoencoder")


def _log_schema_reconciliation_once(
    model_type: str,
    model_version: str,
    report: FeatureSpecReconciliation,
) -> None:
    if not report.has_divergence:
        return
    key = (model_type, model_version)
    if key in _SCHEMA_RECONCILIATION_WARNED:
        return
    _SCHEMA_RECONCILIATION_WARNED.add(key)
    log.warning(
        "live feature schema reconciled to model feature spec",
        extra={
            "model_type": model_type,
            "model_version": model_version,
            "schema_reconciliation": report.to_dict(),
        },
    )


# Model versions already warned about scoring with a missing feature schema,
# so the fail-safe suppression logs once per version instead of every tick.
_IF_MISSING_WARNED: set[str] = set()
#: Phase 7.2a — how many top IF feature contributors (by ``|scaled z|``) the
#: emitter attaches as ``detail.feature_contributors``. Keeps the per-event
#: payload bounded for IF models with dozens of features.
_IF_TOP_FEATURES = 5
_SCHEMA_RECONCILIATION_WARNED: set[tuple[str, str]] = set()


def _finite_or_none(value: Any) -> float | None:
    """JSON has no NaN/Infinity — coerce non-finite scores to ``None`` so the
    /ws/scores payload stays valid JSON the browser can parse."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _publish_score(
    scores: Broadcaster | None,
    *,
    source: str,
    score: Any,
    threshold: Any,
    operating_mode: str | None,
    model_version: str,
    timestamp: Any,
    missing_features: bool,
) -> None:
    """Fan one inference tick's score onto /ws/scores.

    Published every tick — not just anomalies — so the dashboard can show
    each model's live current value (including a normal 0). A non-finite
    score (a missing-feature tick) serializes as ``null`` alongside the
    ``missing_features`` flag rather than a misleading number.
    """
    if scores is None:
        return
    scores.publish(
        {
            "type": "score",
            "source": source,
            "score": _finite_or_none(score),
            "threshold": _finite_or_none(threshold),
            "operating_mode": operating_mode,
            "model_version": model_version,
            "timestamp": str(timestamp),
            "missing_features": bool(missing_features),
        }
    )


@dataclass(frozen=True)
class _IFFusionInput:
    """The IF half of one fused tick (Phase 7.2). ``threshold``/``scale`` are
    ``None`` for an uncalibrated (legacy) mode, so the IF source can't fire in
    fusion — it still emits its own per-source event via the label rule."""

    score: float
    threshold: float | None
    scale: float | None


@dataclass(frozen=True)
class _BehaviorResidual:
    """One behavior/physics model's graded deviation for a tick (Phase 7.4).

    Carries everything the first-class behavior detector needs to emit an
    event *and* feed the fusion path, computed once off the same operating
    point the overlay uses."""

    name: str
    kind: str
    channel: str
    actual: float | None
    expected: float | None
    deviation: float | None
    severity: str
    mode_match: bool
    operating_mode: str | None
    timestamp: str | None


def _score_and_emit(
    loaded: Any,
    row: Any,  # pd.DataFrame, one row
    emitter: AnomalyEventEmitter,
    health: HealthMonitor | None = None,
    *,
    scores: Broadcaster | None = None,
    persistence: PersistenceGate | None = None,
) -> _IFFusionInput | None:
    """Run one feature row through IF.score → emitter. Best-effort.

    Returns the IF fusion input on a trustworthy tick, or ``None`` when the
    verdict was suppressed (missing-feature schema warmup) and so must not
    contribute to fusion either — the per-source emit policy is unchanged.

    ``persistence`` (when given) is advanced once per trustworthy tick with
    the calibrated over-threshold verdict; the per-source event is withheld
    until the streak is sustained. The caller reads ``persistence.active``
    to downgrade the fusion source consistently.

    The model's expected ``feature_columns`` are a subset of what the
    live source produces; missing columns surface as NaN, which
    ``TrainedIsolationForest.score`` flags as ``missing_features`` rather
    than crashing. That behavior is exactly what we want during the
    schema-warmup window immediately after startup.

    When a ``HealthMonitor`` is wired, the per-tick latency / missing-feature
    flag / score / data freshness are recorded for ``/status`` (Task 6.27) —
    a few dict reads + a perf-counter delta, negligible against scoring.
    """
    import pandas as pd

    from src.models.isolation_forest import (
        CALIBRATED_THRESHOLD_COLUMN,
        MISSING_FEATURES_COLUMN,
        MODEL_MODE_COLUMN,
        SCORE_COLUMN,
    )

    engine = IsolationForestInferenceEngine(loaded.artifact)
    aligned, report = reconcile_feature_frame_to_spec(row, _schema_for_loaded(loaded))
    _log_schema_reconciliation_once("isolation_forest", engine.model_version, report)
    started = time.perf_counter()
    scored = engine.observe(aligned.iloc[0])
    latency_ms = (time.perf_counter() - started) * 1000.0
    timestamp = row["timestamp"].iloc[0]
    if health is not None:
        try:
            ts_epoch = pd.Timestamp(timestamp).timestamp()
        except (ValueError, TypeError):
            ts_epoch = None
        health.observe_inference(
            latency_ms=latency_ms,
            missing_features=bool(scored.get(MISSING_FEATURES_COLUMN, False)),
            score=(
                float(scored[SCORE_COLUMN])
                if SCORE_COLUMN in scored and pd.notna(scored[SCORE_COLUMN])
                else None
            ),
            data_timestamp_epoch=ts_epoch,
        )
    missing = bool(scored.get(MISSING_FEATURES_COLUMN, False))
    mode_val = row["operating_mode"].iloc[0] if "operating_mode" in row else None
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
    _publish_score(
        scores,
        source="isolation_forest",
        score=scored.get(SCORE_COLUMN),
        threshold=calibrated_threshold,  # 7.2 per-mode calibrated boundary
        operating_mode=(None if mode_val is None or pd.isna(mode_val) else str(mode_val)),
        model_version=engine.model_version,
        timestamp=timestamp,
        missing_features=missing,
    )
    # Layer 3 fail-safe: an IF verdict computed from a feature vector with
    # missing trained columns is untrustworthy. Suppress the emit (symmetric
    # with the LSTM path, which already withholds output until a clean
    # window) rather than alerting off a half-blind model. Health still
    # recorded above, so the degraded state is visible on /status — silent
    # but observable beats confidently wrong. The raw score still went onto
    # /ws/scores above so the live readout shows the degraded state.
    if missing:
        version = engine.model_version
        if version not in _IF_MISSING_WARNED:
            _IF_MISSING_WARNED.add(version)
            log.warning(
                "isolation_forest scoring with missing feature columns; "
                "suppressing anomaly emit until the live stream supplies the "
                "full trained schema. Watch /status missing_feature_rate.",
                extra={"model_version": version},
            )
        return None
    # Phase 7.2a — cheap first-pass IF attribution: rank features by their
    # distance from the scaler's training baseline (|z|), keep the top K.
    # The scaled row is a one-row transform; the scaler is already loaded.
    feature_contributors: dict[str, float] = {}
    affected_channels: tuple[str, ...] = ()
    try:
        scaled = loaded.artifact.scaler.transform(aligned).iloc[0]
        ranked = sorted(
            (
                (str(column), abs(float(scaled[column])))
                for column in engine.feature_columns
                if column in scaled.index and pd.notna(scaled[column])
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:_IF_TOP_FEATURES]
        feature_contributors = {name: value for name, value in ranked}
        affected_channels = tuple(name for name, _ in ranked)
    except Exception:  # noqa: BLE001 — attribution is best-effort, never blocks an emit
        log.exception("isolation_forest feature_contributors ranking failed")
    score_val = scored.get(SCORE_COLUMN)
    suppress_emit = False
    if persistence is not None:
        # Same firing rule the fusion source uses: calibrated over-threshold.
        # Uncalibrated rows (no threshold) can't latch the gate and keep the
        # legacy ungated emit path; non-firing ticks still flow to the
        # emitter so its suppressed_not_anomaly accounting stays truthful.
        raw_fired = (
            score_val is not None
            and pd.notna(score_val)
            and calibrated_threshold is not None
            and float(score_val) >= calibrated_threshold
        )
        sustained = persistence.observe("isolation_forest", raw_fired)
        if raw_fired and not sustained:
            suppress_emit = True
            emitter.counters.suppressed_persistence += 1
    if not suppress_emit:
        emitter.emit_isolation_forest_row(
            scored,
            model_version=engine.model_version,
            timestamp=str(timestamp),
            affected_channels=affected_channels,
            feature_contributors=feature_contributors or None,
        )
    if score_val is None or pd.isna(score_val):
        return None
    return _IFFusionInput(
        score=float(score_val),
        threshold=calibrated_threshold,
        scale=scale if scale is None else float(scale),
    )


def _num(value: object) -> float | None:
    """Float-cast an operating-point value; ``None`` for NaN / non-numeric.

    Mirrors ``behavior_decoder._scalar`` without reaching for a private
    name — the daemon only needs it to pull the live axis values that
    position the dashboard's "you are here" marker.
    """
    try:
        f = float(value)  # type: ignore[arg-type]  # except handles non-numeric
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _observe_behavior(
    source: LiveFeatureSource, behavior_live: BehaviorLiveState
) -> list[_BehaviorResidual]:
    """Feed one tick of per-model deviations into the live overlay state.

    Returns one :class:`_BehaviorResidual` per model so the caller can both
    emit the first-class behavior detector event (Phase 7.4) and feed the
    fusion path (Phase 7.2) off the exact graded deviation the overlay shows
    — ``deviation()`` already returns ``"ok"`` for an off-mode or no-signal
    model, so a returned band is mode-safe.

    Reads the exact wide row the feature pipeline used (the source's
    ``latest_operating_point``) and runs every behavior model through the
    same ``behavior_decoder.deviation`` the preprocessing path uses. The
    Models▸Behavior overlay therefore shows backend truth — mode-gated,
    domain-clamped, empty-cell aware — not a client-side re-derivation that
    could silently drift from what the IF/LSTM actually see.

    Best-effort: the caller wraps this in the live-inference try/except, so
    one malformed row can't kill the inference stream.
    """
    models = source.behavior_models
    point = source.latest_operating_point
    if models is None or not models.models or point is None:
        return []
    residuals: list[_BehaviorResidual] = []
    raw_mode = point.get(MODE_COLUMN)
    mode = raw_mode if isinstance(raw_mode, str) else None
    ts = point.get("timestamp")
    if ts is None:
        timestamp = None
    elif hasattr(ts, "isoformat"):
        timestamp = ts.isoformat()
    else:
        timestamp = str(ts)
    observations: list[BehaviorObservation] = []
    for model in models.models:
        # Same actual-column resolution as append_deviation_columns: a
        # qualified source:channel output is verbatim; a bare CAN signal
        # name falls back to the legacy decoded:<NAME>.
        actual_col = (
            model.output
            if SOURCE_SEP in model.output
            else channel_key(DECODED_SOURCE, model.output)
        )
        actual = _num(point.get(actual_col))
        result = deviation(model, actual, point)
        if isinstance(model, BehaviorModel1D):
            x = _num(point.get(model.axis.channel))
            y = None
        else:
            x = _num(point.get(model.axes[0].channel))
            y = _num(point.get(model.axes[1].channel))
        mode_match = model.operating_mode is None or mode == model.operating_mode
        observations.append(
            BehaviorObservation(
                name=model.name,
                kind=model.kind,
                x=x,
                y=y,
                actual=actual,
                expected=result.expected,
                deviation=result.deviation,
                severity=result.severity,
                mode_match=mode_match,
            )
        )
        residuals.append(
            _BehaviorResidual(
                name=model.name,
                kind=model.kind,
                channel=actual_col,
                actual=actual,
                expected=result.expected,
                deviation=result.deviation,
                severity=result.severity,
                mode_match=mode_match,
                operating_mode=mode,
                timestamp=timestamp,
            )
        )
    behavior_live.observe(timestamp, mode, observations)
    return residuals


def build_live_feature_source(
    components: DaemonComponents,
    *,
    bindings_path: Path | None = None,
    behavior_models_path: Path | None = None,
) -> LiveFeatureSource:
    """Construct a ``LiveFeatureSource`` from the daemon's config + sidecars.

    Convenience for the daemon entry: loads ``bindings.yaml`` and
    ``behavior_models.yaml`` if present at the given paths (defaults are
    the canonical locations), and wires them into the source so CAN
    payloads decode and deviation columns appear automatically.
    """
    # Repo-root-anchored (see api.DEFAULT_BINDINGS_PATH) so live CAN decode
    # finds bindings.yaml regardless of the daemon's launch cwd.
    bindings_path = bindings_path or DEFAULT_BINDINGS_PATH
    # Scoped per vehicle profile (config.daemon.behavior_models is rewritten to
    # <base>/<slug>/behavior_models.yaml by src.profiles.scope_config).
    behavior_models_path = behavior_models_path or components.config.daemon.behavior_models
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    behavior_models = (
        load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
    )
    if behavior_models is not None:
        mismatched = unknown_operating_modes(
            behavior_models,
            components.config.preprocessing.operating_modes,
        )
        if mismatched:
            log.warning(
                "behavior models gated on operating modes the live segmenter "
                "can't emit — they will NEVER evaluate; add the modes to "
                "preprocessing.operating_modes",
                extra={"models": mismatched},
            )
    # The training feature pipeline decodes CAN twice — bindings.yaml
    # (decoded:*) AND the DBC files (dbc:*). The live source must mirror both
    # or every inference tick fails on the model's required dbc:* columns.
    dbc_database = load_dbc_dir(DEFAULT_DBC_DIR)
    # Mode-22 DID map — lets the live source decode raw DID_<hex> poll
    # responses into named did:<NAME> values, matching the offline
    # build_feature_frame path so did:* feature columns line up.
    did_map = load_did_map(DEFAULT_DIDS_PATH) if DEFAULT_DIDS_PATH.exists() else None
    return LiveFeatureSource(
        config=components.config,
        bindings=bindings,
        behavior_models=behavior_models,
        dbc_database=dbc_database,
        did_map=did_map,
    )


__all__ = [
    "build_live_feature_source",
    "run_live_inference",
    "run_live_inference_lstm",
    "_finite_or_none",
    "_sequence_observe",
    "_observe_behavior",
    "_publish_score",
    "_score_and_emit",
]
