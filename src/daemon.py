"""Daemon assembly — wires producers, sinks, dashboard, and model managers.

The pieces all exist; this module is the *connection diagram*:

* Collectors (CAN, OBD) push ``TelemetryRecord`` onto an asyncio queue.
* ``_drain_queue`` pops each record, writes it to Parquet, *and publishes
  it onto the telemetry Broadcaster* so ``/ws/telemetry`` subscribers see
  it live.
* ``ModelManager`` instances poll the models directory; ``/status`` reports
  whatever's currently loaded.
* ``AnomalyEventEmitter`` is constructed up front and wired to the
  anomalies ``Broadcaster`` so events emitted (Milestone B: when live
  inference is wired) fan out to ``/ws/anomalies`` and the
  ``AnomalyEventLogger``'s JSONL at the same time.
* The FastAPI app runs in the same event loop via ``uvicorn.Server``.

Live inference (the row stream → IF/LSTM observe path) is NOT wired here
yet — it's Milestone B and needs a live preprocessing pipeline
(incremental decode → resample → segment → rolling features → scaler).
This module sets up everything around that gap so the loop is otherwise
complete; the day Milestone B lands, the only new wiring is an inference
task added next to the existing collectors.

Why a separate module from ``src.main``: ``main.py`` is the CLI / signal-
handling shell; this module is the assembly that's directly importable
from tests without going through ``asyncio.run`` or a SIGTERM dance.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn

from src.alerts.emitter import (
    AnomalyEventEmitter,
    IsolationForestInferenceEngine,
)
from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.alerts.feedback import FeedbackStore
from src.alerts.tuning import AlertTuner, load_tuning_config
from src.collection.queueing import QueueDepthMonitor
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.jobs import JobController
from src.dashboard.api import DEFAULT_BINDINGS_PATH, create_app
from src.dashboard.behavior_live import BehaviorLiveState, BehaviorObservation
from src.dashboard.broadcast import Broadcaster
from src.decode.behavior_decoder import deviation
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
    BehaviorModel1D,
    load_behavior_models,
)
from src.decode.bindings import load_bindings
from src.decode.dbc import DEFAULT_DBC_DIR, load_dbc_dir
from src.models.health import HealthMonitor
from src.models.manager import ModelManager, manager_for_artifact
from src.preprocessing.feature_pipeline import (
    FeatureSpecReconciliation,
    reconcile_feature_frame_to_spec,
)
from src.preprocessing.live import LiveFeatureSource
from src.preprocessing.segmenter import MODE_COLUMN
from src.storage.monitor import warn_if_storage_low
from src.storage.parquet_writer import ParquetTelemetryWriter, delete_expired_sessions
from src.telemetry import (
    DECODED_SOURCE,
    SOURCE_SEP,
    TelemetryRecord,
    channel_key,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Assembled state
# ---------------------------------------------------------------------------


@dataclass
class DaemonComponents:
    """Long-lived objects the daemon stitches together.

    Held in one record so a test can construct + inspect them without
    starting any background tasks. The real `run_daemon` builds one of
    these, then starts the asyncio tasks that drive them.
    """

    config: CanaryConfig
    telemetry_broadcaster: Broadcaster
    anomalies_broadcaster: Broadcaster
    scores_broadcaster: Broadcaster
    control_broadcaster: Broadcaster
    event_logger: AnomalyEventLogger
    audit_logger: ControlAuditLogger
    emitter: AnomalyEventEmitter
    model_managers: dict[str, ModelManager[Any]]
    alert_tuner: AlertTuner
    health_monitor: HealthMonitor
    behavior_live: BehaviorLiveState = field(default_factory=BehaviorLiveState)
    capture_controller: CaptureController | None = None
    job_controller: JobController | None = None


def build_components(config: CanaryConfig) -> DaemonComponents:
    """Construct all long-lived components from config. No I/O beyond mkdir."""
    telemetry_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)
    anomalies_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)
    scores_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)
    control_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)

    event_logger = AnomalyEventLogger(config.daemon.anomalies_dir)
    audit_logger = ControlAuditLogger(config.daemon.audit_log)
    tuner = AlertTuner(load_tuning_config(config.daemon.tuning_config))
    emitter = _make_broadcasting_emitter(event_logger, anomalies_bus, tuner=tuner)
    model_managers = _load_model_managers(config)

    return DaemonComponents(
        config=config,
        telemetry_broadcaster=telemetry_bus,
        anomalies_broadcaster=anomalies_bus,
        scores_broadcaster=scores_bus,
        control_broadcaster=control_bus,
        event_logger=event_logger,
        audit_logger=audit_logger,
        emitter=emitter,
        model_managers=model_managers,
        alert_tuner=tuner,
        health_monitor=HealthMonitor(),
        behavior_live=BehaviorLiveState(),
        capture_controller=None,
        job_controller=_make_job_controller(config),
    )


def _make_job_controller(config: CanaryConfig) -> JobController:
    repo_root = Path.cwd()
    config_path = Path(os.environ.get("CANARY_CONFIG", "canary.yaml"))
    python = repo_root / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    return JobController(
        config=config,
        repo_root=repo_root,
        config_path=config_path,
        jobs_dir=config.daemon.jobs_dir,
        python=python,
    )


def _make_broadcasting_emitter(
    logger: AnomalyEventLogger,
    anomalies_bus: Broadcaster,
    *,
    tuner: AlertTuner | None = None,
) -> AnomalyEventEmitter:
    """Wrap ``AnomalyEventLogger`` so every persisted event is also published
    on the broadcaster — keeps the logger-as-source-of-truth + the live-push
    paths in lockstep through one call site."""

    class _BroadcastingLogger:
        """Stand-in that mirrors AnomalyEventLogger's surface but also publishes."""

        def __init__(self, inner: AnomalyEventLogger, bus: Broadcaster) -> None:
            self._inner = inner
            self._bus = bus

        def log(self, event: AnomalyEvent) -> Path:
            path = self._inner.log(event)
            self._bus.publish(event.to_dict())
            return path

        def close(self) -> None:
            self._inner.close()

    # AnomalyEventEmitter just needs `.log(event)`; duck-type satisfies it.
    return AnomalyEventEmitter(
        _BroadcastingLogger(logger, anomalies_bus),  # type: ignore[arg-type]
        tuner=tuner,
    )


def _load_model_managers(config: CanaryConfig) -> dict[str, ModelManager[Any]]:
    """Build one ``ModelManager`` per detector, loading the initial artifact
    if present. Missing artifacts are fine — the manager keeps polling, and
    ``current`` is ``None`` until something lands."""
    managers: dict[str, ModelManager[Any]] = {}
    if_path = config.daemon.models_dir / config.daemon.isolation_forest_artifact
    lstm_path = config.daemon.models_dir / config.daemon.lstm_artifact

    if config.models.isolation_forest.enabled:
        from src.models.isolation_forest import load_isolation_forest_model

        managers["isolation_forest"] = manager_for_artifact(
            if_path, load_isolation_forest_model, model_type="isolation_forest"
        )
    if config.models.lstm_autoencoder.enabled:
        try:
            from src.models.lstm_training import load_lstm_autoencoder_model
        except ImportError:
            log.warning("lstm_autoencoder enabled but torch is unavailable; skipping ModelManager")
        else:
            managers["lstm_autoencoder"] = manager_for_artifact(
                lstm_path, load_lstm_autoencoder_model, model_type="lstm_autoencoder"
            )
    return managers


# ---------------------------------------------------------------------------
# Queue draining with broadcaster fan-out
# ---------------------------------------------------------------------------


async def drain_queue_with_broadcast(
    queue: asyncio.Queue[TelemetryRecord],
    writer: ParquetTelemetryWriter,
    stop_event: asyncio.Event,
    *,
    telemetry_broadcaster: Broadcaster | None = None,
    live_source: LiveFeatureSource | None = None,
    poll_interval: float = 0.5,
    storage_path: Path | None = None,
    storage_warning_mb: int | None = None,
    queue_warning_fraction: float = 0.8,
) -> None:
    """Drain telemetry into storage + the live broadcaster + the live source.

    Extension of ``src.main._drain_queue``: same Parquet write path, same
    queue-depth / disk-space monitoring, but each record is *also*

    * published onto ``telemetry_broadcaster`` so ``/ws/telemetry``
      subscribers see it without polling Parquet;
    * fed into ``live_source.observe(record)`` so the streaming feature
      pipeline (Milestone B) stays in sync with what's hitting disk.

    Both are optional — pass ``None`` to skip either. Publishing is
    best-effort: a full subscriber queue drops the oldest message and a
    ``live_source.observe`` exception is caught + logged. Neither can
    block the writer.
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)

    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            if writer.flush_due():
                writer.flush()
                if storage_path is not None and storage_warning_mb is not None:
                    warn_if_storage_low(storage_path, storage_warning_mb)
            continue

        try:
            writer.write(record)
            if telemetry_broadcaster is not None:
                telemetry_broadcaster.publish(_record_to_dict(record))
                if live_source is not None:
                    for decoded in live_source.decode_for_telemetry(record):
                        telemetry_broadcaster.publish(_record_to_dict(decoded))
            if live_source is not None:
                try:
                    live_source.observe(record)
                except Exception:  # noqa: BLE001 — live inference is best-effort
                    log.exception("live_source.observe failed; skipping record")
        finally:
            queue.task_done()
            queue_monitor.observe(queue)

    writer.close()
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)


def _record_to_dict(record: TelemetryRecord) -> dict[str, Any]:
    """Live-stream shape: same {timestamp, source, channel, value, unit}
    the REST ``/telemetry/latest`` endpoint returns, so the dashboard
    can render the two paths identically.

    ``timestamp`` is ISO-8601 *string* (not a ``datetime``): the WS handler
    serializes this via ``websocket.send_json``, and a raw datetime raises
    ``TypeError: Object of type datetime is not JSON serializable`` —
    closing every telemetry socket. The REST path already stringifies; this
    keeps the two paths genuinely identical (and JSON-safe)."""
    return {
        "timestamp": record.timestamp.isoformat(),
        "source": record.source,
        "channel": record.channel,
        "value": record.value,
        "unit": record.unit,
    }


# ---------------------------------------------------------------------------
# Background tasks the daemon runs alongside the collectors
# ---------------------------------------------------------------------------


async def run_live_inference(
    source: LiveFeatureSource,
    components: DaemonComponents,
    stop_event: asyncio.Event,
    *,
    tick_seconds: float | None = None,
) -> None:
    """Drive live Isolation Forest inference from a ``LiveFeatureSource``.

    Caller wires record-stream → ``source.observe(record)`` somewhere else
    (a queue tee, a Broadcaster subscription, etc.). This task ticks at
    ``tick_seconds`` (default ``1 / preprocessing.resample_hz``), pulls a
    one-row feature frame from ``source``, runs it through
    ``IsolationForestInferenceEngine.observe`` →
    ``AnomalyEventEmitter.emit_isolation_forest_row``. The emitter's
    logger publishes events onto the anomalies broadcaster so
    ``/ws/anomalies`` subscribers see them immediately, and writes the
    JSONL for ``/anomalies`` HTTP queries.

    LSTM live inference is not yet wired here — the LSTM expects
    fixed-schema windows (``feature_columns`` matching the trained set
    exactly), which the live source builds up channel-by-channel as
    records arrive. A separate task can subscribe once the
    schema-alignment piece exists. For now: IF only.

    Best-effort: every error inside the loop is caught + logged. One bad
    row can't kill the inference stream.
    """
    interval = tick_seconds or 1.0 / max(components.config.preprocessing.resample_hz, 1.0)
    if_manager = components.model_managers.get("isolation_forest")
    if if_manager is None:
        log.info(
            "isolation_forest model manager absent; IF scoring disabled — the "
            "behavior overlay still runs off the live feature source"
        )

    while not stop_event.is_set():
        try:
            row = source.tick(datetime.now(UTC))
            loaded = if_manager.current if if_manager is not None else None
            if loaded is not None and row is not None:
                _score_and_emit(
                    loaded,
                    row,
                    components.emitter,
                    components.health_monitor,
                    scores=components.scores_broadcaster,
                )
            # Behavior deviation is independent of the IF model: the
            # operating point exists whenever telemetry is flowing, so the
            # Models▸Behavior overlay must update even with no IF artifact
            # loaded (none trained yet) and before the feature-warmup
            # window fills. Gating it behind a loaded IF model was the bug
            # that left the overlay showing only static bands.
            _observe_behavior(source, components.behavior_live)
        except Exception:  # noqa: BLE001 — live inference is best-effort
            log.exception("live inference tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return  # stop_event fired
        except TimeoutError:
            continue


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
    ``LSTMInferenceEngine.observe``. Schema-alignment policy:

    * **Missing columns are NaN.** The LSTM buffer's
      ``append`` clears itself on a non-finite value, so during the
      schema-warmup window (channels still arriving) we don't emit; once
      every trained ``feature_column`` is present, the buffer fills and
      starts emitting. This matches the model's "wait for a clean window"
      contract from training time.
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
            # idiom as the IF path; an LSTMInferenceEngine is cheap to
            # construct, expensive to load (torch tensors). Hot-swap takes
            # effect on the very next tick after ModelManager.poll() reports
            # ``swapped``.
            new_version = loaded.manifest.model_version
            if engine_version != new_version:
                try:
                    from src.models.inference import LSTMInferenceEngine

                    engine = LSTMInferenceEngine(loaded.artifact)
                    engine_version = new_version
                except Exception:  # noqa: BLE001 — bad checkpoint shouldn't kill the task
                    log.exception("failed to build LSTMInferenceEngine; will retry next tick")
                    engine = None
                    engine_version = None

        if engine is not None:
            try:
                row = source.tick(datetime.now(UTC))
                if row is not None:
                    result = _lstm_observe(engine, row)
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


def _lstm_observe(engine: Any, row: Any) -> Any:
    """Align ``row`` to the LSTM engine's expected feature columns and observe.

    ``LSTMSequenceBuffer.append`` already handles missing-column / non-finite
    cases by clearing the buffer (return False, no result), so we don't need
    to pre-validate — we just present the columns the engine asks for. The
    return value is ``LSTMInferenceResult`` once a full window is ready,
    ``None`` otherwise.
    """
    schema = _schema_for_engine(engine)
    aligned, report = reconcile_feature_frame_to_spec(row, schema)
    _log_schema_reconciliation_once(
        "lstm_autoencoder",
        _engine_version(engine),
        report,
    )
    return engine.observe(aligned.iloc[0])


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


def _score_and_emit(
    loaded: Any,
    row: Any,  # pd.DataFrame, one row
    emitter: AnomalyEventEmitter,
    health: HealthMonitor | None = None,
    *,
    scores: Broadcaster | None = None,
) -> None:
    """Run one feature row through IF.score → emitter. Best-effort.

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

    from src.models.isolation_forest import MISSING_FEATURES_COLUMN, SCORE_COLUMN

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
    _publish_score(
        scores,
        source="isolation_forest",
        score=scored.get(SCORE_COLUMN),
        threshold=None,  # IF has no calibrated threshold; boundary is ~0
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
        return
    emitter.emit_isolation_forest_row(
        scored,
        model_version=engine.model_version,
        timestamp=str(timestamp),
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


def _observe_behavior(source: LiveFeatureSource, behavior_live: BehaviorLiveState) -> None:
    """Feed one tick of per-model deviations into the live overlay state.

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
        return
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
                mode_match=model.operating_mode is None or mode == model.operating_mode,
            )
        )
    behavior_live.observe(timestamp, mode, observations)


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
    behavior_models_path = behavior_models_path or DEFAULT_BEHAVIOR_MODELS_PATH
    bindings = load_bindings(bindings_path) if bindings_path.exists() else None
    behavior_models = (
        load_behavior_models(behavior_models_path) if behavior_models_path.exists() else None
    )
    # The training feature pipeline decodes CAN twice — bindings.yaml
    # (decoded:*) AND the DBC files (dbc:*). The live source must mirror both
    # or every inference tick fails on the model's required dbc:* columns.
    dbc_database = load_dbc_dir(DEFAULT_DBC_DIR)
    return LiveFeatureSource(
        config=components.config,
        bindings=bindings,
        behavior_models=behavior_models,
        dbc_database=dbc_database,
    )


async def poll_model_managers(
    managers: dict[str, ModelManager[Any]],
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
    health: HealthMonitor | None = None,
) -> None:
    """Periodically call ``manager.poll()`` on each ModelManager.

    Light task: the manager's mtime check makes each tick cheap when nothing
    changed. Cadence comes from ``daemon.model_poll_seconds`` — refits are
    minute-to-hour scale so 30s default is fine.
    """
    while not stop_event.is_set():
        for name, manager in managers.items():
            try:
                attempt = manager.poll()
            except Exception:  # noqa: BLE001 — manager.poll already swallows + logs internally
                log.exception("model manager poll raised", extra={"manager": name})
                if health is not None:
                    health.note_swap("error")
                continue
            if health is not None:
                health.note_swap(attempt.kind, version=attempt.new_version)
            if attempt.kind == "swapped":
                log.info(
                    "model swapped",
                    extra={"manager": name, "version": attempt.new_version},
                )
            elif attempt.kind == "rejected":
                log.warning(
                    "swap rejected",
                    extra={"manager": name, "reason": attempt.detail},
                )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def run_retention_sweeper(
    storage_path: Path,
    retention_days: int,
    stop_event: asyncio.Event,
    *,
    interval_seconds: float,
    active_session_path: Callable[[], Path],
) -> None:
    """Periodically delete expired session dirs from the production daemon.

    The active session is read through ``active_session_path`` on every tick,
    not captured once at startup, because ``ParquetTelemetryWriter`` rotates
    sessions when engine run time resets.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    while not stop_event.is_set():
        active = active_session_path()
        try:
            sizes_before = _session_sizes(storage_path)
            deleted = delete_expired_sessions(
                storage_path,
                retention_days,
                protect={active},
            )
            if deleted:
                log.info(
                    "expired sessions deleted",
                    extra={
                        "count": len(deleted),
                        "reclaimed_bytes": sum(sizes_before.get(path, 0) for path in deleted),
                        "sessions": [str(path) for path in deleted],
                        "active_session": str(active),
                    },
                )
        except Exception:  # noqa: BLE001 — cleanup must not kill capture
            log.exception("retention sweep failed", extra={"storage_path": str(storage_path)})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def _session_sizes(sessions_root: Path) -> dict[Path, int]:
    if not sessions_root.exists():
        return {}
    sizes: dict[Path, int] = {}
    for child in sessions_root.iterdir():
        if child.is_dir():
            sizes[child] = _tree_size(child)
    return sizes


def _tree_size(root: Path) -> int:
    total = 0
    with suppress(OSError):
        for child in root.rglob("*"):
            with suppress(OSError):
                if child.is_file():
                    total += child.stat().st_size
    return total


@asynccontextmanager
async def serve_in_background(
    components: DaemonComponents,
    *,
    server_factory: Callable[..., uvicorn.Server] | None = None,
) -> AsyncIterator[uvicorn.Server]:
    """Run the FastAPI dashboard inside the same event loop as the collectors.

    The returned context manager runs the uvicorn server in a background
    task; on exit, sends the graceful-shutdown signal and waits for the
    task. ``server_factory`` is injectable for tests — pass a factory
    that builds a server bound to a high local port, e.g.
    ``lambda **kw: uvicorn.Server(uvicorn.Config(**kw, port=0))``.

    Why uvicorn.Server.serve() rather than uvicorn.run(): the latter spins
    its own event loop, which conflicts with the daemon's. ``Server.serve``
    is the documented "I already have a loop" entry.
    """
    app = create_app(
        components.config,
        anomalies_dir=components.config.daemon.anomalies_dir,
        models_dir=components.config.daemon.models_dir,
        telemetry_broadcaster=components.telemetry_broadcaster,
        anomalies_broadcaster=components.anomalies_broadcaster,
        scores_broadcaster=components.scores_broadcaster,
        control_broadcaster=components.control_broadcaster,
        emitter=components.emitter,
        capture_controller=components.capture_controller,
        job_controller=components.job_controller,
        audit_logger=components.audit_logger,
        feedback_store=FeedbackStore(components.config.daemon.feedback_log),
        alert_tuner=components.alert_tuner,
        health_monitor=components.health_monitor,
        behavior_live=components.behavior_live,
    )
    if components.job_controller is not None:
        loop = asyncio.get_running_loop()
        components.job_controller.set_event_callback(
            lambda event: components.control_broadcaster.publish_threadsafe(loop, event)
        )
    if server_factory is None:
        server_factory = uvicorn.Server
    server_config = uvicorn.Config(
        app,
        host=components.config.dashboard.host,
        port=components.config.dashboard.port,
        log_level="info",
        lifespan="on",
    )
    server = server_factory(config=server_config)
    serve_task = asyncio.create_task(server.serve())
    # uvicorn flips server.started True once the listening socket is open.
    for _ in range(100):
        if getattr(server, "started", False):
            break
        await asyncio.sleep(0.05)
    try:
        yield server
    finally:
        server.should_exit = True
        await serve_task


# ---------------------------------------------------------------------------
# Top-level run loop (extension of src.main.run; not yet wired from main)
# ---------------------------------------------------------------------------


async def run_daemon(
    config: CanaryConfig,
    stop_event: asyncio.Event,
    *,
    collectors: Sequence[Callable[..., Any]] | None = None,
    writer: ParquetTelemetryWriter | None = None,
    components: DaemonComponents | None = None,
    enable_live_inference: bool = True,
) -> DaemonComponents:
    """Run the full daemon until ``stop_event`` fires.

    Builds components (or accepts pre-built ones for tests), spins up:
      * the collectors (CAN, OBD) → ``asyncio.Queue``
      * ``drain_queue_with_broadcast`` → Parquet + telemetry broadcaster +
        optional live source (every drained record fans out to all three)
      * ``run_live_inference`` → IF scoring on each tick → emitter →
        anomalies broadcaster + JSONL  (when ``enable_live_inference`` AND
        the IF model manager has a loaded artifact; otherwise the task
        becomes a passive wait on ``stop_event``)
      * ``poll_model_managers`` → hot-swap loop
      * ``run_retention_sweeper`` → age-based session retention
      * the dashboard via ``serve_in_background``

    Returns the components so a caller can inspect counters / model
    versions for diagnostics or tests.
    """
    components = components or build_components(config)
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=config.collection.queue_max_size)
    storage_writer = (
        ParquetTelemetryWriter(config.collection.storage_path) if writer is None else writer
    )
    components.capture_controller = CaptureController(lambda: storage_writer.paths.root)

    # Build the live source (no-op if bindings/behavior_models are absent on
    # disk — observe still works, just without decoded:* / deviation:* columns).
    live_source = build_live_feature_source(components) if enable_live_inference else None

    # Lazy import — keeps daemon module importable without the collector deps
    # at module-load time.
    from src.main import _default_collectors

    if collectors is not None:
        active_collectors = list(collectors)
    elif config.collection.replay_session:
        # Simulation backend: replay a recorded session instead of opening
        # real CAN/OBD. Same queue → same decode/broadcast/inference path.
        from src.collection.replay import make_replay_collector

        active_collectors = [make_replay_collector(config)]
        log.info(
            "replay mode: real collectors disabled",
            extra={"session": config.collection.replay_session},
        )
    else:
        active_collectors = _default_collectors(
            config,
            obd_scheduler_metrics_callback=components.health_monitor.observe_obd_scheduler,
        )

    drain_task = asyncio.create_task(
        drain_queue_with_broadcast(
            queue,
            storage_writer,
            stop_event,
            telemetry_broadcaster=components.telemetry_broadcaster,
            live_source=live_source,
            storage_path=config.collection.storage_path,
            storage_warning_mb=config.collection.storage_warning_mb,
            queue_warning_fraction=config.collection.queue_warning_fraction,
        )
    )
    poll_task = asyncio.create_task(
        poll_model_managers(
            components.model_managers,
            stop_event,
            interval_seconds=config.daemon.model_poll_seconds,
            health=components.health_monitor,
        )
    )
    retention_task = asyncio.create_task(
        run_retention_sweeper(
            config.collection.storage_path,
            config.collection.session_retention_days,
            stop_event,
            interval_seconds=config.daemon.retention_sweep_seconds,
            active_session_path=lambda: storage_writer.paths.root,
        )
    )
    inference_tasks: list[asyncio.Task[Any]] = []
    if live_source is not None:
        inference_tasks.append(
            asyncio.create_task(run_live_inference(live_source, components, stop_event))
        )
        if "lstm_autoencoder" in components.model_managers:
            inference_tasks.append(
                asyncio.create_task(run_live_inference_lstm(live_source, components, stop_event))
            )
    collector_tasks = [
        asyncio.create_task(starter(queue, stop_event)) for starter in active_collectors
    ]
    stop_task = asyncio.create_task(stop_event.wait())

    async with serve_in_background(components):
        try:
            done, _ = await asyncio.wait(
                [stop_task, *collector_tasks], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stop_task:
                    task.result()
        finally:
            stop_event.set()
            await asyncio.gather(*collector_tasks, return_exceptions=True)
            await queue.join()
            await drain_task
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
            retention_task.cancel()
            try:
                await retention_task
            except asyncio.CancelledError:
                pass
            for task in inference_tasks:
                # Each inference task observes stop_event and returns on its
                # own; this is the join + cancel safety net.
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (TimeoutError, asyncio.CancelledError):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            components.event_logger.close()
    return components


__all__ = [
    "DaemonComponents",
    "build_components",
    "drain_queue_with_broadcast",
    "poll_model_managers",
    "run_retention_sweeper",
    "run_daemon",
    "serve_in_background",
]
