"""Route registration for the companion API."""

from __future__ import annotations

import base64
import binascii
import json
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse

from src.alerts.feedback import FeedbackStore, annotate_events_with_feedback
from src.companion.library import CanaryLibrary, LibrarySession
from src.companion.schemas import (
    ImportSessionRequest,
    JobSubmitRequest,
    ModelUploadRequest,
    PublishModelRequest,
    SessionMetadataRequest,
    SessionPinRequest,
    SyncSessionRequest,
)
from src.companion.services import (
    _artifact_path,
    _base_url,
    _dataset_row_to_dict,
    _library_session,
    _library_session_path,
    _library_session_to_control_dict,
    _manifest_version,
    _model_id,
    _require_optional_bearer,
    _unlink_path,
)
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.control.models import MODEL_TYPES, publish_model_artifact
from src.control.sessions import (
    delete_session,
    pin_session,
    unpin_session,
    write_session_metadata,
    write_session_zip,
)
from src.dashboard.readers import (
    read_anomalies_since,
    read_latest_per_channel,
    read_latest_telemetry_rows,
)
from src.models.training_ledger import TrainingLedger


def register_companion_routes(
    app: FastAPI,
    *,
    lib: CanaryLibrary,
    local_config: CanaryConfig,
    jobs: JobController,
    config_path: Path,
    pi_host: str | None,
    pi_token: str | None,
    resolved_control_token: str | None,
    import_session_archive: Callable[..., LibrarySession],
    _request_json: Callable[..., dict[str, Any]],
    _request_bytes: Callable[..., bytes],
) -> None:
    def require_local_auth(authorization: str | None = Header(default=None)) -> None:
        _require_optional_bearer(resolved_control_token, authorization)

    def default_pi_host(host: str | None) -> str:
        selected = host or pi_host
        if not selected:
            raise HTTPException(status_code=400, detail="pi_host is required")
        return _base_url(selected)

    def default_pi_token(token: str | None) -> str | None:
        return token or pi_token

    @app.get("/companion/status")
    def companion_status() -> dict[str, Any]:
        sessions = lib.list_sessions()
        return {
            "mode": "local_library",
            "library_root": str(lib.root),
            "sessions": len(sessions),
            "models": len(lib.list_model_artifacts()),
            "datasets": len(lib.list_datasets()),
            "queued_actions": len(lib.list_actions(status="queued")),
            "config_path": str(config_path),
        }

    @app.post("/companion/sessions/import", status_code=201)
    def import_session_archive_endpoint(
        request: ImportSessionRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        try:
            archive = base64.b64decode(request.archive_b64, validate=True)
            session = import_session_archive(
                archive,
                lib,
                config=local_config if request.index_with_config else None,
                source_host=request.source_host,
            )
        except (binascii.Error, ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session": _library_session_to_control_dict(session)}

    @app.post("/companion/sync/sessions/{session_id}", status_code=201)
    def sync_session_from_pi(
        session_id: str,
        request: SyncSessionRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        host = default_pi_host(request.pi_host)
        token = default_pi_token(request.token)
        if request.pin:
            _request_json(
                host,
                "POST",
                f"/control/sessions/{session_id}/pin",
                token=token,
                body={"reason": request.pin_reason, "metadata": {}},
            )
        try:
            archive = _request_bytes(
                host,
                "GET",
                f"/control/sessions/{session_id}/export",
                token=token,
            )
            session = import_session_archive(
                archive,
                lib,
                config=local_config if request.index_with_config else None,
                source_host=host,
            )
        except (ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session": _library_session_to_control_dict(session)}

    @app.get("/control/sessions")
    def list_local_sessions(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        sessions = [_library_session_to_control_dict(s) for s in lib.list_sessions()]
        return {"sessions": sessions, "count": len(sessions)}

    @app.get("/control/sessions/{session_id}/export")
    def export_local_session(
        session_id: str,
        background_tasks: BackgroundTasks,
        _auth: None = Depends(require_local_auth),
    ) -> FileResponse:
        path = _library_session_path(lib, session_id)
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"canary-local-{session_id}-",
            suffix=".zip",
            delete=False,
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        write_session_zip(path, tmp_path)
        background_tasks.add_task(_unlink_path, tmp_path)
        return FileResponse(tmp_path, media_type="application/zip", filename=f"{session_id}.zip")

    @app.get("/control/sessions/{session_id}")
    def get_local_session(
        session_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        session = _library_session(lib, session_id)
        return {"session": _library_session_to_control_dict(session)}

    @app.post("/control/sessions/{session_id}/metadata")
    def update_local_session_metadata(
        session_id: str,
        request: SessionMetadataRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        path = _library_session_path(lib, session_id)
        metadata = write_session_metadata(path, request.metadata, merge=request.merge)
        refreshed = lib.import_session(path, config=local_config, copy=False)
        return {
            "session_id": session_id,
            "metadata": metadata,
            "session": _library_session_to_control_dict(refreshed),
        }

    @app.post("/control/sessions/{session_id}/pin")
    def pin_local_session(
        session_id: str,
        request: SessionPinRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        path = _library_session_path(lib, session_id)
        metadata = pin_session(path, reason=request.reason)
        if request.metadata:
            metadata = write_session_metadata(path, request.metadata)
        refreshed = lib.import_session(path, config=local_config, copy=False)
        return {"session": _library_session_to_control_dict(refreshed), "metadata": metadata}

    @app.post("/control/sessions/{session_id}/unpin")
    def unpin_local_session(
        session_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        path = _library_session_path(lib, session_id)
        metadata = unpin_session(path)
        refreshed = lib.import_session(path, config=local_config, copy=False)
        return {"session": _library_session_to_control_dict(refreshed), "metadata": metadata}

    @app.delete("/control/sessions/{session_id}")
    def delete_local_session(
        session_id: str,
        force: bool = Query(default=False),
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        path = _library_session_path(lib, session_id)
        try:
            delete_session(path, force=force)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        lib.remove_session(session_id, delete_files=False)
        return {"session_id": session_id, "deleted": True, "force": force}

    @app.get("/telemetry/latest")
    def get_local_telemetry_latest(
        n: int = Query(default=100, ge=1, le=10_000),
        per_channel: bool = Query(default=False),
    ) -> dict[str, Any]:
        rows = (
            read_latest_per_channel(local_config.collection.storage_path)
            if per_channel
            else read_latest_telemetry_rows(local_config.collection.storage_path, n=n)
        )
        return {"rows": rows, "count": len(rows)}

    @app.get("/anomalies")
    def get_local_anomalies(since: str | None = Query(default=None)) -> dict[str, Any]:
        try:
            events = read_anomalies_since(local_config.daemon.anomalies_dir, since=since)
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"bad `since`: {exc}") from exc
        events = annotate_events_with_feedback(
            events,
            FeedbackStore(local_config.daemon.feedback_log),
        )
        return {"events": events, "count": len(events)}

    @app.post("/control/jobs", status_code=201)
    def start_local_job(
        request: JobSubmitRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        try:
            job = jobs.submit(request.kind, request.params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job": job.to_dict()}

    @app.get("/control/jobs")
    def list_local_jobs(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        records = [job.to_dict() for job in jobs.list_jobs()]
        return {"jobs": records, "count": len(records)}

    @app.get("/control/jobs/{job_id}/logs")
    def local_job_logs(
        job_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        logs = jobs.logs(job_id)
        if logs is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return {"job_id": job_id, "log": logs}

    @app.get("/control/jobs/{job_id}")
    def get_local_job(
        job_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return {"job": job.to_dict()}

    @app.post("/control/jobs/{job_id}/cancel")
    def cancel_local_job(
        job_id: str,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        job = jobs.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return {"job": job.to_dict()}

    @app.get("/control/training-runs")
    def local_training_runs(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        ledger = TrainingLedger(local_config.daemon.training_ledger)
        runs = [r.to_dict() for r in ledger.records()]
        for run in runs:
            lib.record_training_run(run, compute_target="laptop")
        return {"runs": runs, "count": len(runs)}

    @app.get("/control/datasets")
    def local_datasets(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        datasets = [_dataset_row_to_dict(row) for row in lib.list_datasets()]
        return {"datasets": datasets, "count": len(datasets)}

    @app.get("/control/datasets/health")
    def local_dataset_health(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        from src.datasets.health import analyze_corpus

        sessions = [
            Path(session.path) for session in lib.list_sessions() if Path(session.path).is_dir()
        ]
        return analyze_corpus(sessions, local_config).to_dict()

    @app.get("/companion/models")
    def companion_models(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        models = lib.list_model_artifacts()
        return {"models": models, "count": len(models)}

    @app.post("/control/models/{model_type}/artifact")
    def upload_local_model_artifact(
        model_type: str,
        request: ModelUploadRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        try:
            published = publish_model_artifact(
                local_config,
                model_type=model_type,
                artifact_b64=request.artifact_b64,
                manifest=request.manifest,
            )
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        artifact = Path(published["artifact_path"])
        lib.add_model_artifact(
            _model_id(model_type, request.manifest),
            model_type=model_type,
            artifact_path=artifact,
            manifest=request.manifest,
            version=_manifest_version(request.manifest),
            copy=False,
        )
        return {"model": published}

    @app.post("/companion/models/{model_type}/publish")
    def publish_model_to_pi(
        model_type: str,
        request: PublishModelRequest,
        _auth: None = Depends(require_local_auth),
    ) -> dict[str, Any]:
        if model_type not in MODEL_TYPES:
            raise HTTPException(status_code=400, detail=f"unsupported model type: {model_type}")
        host = default_pi_host(request.pi_host)
        token = default_pi_token(request.token)
        artifact_path = (
            Path(request.artifact_path)
            if request.artifact_path
            else _artifact_path(local_config, model_type)
        )
        manifest_path = (
            Path(request.manifest_path)
            if request.manifest_path
            else artifact_path.with_suffix(".manifest.json")
        )
        try:
            manifest = json.loads(manifest_path.read_text())
            artifact_b64 = base64.b64encode(artifact_path.read_bytes()).decode("ascii")
            result = _request_json(
                host,
                "POST",
                f"/control/models/{model_type}/artifact",
                token=token,
                body={"artifact_b64": artifact_b64, "manifest": manifest},
            )
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        model_id = request.model_id or _model_id(model_type, manifest)
        lib.add_model_artifact(
            model_id,
            model_type=model_type,
            artifact_path=artifact_path,
            manifest=manifest if isinstance(manifest, dict) else {},
            version=_manifest_version(manifest if isinstance(manifest, dict) else {}),
            copy=False,
        )
        lib.record_publish(model_id, target=host, status="uploaded", detail=json.dumps(result))
        return {"publish": {"model_id": model_id, "target": host, "result": result}}

    @app.get("/companion/actions")
    def queued_actions(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        actions = lib.list_actions()
        return {"actions": actions, "count": len(actions)}

    @app.get("/companion/sync/state")
    def companion_sync_state(_auth: None = Depends(require_local_auth)) -> dict[str, Any]:
        sessions = lib.list_sessions()
        actions = lib.list_actions()
        queued = [action for action in actions if action.get("status") == "queued"]
        return {
            "source_hosts": sorted({s.source_host for s in sessions if s.source_host}),
            "queued_actions": queued,
            "actions": actions,
            "publish_history": lib.publish_history(),
        }


__all__ = ["register_companion_routes"]
