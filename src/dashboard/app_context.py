"""What every dashboard route module is built from (R29.6).

``create_app`` takes about 45 daemon objects and used to spell each of them out
again at every layer below it: the ``register_public_routes(...)`` call, the
``register_control_routes(...)`` call, ``ControlContext(...)`` and one keyword
call per non-control module. A new object had to be added in each, and one that
was forgotten reached its routes as ``None`` with nothing failing.

:class:`AppContext` is the one record. ``create_app`` resolves its arguments
once, builds it once, and passes it to every ``register_*``; a route reads the
object it needs as ``ctx.<name>``. The control routers (R03.6) already worked
this way, so ``ControlContext`` is this class.

Auth is on the context too: :meth:`router` returns an ``APIRouter`` whose one
dependency is the shared ``security.control_auth_dependency`` built from the
context's token, so a module cannot register a guarded route with a token of
its own. ``tests/test_route_table_snapshot.py`` pins the result, and
``tests/test_app_context_wiring.py`` pins that each field is the object the
daemon (or ``create_app``'s caller) passed, never a default.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.tuning import AlertTuner
from src.analysis.gps_tracker import GpsTracker
from src.collection.capture_profiles import CaptureProfileStore
from src.collection.imu_collector import ImuHealth
from src.collection.obd_scheduler import ForcePollTrigger
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureController
from src.control.dtc_state import DtcState
from src.control.jobs import JobController
from src.control.key_cycle_pairing import KeyCyclePairing
from src.control.monitor_state import MonitorState
from src.control.paired_devices import PairedDevices
from src.control.profile_capture import CaptureAutostart, EventPublisher, ProfileCaptureSession
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.control.updater import UpdatePaths
from src.daemon.behavior_live import BehaviorLiveState
from src.daemon.broadcast import Broadcaster
from src.dashboard.security import control_auth_dependency
from src.models.feedback import FeedbackStore
from src.provisioning.reader import NetworkStatusReader
from src.storage.async_writer import AsyncParquetTelemetryWriter
from src.storage.sessions import resolve_session
from src.vehicle.vehicle_ledger import VehicleLedgerStore

if TYPE_CHECKING:
    # Annotation only: importing it at runtime loads the whole src.daemon package (R04.8).
    from src.daemon.health import HealthMonitor
    from src.daemon.inference_metrics import InferenceTimingRecorder

_T = TypeVar("_T")


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
class AppContext:
    """The daemon objects behind every dashboard route, resolved once at app build.

    Fields without a default are always present once ``create_app`` has resolved
    them (a path, a bus, a store, the capture lifecycle); the ``None`` ones are
    the components a bare app (tests, replay-only) does not have, and the route
    that needs one says so (``_require``) rather than guessing.
    """

    config: CanaryConfig
    anomalies_dir: Path
    behavior_models_path: Path
    accepted_baselines_path: Path
    bindings_path: Path
    dids_path: Path
    test_profiles_dir: Path
    ups_status_path: Path
    ups_keepalive_path: Path
    #: The BLE provisioning window file, shared with the root
    #: ``canary-provision`` service (``src/provisioning/gate.py``).
    provision_window_path: Path
    #: The software-update request/status files shared with the root
    #: ``canary-update`` units (``src/control/updater.py``).
    update_paths: UpdatePaths
    resolved_control_token: str | None
    publish_control_event: EventPublisher
    telemetry_bus: Broadcaster
    anomalies_bus: Broadcaster
    scores_bus: Broadcaster
    control_bus: Broadcaster
    profile_store: CaptureProfileStore
    #: The capture lifecycle and its autostart, shared with daemon shutdown (R03.5).
    session: ProfileCaptureSession
    autostart: CaptureAutostart
    #: Phones and laptops with their own control tokens (P2.4), and the key-cycle
    #: pairing over them.
    paired_devices: PairedDevices
    pairing: KeyCyclePairing
    models_dir: Path | None = None
    emitter: AnomalyEventEmitter | None = None
    capture_controller: CaptureController | None = None
    job_controller: JobController | None = None
    replay_controller: ReplayController | None = None
    audit_logger: ControlAuditLogger | None = None
    feedback_store: FeedbackStore | None = None
    alert_tuner: AlertTuner | None = None
    vehicle_ledger_store: VehicleLedgerStore | None = None
    sequence_detector_controller: SequenceDetectorController | None = None
    force_poll_trigger: ForcePollTrigger | None = None
    #: Live telemetry writer, so arming tuning mode can redirect storage into a
    #: quarantined ``_tuning-*`` session. ``None`` leaves the poll-profile switch
    #: working without isolation.
    storage_writer: AsyncParquetTelemetryWriter | None = None
    #: Source of the achieved rates, gaps and storage-drop counters the capture
    #: contract is scored on. ``None`` leaves captures working without a quality
    #: sidecar: the verdict then reads as unknown, never as a pass.
    health_monitor: HealthMonitor | None = None
    behavior_live: BehaviorLiveState | None = None
    dtc_state: DtcState | None = None
    monitor_state: MonitorState | None = None
    gps_tracker: GpsTracker | None = None
    imu_health: ImuHealth | None = None
    #: The daemon's in-memory observations, so the capture-autostart tick and the
    #: vehicle-state reads classify without Parquet I/O. ``None`` (tests,
    #: replay-only apps) leaves arming available but never firing, and says so.
    live_source: Any = None
    #: The inference scheduler's recorder (``/status`` ``detectors.inference``),
    #: read by the brain routes for the live twin block; ``None`` without one.
    inference_timing: InferenceTimingRecorder | None = None
    #: Cached ``nmcli``/``ip`` reads for ``GET /control/network`` (P2.3);
    #: ``None`` (tests, bare apps) reports the route as not wired.
    network_reader: NetworkStatusReader | None = None
    #: The root service's WiFi socket (``src/provisioning/netsock.py``) behind
    #: ``/control/network/wifi``; ``None`` is its default, which tests override.
    network_socket_path: Path | None = None
    #: The one control-auth dependency (``security.control_auth_dependency``),
    #: built from ``resolved_control_token`` so the two cannot disagree.
    require_auth: Callable[..., None] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "require_auth", control_auth_dependency(self.resolved_control_token)
        )

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


__all__ = ["AppContext", "_require"]
