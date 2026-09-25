"""Dashboard, public observation, and authenticated control API.

The route packages separate unauthenticated status/telemetry/analysis GETs,
authenticated ``/control/*`` operations, and live WebSocket streams. Control
operations include capture and replay management, jobs, model publishing,
diagnostic requests, and volatile vehicle actions; CANary never writes ECU
non-volatile memory or performs a reflash.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.feedback import FeedbackStore
from src.alerts.tuning import AlertTuner
from src.analysis.gps_tracker import GpsTracker
from src.analysis.vehicle_ledger import VehicleLedgerStore
from src.collection.capture_profiles import CaptureProfileStore
from src.collection.obd_scheduler import ForcePollTrigger
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.dtc_state import DtcState
from src.control.jobs import JobController
from src.control.monitor_state import MonitorState
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.dashboard.behavior_live import BehaviorLiveState
from src.dashboard.broadcast import Broadcaster
from src.dashboard.paths import (
    DEFAULT_BINDINGS_PATH,
    DEFAULT_DIDS_PATH,
    DEFAULT_STATIC_DIR,
    DEFAULT_TEST_PROFILES_DIR,
)
from src.dashboard.readers import (
    echo_config,
    read_accepted_baselines,
    read_anomalies_since,
    read_behavior_models,
    read_bindings_for_display,
    read_can_coverage,
    read_latest_per_channel,
    read_latest_telemetry_rows,
)
from src.dashboard.routes import (
    register_control_routes,
    register_public_routes,
    register_stream_routes,
)
from src.dashboard.routes.calibration import register_calibration_routes
from src.dashboard.routes.rom_maps import register_rom_routes
from src.dashboard.routes.workbench import register_workbench_routes
from src.dashboard.security import _client_is_loopback, _require_control_auth
from src.dashboard.status import (
    StatusSnapshot,
    StatusSources,
    build_status,
    compact_status,
    gather_status_sources,
)
from src.dashboard.ups import DEFAULT_UPS_KEEPALIVE_PATH, DEFAULT_UPS_STATUS_PATH
from src.dashboard.version import _canary_version
from src.dashboard.websockets import _publish_on_app_loop
from src.decode.accepted_baselines import DEFAULT_ACCEPTED_BASELINES_PATH
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
)
from src.models.health import HealthMonitor
from src.storage.async_writer import AsyncParquetTelemetryWriter

if TYPE_CHECKING:  # annotation only — src.daemon's package __init__ imports this module
    from src.control.profile_capture import CaptureAutostart, ProfileCaptureSession
    from src.daemon.inference_metrics import InferenceTimingRecorder

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    config: CanaryConfig,
    *,
    anomalies_dir: Path,
    models_dir: Path | None = None,
    telemetry_broadcaster: Broadcaster | None = None,
    anomalies_broadcaster: Broadcaster | None = None,
    scores_broadcaster: Broadcaster | None = None,
    emitter: AnomalyEventEmitter | None = None,
    behavior_models_path: Path = DEFAULT_BEHAVIOR_MODELS_PATH,
    accepted_baselines_path: Path = DEFAULT_ACCEPTED_BASELINES_PATH,
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
    dids_path: Path = DEFAULT_DIDS_PATH,
    test_profiles_dir: Path = DEFAULT_TEST_PROFILES_DIR,
    static_dir: Path | None = None,
    enable_dev_cors: bool = False,
    capture_controller: CaptureController | None = None,
    job_controller: JobController | None = None,
    replay_controller: ReplayController | None = None,
    control_broadcaster: Broadcaster | None = None,
    audit_logger: ControlAuditLogger | None = None,
    control_token: str | None = None,
    feedback_store: FeedbackStore | None = None,
    vehicle_ledger_store: VehicleLedgerStore | None = None,
    alert_tuner: AlertTuner | None = None,
    health_monitor: HealthMonitor | None = None,
    behavior_live: BehaviorLiveState | None = None,
    dtc_state: DtcState | None = None,
    monitor_state: MonitorState | None = None,
    gps_tracker: GpsTracker | None = None,
    sequence_detector_controller: SequenceDetectorController | None = None,
    #: Bridges `trigger_poll` into the OBD collector loop (`None` in tests
    #: that don't wire the live collector — the route reports 503).
    force_poll_trigger: ForcePollTrigger | None = None,
    #: Live writer, so arming tuning mode can quarantine the capture into a
    #: ``_tuning-*`` session (engine-tuning-support.md 1.2).
    storage_writer: AsyncParquetTelemetryWriter | None = None,
    capture_profile_store: CaptureProfileStore | None = None,
    ups_status_path: Path = DEFAULT_UPS_STATUS_PATH,
    ups_keepalive_path: Path = DEFAULT_UPS_KEEPALIVE_PATH,
    #: C08 — live-inference scheduler timing for ``/status``
    #: ``detectors.inference`` (``None`` in tests that don't run inference).
    inference_timing: InferenceTimingRecorder | None = None,
    live_source: Any = None,
    #: R03.5 — the daemon's capture lifecycle and autostart, shared with its
    #: shutdown path. ``None`` builds them over the components above.
    profile_capture: ProfileCaptureSession | None = None,
    capture_autostart: CaptureAutostart | None = None,
) -> FastAPI:
    """Build a stateless FastAPI app over the configured paths.

    Routes delegate to pure helpers so the same logic is unit-testable
    without spinning up the web stack. The broadcasters power the
    WebSocket streams (5.4); pass ``None`` and those endpoints will
    accept connections but never emit (useful in tests or when the
    daemon isn't producing yet).
    """
    app = FastAPI(title="CANary", version=_canary_version())
    if enable_dev_cors:
        # `pnpm dev` serves the SPA on :5173 and proxies the backend.
        # Add CORS so a developer can also hit the backend directly from
        # the browser without going through the Vite proxy. Production
        # serves the SPA from the same origin and doesn't need CORS.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    telemetry_bus = telemetry_broadcaster or Broadcaster()
    anomalies_bus = anomalies_broadcaster or Broadcaster()
    scores_bus = scores_broadcaster or Broadcaster()
    control_bus = control_broadcaster or Broadcaster()
    resolved_control_token = (
        os.environ.get(config.daemon.control_token_env) if control_token is None else control_token
    )
    if capture_profile_store is None and profile_capture is not None:
        # One store for the routes and the lifecycle they drive.
        capture_profile_store = profile_capture.profile_store
    resolved_capture_profile_store = capture_profile_store or CaptureProfileStore(
        config.daemon.capture_profiles,
        config.collection,
        behavior_models_path,
        custom_pids_path=config.daemon.custom_pids,
    )
    # Stash them on app.state so producer threads / app extensions can reach
    # the same instances after construction.
    app.state.telemetry_broadcaster = telemetry_bus
    app.state.anomalies_broadcaster = anomalies_bus
    app.state.scores_broadcaster = scores_bus
    app.state.control_broadcaster = control_bus
    app.state.control_loop = None

    def publish_control_event(action: str, result: str, payload: dict[str, Any]) -> None:
        event = (
            audit_logger.record(action=action, result=result, payload=payload)
            if audit_logger is not None
            else {
                "action": action,
                "result": result,
                "payload": payload,
            }
        )
        _publish_on_app_loop(control_bus, event, getattr(app.state, "control_loop", None))

    register_public_routes(
        app,
        config=config,
        anomalies_dir=anomalies_dir,
        models_dir=models_dir,
        emitter=emitter,
        feedback_store=feedback_store,
        behavior_models_path=behavior_models_path,
        accepted_baselines_path=accepted_baselines_path,
        storage_writer=storage_writer,
        bindings_path=bindings_path,
        dids_path=dids_path,
        test_profiles_dir=test_profiles_dir,
        replay_controller=replay_controller,
        health_monitor=health_monitor,
        behavior_live=behavior_live,
        dtc_state=dtc_state,
        monitor_state=monitor_state,
        gps_tracker=gps_tracker,
        sequence_detector_controller=sequence_detector_controller,
        inference_timing=inference_timing,
        live_source=live_source,
        ups_status_path=ups_status_path,
    )
    register_control_routes(
        app,
        config=config,
        anomalies_dir=anomalies_dir,
        resolved_control_token=resolved_control_token,
        capture_controller=capture_controller,
        job_controller=job_controller,
        replay_controller=replay_controller,
        control_bus=control_bus,
        audit_logger=audit_logger,
        feedback_store=feedback_store,
        vehicle_ledger_store=vehicle_ledger_store,
        alert_tuner=alert_tuner,
        sequence_detector_controller=sequence_detector_controller,
        force_poll_trigger=force_poll_trigger,
        storage_writer=storage_writer,
        capture_profile_store=resolved_capture_profile_store,
        health_monitor=health_monitor,
        ups_status_path=ups_status_path,
        ups_keepalive_path=ups_keepalive_path,
        emitter=emitter,
        live_source=live_source,
        profile_capture=profile_capture,
        capture_autostart=capture_autostart,
        publish_control_event=publish_control_event,
    )
    register_rom_routes(
        app, config=config, resolved_control_token=resolved_control_token, dids_path=dids_path
    )
    register_calibration_routes(
        app,
        config=config,
        dids_path=dids_path,
        resolved_control_token=resolved_control_token,
    )
    register_workbench_routes(
        app,
        config=config,
        bindings_path=bindings_path,
        dids_path=dids_path,
        resolved_control_token=resolved_control_token,
    )
    register_stream_routes(
        app,
        telemetry_bus=telemetry_bus,
        anomalies_bus=anomalies_bus,
        scores_bus=scores_bus,
        control_bus=control_bus,
        resolved_control_token=resolved_control_token,
    )

    # Serve the built SPA from /, with SPA fall-through on 404 (the React
    # router could later own client-side paths like /diagnostics).
    # The mount is added last so the API routes above always win when their
    # paths overlap. ``html=True`` makes ``index.html`` the fallback for
    # any unmatched path, exactly what an SPA wants.
    chosen_static = static_dir if static_dir is not None else DEFAULT_STATIC_DIR
    if chosen_static.exists():
        app.mount("/", StaticFiles(directory=str(chosen_static), html=True), name="spa")
    else:
        log.info(
            "static dir not built; SPA not mounted",
            extra={"static_dir": str(chosen_static)},
        )

    return app


__all__ = [
    "DEFAULT_BINDINGS_PATH",
    "DEFAULT_DIDS_PATH",
    "DEFAULT_STATIC_DIR",
    "DEFAULT_TEST_PROFILES_DIR",
    "StatusSnapshot",
    "StatusSources",
    "build_status",
    "compact_status",
    "create_app",
    "echo_config",
    "gather_status_sources",
    "_client_is_loopback",
    "_require_control_auth",
    "read_accepted_baselines",
    "read_anomalies_since",
    "read_behavior_models",
    "read_bindings_for_display",
    "read_can_coverage",
    "read_latest_per_channel",
    "read_latest_telemetry_rows",
]
