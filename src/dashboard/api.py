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

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.alerts.emitter import AnomalyEventEmitter
from src.alerts.feedback import (
    FEEDBACK_LABELS,
    FeedbackStore,
    annotate_events_with_feedback,
)
from src.alerts.tuning import (
    AlertTuner,
    TuningConfig,
    estimate_before_after,
    load_tuning_version,
    save_tuning_config,
)
from src.config import CanaryConfig
from src.control.audit import ControlAuditLogger
from src.control.capture import CaptureConflictError, CaptureController, NoActiveCaptureError
from src.control.jobs import JobController
from src.control.models import publish_model_artifact
from src.control.preflight import build_preflight
from src.control.replay_control import (
    ReplayConflictError,
    ReplayController,
    ReplayNotFoundError,
    ReplayNotRunningError,
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
    read_latest_per_channel,
    read_latest_telemetry_rows,
    read_obd_coverage,
)
from src.dashboard.schemas import (
    CaptureStartRequest,
    CaptureStopRequest,
    CaptureTagRequest,
    FeedbackRequest,
    JobSubmitRequest,
    ModelUploadRequest,
    ReplayStartRequest,
    SessionMetadataRequest,
    SessionPinRequest,
    TuningRevertRequest,
    TuningUpdateRequest,
)
from src.dashboard.security import _client_is_loopback, _require_control_auth
from src.dashboard.status import StatusSnapshot, _resolve_latest_session, build_status
from src.dashboard.version import _canary_version, _git_sha
from src.dashboard.websockets import _publish_on_app_loop, _run_websocket
from src.decode.behavior_models import (
    DEFAULT_BEHAVIOR_MODELS_PATH,
)
from src.models.health import HealthMonitor
from src.models.training_ledger import TrainingLedger
from src.preprocessing.loader import discover_sessions

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
    alert_tuner: AlertTuner | None = None,
    health_monitor: HealthMonitor | None = None,
    behavior_live: BehaviorLiveState | None = None,
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

    @app.get("/status")
    def get_status() -> dict[str, Any]:
        payload = build_status(
            config,
            models_dir=models_dir,
            emitter=emitter,
            health_monitor=health_monitor,
        ).to_dict()
        if replay_controller is not None:
            payload["replay"] = replay_controller.status().to_dict()
        return payload

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return echo_config(config)

    @app.get("/anomalies")
    def get_anomalies(since: str | None = Query(default=None)) -> dict[str, Any]:
        try:
            events = read_anomalies_since(anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        # Join the operator's verdict onto each event so the timeline can
        # show "you marked this" without a second round-trip (Task 6.24).
        if feedback_store is not None:
            events = annotate_events_with_feedback(events, feedback_store)
        return {"events": events, "count": len(events)}

    @app.get("/telemetry/latest")
    def get_telemetry_latest(
        n: int = Query(default=100, ge=1, le=10_000),
        per_channel: bool = Query(default=False),
    ) -> dict[str, Any]:
        rows = (
            read_latest_per_channel(config.collection.storage_path)
            if per_channel
            else read_latest_telemetry_rows(config.collection.storage_path, n=n)
        )
        return {"rows": rows, "count": len(rows)}

    @app.get("/models/behavior")
    def get_behavior_models() -> dict[str, Any]:
        # Re-read on every request so a `scripts/refit_behavior_models.py`
        # run lands in the dashboard without a daemon restart. Cheap — the
        # file is < 100 KB even with a full surface catalog.
        return read_behavior_models(behavior_models_path)

    @app.get("/models/behavior/live")
    def get_behavior_live() -> dict[str, Any]:
        # Live "you are here" overlay for Models▸Behavior: per-model
        # expected/actual/deviation/severity + a short server-side trail,
        # fed by the daemon's inference loop. Empty (not an error) when no
        # daemon is wired — the static surfaces still render standalone.
        if behavior_live is None:
            return {"observed": 0, "timestamp": None, "operating_mode": None, "models": {}}
        return behavior_live.snapshot()

    @app.get("/bindings")
    def get_bindings() -> dict[str, Any]:
        # Re-read per request so a hand-edit to bindings.yaml (during RE)
        # surfaces in the dashboard immediately. The file is small.
        return read_bindings_for_display(bindings_path)

    @app.get("/dids")
    def get_dids() -> dict[str, Any]:
        # Read-only Nissan Mode-22 DID catalog for the 6.16 workbench.
        # Re-read per request like /bindings so a hand-edit during RE shows
        # up immediately; empty doc when the file is absent.
        from src.decode.dids import load_did_map

        if not dids_path.exists():
            return {"schema_version": None, "vehicle": {}, "access": None, "dids": []}
        return load_did_map(dids_path).model_dump(mode="json")

    @app.get("/test-profiles")
    def get_test_profiles() -> dict[str, Any]:
        # Read-only catalog for the Test panel, scanned per request so a
        # dropped-in / hand-edited profile YAML appears without a daemon
        # restart. Unauthenticated like /dids + /bindings (no secrets;
        # listing profiles must work before the control token is entered).
        from src.control.test_profiles import load_test_profiles

        return load_test_profiles(test_profiles_dir).model_dump(mode="json")

    @app.get("/obd/coverage")
    def get_obd_coverage() -> dict[str, Any]:
        # Read-only honesty surface (Task 6.3 / Risk R7): which public PIDs
        # the ECU supports, which CANary decodes, which are undecoded gaps.
        return read_obd_coverage(config.collection.storage_path)

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
        metadata = pin_session(session, reason=request.reason)
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
        metadata = unpin_session(session)
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
        return analyze_corpus(sessions, config).to_dict()

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

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(websocket: WebSocket) -> None:
        await _run_websocket(websocket, telemetry_bus)

    @app.websocket("/ws/anomalies")
    async def ws_anomalies(websocket: WebSocket) -> None:
        await _run_websocket(websocket, anomalies_bus)

    @app.websocket("/ws/scores")
    async def ws_scores(websocket: WebSocket) -> None:
        # Every inference tick (both models), not just anomalies — powers
        # the live per-model score readout.
        await _run_websocket(websocket, scores_bus)

    @app.websocket("/ws/control")
    async def ws_control(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token")
        if not resolved_control_token or token != resolved_control_token:
            await websocket.close(code=1008)
            return
        app.state.control_loop = asyncio.get_running_loop()
        await _run_websocket(websocket, control_bus)

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


def _unlink_path(path: Path) -> None:
    path.unlink(missing_ok=True)


__all__ = [
    "DEFAULT_BINDINGS_PATH",
    "DEFAULT_DIDS_PATH",
    "DEFAULT_STATIC_DIR",
    "DEFAULT_TEST_PROFILES_DIR",
    "StatusSnapshot",
    "build_status",
    "create_app",
    "echo_config",
    "read_anomalies_since",
    "read_latest_per_channel",
    "read_latest_telemetry_rows",
]
