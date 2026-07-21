"""Local companion API for laptop-side sync, training, and model publish.

The Pi daemon remains the only live collector. This service is the local
library backend described in ``docs/companion-app-plan.md``: it stores synced
sessions under ``~/.canary/library``, runs the existing training scripts
against those local files, and publishes approved artifacts back to a reachable
Pi through the existing authenticated control API.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.companion.library import DEFAULT_LIBRARY_ROOT, CanaryLibrary
from src.companion.routes import register_companion_routes
from src.companion.schemas import (
    ImportSessionRequest as ImportSessionRequest,
)
from src.companion.schemas import (
    JobSubmitRequest as JobSubmitRequest,
)
from src.companion.schemas import (
    ModelUploadRequest as ModelUploadRequest,
)
from src.companion.schemas import (
    PublishModelRequest as PublishModelRequest,
)
from src.companion.schemas import (
    SessionMetadataRequest as SessionMetadataRequest,
)
from src.companion.schemas import (
    SessionPinRequest as SessionPinRequest,
)
from src.companion.schemas import (
    SyncSessionRequest as SyncSessionRequest,
)
from src.companion.services import (
    _artifact_path as _artifact_path,
)
from src.companion.services import (
    _base_url as _base_url,
)
from src.companion.services import (
    _dataset_row_to_dict as _dataset_row_to_dict,
)
from src.companion.services import (
    _download_session_export as _download_session_export,
)
from src.companion.services import (
    _library_session as _library_session,
)
from src.companion.services import (
    _library_session_path as _library_session_path,
)
from src.companion.services import (
    _library_session_to_control_dict as _library_session_to_control_dict,
)
from src.companion.services import (
    _make_job_controller,
    _request_bytes,
    _request_json,
    companion_config,
    import_session_archive,
    write_companion_config,
)
from src.companion.services import (
    _manifest_version as _manifest_version,
)
from src.companion.services import (
    _model_id as _model_id,
)
from src.companion.services import (
    _require_optional_bearer as _require_optional_bearer,
)
from src.companion.services import (
    _unlink_path as _unlink_path,
)
from src.config import CanaryConfig
from src.control.jobs import JobController

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STATIC_DIR = _REPO_ROOT / "src" / "dashboard" / "static" / "dist"
_COMPANION_TOKEN_ENV = "CANARY_COMPANION_TOKEN"


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

    register_companion_routes(
        app,
        lib=lib,
        local_config=local_config,
        jobs=jobs,
        config_path=config_path,
        pi_host=pi_host,
        pi_token=pi_token,
        resolved_control_token=resolved_control_token,
        import_session_archive=import_session_archive,
        _request_json=_request_json,
        _request_bytes=_request_bytes,
        _download_session_export=_download_session_export,
    )

    if static_dir is not None and (static_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


__all__ = [
    "create_app",
    "companion_config",
    "import_session_archive",
    "write_companion_config",
]
