"""The ``/control/*`` routes, one ``APIRouter`` per domain (R03.6).

``register_control_routes`` keeps the signature ``create_app`` has always
called, builds one :class:`~.context.ControlContext`, and includes each
domain's router. Auth is router-level: every router is built by
``ControlContext.router()``, whose one dependency is the shared
``security.control_auth_dependency``, so a route cannot be registered here
without it (``tests/test_route_table_snapshot.py`` pins the result).

Include order is registration order, which Starlette matches in; the snapshot
test also asserts no route is shadowed by an earlier one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, FastAPI

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.tuning import AlertTuner
from src.collection.capture_profiles import CaptureProfileStore
from src.collection.obd_scheduler import ForcePollTrigger
from src.config import CanaryConfig
from src.control import profile_capture as profile_capture_module
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.jobs import JobController
from src.control.profile_capture import CaptureAutostart, ProfileCaptureSession
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.control.updater import DEFAULT_UPDATE_PATHS, UpdatePaths
from src.daemon.broadcast import Broadcaster
from src.daemon.ups import DEFAULT_UPS_KEEPALIVE_PATH, DEFAULT_UPS_STATUS_PATH
from src.dashboard.routes.session_reads import build_session_read_router
from src.dashboard.security import control_auth_dependency
from src.dashboard.status_errors import register_status_error_handler
from src.models.feedback import FeedbackStore
from src.provisioning.gate import DEFAULT_WINDOW_PATH
from src.provisioning.reader import NetworkStatusReader
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.vehicle.vehicle_ledger import VehicleLedgerStore

from . import (
    brain,
    bus,
    captures,
    classification,
    custom_pids,
    diagnostics,
    feedback,
    jobs,
    models,
    network,
    profile_capture,
    replay,
    sessions,
    system,
    tires,
    tuning,
    update,
)
from .context import ControlContext, SessionDir, _require

if TYPE_CHECKING:
    # Annotation only: importing it at runtime loads the whole src.daemon package (R04.8).
    from src.daemon.health import HealthMonitor

#: How often the capture-autostart tick classifies the vehicle; the one
#: definition is ``src.control.profile_capture`` (R03.5). Kept here because an
#: app built without an injected autostart reads it per tick, which is how a
#: test shortens it.
AUTOSTART_TICK_SECONDS = profile_capture_module.AUTOSTART_TICK_SECONDS


def _session_reads(ctx: ControlContext) -> APIRouter:
    """The chart/GPS reads shared with the companion (R03.7)."""
    return build_session_read_router(lambda: ctx.config.collection.storage_path, ctx.require_auth)


#: Domain router builders in registration order.
_ROUTER_BUILDERS: tuple[Callable[[ControlContext], APIRouter], ...] = (
    system.build_router,
    network.build_router,
    captures.build_router,
    replay.build_router,
    sessions.build_router,
    sessions.build_store_router,
    _session_reads,
    bus.build_router,
    diagnostics.build_router,
    profile_capture.build_router,
    classification.build_router,
    jobs.build_router,
    models.build_router,
    feedback.build_router,
    tuning.build_router,
    custom_pids.build_router,
    tires.build_router,
    brain.build_router,
    update.build_router,
)


def register_control_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    anomalies_dir: Path,
    resolved_control_token: str | None,
    capture_controller: CaptureController | None,
    job_controller: JobController | None,
    replay_controller: ReplayController | None,
    control_bus: Broadcaster,
    audit_logger: ControlAuditLogger | None,
    feedback_store: FeedbackStore | None,
    alert_tuner: AlertTuner | None,
    vehicle_ledger_store: VehicleLedgerStore | None = None,
    sequence_detector_controller: SequenceDetectorController | None = None,
    force_poll_trigger: ForcePollTrigger | None = None,
    #: Live telemetry writer, so arming tuning mode can redirect storage into
    #: a quarantined ``_tuning-*`` session (engine-tuning-support.md 1.2).
    #: ``None`` leaves the poll-profile switch working without isolation.
    storage_writer: AsyncParquetTelemetryWriter | None = None,
    #: Always the store ``create_app`` resolved; the old fallback built here
    #: was dead (R03.5).
    capture_profile_store: CaptureProfileStore,
    #: Source of the achieved rates, gaps and storage-drop counters the capture
    #: contract is scored on. ``None`` leaves captures working without a
    #: quality sidecar — the verdict then reads as unknown, never as a pass.
    health_monitor: HealthMonitor | None = None,
    ups_status_path: Path = DEFAULT_UPS_STATUS_PATH,
    ups_keepalive_path: Path = DEFAULT_UPS_KEEPALIVE_PATH,
    provision_window_path: Path = DEFAULT_WINDOW_PATH,
    network_reader: NetworkStatusReader | None = None,
    network_socket_path: Path | None = None,
    update_paths: UpdatePaths = DEFAULT_UPDATE_PATHS,
    emitter: AnomalyEventEmitter | None = None,
    #: The daemon's in-memory observations, so the capture-autostart tick can
    #: classify the vehicle without Parquet I/O. ``None`` (tests, replay-only
    #: apps) leaves arming available but never firing, and says so.
    live_source: Any = None,
    #: The daemon's own capture lifecycle (R03.5), shared with runtime
    #: shutdown. ``None`` (tests, bare apps) builds one over the components
    #: above.
    profile_capture: ProfileCaptureSession | None = None,
    capture_autostart: CaptureAutostart | None = None,
    publish_control_event: Callable[[str, str, dict[str, Any]], None],
    #: ``/status``'s inference recorder, for the brain's live twin block.
    inference_timing: Any = None,
) -> None:
    # A refusal any of these routes lets escape answers with its own status and body.
    register_status_error_handler(app)
    session = profile_capture or ProfileCaptureSession(
        storage_writer=storage_writer,
        capture_controller=capture_controller,
        profile_store=capture_profile_store,
        health_monitor=health_monitor,
    )
    autostart = capture_autostart or CaptureAutostart(
        session,
        live_source,
        replay_controller,
        # Read per tick, so a test can shorten the module constant.
        tick_seconds=lambda: AUTOSTART_TICK_SECONDS,
    )
    ctx = ControlContext(
        config=config,
        anomalies_dir=anomalies_dir,
        resolved_control_token=resolved_control_token,
        require_auth=control_auth_dependency(resolved_control_token),
        publish_control_event=publish_control_event,
        control_bus=control_bus,
        profile_store=capture_profile_store,
        session=session,
        autostart=autostart,
        ups_status_path=ups_status_path,
        ups_keepalive_path=ups_keepalive_path,
        capture_controller=capture_controller,
        job_controller=job_controller,
        replay_controller=replay_controller,
        audit_logger=audit_logger,
        feedback_store=feedback_store,
        alert_tuner=alert_tuner,
        vehicle_ledger_store=vehicle_ledger_store,
        sequence_detector_controller=sequence_detector_controller,
        force_poll_trigger=force_poll_trigger,
        storage_writer=storage_writer,
        health_monitor=health_monitor,
        emitter=emitter,
        live_source=live_source,
        inference_timing=inference_timing,
        provision_window_path=provision_window_path,
        network_reader=network_reader,
        network_socket_path=network_socket_path,
        update_paths=update_paths,
    )
    # `SessionDir` resolves against the app's context.
    app.state.control_context = ctx
    for build in _ROUTER_BUILDERS:
        app.include_router(build(ctx))


__all__ = [
    "AUTOSTART_TICK_SECONDS",
    "ControlContext",
    "SessionDir",
    "_require",
    "register_control_routes",
]
