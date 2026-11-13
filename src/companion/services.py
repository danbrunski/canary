"""Service helpers for the companion API: the library import, the HTTP client
to a Pi's control API, and the queued-action bookkeeping.

The backend-agnostic ``/control/*`` handlers this module once shared with the
dashboard (jobs, training runs, feedback, dataset health) went with the
standalone companion app (R29.15); the per-session routes both apps serve are
one router (:mod:`src.dashboard.routes.stored_sessions`, R03.7).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from src.artifact_hash import sha256_file
from src.companion.library import CanaryLibrary, LibrarySession
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.mcp_server.client import DaemonClient, DaemonError
from src.storage.sessions import summarize_session


def import_session_archive(
    archive: bytes | Path,
    library: CanaryLibrary,
    *,
    config: CanaryConfig | None = None,
    source_host: str | None = None,
) -> LibrarySession:
    """Safely extract a Pi session zip and import it into the local library.

    ``archive`` is the zip's bytes or a path to it. A path is opened in place,
    so a multi-hundred-MB export is never held in memory (R29.16); deleting it
    afterwards is the caller's.
    """

    with tempfile.TemporaryDirectory(prefix="session-import-", dir=library.sync_dir) as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(BytesIO(archive) if isinstance(archive, bytes) else archive) as zf:
            _safe_extract_zip(zf, tmp_path)
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
        compute_target="laptop",
    )


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


def _download_session_export(
    host: str,
    session_id: str,
    *,
    token: str | None,
    dest_dir: Path,
    expected_sha256: str | None = None,
) -> Path:
    """Resumable, checksum-verified fetch of a Pi session export (6.34).

    Streams into ``dest_dir/{session_id}.zip`` via `DaemonClient.download_file`'s
    Range-based resume: a connection dropped mid-transfer leaves the partial
    file on disk, so calling this again for the same session continues from
    where it left off instead of re-downloading from byte 0 (the
    "retry-after-interrupt" story). If a *complete*, checksum-matching
    download from an earlier attempt is already sitting in ``dest_dir`` (the
    process died after the transfer finished but before import completed),
    skip the network entirely — a byte-for-byte duplicate of what we're
    about to fetch.

    Returns the path of the verified zip, left on disk for the caller to
    import from and delete: the export is never read into memory (R29.16).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{session_id}.zip"
    if expected_sha256 is not None and dest.exists() and sha256_file(dest) == expected_sha256:
        return dest

    async def _call() -> Path:
        async with DaemonClient(host, token, timeout=300.0) as client:
            return await client.download_file(f"/control/sessions/{session_id}/export", dest)

    try:
        result_path = asyncio.run(_call())
    except DaemonError as exc:
        raise RuntimeError(f"GET /control/sessions/{session_id}/export failed: {exc}") from exc

    digest = sha256_file(result_path)
    if expected_sha256 is not None and digest != expected_sha256:
        result_path.unlink(missing_ok=True)
        raise ValueError(
            f"checksum mismatch for session {session_id} export: "
            f"expected {expected_sha256}, got {digest}"
        )
    return result_path


def run_queued_action(
    library: CanaryLibrary,
    kind: str,
    params: dict[str, Any],
    fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Run ``fn`` under library-persisted queue/retry bookkeeping (6.34/6.37).

    Every companion-initiated Pi round trip (session sync, model publish) is
    recorded as a `queued_actions` row before it runs and flipped to
    `done`/`failed` after, so a dropped Wi-Fi link leaves a durable,
    inspectable row in `/companion/actions` (with the error message) instead
    of a silently-lost request — and the *same* call, retried, resumes
    rather than restarting (session downloads via `_download_session_export`'s
    `.part` file; model publish by re-reading the still-on-disk artifact).
    """
    action_id = library.queue_action(kind, params)
    try:
        result = fn()
    except Exception as exc:
        library.update_action(action_id, "failed", detail=str(exc))
        raise
    library.update_action(action_id, "done", detail=json.dumps(result, default=str))
    return {**result, "action_id": action_id}


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


__all__ = [
    "import_session_archive",
    "run_queued_action",
    "_base_url",
    "_download_session_export",
    "_library_session_to_control_dict",
    "_make_job_controller",
    "_manifest_version",
    "_model_id",
    "_request_bytes",
    "_request_json",
]
