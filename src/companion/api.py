"""Local companion API for laptop-side sync, training, and model publish.

The Pi daemon remains the only live collector. This service is the local
library backend described in ``docs/companion-app-plan.md``: it stores synced
sessions under ``~/.canary/library``, runs the existing training scripts
against those local files, and publishes approved artifacts back to a reachable
Pi through the existing authenticated control API.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from src.companion.library import DEFAULT_LIBRARY_ROOT, CanaryLibrary, LibrarySession
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.control.models import MODEL_TYPES, publish_model_artifact
from src.control.sessions import (
    delete_session,
    pin_session,
    summarize_session,
    unpin_session,
    write_session_metadata,
    write_session_zip,
)
from src.models.training_ledger import TrainingLedger

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STATIC_DIR = _REPO_ROOT / "src" / "dashboard" / "static" / "dist"
_COMPANION_TOKEN_ENV = "CANARY_COMPANION_TOKEN"


class _ApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportSessionRequest(_ApiRequest):
    archive_b64: str
    source_host: str | None = None
    index_with_config: bool = True


class SyncSessionRequest(_ApiRequest):
    pi_host: str | None = None
    token: str | None = None
    pin: bool = False
    pin_reason: str | None = "synced to companion library"
    index_with_config: bool = True


class JobSubmitRequest(_ApiRequest):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)


class SessionMetadataRequest(_ApiRequest):
    metadata: dict[str, Any] = Field(default_factory=dict)
    merge: bool = True


class SessionPinRequest(_ApiRequest):
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelUploadRequest(_ApiRequest):
    artifact_b64: str
    manifest: dict[str, Any]


class PublishModelRequest(_ApiRequest):
    pi_host: str | None = None
    token: str | None = None
    artifact_path: str | None = None
    manifest_path: str | None = None
    model_id: str | None = None


def create_app(
    base_config: CanaryConfig,
    *,
    library: CanaryLibrary | None = None,
    library_root: Path | None = None,
    pi_host: str | None = None,
    pi_token: str | None = None,
    control_token: str | None = None,
    repo_root: Path = _REPO_ROOT,
    python: Path | None = None,
    job_controller: JobController | None = None,
    static_dir: Path | None = _DEFAULT_STATIC_DIR,
    enable_dev_cors: bool = False,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    """Build the local companion FastAPI app.

    ``control_token`` protects local mutating routes only when explicitly set
    or when ``CANARY_COMPANION_TOKEN`` is present. By default the service is
    intended to bind to loopback and local routes are unlocked.
    """

    lib = library or CanaryLibrary(library_root or DEFAULT_LIBRARY_ROOT)
    local_config = companion_config(base_config, lib)
    config_path = write_companion_config(local_config, lib.root / "canary.local.yaml")
    jobs = job_controller or _make_job_controller(
        local_config,
        repo_root=repo_root,
        config_path=config_path,
        python=python,
    )
    resolved_control_token = (
        os.environ.get(_COMPANION_TOKEN_ENV) if control_token is None else control_token
    )

    app = FastAPI(title="CANary Companion", version="0.1.0")
    app.state.library = lib
    app.state.config = local_config
    app.state.job_controller = jobs
    origins = list(cors_origins or [])
    if enable_dev_cors:
        origins.extend(["http://localhost:5173", "http://127.0.0.1:5173"])
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=sorted(set(origins)),
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

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
        artifact_path = Path(request.artifact_path) if request.artifact_path else _artifact_path(
            local_config, model_type
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

    if static_dir is not None and (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def companion_config(base_config: CanaryConfig, library: CanaryLibrary) -> CanaryConfig:
    """Return a config pointed at the local companion library."""

    anomalies_dir = library.root / "anomalies"
    control_dir = library.root / "control"
    for path in (anomalies_dir, control_dir):
        path.mkdir(parents=True, exist_ok=True)
    collection = base_config.collection.model_copy(update={"storage_path": library.sessions_dir})
    daemon = base_config.daemon.model_copy(
        update={
            "anomalies_dir": anomalies_dir,
            "models_dir": library.models_dir,
            "jobs_dir": library.jobs_dir,
            "audit_log": control_dir / "audit.jsonl",
            "feedback_log": control_dir / "anomaly_feedback.jsonl",
            "training_ledger": library.training_runs_dir / "training_runs.jsonl",
            "tuning_config": control_dir / "tuning.yaml",
            "tuning_audit": control_dir / "tuning_audit.jsonl",
        }
    )
    return base_config.model_copy(update={"collection": collection, "daemon": daemon})


def write_companion_config(config: CanaryConfig, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json")
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def import_session_archive(
    archive_bytes: bytes,
    library: CanaryLibrary,
    *,
    config: CanaryConfig | None = None,
    source_host: str | None = None,
) -> LibrarySession:
    """Safely extract a Pi session zip and import it into the local library."""

    with tempfile.TemporaryDirectory(prefix="session-import-", dir=library.sync_dir) as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            _safe_extract_zip(archive, tmp_path)
        session_dirs = [path for path in tmp_path.iterdir() if path.is_dir()]
        if len(session_dirs) != 1:
            raise ValueError("expected archive to contain exactly one session directory")
        return library.import_session(session_dirs[0], config=config, source_host=source_host)


def _make_job_controller(
    config: CanaryConfig,
    *,
    repo_root: Path,
    config_path: Path,
    python: Path | None,
) -> JobController:
    py = python
    if py is None:
        candidate = repo_root / ".venv" / "bin" / "python"
        py = candidate if candidate.exists() else Path(sys.executable)
    return JobController(
        config=config,
        repo_root=repo_root,
        config_path=config_path,
        jobs_dir=config.daemon.jobs_dir,
        python=py,
    )


def _library_session(library: CanaryLibrary, session_id: str) -> LibrarySession:
    session = library.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return session


def _library_session_path(library: CanaryLibrary, session_id: str) -> Path:
    session = _library_session(library, session_id)
    path = Path(session.path)
    if not path.is_dir():
        raise HTTPException(status_code=404, detail=f"session files missing: {session_id}")
    return path


def _library_session_to_control_dict(session: LibrarySession) -> dict[str, Any]:
    path = Path(session.path)
    if path.is_dir():
        data = summarize_session(path).to_dict()
    else:
        data = {
            "session_id": session.session_id,
            "session_path": session.path,
            "started_at": session.started_at,
            "telemetry_chunks": 0,
            "tag_count": len(session.tags),
            "has_manifest": False,
            "pinned": session.pinned,
            "metadata": {},
            "size_bytes": session.size_bytes or 0,
        }
    data.update(
        {
            "source_host": session.source_host,
            "imported_at": session.imported_at,
            "training_candidate": session.training_candidate,
            "quality_warnings": session.quality_warnings,
            "tags": session.tags,
        }
    )
    return data


def _safe_extract_zip(archive: zipfile.ZipFile, target_root: Path) -> None:
    root = target_root.resolve()
    for item in archive.infolist():
        name = PurePosixPath(item.filename)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError(f"unsafe archive path: {item.filename}")
        if item.is_dir():
            continue
        target = root.joinpath(*name.parts)
        resolved_target = target.resolve()
        if root not in (resolved_target, *resolved_target.parents):
            raise ValueError(f"unsafe archive path: {item.filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(item) as source, target.open("wb") as dest:
            shutil.copyfileobj(source, dest)


def _require_optional_bearer(expected_token: str | None, authorization: str | None) -> None:
    if not expected_token:
        return
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="missing bearer token")
    provided = authorization[len(prefix) :]
    if not secrets.compare_digest(provided, expected_token):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _base_url(host: str) -> str:
    base = host.rstrip("/")
    if not base.startswith(("http://", "https://")):
        return f"http://{base}"
    return base


def _request_json(
    host: str,
    method: str,
    path: str,
    *,
    token: str | None,
    body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{host}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=120) as response:  # noqa: S310 - operator-provided LAN URL
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return parsed


def _request_bytes(host: str, method: str, path: str, *, token: str | None) -> bytes:
    headers = {"Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{host}{path}", headers=headers, method=method)
    try:
        with urlopen(req, timeout=300) as response:  # noqa: S310 - operator-provided LAN URL
            return bytes(response.read())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc


def _artifact_path(config: CanaryConfig, model_type: str) -> Path:
    if model_type == "isolation_forest":
        return config.daemon.models_dir / config.daemon.isolation_forest_artifact
    if model_type == "lstm_autoencoder":
        return config.daemon.models_dir / config.daemon.lstm_artifact
    raise HTTPException(status_code=400, detail=f"unsupported model type: {model_type}")


def _model_id(model_type: str, manifest: Mapping[str, Any]) -> str:
    for key in ("run_id", "model_version", "version"):
        value = manifest.get(key)
        if value:
            return str(value)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{model_type}-{stamp}"


def _manifest_version(manifest: Mapping[str, Any]) -> str | None:
    for key in ("model_version", "version", "run_id"):
        value = manifest.get(key)
        if value:
            return str(value)
    return None


def _unlink_path(path: Path) -> None:
    path.unlink(missing_ok=True)


__all__ = [
    "create_app",
    "companion_config",
    "import_session_archive",
    "write_companion_config",
]
