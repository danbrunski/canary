from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.event_logger import AnomalyEventLogger
from src.alerts.events import AnomalyEvent
from src.alerts.fusion import PersistenceGate
from src.alerts.notifier import Notifier
from src.alerts.tuning import AlertTuner, load_tuning_config
from src.analysis.gps_tracker import GpsTracker
from src.analysis.vehicle_ledger import VehicleLedgerStore
from src.collection.obd_scheduler import ForcePollTrigger, FreezeFrameTrigger
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.dtc_state import DEFAULT_STALE_AFTER_SECONDS as DEFAULT_DTC_STALE_AFTER_SECONDS
from src.control.dtc_state import DtcState
from src.control.jobs import JobController
from src.control.monitor_state import (
    DEFAULT_STALE_AFTER_SECONDS as DEFAULT_MONITOR_STALE_AFTER_SECONDS,
)
from src.control.monitor_state import MonitorState
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.daemon.idle_shutdown import IdleShutdownMonitor
from src.daemon.inference_metrics import InferenceTimingRecorder
from src.dashboard.behavior_live import BehaviorLiveState
from src.dashboard.broadcast import Broadcaster
from src.models.health import HealthMonitor
from src.models.manager import ModelManager, manager_for_artifact
from src.models.sequence_registry import resolve_sequence_backend
from src.storage.async_writer import AsyncParquetTelemetryWriter

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
    #: ntfy pusher (Task 5.2) — ships disabled by default (see
    #: ``NotificationsConfig``); the emitter uses this for critical alerts
    #: (5.3) and the trip-end hook uses it for the digest push (5.5).
    notifier: Notifier
    model_managers: dict[str, ModelManager[Any]]
    alert_tuner: AlertTuner
    health_monitor: HealthMonitor
    #: Shared across every tick of ``run_live_inference`` (IF/LSTM/behavior
    #: keys all latch on this one instance) — held here, not as a local
    #: inside the inference loop, so ``ReplayController`` can reach it and
    #: reset it on the replay boundary (Phase 7.1).
    persistence_gate: PersistenceGate
    #: ``model_type`` of the active sequence-detector slot (e.g.
    #: ``"lstm_autoencoder"``), or ``None`` when no sequence detector is
    #: enabled. The serve loop looks up its manager by this key, so switching
    #: backbones is a config change, not a code change.
    sequence_model_type: str | None = None
    behavior_live: BehaviorLiveState = field(default_factory=BehaviorLiveState)
    #: Cached latest DTC report + per-session diff/sidecar state (Task 1.4).
    dtc_state: DtcState = field(default_factory=DtcState)
    #: Cached latest Mode 06 monitor sweep + per-session misfire diff (Task 3.1).
    monitor_state: MonitorState = field(default_factory=MonitorState)
    #: Shared flag bridging ``dtc_state`` (asyncio DTC sink) and the OBD
    #: collector loop (its own thread) — a new stored DTC requests a one-shot
    #: Mode 02 freeze-frame read (Task 3.3). Same instance is threaded into
    #: both ``dtc_state`` (constructed below) and the collector's
    #: ``CanObdSpec`` (``_default_collectors``, via ``run_daemon``).
    freeze_frame_trigger: FreezeFrameTrigger = field(default_factory=FreezeFrameTrigger)
    #: Bridges the ``trigger_poll`` control route (asyncio) and the OBD
    #: collector loop (its own thread) — forces specific channels due on the
    #: collector's next tick instead of waiting out their tier cadence.
    #: Threaded into the collector's ``CanObdSpec`` the same way
    #: ``freeze_frame_trigger`` is (``_default_collectors``, via
    #: ``run_daemon``); inert (never consumed) when the OBD source isn't
    #: ``canable``.
    force_poll_trigger: ForcePollTrigger = field(default_factory=ForcePollTrigger)
    #: Vehicle maintenance/intervention ledger (Task 3.1) — a new stored DTC
    #: (Task 3.4) and operator/MCP writes both append here.
    vehicle_ledger: VehicleLedgerStore = field(default_factory=VehicleLedgerStore)
    capture_controller: CaptureController | None = None
    job_controller: JobController | None = None
    replay_controller: ReplayController | None = None
    #: GPS live-state + trip odometer, or ``None`` when no GPS is enabled.
    gps_tracker: GpsTracker | None = None
    #: Live read/swap of the active sequence-detector backbone (set in
    #: ``build_components`` once the component record exists).
    sequence_detector_controller: SequenceDetectorController | None = None
    #: The daemon's one durable Parquet writer (set by ``run_daemon`` after it
    #: builds ``storage_writer`` — ``None`` in tests that never wire storage).
    #: Task 1.1 follow-up: lets ``run_live_inference`` persist derived
    #: performance channels through the *same* writer ``run_storage_sink``
    #: uses, instead of a second Parquet-writing path.
    storage_writer: AsyncParquetTelemetryWriter | None = None
    #: Escalates a sustained parked-with-power state to a poweroff (spec
    #: §3.7.6). ``None`` when ``IdleShutdownConfig.enabled`` is false, which
    #: is the shipped default — the inference tick then skips it entirely.
    idle_shutdown: IdleShutdownMonitor | None = None
    #: C08 — per-tick stage timing, loop lateness and overload counters for
    #: the live-inference scheduler; written by ``run_live_inference`` (loop
    #: only), read by ``/status`` ``detectors.inference``.
    inference_timing: InferenceTimingRecorder = field(default_factory=InferenceTimingRecorder)


def build_components(config: CanaryConfig) -> DaemonComponents:
    """Construct all long-lived components from config. No I/O beyond mkdir."""
    telemetry_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)
    anomalies_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)
    scores_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)
    control_bus = Broadcaster(queue_size=config.daemon.broadcaster_queue_size)

    event_logger = AnomalyEventLogger(config.daemon.anomalies_dir)
    audit_logger = ControlAuditLogger(config.daemon.audit_log)
    tuner = AlertTuner(load_tuning_config(config.daemon.tuning_config))
    notifier = Notifier(config.notifications)
    emitter = _make_broadcasting_emitter(
        event_logger,
        anomalies_bus,
        tuner=tuner,
        min_seconds_between_events=config.alerts.min_seconds_between_events,
        notifier=notifier,
        notify_min_severity=config.notifications.min_severity,
        notify_max_pushes_per_session=config.notifications.max_pushes_per_session,
    )
    model_managers = _load_model_managers(config)
    alerts_cfg = config.alerts
    persistence_gate = PersistenceGate(
        alerts_cfg.min_consecutive_ticks,
        window=alerts_cfg.persistence_window_ticks,
        window_seconds=alerts_cfg.persistence_window_seconds,
    )
    freeze_frame_trigger = FreezeFrameTrigger()

    components = DaemonComponents(
        config=config,
        telemetry_broadcaster=telemetry_bus,
        anomalies_broadcaster=anomalies_bus,
        scores_broadcaster=scores_bus,
        control_broadcaster=control_bus,
        event_logger=event_logger,
        audit_logger=audit_logger,
        emitter=emitter,
        notifier=notifier,
        model_managers=model_managers,
        sequence_model_type=_sequence_model_type(config),
        alert_tuner=tuner,
        health_monitor=HealthMonitor(),
        persistence_gate=persistence_gate,
        behavior_live=BehaviorLiveState(),
        dtc_state=DtcState(
            stale_after_seconds=max(
                config.collection.obd_dtc_poll_interval_seconds * 3,
                DEFAULT_DTC_STALE_AFTER_SECONDS,
            ),
            catalog_db=config.daemon.catalog_db,
            freeze_frame_trigger=freeze_frame_trigger,
        ),
        monitor_state=MonitorState(
            stale_after_seconds=max(
                config.collection.obd_monitor_poll_interval_seconds * 3,
                DEFAULT_MONITOR_STALE_AFTER_SECONDS,
            ),
        ),
        freeze_frame_trigger=freeze_frame_trigger,
        vehicle_ledger=VehicleLedgerStore(config.daemon.vehicle_ledger),
        capture_controller=None,
        job_controller=_make_job_controller(config),
        gps_tracker=(
            GpsTracker(state_path=config.daemon.trip_state) if config.hardware.gps.enabled else None
        ),
        idle_shutdown=(
            IdleShutdownMonitor.from_config(config.collection.idle_shutdown)
            if config.collection.idle_shutdown.enabled
            else None
        ),
    )
    # Needs a reference back to the assembled record (model_managers +
    # sequence_model_type are what it swaps), so wire it after construction.
    components.sequence_detector_controller = SequenceDetectorController(components)
    return components


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
    min_seconds_between_events: float = 1.0,
    notifier: Notifier | None = None,
    notify_min_severity: float = 0.8,
    notify_max_pushes_per_session: int = 3,
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
        min_seconds_between_events=min_seconds_between_events,
        notifier=notifier,
        notify_min_severity=notify_min_severity,
        notify_max_pushes_per_session=notify_max_pushes_per_session,
    )


def _load_model_managers(config: CanaryConfig) -> dict[str, ModelManager[Any]]:
    """Build one ``ModelManager`` per active detector, loading the initial
    artifact if present. Missing artifacts are fine — the manager keeps polling,
    and ``current`` is ``None`` until something lands.

    Two detector roles: the always-on row-wise Isolation Forest, and one
    *selectable* sequence detector resolved from the
    :mod:`~src.models.sequence_registry` (GRU-D LSTM today; a TCN later). The
    sequence manager is keyed by its ``model_type`` so the published anomaly
    source and dashboard vocab stay stable across backbones.
    """
    managers: dict[str, ModelManager[Any]] = {}

    if config.models.isolation_forest.enabled:
        from src.models.isolation_forest import load_isolation_forest_model

        if_path = config.daemon.models_dir / config.daemon.isolation_forest_artifact
        managers["isolation_forest"] = manager_for_artifact(
            if_path, load_isolation_forest_model, model_type="isolation_forest"
        )

    backend = resolve_sequence_backend(config)
    if backend is not None:
        missing = backend.missing_requirements()
        if missing:
            log.warning(
                "sequence detector %s enabled but %s unavailable; skipping ModelManager",
                backend.model_type,
                ", ".join(missing),
            )
        else:
            managers[backend.model_type] = manager_for_artifact(
                backend.artifact_path(config),
                backend.load_model,
                model_type=backend.model_type,
            )
    return managers


def _sequence_model_type(config: CanaryConfig) -> str | None:
    """``model_type`` of the configured sequence detector, or ``None`` if off."""
    backend = resolve_sequence_backend(config)
    return backend.model_type if backend is not None else None


# ---------------------------------------------------------------------------
# Queue draining with broadcaster fan-out

__all__ = [
    "DaemonComponents",
    "build_components",
]
