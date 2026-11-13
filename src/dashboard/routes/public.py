"""The public (no-token) read routes, registered as one set (R29.7).

``register_public_routes`` was one 383-line function holding seventeen closures.
The routes now live by concern, each module with its own registrar:

- :mod:`~src.dashboard.routes.status_routes`: identity, ``/status``, the config
  echo, recent anomalies and the telemetry seed;
- :mod:`~src.dashboard.routes.catalog_reads`: behavior surfaces, bindings, the
  DID catalog, test profiles, OBD and CAN coverage, cached DTCs and monitors;
- :mod:`~src.dashboard.routes.analysis_reads`: binned analysis, LLM-assist cards
  and trip summaries.

This stays the entry point ``create_app`` and the bare-app tests call. The
route table is the one it was (``tests/fixtures/route_table_snapshot.json``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI

from src.alerts.emitter import AnomalyEventEmitter
from src.analysis.gps_tracker import GpsTracker
from src.behavior.accepted_baselines import DEFAULT_ACCEPTED_BASELINES_PATH
from src.collection.imu_collector import ImuHealth
from src.config import CanaryConfig
from src.control.dtc_state import DtcState
from src.control.monitor_state import MonitorState
from src.control.replay_control import ReplayController
from src.control.sequence_detector import SequenceDetectorController
from src.daemon.behavior_live import BehaviorLiveState
from src.daemon.ups import DEFAULT_UPS_STATUS_PATH
from src.dashboard.routes.analysis_reads import register_analysis_read_routes
from src.dashboard.routes.catalog_reads import register_catalog_read_routes
from src.dashboard.routes.status_routes import register_status_routes
from src.models.feedback import FeedbackStore
from src.provisioning.reader import NetworkStatusReader

if TYPE_CHECKING:
    from src.daemon.health import HealthMonitor
    from src.daemon.inference_metrics import InferenceTimingRecorder


def register_public_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    anomalies_dir: Path,
    models_dir: Path | None,
    emitter: AnomalyEventEmitter | None,
    feedback_store: FeedbackStore | None,
    behavior_models_path: Path,
    bindings_path: Path,
    dids_path: Path,
    test_profiles_dir: Path,
    replay_controller: ReplayController | None,
    health_monitor: HealthMonitor | None,
    behavior_live: BehaviorLiveState | None,
    dtc_state: DtcState | None = None,
    monitor_state: MonitorState | None = None,
    gps_tracker: GpsTracker | None = None,
    imu_health: ImuHealth | None = None,
    sequence_detector_controller: SequenceDetectorController | None = None,
    accepted_baselines_path: Path = DEFAULT_ACCEPTED_BASELINES_PATH,
    storage_writer: Any = None,
    inference_timing: InferenceTimingRecorder | None = None,
    live_source: Any = None,
    ups_status_path: Path = DEFAULT_UPS_STATUS_PATH,
    network_reader: NetworkStatusReader | None = None,
) -> None:
    register_status_routes(
        app,
        config=config,
        anomalies_dir=anomalies_dir,
        models_dir=models_dir,
        emitter=emitter,
        feedback_store=feedback_store,
        dids_path=dids_path,
        replay_controller=replay_controller,
        health_monitor=health_monitor,
        gps_tracker=gps_tracker,
        imu_health=imu_health,
        sequence_detector_controller=sequence_detector_controller,
        accepted_baselines_path=accepted_baselines_path,
        storage_writer=storage_writer,
        inference_timing=inference_timing,
        live_source=live_source,
        ups_status_path=ups_status_path,
        network_reader=network_reader,
    )
    register_catalog_read_routes(
        app,
        config=config,
        behavior_models_path=behavior_models_path,
        bindings_path=bindings_path,
        dids_path=dids_path,
        test_profiles_dir=test_profiles_dir,
        behavior_live=behavior_live,
        dtc_state=dtc_state,
        monitor_state=monitor_state,
    )
    register_analysis_read_routes(app, config=config)
