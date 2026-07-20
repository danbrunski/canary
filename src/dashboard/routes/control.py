from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.feedback import FEEDBACK_LABELS, FeedbackStore
from src.alerts.tuning import (
    AlertTuner,
    TuningConfig,
    estimate_before_after,
    load_tuning_version,
    save_tuning_config,
)
from src.analysis.vehicle_ledger import VEHICLE_EVENT_KINDS, VehicleLedgerStore
from src.collection.custom_pids import (
    CustomPid,
    CustomPidSet,
    load_custom_pids,
    save_custom_pids,
    torque_csv_to_custom_pids,
)
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureConflictError, CaptureController, NoActiveCaptureError
from src.control.classification import (
    classify_one_session,
    corpus_mode_summary,
    get_session_classification,
    list_events_by_kind,
)
from src.control.jobs import JobController
from src.control.models import publish_model_artifact
from src.control.preflight import build_preflight
from src.control.replay_control import (
    ReplayConflictError,
    ReplayController,
    ReplayNotFoundError,
    ReplayNotRunningError,
)
from src.control.sequence_detector import (
    SequenceDetectorController,
    SequenceDetectorUnavailableError,
)
from src.control.sessions import (
    delete_session,
    list_sessions,
    pin_session,
    resolve_session,
    summarize_session,
    unpin_session,
    write_session_metadata,
    write_session_zip,
)
from src.control.trends import corpus_trend_summary
from src.dashboard.broadcast import Broadcaster
from src.dashboard.readers import read_anomalies_since
from src.dashboard.schemas import (
    CaptureStartRequest,
    CaptureStopRequest,
    CaptureTagRequest,
    CustomPidCreateRequest,
    CustomPidCsvImportRequest,
    FeedbackRequest,
    JobSubmitRequest,
    ModelUploadRequest,
    ProfileNameUpdateRequest,
    ReplayStartRequest,
    SequenceDetectorRequest,
    SessionMetadataRequest,
    SessionPinRequest,
    TuningRevertRequest,
    TuningUpdateRequest,
    UpsKeepaliveRequest,
    VehicleEventRequest,
)
from src.dashboard.security import _client_is_loopback, _require_control_auth
from src.dashboard.status import _derive_vehicle_state, _resolve_latest_session
from src.dashboard.ups import (
    DEFAULT_UPS_KEEPALIVE_PATH,
    DEFAULT_UPS_STATUS_PATH,
    build_power_snapshot,
    read_keepalive_remaining,
    read_watchdog_status,
    write_keepalive,
)
from src.dashboard.version import _canary_version, _git_sha
from src.models.training_ledger import TrainingLedger
from src.preprocessing.loader import discover_sessions
from src.profiles import load_registry, update_profile_name


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
    ups_status_path: Path = DEFAULT_UPS_STATUS_PATH,
    ups_keepalive_path: Path = DEFAULT_UPS_KEEPALIVE_PATH,
    emitter: AnomalyEventEmitter | None = None,
    publish_control_event: Callable[[str, str, dict[str, Any]], None],
) -> None:

    def _unlink_path(path: Path) -> None:
        path.unlink(missing_ok=True)

    def require_control_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        _require_control_auth(
            resolved_control_token,
            authorization,
            client_is_loopback=_client_is_loopback(request),
        )

    def _controller() -> CaptureController:
        if capture_controller is None:
            raise HTTPException(status_code=503, detail="control API is not available")
        return capture_controller

    def _jobs() -> JobController:
        if job_controller is None:
            raise HTTPException(status_code=503, detail="job API is not available")
        return job_controller

    def _sequence_detector() -> SequenceDetectorController:
        if sequence_detector_controller is None:
            raise HTTPException(
                status_code=503, detail="sequence-detector control is not available"
            )
        return sequence_detector_controller

    @app.get("/control/sequence-detector")
    def get_sequence_detector(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        return {"sequence_detector": _sequence_detector().status().to_dict()}

    @app.post("/control/sequence-detector")
    def post_sequence_detector(
        request: SequenceDetectorRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _sequence_detector()
        try:
            status = controller.switch(request.backbone)
        except SequenceDetectorUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish_control_event("sequence_detector.switch", "ok", status.to_dict())
        return {"sequence_detector": status.to_dict()}

    @app.post("/control/captures", status_code=201)
    def post_capture(
        request: CaptureStartRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            capture = controller.start_capture(
                purpose=request.purpose,
                protocol=request.protocol,
                vehicle_state=request.vehicle_state,
                fields=request.fields,
            )
        except CaptureConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        publish_control_event("capture.start", "ok", capture.to_dict())
        return {"capture": capture.to_dict()}

    @app.get("/control/captures/current")
    def get_current_capture(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        capture = controller.current_capture()
        return {"capture": None if capture is None else capture.to_dict()}

    @app.post("/control/captures/current/tags")
    def post_capture_tag(
        request: CaptureTagRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            tag = controller.add_tag(
                request.label,
                kind=request.kind,
                timestamp=request.timestamp,
                fields=request.fields,
            )
        except NoActiveCaptureError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        capture = controller.current_capture()
        payload = {
            "tag": tag,
            "capture": None if capture is None else capture.to_dict(),
        }
        publish_control_event("capture.tag", "ok", payload)
        return payload

    @app.post("/control/captures/current/stop")
    def post_capture_stop(
        request: CaptureStopRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            capture = controller.stop_capture(reason=request.reason, fields=request.fields)
        except NoActiveCaptureError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        publish_control_event("capture.stop", "ok", capture.to_dict())
        return {"capture": capture.to_dict()}

    def _replay() -> ReplayController:
        if replay_controller is None:
            raise HTTPException(status_code=503, detail="replay API is not available")
        return replay_controller

    @app.post("/control/replay", status_code=201)
    async def post_replay_start(
        request: ReplayStartRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _replay()
        # 7.2 (operator-approved, see OVERNIGHT-QUEUE.md "Replay vs live"):
        # replay is a bench/desk tool, not something to run while actually
        # driving — refuse outright rather than gating live collectors out
        # of the queue while a replay is active.
        vehicle_state = _derive_vehicle_state(
            config.collection.storage_path, replay_controller=controller
        )
        if vehicle_state.state == "engine_running":
            raise HTTPException(
                status_code=409,
                detail=(
                    "refusing to start replay: vehicle_state=engine_running "
                    "(replay is a bench/desk tool; wait for ignition_only/engine_off "
                    "or key out first)"
                ),
            )
        try:
            status = await controller.start(
                request.session_id,
                speed=request.speed,
                loop=request.loop,
            )
        except ReplayConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReplayNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        publish_control_event("replay.start", "ok", status.to_dict())
        return {"replay": status.to_dict()}

    @app.post("/control/replay/stop")
    async def post_replay_stop(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _replay()
        try:
            status = await controller.stop()
        except ReplayNotRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        publish_control_event("replay.stop", "ok", status.to_dict())
        return {"replay": status.to_dict()}

    @app.get("/control/replay")
    def get_replay(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        controller = _replay()
        return {"replay": controller.status().to_dict()}

    @app.get("/control/sessions")
    def get_control_sessions(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        sessions = [session.to_dict() for session in list_sessions(config.collection.storage_path)]
        return {"sessions": sessions, "count": len(sessions)}

    @app.get("/control/sessions/{session_id}")
    def get_control_session(
        session_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"session": summarize_session(session).to_dict()}

    @app.get("/control/profiles")
    def get_control_profiles(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        # The active profile's slug is the scoped storage path's final component
        # (see src.profiles.scope_config).
        active_slug = config.collection.storage_path.name
        profiles = load_registry(config.profiles.registry_path)
        return {
            "profiles": [profile.to_dict() for profile in profiles],
            "active_slug": active_slug,
            "count": len(profiles),
        }

    @app.post("/control/profiles/{slug}/name")
    def post_control_profile_name(
        slug: str,
        request: ProfileNameUpdateRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            profile = update_profile_name(config.profiles.registry_path, slug, request.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no profile with slug {slug!r}") from exc
        payload = {"profile": profile.to_dict()}
        publish_control_event("profile.rename", "ok", payload)
        return payload

    @app.get("/control/power")
    def get_control_power(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        status = read_watchdog_status(ups_status_path)
        keepalive_remaining_s = read_keepalive_remaining(ups_keepalive_path)
        return {"power": build_power_snapshot(status, keepalive_remaining_s)}

    @app.post("/control/power/keepalive")
    def post_control_power_keepalive(
        request: UpsKeepaliveRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            expiry = write_keepalive(request.minutes, ups_keepalive_path)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"UPS keep-alive file not writable ({ups_keepalive_path}): {exc}",
            ) from exc
        status = read_watchdog_status(ups_status_path)
        keepalive_remaining_s = read_keepalive_remaining(ups_keepalive_path)
        payload = {
            "power": build_power_snapshot(status, keepalive_remaining_s),
            "expiry": expiry,
        }
        publish_control_event("power.keepalive", "ok", payload)
        return payload

    @app.post("/control/sessions/{session_id}/metadata")
    def post_control_session_metadata(
        session_id: str,
        request: SessionMetadataRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        metadata = write_session_metadata(session, request.metadata, merge=request.merge)
        payload = {"session_id": session_id, "metadata": metadata}
        publish_control_event("session.metadata", "ok", payload)
        return payload

    @app.post("/control/sessions/{session_id}/pin")
    def post_control_session_pin(
        session_id: str,
        request: SessionPinRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        metadata = pin_session(session, reason=request.reason, catalog_db=config.daemon.catalog_db)
        if request.metadata:
            metadata = write_session_metadata(session, request.metadata)
        payload = {"session": summarize_session(session).to_dict(), "metadata": metadata}
        publish_control_event("session.pin", "ok", payload)
        return payload

    @app.post("/control/sessions/{session_id}/unpin")
    def post_control_session_unpin(
        session_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        metadata = unpin_session(session, catalog_db=config.daemon.catalog_db)
        payload = {"session": summarize_session(session).to_dict(), "metadata": metadata}
        publish_control_event("session.unpin", "ok", payload)
        return payload

    @app.delete("/control/sessions/{session_id}")
    def delete_control_session(
        session_id: str,
        force: bool = Query(default=False),
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        active = capture_controller.current_capture() if capture_controller is not None else None
        if active is not None and active.session_id == session_id:
            raise HTTPException(status_code=409, detail="cannot delete the active capture session")
        try:
            delete_session(session, force=force)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = {"session_id": session_id, "deleted": True, "force": force}
        publish_control_event("session.delete", "ok", payload)
        return payload

    @app.get("/control/sessions/{session_id}/export")
    def export_control_session(
        session_id: str,
        background_tasks: BackgroundTasks,
        _auth: None = Depends(require_control_auth),
    ) -> FileResponse:
        try:
            session = resolve_session(config.collection.storage_path, session_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"canary-{session_id}-",
            suffix=".zip",
            delete=False,
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        write_session_zip(session, tmp_path)
        background_tasks.add_task(_unlink_path, tmp_path)
        return FileResponse(
            tmp_path,
            media_type="application/zip",
            filename=f"{session_id}.zip",
        )

    @app.get("/control/sessions/{session_id}/classification")
    def get_session_classification_endpoint(
        session_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        payload = get_session_classification(config.collection.storage_path, session_id)
        if payload is None:
            raise HTTPException(
                status_code=404,
                detail=f"no classification sidecar for session {session_id!r}",
            )
        return {"classification": payload}

    @app.post("/control/sessions/{session_id}/classification", status_code=201)
    def post_session_classification(
        session_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            payload = classify_one_session(
                config, session_id, emitter=emitter, catalog_db=config.daemon.catalog_db
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        publish_control_event(
            "classification.write",
            "ok",
            {"session_id": session_id, "disposition": payload.get("disposition")},
        )
        return {"classification": payload}

    @app.get("/control/sessions/{session_id}/digest")
    def get_session_digest_route(
        session_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        # Task 5.6 follow-up: authenticated, not public — the digest
        # aggregates the vehicle ledger and per-session fingerprint z-scores,
        # both of which are otherwise only readable via authenticated
        # /control/vehicle-ledger and /control/trends. Gating the whole
        # endpoint (not just those fields) matches this codebase's existing
        # public-XOR-authenticated posture per route. Computed on first
        # request if the trip-end hook (Task 5.4) hasn't already written
        # digest.json for this session.
        from src.control.digest import get_session_digest

        result = get_session_digest(config, session_id, catalog_db=config.daemon.catalog_db)
        if result is None:
            raise HTTPException(status_code=404, detail=f"no digest for session {session_id!r}")
        return result

    @app.get("/control/classifications/summary")
    def get_corpus_classification_summary(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        return corpus_mode_summary(config.collection.storage_path)

    @app.get("/control/trends")
    def get_control_trends(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Per-stat drift trend over the fingerprint history (Task 4.5)."""
        return corpus_trend_summary(config)

    @app.get("/control/classifications/events")
    def get_corpus_events(
        kind: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        return list_events_by_kind(
            config.collection.storage_path,
            kind=kind,
            limit=limit,
        )

    @app.post("/control/jobs", status_code=201)
    def post_control_job(
        request: JobSubmitRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            job = _jobs().submit(request.kind, request.params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish_control_event("job.submit", "ok", job.to_dict())
        return {"job": job.to_dict()}

    @app.get("/control/jobs")
    def get_control_jobs(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        jobs = [job.to_dict() for job in _jobs().list_jobs()]
        return {"jobs": jobs, "count": len(jobs)}

    @app.get("/control/jobs/{job_id}/logs")
    def get_control_job_logs(
        job_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        logs = _jobs().logs(job_id)
        if logs is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return {"job_id": job_id, "log": logs}

    @app.get("/control/jobs/{job_id}")
    def get_control_job(
        job_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        job = _jobs().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return {"job": job.to_dict()}

    @app.post("/control/jobs/{job_id}/cancel")
    def post_control_job_cancel(
        job_id: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        job = _jobs().cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        publish_control_event("job.cancel", "ok", job.to_dict())
        return {"job": job.to_dict()}

    @app.get("/control/preflight")
    def get_control_preflight(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        current_session = (
            capture_controller.active.session_path
            if capture_controller is not None and capture_controller.active is not None
            else _resolve_latest_session(config.collection.storage_path)
        )
        return build_preflight(
            config,
            control_token_configured=bool(resolved_control_token),
            current_session=current_session,
        )

    @app.get("/control/version")
    def get_control_version(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        return {
            "api_version": 1,
            "package_version": _canary_version(),
            "git_sha": _git_sha(),
            "config_token_env": config.daemon.control_token_env,
        }

    @app.post("/control/models/{model_type}/artifact")
    def post_control_model_artifact(
        model_type: str,
        request: ModelUploadRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        try:
            published = publish_model_artifact(
                config,
                model_type=model_type,
                artifact_b64=request.artifact_b64,
                manifest=request.manifest,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish_control_event("model.upload", "ok", published)
        return {"model": published}

    def _feedback() -> FeedbackStore:
        if feedback_store is None:
            raise HTTPException(status_code=503, detail="feedback API is not available")
        return feedback_store

    @app.get("/control/feedback")
    def get_feedback(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        store = _feedback()
        records = [fb.to_dict() for fb in store.labels().values()]
        return {"feedback": records, "count": len(records), "summary": store.summary()}

    @app.get("/control/training-runs")
    def get_training_runs(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Training-run history for the 6.22 dashboard (Task 6.21 ledger).

        Latest record per run (the append-only chain is collapsed); read
        per request so a fresh ``retrain.py`` run shows up without a daemon
        restart. Empty list when nothing's been recorded yet."""
        ledger = TrainingLedger(config.daemon.training_ledger)
        runs = [r.to_dict() for r in ledger.records()]
        return {"runs": runs, "count": len(runs)}

    @app.post("/control/feedback", status_code=201)
    def post_feedback(
        request: FeedbackRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        store = _feedback()
        if request.label not in FEEDBACK_LABELS:
            raise HTTPException(
                status_code=422,
                detail=f"label must be one of {sorted(FEEDBACK_LABELS)}",
            )
        fb = store.record(
            event=request.event,
            label=request.label,  # type: ignore[arg-type]  # checked above
            reason=request.reason,
            driving_context=request.driving_context,
            labeled_by=request.labeled_by,
        )
        publish_control_event("anomaly.feedback", "ok", fb.to_dict())
        return {"feedback": fb.to_dict()}

    def _vehicle_ledger() -> VehicleLedgerStore:
        if vehicle_ledger_store is None:
            raise HTTPException(status_code=503, detail="vehicle ledger API is not available")
        return vehicle_ledger_store

    @app.get("/control/vehicle-ledger")
    def get_vehicle_ledger(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Vehicle maintenance/intervention ledger (Task 3.3), oldest-first."""
        store = _vehicle_ledger()
        records = [e.to_dict() for e in store.records()]
        return {"events": records, "count": len(records)}

    @app.post("/control/vehicle-ledger", status_code=201)
    def post_vehicle_ledger(
        request: VehicleEventRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        store = _vehicle_ledger()
        if request.kind not in VEHICLE_EVENT_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"kind must be one of {sorted(VEHICLE_EVENT_KINDS)}",
            )
        event = store.record(
            occurred_at=request.occurred_at,
            kind=request.kind,  # type: ignore[arg-type]  # checked above
            title=request.title,
            details=request.details,
            odometer_km=request.odometer_km,
            affected_channels=request.affected_channels,
            corpus_boundary=request.corpus_boundary,
        )
        publish_control_event("vehicle_ledger.record", "ok", event.to_dict())
        return {"event": event.to_dict()}

    def _tuner() -> AlertTuner:
        if alert_tuner is None:
            raise HTTPException(status_code=503, detail="tuning API is not available")
        return alert_tuner

    def _ensure_baseline_audited(tuner: AlertTuner) -> None:
        """Snapshot the pre-change config so the first change is revertible.

        The in-memory default (version 1) is never written until the operator
        makes a change; without this, you couldn't undo your *first* tuning
        edit. Idempotent — only writes if that version isn't already in the
        audit log.
        """
        current = tuner.config
        if load_tuning_version(current.version, config.daemon.tuning_audit) is None:
            save_tuning_config(
                current,
                config.daemon.tuning_config,
                audit_path=config.daemon.tuning_audit,
                change="baseline snapshot",
                by="system",
            )

    @app.get("/control/datasets/health")
    def get_dataset_health(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Corpus readiness + per-session coverage (Task 6.23)."""
        from src.datasets.health import analyze_corpus

        storage = config.collection.storage_path
        sessions = discover_sessions(storage) if storage.exists() else []
        boundary_event = (
            vehicle_ledger_store.latest_boundary() if vehicle_ledger_store is not None else None
        )
        return analyze_corpus(
            sessions,
            config,
            corpus_boundary=boundary_event.occurred_at if boundary_event is not None else None,
        ).to_dict()

    @app.get("/control/tuning")
    def get_tuning(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        return {"tuning": _tuner().config.to_dict()}

    @app.post("/control/tuning")
    def post_tuning(
        request: TuningUpdateRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        tuner = _tuner()
        try:
            proposed = TuningConfig.model_validate(request.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid tuning config: {exc}") from exc
        _ensure_baseline_audited(tuner)
        # Monotonic version bump regardless of what the client sent — the
        # version is the audit handle, not client-controlled.
        proposed = proposed.model_copy(update={"version": tuner.config.version + 1})
        save_tuning_config(
            proposed,
            config.daemon.tuning_config,
            audit_path=config.daemon.tuning_audit,
            change=request.change,
            by=request.by,
        )
        # Mutate the live tuner in place so the change takes effect on the
        # very next inference tick — no daemon restart.
        tuner.config = proposed
        publish_control_event(
            "tuning.update", "ok", {"version": proposed.version, "change": request.change}
        )
        return {"tuning": proposed.to_dict()}

    @app.post("/control/tuning/estimate")
    def post_tuning_estimate(
        request: TuningUpdateRequest,
        since: str | None = Query(default=None),
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Before/after FP estimate of a proposed config over recent
        anomalies — run *before* committing (Risk R11)."""
        tuner = _tuner()
        try:
            proposed = TuningConfig.model_validate(request.config)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid tuning config: {exc}") from exc
        try:
            events = read_anomalies_since(anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        return estimate_before_after(events, tuner.config, proposed)

    @app.post("/control/tuning/revert")
    def post_tuning_revert(
        request: TuningRevertRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        tuner = _tuner()
        snapshot = load_tuning_version(request.version, config.daemon.tuning_audit)
        if snapshot is None:
            raise HTTPException(
                status_code=404, detail=f"no tuning version {request.version} in audit log"
            )
        # Reverting is itself a new, audited version (never rewinds the log).
        reverted = snapshot.model_copy(update={"version": tuner.config.version + 1})
        save_tuning_config(
            reverted,
            config.daemon.tuning_config,
            audit_path=config.daemon.tuning_audit,
            change=f"revert to v{request.version}",
            by=request.by,
        )
        tuner.config = reverted
        publish_control_event(
            "tuning.revert", "ok", {"to_version": request.version, "new_version": reverted.version}
        )
        return {"tuning": reverted.to_dict()}

    # -----------------------------------------------------------------
    # Custom PIDs (Task 2.8c) — CRUD + Torque CSV import over
    # `data/custom_pids.yaml`. `src.main` loads this file once at daemon
    # startup (Task 2.6), so an edit here takes effect on the *next*
    # daemon restart, not live — matching 2.6's own load-once contract
    # rather than inventing a second hot-reload path for one file.
    # -----------------------------------------------------------------

    @app.get("/control/custom-pids")
    def get_custom_pids(
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        pid_set = load_custom_pids(config.daemon.custom_pids)
        return {
            "custom_pids": [p.model_dump(mode="json") for p in pid_set.custom_pids],
            "count": len(pid_set.custom_pids),
        }

    @app.post("/control/custom-pids", status_code=201)
    def post_custom_pids(
        request: CustomPidCreateRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        pid_set = load_custom_pids(config.daemon.custom_pids)
        try:
            new_pid = CustomPid(**request.model_dump())
            updated = CustomPidSet(custom_pids=[*pid_set.custom_pids, new_pid])
        except ValueError as exc:
            # Covers both CustomPid's own validators (read-only mode, safe
            # formula) and CustomPidSet's duplicate-name check.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        save_custom_pids(updated, config.daemon.custom_pids)
        publish_control_event("custom_pid.add", "ok", {"name": new_pid.name})
        return {"custom_pid": new_pid.model_dump(mode="json")}

    @app.delete("/control/custom-pids/{name}")
    def delete_custom_pid(
        name: str,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        pid_set = load_custom_pids(config.daemon.custom_pids)
        remaining = [p for p in pid_set.custom_pids if p.name != name]
        if len(remaining) == len(pid_set.custom_pids):
            raise HTTPException(status_code=404, detail=f"no custom PID named {name!r}")
        save_custom_pids(CustomPidSet(custom_pids=remaining), config.daemon.custom_pids)
        publish_control_event("custom_pid.delete", "ok", {"name": name})
        return {"name": name, "deleted": True}

    @app.post("/control/custom-pids/import")
    def post_custom_pids_import(
        request: CustomPidCsvImportRequest,
        _auth: None = Depends(require_control_auth),
    ) -> dict[str, Any]:
        """Torque Pro custom-PID CSV import. A row that names a write mode,
        has a bad formula, or duplicates an already-defined name is skipped
        with a warning — the rest of the import still lands."""
        accepted, warnings = torque_csv_to_custom_pids(request.csv_text)
        pid_set = load_custom_pids(config.daemon.custom_pids)
        existing_names = {p.name for p in pid_set.custom_pids}
        added: list[CustomPid] = []
        for candidate in accepted:
            if candidate.name in existing_names:
                warnings.append(f"{candidate.name!r} already exists; skipped")
                continue
            added.append(candidate)
            existing_names.add(candidate.name)
        if added:
            save_custom_pids(
                CustomPidSet(custom_pids=[*pid_set.custom_pids, *added]),
                config.daemon.custom_pids,
            )
            publish_control_event(
                "custom_pid.import",
                "ok",
                {"added": [p.name for p in added], "warnings": warnings},
            )
        return {
            "added": [p.model_dump(mode="json") for p in added],
            "added_count": len(added),
            "warnings": warnings,
        }
