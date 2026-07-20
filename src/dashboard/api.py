"""Read-only REST surface for the dashboard (Task 5.3).

Four endpoints, all read-only, all backed by files the daemon already
writes — no live producer wiring yet (the WebSocket layer in Task 5.4
will do that). The goal here is to unblock the frontend so the dashboard
can be built against a real backend without depending on Phase 5's full
inference loop landing first.

* ``GET /status`` — service health: storage path, free disk, the most
  recent session directory observed under ``collection.storage_path``,
  loaded model versions (best-effort: reads JSON model manifests if a
  models directory is configured), and the loaded config's vehicle line.
* ``GET /config`` — read-only echo of the loaded ``canary.yaml`` with
  internal-only paths surfaced. Useful for "what is this Pi actually
  running" diagnostics.
* ``GET /anomalies?since=...`` — reads the ``AnomalyEventLogger``'s
  ``anomalies-YYYY-MM-DD.jsonl`` files under a configured anomalies
  directory; filters by ``since`` ISO-8601 timestamp.
* ``GET /telemetry/latest?n=...`` — the most recent ``n`` rows from the
  newest Parquet chunk in the newest session, as a list of
  ``{timestamp, source, channel, value, unit}`` dicts. Pure file read;
  no daemon coupling.

The factory ``create_app(config, *, anomalies_dir, models_dir)`` keeps the
app stateless — every request re-reads from disk (cheap for v1; if the
anomalies file ever grows large enough to matter, the dashboard can ask
with a ``since=`` filter to bound the work).

Bind-host policy (Task 5.5) is enforced at the entry point that runs
``uvicorn.run`` — not here, since the app itself doesn't know where it's
mounted.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.feedback import FeedbackStore
from src.alerts.tuning import AlertTuner
from src.analysis.gps_tracker import GpsTracker
from src.analysis.vehicle_ledger import VehicleLedgerStore
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
from src.dashboard.security import _client_is_loopback, _require_control_auth
from src.dashboard.status import StatusSnapshot, build_status
from src.dashboard.ups import DEFAULT_UPS_KEEPALIVE_PATH, DEFAULT_UPS_STATUS_PATH
from src.dashboard.version import _canary_version
from src.dashboard.websockets import _publish_on_app_loop
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
)
from src.models.health import HealthMonitor

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
    ups_status_path: Path = DEFAULT_UPS_STATUS_PATH,
    ups_keepalive_path: Path = DEFAULT_UPS_KEEPALIVE_PATH,
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
        ups_status_path=ups_status_path,
        ups_keepalive_path=ups_keepalive_path,
        emitter=emitter,
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
    "build_status",
    "create_app",
    "echo_config",
    "_client_is_loopback",
    "_require_control_auth",
    "read_anomalies_since",
    "read_behavior_models",
    "read_bindings_for_display",
    "read_can_coverage",
    "read_latest_per_channel",
    "read_latest_telemetry_rows",
]
