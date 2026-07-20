"""Service helpers for the companion API."""

from __future__ import annotations

import json
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
from fastapi import HTTPException

from src.companion.library import CanaryLibrary, LibrarySession
from src.config import CanaryConfig
from src.control.jobs import JobController
from src.control.sessions import summarize_session


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
]
