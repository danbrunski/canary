from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from src.alerts.emitter import AnomalyEventEmitter, EmitterCounters, IsolationForestInferenceEngine
from src.alerts.fusion import (
    FusionScorer,
    FusionSource,
    PersistenceGate,
    downgrade_unsustained,
    isolation_forest_source,
    lstm_source,
)
from src.alerts.tuning import AlertTuner
from src.dashboard.behavior_live import BehaviorLiveState, BehaviorObservation
from src.dashboard.broadcast import Broadcaster
from src.dashboard.paths import DEFAULT_BINDINGS_PATH
from src.dashboard.status import VehicleState, classify_vehicle_state
from src.decode.accepted_baselines import load_accepted_baselines
from src.decode.behavior_decoder import deviation, deviation_for_override
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
    ALL_MODE,
    DEFAULT_DIDS_PATH,
    FeatureSpecReconciliation,
    reconcile_feature_frame_to_spec,
)
from src.preprocessing.live import LiveFeatureSource
from src.preprocessing.segmenter import MODE_COLUMN
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.telemetry import DECODED_SOURCE, SOURCE_SEP, TelemetryRecord, channel_key, channel_of

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


def _persistence_sustained(
    persistence: PersistenceGate,
    key: str,
    firing: bool | None,
    *,
    tuner: AlertTuner | None,
    source: str,
    mode: str | None,
    now: float,
) -> bool:
    """Advance ``key`` through the persistence gate, honouring 6.3's
    debounce-ownership rule: when an operator ``DebouncePolicy`` is active
    for ``(source, mode)``, it fully OWNS that key's persistence
    requirement (applied later, inside the emitter, via
    ``AlertTuner.min_consecutive``) — the built-in gate's own M-of-N is
    bypassed (every trustworthy tick passes straight through) rather than
    stacking a second, redundant requirement on top of the tuner's.

    A missing verdict (``firing=None``, see ``PersistenceGate``'s docstring)
    always goes through the gate itself, bypass or not — there's no tuner
    debounce state to "pass through" for a tick that produced no verdict at
    all, and doing so keeps the gate's own streak correctly frozen (not
    reset to False) for the ticks where the bypass isn't in effect.
    """
    if firing is not None and tuner is not None and tuner.has_debounce_override(source, mode):
        return firing
    return persistence.observe(key, firing, now=now)


def gate_and_collect(
    persistence: PersistenceGate,
    key: str,
    firing: bool | None,
    *,
    tuner: AlertTuner | None,
    source: str,
    mode: str | None,
    now: float,
    counters: EmitterCounters,
) -> bool:
    """Advance ``key`` through the persistence gate and account for a
    tick that fired but didn't survive it (Task 3.1).

    The three per-tick detectors (IF, sequence, behavior) each used to
    open-code the same "was this tick's verdict actually sustained, and if
    it fired-but-not-sustained bump ``suppressed_persistence``" pairing
    around their own :func:`_persistence_sustained` call — this is that
    shared tail. Callers still apply :func:`~src.alerts.fusion.downgrade_unsustained`
    themselves (IF defers it a layer up, in ``IsolationForestDetector.tick``,
    since ``_score_and_emit`` needs the raw ``sustained`` flag to decide
    whether to withhold its own per-source emit too).
    """
    sustained = _persistence_sustained(
        persistence, key, firing, tuner=tuner, source=source, mode=mode, now=now
    )
    if firing and not sustained:
        counters.suppressed_persistence += 1
    return sustained


@dataclass(frozen=True)
class TickContext:
    """Per-tick state every :class:`Detector` needs, computed once by
    ``run_live_inference`` instead of re-derived by each detector (Task 3.1).
    """

    now: datetime
    operating_mode: str | None
    timestamp_str: str | None
    #: Phase 1.5 vehicle-state gate — ``False`` means IF/sequence must not
    #: score this tick (neither was trained on a non-running regime); the
    #: behavior overlay ignores this and keeps its own per-model gating.
    scoring_allowed: bool


class Detector(Protocol):
    """One per-tick anomaly detector (Task 3.1).

    Implementations own their detector's full observe → per-source emit →
    persistence → fusion-source shape, so ``run_live_inference`` reduces to
    "tick every detector, collect fusion sources, fuse." A detector may
    return zero, one, or several :class:`FusionSource` instances for one
    tick (IF/sequence contribute at most one; behavior contributes one per
    fired model).
    """

    def tick(self, row: Any, ctx: TickContext) -> list[FusionSource]: ...


#: Instantaneous performance channels (Task 1.1) — the row-local subset
#: ``compute_instantaneous_channels`` appends inside ``featurize_wide_frame``,
#: so they land on ``source.latest_operating_point`` for free every tick.
_DERIVED_INSTANTANEOUS_CHANNELS: tuple[str, ...] = (
    "derived:FUEL_RATE_LPH",
    "derived:INSTANT_ECONOMY_L_PER_100KM",
    "derived:INSTANT_ECONOMY_MPG",
    "derived:BOOST_VACUUM_KPA",
    "derived:ESTIMATED_POWER_HP",
    "derived:ESTIMATED_TORQUE_NM",
)


async def _publish_derived_channels(
    broadcaster: Broadcaster,
    source: LiveFeatureSource,
    now: datetime,
    storage_writer: AsyncParquetTelemetryWriter | None,
) -> None:
    """Stream Task 1.1's performance channels over ``/ws/telemetry`` **and**
    persist them to Parquet through the daemon's one storage writer.

    Instantaneous channels (fuel rate, instant economy, boost/vacuum,
    estimated power/torque) come off ``latest_operating_point`` — the raw
    operating-frame row ``compute_instantaneous_channels`` already appended
    inside ``featurize_wide_frame``, identical for a live window or a full
    session since none of them need history. The session-scoped trip
    integrals (distance/fuel/CO2/idle/avg-economy) come off
    ``latest_derived_channels`` — Task 1.3's accumulator, not a windowed
    cumsum (the ring buffer evicts rows continuously; cumsum-ing it would
    reset the integrals far more often than a session boundary).

    Both are reconstructable from raw signals offline, but ``query_telemetry``
    (the documented DuckDB view over stored Parquet, see docs/mcp-server.md)
    has no such reconstruction fallback — an operator querying trip/fuel/boost
    history through it must find the values already in the file. So every
    value built here also becomes a ``derived:*`` ``TelemetryRecord`` and goes
    through ``storage_writer.write_many`` — the same ``ParquetTelemetryWriter``
    instance ``run_storage_sink`` writes raw records through (Task 1.1
    follow-up), not a second Parquet-writing path. ``storage_writer`` is
    ``None`` in tests that never wire storage, in which case persistence is
    skipped and only the broadcast happens.

    Best-effort and silent on nothing-to-publish, matching every other
    broadcast call in this loop — a stalled dashboard socket, or a storage
    write failure, must never interrupt scoring.
    """
    point = source.latest_operating_point or {}
    values: dict[str, Any] = {
        name: point[name] for name in _DERIVED_INSTANTANEOUS_CHANNELS if name in point
    }
    values.update(source.latest_derived_channels or {})
    if not values:
        return
    timestamp = now.isoformat()
    records: list[TelemetryRecord] = []
    for name, value in values.items():
        if value is None or (isinstance(value, float) and math.isnan(value)):
            continue
        numeric = float(value)
        channel = channel_of(name)
        broadcaster.publish(
            {
                "timestamp": timestamp,
                "source": "derived",
                "channel": channel,
                "value": numeric,
                "unit": "",
            }
        )
        records.append(TelemetryRecord.derived(now, channel, numeric))
    if storage_writer is not None and records:
        try:
            await storage_writer.write_many(records)
        except Exception:  # noqa: BLE001 — storage is best-effort, must not kill the tick
            log.exception("failed to persist derived channels to storage")


class IsolationForestDetector:
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

    def tick(self, row: Any, ctx: TickContext) -> list[FusionSource]:
        loaded = self._manager.current if self._manager is not None else None
        self.model_version = loaded.manifest.model_version if loaded is not None else None
        if loaded is None or row is None or not ctx.scoring_allowed:
            return []
        if_input = _score_and_emit(
            loaded,
            row,
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
            downgrade_unsustained(
                isolation_forest_source(if_input.score, if_input.threshold, scale=if_input.scale),
                if_input.sustained,
            )
        ]


class SequenceDetector:
    """Sequence-autoencoder (LSTM|TCN, Task 2.1) per-tick scoring → per-source
    ``lstm_autoencoder`` event → sequence fusion source (Task 3.1).

    Backbone-agnostic: drives whichever ``components.sequence_model_type``
    selected. Owns the engine hot-swap-on-version-change bookkeeping that
    used to live as loop-local state in ``run_live_inference``.
    """

    def __init__(self, components: DaemonComponents) -> None:
        self._components = components
        self._manager = (
            components.model_managers.get(components.sequence_model_type)
            if components.sequence_model_type is not None
            else None
        )
        self._engine: Any = None
        self._engine_version: str | None = None
        #: Last-scored model version, read by ``run_live_inference`` for the
        #: fused event's ``lstm=...`` version tag.
        self.model_version: str | None = None

    def tick(self, row: Any, ctx: TickContext) -> list[FusionSource]:
        loaded = self._manager.current if self._manager is not None else None
        self.model_version = loaded.manifest.model_version if loaded is not None else None
        if loaded is None or row is None or not ctx.scoring_allowed:
            return []
        if self._engine_version != self.model_version:
            self._engine = _build_sequence_engine(
                loaded, suppress_channels=_suppress_channels_for(self._components, loaded)
            )
            self._engine_version = self.model_version if self._engine is not None else None
        if self._engine is None:
            return []
        result = _sequence_observe(self._engine, row)
        if result is None:
            return []
        _publish_score(
            self._components.scores_broadcaster,
            source="lstm_autoencoder",
            score=result.global_score,
            threshold=result.global_threshold,
            operating_mode=ctx.operating_mode,
            model_version=result.model_version,
            timestamp=result.timestamp,
            missing_features=False,
        )
        # 6.3/3.1: a tuner override for (lstm_autoencoder, mode) fully owns
        # persistence for this key instead of stacking on the gate's own
        # M-of-N — see `gate_and_collect`, shared with the IF + behavior
        # detectors.
        sustained = gate_and_collect(
            self._components.persistence_gate,
            "lstm_autoencoder",
            result.is_anomaly,
            tuner=self._components.alert_tuner,
            source="lstm_autoencoder",
            mode=ctx.operating_mode,
            now=ctx.now.timestamp(),
            counters=self._components.emitter.counters,
        )
        if not (result.is_anomaly and not sustained):
            self._components.emitter.emit_lstm(result, operating_mode=ctx.operating_mode)
        return [
            downgrade_unsustained(
                lstm_source(result.global_score, result.global_threshold), sustained
            )
        ]


class BehaviorDetector:
    """Behavior/physics-model residual overlay → first-class behavior events
    + fusion sources (Task 3.1; Phase 7.4).

    Unlike IF/sequence, one tick can contribute zero, one, or several
    ``FusionSource`` instances — one per fired behavior model, since sibling
    models graded on the same output channel share a fusion group but models
    on different channels are independent voices. Reads
    ``source.latest_operating_point`` (via ``_observe_behavior``), not
    ``row`` — the live overlay predates the shared per-tick ``row`` and
    already has everything it needs off the feature source directly.
    """

    def __init__(
        self, components: DaemonComponents, fusion: FusionScorer, source: LiveFeatureSource
    ) -> None:
        self._components = components
        self._fusion = fusion
        self._source = source

    def tick(self, row: Any, ctx: TickContext) -> list[FusionSource]:
        del row
        sources: list[FusionSource] = []
        warn_severity = self._components.config.models.fusion.behavior_warning_severity
        persistence = self._components.persistence_gate
        tuner = self._components.alert_tuner
        for residual in _observe_behavior(self._source, self._components.behavior_live):
            # Sibling surfaces grading the same output channel share a
            # fusion group so they count as one agreeing voice, not two.
            behavior = self._fusion.behavior_source(
                f"behavior:{residual.name}",
                residual.severity,
                group=f"behavior:{residual.channel}",
            )
            # 6.1 — off-mode is a missing verdict (the model didn't evaluate
            # this tick at all), not a miss: freeze the M-of-N window instead
            # of pushing a False that would decay real evidence accumulated
            # the last time this model was in-mode. On-mode-but-not-deviating
            # is a genuine miss and still displaces a stale hit (flood
            # protection intact).
            verdict: bool | None = behavior.fired if residual.mode_match else None
            # 6.3/3.1: a tuner override for (behavior, mode) fully owns
            # persistence for this key instead of stacking — see
            # `gate_and_collect`, shared with the IF + sequence detectors.
            sustained = gate_and_collect(
                persistence,
                f"behavior:{residual.name}",
                verdict,
                tuner=tuner,
                source="behavior",
                mode=residual.operating_mode,
                now=ctx.now.timestamp(),
                counters=self._components.emitter.counters,
            )
            if verdict is not True or not sustained:
                continue
            sources.append(behavior)
            ev_ts = ctx.timestamp_str or residual.timestamp
            if ev_ts is None:
                continue
            _publish_score(
                self._components.scores_broadcaster,
                source="behavior",
                score=behavior.severity,
                threshold=warn_severity,
                operating_mode=residual.operating_mode,
                model_version=f"behavior/{residual.name}",
                timestamp=ev_ts,
                missing_features=False,
            )
            self._components.emitter.emit_behavior(
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
                override_reason=residual.override_reason,
            )
        return sources


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
    pulls *one* feature row and ticks every :class:`Detector` against it
    (Task 3.1 — ``IsolationForestDetector``, ``SequenceDetector``,
    ``BehaviorDetector``), each owning its own observe → per-source emit →
    persistence → fusion-source shape:

    * **IF** → per-source ``isolation_forest`` event (unchanged) + an IF
      fusion source from the 7.2 calibrated threshold;
    * **LSTM|TCN** → engine built/hot-swapped inside the detector,
      per-source ``lstm_autoencoder`` event (unchanged) + a sequence fusion
      source;
    * **behavior residuals** → the live overlay (unchanged) + a fusion
      source per graded model.

    The fusion sources are combined into one agreement-aware calibrated
    ``fused`` event (deduped/tuned through the same emitter), emitted
    *alongside* the per-source events. A single unified task ticks the
    source once per interval (the old split IF/LSTM tasks double-ticked the
    buffer; the standalone LSTM-only task, ``run_live_inference_lstm``, was
    production-dead by the time of Task 4.1 and was deleted).

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
    fusion = FusionScorer(components.config.models.fusion)
    severity_threshold = components.config.models.fusion.severity_threshold
    if components.model_managers.get("isolation_forest") is None:
        log.info(
            "isolation_forest model manager absent; IF scoring disabled — the "
            "behavior overlay + fusion still run off the live feature source"
        )
    if_detector = IsolationForestDetector(components)
    sequence_detector = SequenceDetector(components)
    behavior_detector = BehaviorDetector(components, fusion, source)
    detectors: tuple[Detector, ...] = (if_detector, sequence_detector, behavior_detector)

    while not stop_event.is_set():
        try:
            now = datetime.now(UTC)
            row = source.tick(now)
            timestamp_str: str | None = None
            operating_mode: str | None = None
            if row is not None:
                timestamp_str = str(row["timestamp"].iloc[0])
                mode_val = row["operating_mode"].iloc[0] if "operating_mode" in row else None
                operating_mode = None if mode_val is None or pd.isna(mode_val) else str(mode_val)
                await _publish_derived_channels(
                    components.telemetry_broadcaster, source, now, components.storage_writer
                )

            # 4.7 — the segmenter falling back to constant "all" mode means
            # every mode-gated behavior surface silently never fires; make
            # that visible instead of nowhere, same edge-triggered pattern as
            # IF's blind_modes (2.1).
            newly_fallback = components.health_monitor.observe_segmenter_fallback(
                source.last_segmenter_fallback
            )
            if newly_fallback:
                log.warning(
                    "operating-mode segmenter fell back to constant %r mode — segmenter "
                    "input columns are missing from the live stream; every mode-gated "
                    "behavior surface is suppressed until it recovers. See /status "
                    "detectors.segmenter.fallback_active.",
                    ALL_MODE,
                )

            scoring_allowed = True
            if row is not None:
                vehicle_state = _live_vehicle_state(source, components, now)
                scoring_allowed = vehicle_state.state in _SCORABLE_VEHICLE_STATES
                if not scoring_allowed:
                    components.emitter.counters.skipped_vehicle_state += 1

            ctx = TickContext(
                now=now,
                operating_mode=operating_mode,
                timestamp_str=timestamp_str,
                scoring_allowed=scoring_allowed,
            )
            sources: list[FusionSource] = []
            for detector in detectors:
                sources.extend(detector.tick(row, ctx))

            if fusion.enabled and sources and timestamp_str is not None:
                fused_version = (
                    f"if={if_detector.model_version or '-'}"
                    f"|lstm={sequence_detector.model_version or '-'}"
                )
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
    #: 2.2 — the active accepted-baseline override's provenance reason when
    #: this tick's severity was graded against a recentered one-sided band
    #: instead of the model's own static config; ``None`` otherwise.
    override_reason: str | None = None


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
    loaded: Any, engine: IsolationForestInferenceEngine, aligned: Any
) -> tuple[dict[str, float], tuple[str, ...]]:
    """Task 3.1 / Phase 7.2a: cheap first-pass IF attribution, split out of
    ``_score_and_emit`` as its own best-effort, side-effect-free step — rank
    features by their distance from the scaler's training baseline (``|z|``),
    keep the top :data:`_IF_TOP_FEATURES`. The scaled row is a one-row
    transform; the scaler is already loaded on the artifact.
    """
    import pandas as pd

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
    except Exception:  # noqa: BLE001 — attribution is best-effort, never blocks an emit
        log.exception("isolation_forest feature_contributors ranking failed")
        return {}, ()
    return {name: value for name, value in ranked}, tuple(name for name, _ in ranked)


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
    """Run one feature row through IF.score → emitter. Best-effort.

    Returns the IF fusion input on a trustworthy tick, or ``None`` when the
    verdict was suppressed (missing-feature schema warmup) and so must not
    contribute to fusion either — the per-source emit policy is unchanged.

    ``persistence`` (when given) is advanced once per trustworthy tick with
    the calibrated over-threshold verdict (6.3: through ``tuner``'s
    debounce-ownership rule, see ``_persistence_sustained``); the per-source
    event is withheld
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
    missing = bool(scored.get(MISSING_FEATURES_COLUMN, False))
    mode_val = row["operating_mode"].iloc[0] if "operating_mode" in row else None
    mode_str = None if mode_val is None or pd.isna(mode_val) else str(mode_val)
    _record_if_health(
        health,
        latency_ms=latency_ms,
        missing=missing,
        score=scored.get(SCORE_COLUMN),
        timestamp=timestamp,
        mode_str=mode_str,
        model_version=engine.model_version,
    )
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
        operating_mode=mode_str,
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
        return None
    # Phase 7.2a — cheap first-pass IF attribution: rank features by their
    # distance from the scaler's training baseline (|z|), keep the top K.
    feature_contributors, affected_channels = _rank_feature_contributors(loaded, engine, aligned)
    score_val = scored.get(SCORE_COLUMN)
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
            mode=mode_str,
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
        sustained=sustained,
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

    Also grades any ``channel``-targeted accepted-baseline override (2.2) —
    unlike a ``behavior_model``-targeted one, there is no surface backing a
    raw channel, so these never appear in ``source.behavior_models`` and
    need their own residual straight off ``deviation_for_override``; see
    the loop below. Doesn't touch ``observations``/the live overlay — that
    panel is surface-model-shaped (x/y axis position) and a raw channel
    has no axis to place a "you are here" marker at.

    Best-effort: the caller wraps this in the live-inference try/except, so
    one malformed row can't kill the inference stream.
    """
    models = source.behavior_models
    point = source.latest_operating_point
    if point is None:
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
    for model in models.models if models is not None else []:
        # Same actual-column resolution as append_deviation_columns: a
        # qualified source:channel output is verbatim; a bare CAN signal
        # name falls back to the legacy decoded:<NAME>.
        actual_col = (
            model.output
            if SOURCE_SEP in model.output
            else channel_key(DECODED_SOURCE, model.output)
        )
        actual = _num(point.get(actual_col))
        # 2.2 — an active, non-expired override for this model recenters
        # the band (worse side trips, better side never alerts, per
        # `behavior_decoder._grade_directional`); `active_override_for`
        # already returns `None` past `review_by`, so an expired override
        # is silently inert here — the model grades against its own static
        # config (if any) exactly as if no override existed.
        override = (
            source.accepted_baselines.active_override_for(model.name, mode)
            if source.accepted_baselines is not None
            else None
        )
        result = deviation(model, actual, point, override=override)
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
                override_reason=None if override is None else override.provenance.reason,
            )
        )
    # 2.2 — raw-channel overrides (e.g. `obd:SHORT_FUEL_TRIM_1`) have no
    # behavior_model/surface behind them, so they're never covered by the
    # loop above; grade each distinct channel target directly against
    # `deviation_for_override` instead of `deviation()`. Skips cleanly (no
    # residual) when no override is active for this tick's mode (expired,
    # or scoped to a different mode — `active_override_for` already
    # encodes both) or the channel isn't present/decoded this tick — same
    # "missing signal ⇒ no residual" posture as the behavior_model branch.
    baselines = source.accepted_baselines
    if baselines is not None:
        seen_channels: set[str] = set()
        for candidate in baselines.overrides:
            if candidate.channel is None or candidate.channel in seen_channels:
                continue
            seen_channels.add(candidate.channel)
            channel_override = baselines.active_override_for(candidate.channel, mode)
            if channel_override is None:
                continue
            actual_col = (
                candidate.channel
                if SOURCE_SEP in candidate.channel
                else channel_key(DECODED_SOURCE, candidate.channel)
            )
            actual = _num(point.get(actual_col))
            if actual is None:
                continue
            result = deviation_for_override(channel_override, actual)
            residuals.append(
                _BehaviorResidual(
                    name=f"override:{candidate.channel}",
                    kind="channel_override",
                    channel=actual_col,
                    actual=actual,
                    expected=result.expected,
                    deviation=result.deviation,
                    severity=result.severity,
                    # `active_override_for` already matched this tick's mode
                    # (or the override is unscoped) to get here, so this
                    # residual always speaks for the current tick.
                    mode_match=True,
                    operating_mode=mode,
                    timestamp=timestamp,
                    override_reason=channel_override.provenance.reason,
                )
            )
    # Only touch the overlay when there were surface models to report on —
    # a channel-override-only tick (no behavior_models.yaml models at all)
    # has nothing overlay-shaped to add, same no-op posture as before 2.2's
    # channel-override branch existed.
    if models is not None and models.models:
        behavior_live.observe(timestamp, mode, observations)
    return residuals


def build_live_feature_source(
    components: DaemonComponents,
    *,
    bindings_path: Path | None = None,
    behavior_models_path: Path | None = None,
    accepted_baselines_path: Path | None = None,
) -> LiveFeatureSource:
    """Construct a ``LiveFeatureSource`` from the daemon's config + sidecars.

    Convenience for the daemon entry: loads ``bindings.yaml``,
    ``behavior_models.yaml``, and ``accepted_baselines.yaml`` if present at
    the given paths (defaults are the canonical, profile-scoped locations),
    and wires them into the source so CAN payloads decode, deviation
    columns appear automatically, and the behavior detector (2.2) can
    consult active overrides.
    """
    # Repo-root-anchored (see api.DEFAULT_BINDINGS_PATH) so live CAN decode
    # finds bindings.yaml regardless of the daemon's launch cwd.
    bindings_path = bindings_path or DEFAULT_BINDINGS_PATH
    # Scoped per vehicle profile (config.daemon.behavior_models is rewritten to
    # <base>/<slug>/behavior_models.yaml by src.profiles.scope_config).
    behavior_models_path = behavior_models_path or components.config.daemon.behavior_models
    # Likewise profile-scoped (config.daemon.accepted_baselines); a missing
    # file is a valid, empty document (load_accepted_baselines handles that),
    # so this is always safe to load unconditionally.
    accepted_baselines_path = accepted_baselines_path or components.config.daemon.accepted_baselines
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
    accepted_baselines = load_accepted_baselines(accepted_baselines_path)
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
        accepted_baselines=accepted_baselines,
        dbc_database=dbc_database,
        did_map=did_map,
    )


__all__ = [
    "build_live_feature_source",
    "run_live_inference",
    "_finite_or_none",
    "_sequence_observe",
    "_observe_behavior",
    "_publish_score",
    "_score_and_emit",
]
