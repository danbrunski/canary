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
from src.control.sequence_detector import SequenceDetectorController
from src.dashboard.behavior_live import BehaviorLiveState
from src.dashboard.broadcast import Broadcaster
from src.models.health import HealthMonitor
from src.models.manager import ModelManager, manager_for_artifact
from src.models.sequence_registry import resolve_sequence_backend

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
    #: ``model_type`` of the active sequence-detector slot (e.g.
    #: ``"lstm_autoencoder"``), or ``None`` when no sequence detector is
    #: enabled. The serve loop looks up its manager by this key, so switching
    #: backbones is a config change, not a code change.
    sequence_model_type: str | None = None
    behavior_live: BehaviorLiveState = field(default_factory=BehaviorLiveState)
    capture_controller: CaptureController | None = None
    job_controller: JobController | None = None
    replay_controller: ReplayController | None = None
    #: GPS live-state + trip odometer, or ``None`` when no GPS is enabled.
    gps_tracker: GpsTracker | None = None
    #: Live read/swap of the active sequence-detector backbone (set in
    #: ``build_components`` once the component record exists).
    sequence_detector_controller: SequenceDetectorController | None = None


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

    components = DaemonComponents(
        config=config,
        telemetry_broadcaster=telemetry_bus,
        anomalies_broadcaster=anomalies_bus,
        scores_broadcaster=scores_bus,
        control_broadcaster=control_bus,
        event_logger=event_logger,
        audit_logger=audit_logger,
        emitter=emitter,
        model_managers=model_managers,
        sequence_model_type=_sequence_model_type(config),
        alert_tuner=tuner,
        health_monitor=HealthMonitor(),
        behavior_live=BehaviorLiveState(),
        capture_controller=None,
        job_controller=_make_job_controller(config),
        gps_tracker=(
            GpsTracker(state_path=config.daemon.trip_state) if config.hardware.gps.enabled else None
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
