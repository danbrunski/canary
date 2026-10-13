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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.tuning import AlertTuner
from src.analysis.gps_tracker import GpsTracker
from src.behavior.accepted_baselines import DEFAULT_ACCEPTED_BASELINES_PATH
from src.collection.capture_profiles import CaptureProfileSources, CaptureProfileStore
from src.collection.imu_collector import ImuHealth
from src.collection.kline.plan import KlineCatalog
from src.collection.obd_scheduler import ForcePollTrigger
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.dtc_state import DtcState
from src.control.jobs import JobController
from src.control.key_cycle_pairing import KeyCyclePairing
from src.control.monitor_state import MonitorState
from src.control.paired_devices import PairedDevices
from src.control.paired_devices import default_path as paired_devices_default_path
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.daemon.behavior_live import BehaviorLiveState
from src.daemon.broadcast import Broadcaster
from src.daemon.ups import DEFAULT_UPS_KEEPALIVE_PATH, DEFAULT_UPS_STATUS_PATH
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
from src.dashboard.routes.pairing import register_pairing_routes
from src.dashboard.routes.rom_library import register_rom_library_routes
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
from src.dashboard.version import _canary_version
from src.dashboard.websockets import _publish_on_app_loop
from src.models.feedback import FeedbackStore
from src.provisioning.gate import DEFAULT_WINDOW_PATH
from src.provisioning.reader import NetworkStatusReader
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.vehicle.vehicle_ledger import VehicleLedgerStore
from src.vehicle_state import derive_from_live_source

if TYPE_CHECKING:  # annotation only — src.daemon's package __init__ imports this module
    # Annotation only: importing it at runtime loads the whole src.daemon package (R04.8).
    from src.control.profile_capture import CaptureAutostart, ProfileCaptureSession
    from src.daemon.health import HealthMonitor
    from src.daemon.inference_metrics import InferenceTimingRecorder

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the key-cycle pairing tick while the app serves.

    It watches the key even with no pairing request waiting, because a lone
    ACC -> ON -> ACC is also how the owner reopens BLE setup to change WiFi.
    """
    pairing: KeyCyclePairing | None = getattr(app.state, "pairing", None)
    if pairing is not None:
        pairing.ensure_running()
    try:
        yield
    finally:
        if pairing is not None:
            await pairing.aclose()


def create_app(
    config: CanaryConfig,
    *,
    anomalies_dir: Path,
    models_dir: Path | None = None,
    telemetry_broadcaster: Broadcaster | None = None,
    anomalies_broadcaster: Broadcaster | None = None,
    scores_broadcaster: Broadcaster | None = None,
    emitter: AnomalyEventEmitter | None = None,
    behavior_models_path: Path | None = None,
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
    #: The IMU collector's counters for ``/status.imu`` (H02.5).
    imu_health: ImuHealth | None = None,
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
    #: BLE provisioning window (``src/provisioning/gate.py``).
    provision_window_path: Path = DEFAULT_WINDOW_PATH,
    #: Cached ``nmcli``/``ip`` reads behind ``GET /control/network`` and
    #: ``/status.network`` (P2.3). ``None`` (tests, bare apps) reports them as
    #: not wired; the daemon passes one.
    network_reader: NetworkStatusReader | None = None,
    #: Phones and laptops with their own control tokens (P2.4); ``None`` uses
    #: ``paired_devices.json`` beside the control state.
    paired_devices: PairedDevices | None = None,
    #: Key-cycle pairing over ``paired_devices``; ``None`` builds one that
    #: watches ``live_source``. Tests inject one with a scripted key.
    pairing: KeyCyclePairing | None = None,
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

    ``behavior_models_path`` defaults to ``config.daemon.behavior_models``
    (P02.12), which the daemon has scoped to the vehicle, never the repo-level
    ``data/behavior_models.yaml``.
    """
    if behavior_models_path is None:
        behavior_models_path = config.daemon.behavior_models
    app = FastAPI(title="CANary", version=_canary_version(), lifespan=_lifespan)
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
        CaptureProfileSources.of(config.daemon),
        config.collection,
        behavior_models_path,
        custom_pids_path=config.daemon.custom_pids,
        kline_catalog=KlineCatalog.from_config(config),
    )
    # Stash them on app.state so producer threads / app extensions can reach
    # the same instances after construction.
    app.state.telemetry_broadcaster = telemetry_bus
    app.state.anomalies_broadcaster = anomalies_bus
    app.state.scores_broadcaster = scores_bus
    app.state.control_broadcaster = control_bus
    app.state.control_loop = None
    # Read by the shared control-auth dependency (src/dashboard/security.py),
    # so every guarded route accepts paired-device tokens without wiring.
    app.state.paired_devices = paired_devices or PairedDevices(
        paired_devices_default_path(config.daemon.state_dir)
    )

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
        imu_health=imu_health,
        sequence_detector_controller=sequence_detector_controller,
        inference_timing=inference_timing,
        live_source=live_source,
        ups_status_path=ups_status_path,
        network_reader=network_reader,
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
        provision_window_path=provision_window_path,
        network_reader=network_reader,
        emitter=emitter,
        live_source=live_source,
        profile_capture=profile_capture,
        capture_autostart=capture_autostart,
        publish_control_event=publish_control_event,
        inference_timing=inference_timing,
    )
    register_rom_routes(
        app, config=config, resolved_control_token=resolved_control_token, dids_path=dids_path
    )
    register_rom_library_routes(
        app,
        config=config,
        resolved_control_token=resolved_control_token,
        live_source=live_source,
        replay_controller=replay_controller,
        capture_controller=capture_controller,
        publish_control_event=publish_control_event,
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

    def live_vehicle_state() -> str | None:
        if live_source is None:
            return None
        vehicle = derive_from_live_source(live_source, replay_controller=replay_controller)
        # Key-on without the engine shows only as the ECM answering OBD. While
        # passive, CANary is not asking, so no verdict is evidence of the key.
        return "passive" if vehicle.passive else vehicle.state

    app.state.pairing = pairing or KeyCyclePairing(
        app.state.paired_devices,
        live_vehicle_state,
        window_path=provision_window_path,
        publish=publish_control_event,
    )
    register_pairing_routes(
        app,
        devices=app.state.paired_devices,
        pairing=app.state.pairing,
        resolved_control_token=resolved_control_token,
        publish_control_event=publish_control_event,
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
