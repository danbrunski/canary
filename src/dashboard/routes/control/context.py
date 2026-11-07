"""What every ``/control/*`` router is built from (R03.6).

``register_control_routes`` used to be one 1,640-line function holding 78
route closures over its arguments. Each domain module now exposes
``build_router(ctx) -> APIRouter``; this is the ``ctx``: the daemon objects
the routes act on, the one auth dependency (applied at router level, so no
route can be registered without it), and the session-directory dependency
that owns the ``resolve_session`` → 404 mapping.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.tuning import AlertTuner
from src.collection.capture_profiles import CaptureProfileStore
from src.collection.obd_scheduler import ForcePollTrigger
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.jobs import JobController
from src.control.profile_capture import CaptureAutostart, EventPublisher, ProfileCaptureSession
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.control.updater import DEFAULT_UPDATE_PATHS, UpdatePaths
from src.daemon.broadcast import Broadcaster
from src.models.feedback import FeedbackStore
from src.provisioning.gate import DEFAULT_WINDOW_PATH
from src.provisioning.reader import NetworkStatusReader
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.sessions import resolve_session
from src.vehicle.vehicle_ledger import VehicleLedgerStore

if TYPE_CHECKING:
    # Annotation only: importing it at runtime loads the whole src.daemon package (R04.8).
    from src.daemon.health import HealthMonitor

_T = TypeVar("_T")

#: The routes' logger keeps the name it had as one module, so log filters and
#: journal greps written against it still match.
log = logging.getLogger("src.dashboard.routes.control")


def _require(component: _T | None, name: str, *, detail: str | None = None) -> _T:
    """Null-guard a controller/store dependency: 503 when it wasn't wired up.

    Collapses the identical ``if x is None: raise HTTPException(503, ...)``
    closures repeated per-dependency (``_controller``, ``_jobs``, etc.) —
    ``detail`` overrides the default message for the one call site whose
    existing wording doesn't follow the "{name} API is not available" shape.
    """
    if component is None:
        raise HTTPException(status_code=503, detail=detail or f"{name} API is not available")
    return component


@dataclass(frozen=True)
class ControlContext:
    """The daemon objects behind ``/control/*``, read once at app build."""

    config: CanaryConfig
    anomalies_dir: Path
    resolved_control_token: str | None
    #: The one control-auth dependency (``security.control_auth_dependency``).
    require_auth: Callable[..., None]
    publish_control_event: EventPublisher
    control_bus: Broadcaster
    profile_store: CaptureProfileStore
    session: ProfileCaptureSession
    autostart: CaptureAutostart
    ups_status_path: Path
    ups_keepalive_path: Path
    capture_controller: CaptureController | None = None
    job_controller: JobController | None = None
    replay_controller: ReplayController | None = None
    audit_logger: ControlAuditLogger | None = None
    feedback_store: FeedbackStore | None = None
    alert_tuner: AlertTuner | None = None
    vehicle_ledger_store: VehicleLedgerStore | None = None
    sequence_detector_controller: SequenceDetectorController | None = None
    force_poll_trigger: ForcePollTrigger | None = None
    storage_writer: AsyncParquetTelemetryWriter | None = None
    health_monitor: HealthMonitor | None = None
    emitter: AnomalyEventEmitter | None = None
    live_source: Any = None
    #: The inference scheduler's recorder (``/status`` ``detectors.inference``),
    #: read by the brain routes for the live twin block; ``None`` without one.
    inference_timing: Any = None
    #: The BLE provisioning window file, shared with the root
    #: ``canary-provision`` service (``src/provisioning/gate.py``).
    provision_window_path: Path = DEFAULT_WINDOW_PATH
    #: Cached ``nmcli``/``ip`` reads for ``GET /control/network`` (P2.3);
    #: ``None`` (tests, bare apps) reports the route as not wired.
    network_reader: NetworkStatusReader | None = None
    #: The root service's WiFi socket (``src/provisioning/netsock.py``) behind
    #: ``/control/network/wifi``; ``None`` is its default, which tests override.
    network_socket_path: Path | None = None
    #: The software-update request/status files shared with the root
    #: ``canary-update`` units (``src/control/updater.py``).
    update_paths: UpdatePaths = DEFAULT_UPDATE_PATHS

    def router(self) -> APIRouter:
        """A router whose every route requires the control token."""
        return APIRouter(dependencies=[Depends(self.require_auth)])

    def resolve_session(self, session_id: str) -> Path:
        """``session_id`` under the storage root, or a 404 naming why."""
        try:
            return resolve_session(self.config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def require_capture_controller(self) -> CaptureController:
        return _require(self.capture_controller, "control")

    def require_job_controller(self) -> JobController:
        return _require(self.job_controller, "job")


def _session_dir(request: Request, session_id: str) -> Path:
    context: ControlContext = request.app.state.control_context
    return context.resolve_session(session_id)


#: A route parameter that resolves ``{session_id}`` to its directory, or 404s.
SessionDir = Annotated[Path, Depends(_session_dir)]


__all__ = ["ControlContext", "SessionDir", "_require", "log"]
