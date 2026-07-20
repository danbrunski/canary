"""Service helpers for the companion API.

Also home to the backend-agnostic ``/control/*`` handlers shared with
``src/dashboard/routes/control.py`` (refactoring.md 1.2): each takes the bits
that differ between the companion (local library-backed) and control
(live-daemon, filesystem-backed) callers as parameters — a path resolver, a
session serializer, and a handful of optional hooks for the one caller's
extra side effect (companion's library-index refresh on write, control's
audit-event publish, control's active-capture delete guard). Callers keep
their own request-schema classes and unpack them to primitives before
calling in, so this module never depends on either schema module.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from src.companion.library import CanaryLibrary, LibrarySession
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.control.sessions import (
    delete_session,
    pin_session,
    summarize_session,
    unpin_session,
    write_session_metadata,
    write_session_zip,
)
from src.mcp_server.client import DaemonClient, DaemonError
from src.models.training_ledger import TrainingLedger


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
            "vehicle_ledger": control_dir / "vehicle_ledger.jsonl",
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


def _dataset_row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    raw_manifest = payload.get("manifest")
    if isinstance(raw_manifest, str):
        try:
            payload["manifest"] = json.loads(raw_manifest)
        except json.JSONDecodeError:
            payload["manifest"] = {}
    elif raw_manifest is None:
        payload["manifest"] = {}
    return payload


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
    """``method path`` on the remote Pi's control API, via `DaemonClient`.

    Kept synchronous (wraps its own event loop) rather than turned into an
    ``async def`` — every caller is a plain (non-async) FastAPI route handler
    that Starlette already runs off the event loop in a worker thread, so
    ``asyncio.run`` here is safe and avoids rippling ``async``/``await``
    through routes.py, api.py, and their tests for a two-call-site client
    swap.
    """

    async def _call() -> Any:
        async with DaemonClient(host, token, timeout=120.0) as client:
            if method == "GET":
                return await client.get_json(path)
            if method == "POST":
                return await client.post_json(path, dict(body) if body is not None else None)
            raise RuntimeError(f"{method} {path} failed: unsupported method")

    try:
        result = asyncio.run(_call())
    except DaemonError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return result


def _request_bytes(host: str, method: str, path: str, *, token: str | None) -> bytes:
    """``method path`` on the remote Pi's control API, raw bytes (see `_request_json`)."""

    async def _call() -> bytes:
        if method != "GET":
            raise RuntimeError(f"{method} {path} failed: unsupported method")
        async with DaemonClient(host, token, timeout=300.0) as client:
            return await client.get_bytes(path)

    try:
        return asyncio.run(_call())
    except DaemonError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc}") from exc


def _artifact_path(config: CanaryConfig, model_type: str) -> Path:
    if model_type == "isolation_forest":
        return config.daemon.models_dir / config.daemon.isolation_forest_artifact
    if model_type == "lstm_autoencoder":
        return config.daemon.models_dir / config.daemon.lstm_artifact
    if model_type == "tcn_autoencoder":
        return config.daemon.models_dir / config.daemon.tcn_artifact
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


# -- shared `/control/*` handlers (refactoring.md 1.2) -----------------------
#
# Both the companion app and the dashboard's control API register the same
# ~15 session/job/training-run/dataset-health routes against different
# backing stores (companion: `CanaryLibrary`'s sqlite index; control: the
# live daemon's session directory + `SessionCatalog`). Each handler below
# takes that difference as a parameter (a path resolver, a session
# serializer, or a small optional hook) instead of being written twice.


def sessions_list_response(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"sessions": sessions, "count": len(sessions)}


def get_session_response(
    session_id: str, *, resolve_and_serialize: Callable[[str], dict[str, Any]]
) -> dict[str, Any]:
    return {"session": resolve_and_serialize(session_id)}


def export_session_response(
    session_id: str,
    background_tasks: BackgroundTasks,
    *,
    resolve: Callable[[str], Path],
    tmp_prefix: str,
) -> FileResponse:
    path = resolve(session_id)
    tmp = tempfile.NamedTemporaryFile(
        prefix=f"{tmp_prefix}{session_id}-", suffix=".zip", delete=False
    )
    tmp_path = Path(tmp.name)
    tmp.close()
    write_session_zip(path, tmp_path)
    background_tasks.add_task(_unlink_path, tmp_path)
    return FileResponse(tmp_path, media_type="application/zip", filename=f"{session_id}.zip")


def update_session_metadata_response(
    session_id: str,
    metadata: dict[str, Any],
    *,
    merge: bool,
    resolve: Callable[[str], Path],
    finalize: Callable[[Path], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shared body of the `/control/sessions/{id}/metadata` handlers.

    ``finalize`` is companion's seam for its library-index refresh: passing
    one makes the response include a freshly-reindexed ``"session"`` key
    (companion's behavior today); leaving it unset reproduces control's
    current response, which has no ``"session"`` key at all.
    """
    path = resolve(session_id)
    written = write_session_metadata(path, metadata, merge=merge)
    payload: dict[str, Any] = {"session_id": session_id, "metadata": written}
    if finalize is not None:
        payload["session"] = finalize(path)
    return payload


def pin_session_response(
    session_id: str,
    *,
    reason: str | None,
    extra_metadata: dict[str, Any],
    resolve: Callable[[str], Path],
    serialize: Callable[[Path], dict[str, Any]],
    catalog_db: Path | None = None,
) -> dict[str, Any]:
    """Shared body of the `/control/sessions/{id}/pin` handlers.

    ``serialize`` is companion's seam for its library-index refresh: its
    caller supplies a serializer that re-imports the session into the
    library (picking up the just-written pin/metadata state) before
    converting it, where control's serializer just re-reads the session
    directory it already resolved.
    """
    path = resolve(session_id)
    metadata = pin_session(path, reason=reason, catalog_db=catalog_db)
    if extra_metadata:
        metadata = write_session_metadata(path, extra_metadata)
    return {"session": serialize(path), "metadata": metadata}


def unpin_session_response(
    session_id: str,
    *,
    resolve: Callable[[str], Path],
    serialize: Callable[[Path], dict[str, Any]],
    catalog_db: Path | None = None,
) -> dict[str, Any]:
    path = resolve(session_id)
    metadata = unpin_session(path, catalog_db=catalog_db)
    return {"session": serialize(path), "metadata": metadata}


def delete_session_response(
    session_id: str,
    *,
    force: bool,
    resolve: Callable[[str], Path],
    guard: Callable[[str], None] | None = None,
    after_delete: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Shared body of the `DELETE /control/sessions/{id}` handlers.

    ``guard`` is control's seam for its active-capture conflict check
    (companion has no capture concept, so it passes none); ``after_delete``
    is companion's seam for dropping the session from its library index
    without re-deleting the (already-deleted) files.
    """
    path = resolve(session_id)
    if guard is not None:
        guard(session_id)
    try:
        delete_session(path, force=force)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if after_delete is not None:
        after_delete(session_id)
    return {"session_id": session_id, "deleted": True, "force": force}


def submit_job_response(
    jobs: JobController,
    kind: str,
    params: dict[str, Any],
    *,
    on_submit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    try:
        job = jobs.submit(kind, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record = job.to_dict()
    if on_submit is not None:
        on_submit(record)
    return {"job": record}


def list_jobs_response(jobs: JobController) -> dict[str, Any]:
    records = [job.to_dict() for job in jobs.list_jobs()]
    return {"jobs": records, "count": len(records)}


def job_logs_response(jobs: JobController, job_id: str) -> dict[str, Any]:
    logs = jobs.logs(job_id)
    if logs is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"job_id": job_id, "log": logs}


def get_job_response(jobs: JobController, job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    return {"job": job.to_dict()}


def cancel_job_response(
    jobs: JobController,
    job_id: str,
    *,
    on_cancel: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    record = job.to_dict()
    if on_cancel is not None:
        on_cancel(record)
    return {"job": record}


def training_runs_response(
    training_ledger_path: Path,
    *,
    on_run: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    ledger = TrainingLedger(training_ledger_path)
    runs = [r.to_dict() for r in ledger.records()]
    if on_run is not None:
        for run in runs:
            on_run(run)
    return {"runs": runs, "count": len(runs)}


def dataset_health_response(
    sessions: list[Path],
    config: CanaryConfig,
    *,
    corpus_boundary: date | None = None,
) -> dict[str, Any]:
    from src.datasets.health import analyze_corpus

    return analyze_corpus(sessions, config, corpus_boundary=corpus_boundary).to_dict()


__all__ = [
    "companion_config",
    "import_session_archive",
    "write_companion_config",
    "_artifact_path",
    "_base_url",
    "_dataset_row_to_dict",
    "_library_session",
    "_library_session_path",
    "_library_session_to_control_dict",
    "_make_job_controller",
    "_manifest_version",
    "_model_id",
    "_request_bytes",
    "_request_json",
    "_require_optional_bearer",
    "_unlink_path",
    "cancel_job_response",
    "dataset_health_response",
    "delete_session_response",
    "export_session_response",
    "get_job_response",
    "get_session_response",
    "job_logs_response",
    "list_jobs_response",
    "pin_session_response",
    "sessions_list_response",
    "submit_job_response",
    "training_runs_response",
    "unpin_session_response",
    "update_session_metadata_response",
]
