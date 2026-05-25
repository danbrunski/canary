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
from src.alerts.tuning import AlertTuner, load_tuning_config
from src.analysis.gps_tracker import GpsTracker
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.jobs import JobController
from src.control.replay_control import ReplayController
from src.dashboard.behavior_live import BehaviorLiveState
from src.dashboard.broadcast import Broadcaster
from src.models.health import HealthMonitor
from src.models.manager import ModelManager, manager_for_artifact

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
    replay_controller: ReplayController | None = None
    #: GPS live-state + trip odometer, or ``None`` when no GPS is enabled.
    gps_tracker: GpsTracker | None = None


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
        gps_tracker=(
            GpsTracker(state_path=config.daemon.trip_state) if config.hardware.gps.enabled else None
        ),
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

__all__ = [
    "DaemonComponents",
    "build_components",
]
