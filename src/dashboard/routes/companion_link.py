"""Companion mode on the daemon: the link to a CANary Pi (``/companion/*``).

Registered by :func:`src.dashboard.api.create_app` when ``companion.enabled``
(``src.config.CompanionConfig``). The daemon then keeps a library
(``src.companion.library``) whose ``sessions/`` is its own session store,
pulls sessions from the linked Pi, publishes models back, and knows which Pi
it is linked to:

- ``GET /companion/status`` (public): the library's counts, the linked Pi
  with its identity, and the state of the last network search.
- ``GET /companion/discover``: search the network again
  (``src.companion.discovery``) and list every daemon found.
- ``POST`` / ``DELETE /companion/pi``: link to one daemon (it must answer
  now) or unlink. A link is remembered in the library (``pi.json``) and wins
  over ``companion.pi_host``.
- ``POST /companion/sync/sessions/{id}``, ``POST /companion/sessions/import``,
  ``POST /companion/models/{type}/publish``, ``GET /companion/actions``,
  ``POST /companion/actions/{id}/retry``, ``GET /companion/sync/state``: the
  sync and publish routes, unchanged from the standalone companion API they
  replace (2026-10-16; one site, one port).

At startup, with ``companion.discover`` on and no Pi configured or
remembered, the network is searched once in a background thread and a lone
daemon found is linked (``source: discovered``). Several found are listed
for the dashboard to choose from. Every mutating route is guarded by the
daemon's control-token dependency, the same as the rest of the control plane.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import threading
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query

from src.companion.discovery import (
    DaemonIdentity,
    DiscoveredPi,
    base_url_of,
    discover,
    mdns_available,
    probe,
)
from src.companion.library import CanaryLibrary, LibrarySession
from src.companion.pi_selection import FILENAME as PI_SELECTION_FILENAME
from src.companion.pi_selection import PiSelection
from src.companion.schemas import (
    ImportSessionRequest,
    PublishModelRequest,
    SelectPiRequest,
    SyncSessionRequest,
)
from src.companion.services import (
    _base_url,
    _download_session_export,
    _library_session_to_control_dict,
    _manifest_version,
    _model_id,
    _request_json,
    import_session_archive,
    run_queued_action,
)
from src.config import CanaryConfig
from src.control.models import MODEL_TYPES
from src.dashboard.routes.control.responses import http_artifact_path
from src.dashboard.security import control_auth_dependency
from src.models.manifest import compatibility_report, manifest_path_for

DiscoverFn = Callable[..., list[DiscoveredPi]]
ProbeFn = Callable[[str], DaemonIdentity | None]


@dataclass
class DiscoveryState:
    """The last network search: never started, running, or done with what it found."""

    state: str = "idle"
    ran_at: str | None = None
    found: list[DiscoveredPi] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ran_at": self.ran_at,
            "found": [item.to_dict() for item in self.found],
            "error": self.error,
        }


class CompanionLink:
    """The daemon's link to a Pi: the remembered selection plus the search state."""

    def __init__(
        self,
        *,
        lib: CanaryLibrary,
        configured_host: str | None,
        discover_fn: DiscoverFn,
        probe_fn: ProbeFn,
        sweep: bool,
    ) -> None:
        self.lib = lib
        self.configured_host = configured_host
        self.selection = PiSelection(lib.root / PI_SELECTION_FILENAME)
        self._discover = discover_fn
        self._probe = probe_fn
        self._sweep = sweep
        self.discovery = DiscoveryState()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- the linked Pi -----------------------------------------------------------------------

    def pi(self) -> dict[str, Any] | None:
        return self.selection.describe(configured=self.configured_host)

    def pi_base_url(self, requested: str | None) -> str:
        selected = requested or self.selection.base_url() or self.configured_host
        if not selected:
            raise HTTPException(
                status_code=400,
                detail="pi_host is required: no Pi linked (POST /companion/pi) or configured",
            )
        return _base_url(selected)

    def link(self, host_or_url: str, *, source: str) -> dict[str, Any]:
        url = base_url_of(host_or_url)
        identity = self._probe(url)
        if identity is None:
            raise HTTPException(status_code=400, detail=f"no CANary daemon answers at {url}")
        return self.selection.select(url, identity, source=source)

    def unlink(self) -> None:
        self.selection.clear()

    # -- the search ---------------------------------------------------------------------------

    def run_discovery(self, *, sweep: bool | None = None) -> list[DiscoveredPi]:
        """Search now, in this thread; link a lone find when nothing is linked yet."""
        with self._lock:
            self.discovery = DiscoveryState(state="running")
        try:
            found = self._discover(sweep=self._sweep if sweep is None else sweep)
            error = None
        except Exception as exc:  # noqa: BLE001 - a failed search is reported, never raised
            found, error = [], f"{type(exc).__name__}: {exc}"
        with self._lock:
            self.discovery = DiscoveryState(
                state="done",
                ran_at=datetime.now(UTC).isoformat(),
                found=list(found),
                error=error,
            )
        if len(found) == 1 and self.selection.read() is None and not self.configured_host:
            self.selection.select(found[0].base_url, found[0].identity, source="discovered")
        return list(found)

    def start_discovery(self) -> threading.Thread:
        """The startup search, off the request path."""
        thread = threading.Thread(
            target=self.run_discovery, name="companion-discovery", daemon=True
        )
        self._thread = thread
        thread.start()
        return thread

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


def register_companion_link_routes(
    app: FastAPI,
    *,
    config: CanaryConfig,
    resolved_control_token: str | None,
    lib: CanaryLibrary | None = None,
    discover_fn: DiscoverFn = discover,
    probe_fn: ProbeFn = probe,
    request_json: Callable[..., dict[str, Any]] = _request_json,
    download_session_export: Callable[..., bytes | Path] = _download_session_export,
    import_archive: Callable[..., LibrarySession] = import_session_archive,
    environ: dict[str, str] | None = None,
) -> CompanionLink:
    """Mount the link routes; returns the link, also kept at ``app.state.companion_link``."""
    settings = config.companion
    root = settings.library_root
    if root is None:  # the config validator fills it when enabled; be plain if not
        raise ValueError("companion.library_root is unset; enable companion mode in the config")
    library = lib or CanaryLibrary(Path(root))
    env = os.environ if environ is None else environ
    pi_token = env.get(settings.pi_token_env) or None
    link = CompanionLink(
        lib=library,
        configured_host=settings.pi_host,
        discover_fn=discover_fn,
        probe_fn=probe_fn,
        sweep=settings.discover_sweep,
    )
    app.state.companion_link = link
    app.state.companion_library = library
    authorize = control_auth_dependency(resolved_control_token)

    def default_pi_token(token: str | None) -> str | None:
        return token or pi_token

    @app.get("/companion/status")
    def companion_status() -> dict[str, Any]:
        sessions = library.list_sessions()
        return {
            "mode": "local_library",
            "library_root": str(library.root),
            "sessions": len(sessions),
            "models": len(library.list_model_artifacts()),
            "datasets": len(library.list_datasets()),
            "queued_actions": len(library.list_actions(status="queued")),
            "pi": link.pi(),
            "pi_token_present": pi_token is not None,
            "discovery": {**link.discovery.to_dict(), "mdns": mdns_available()},
        }

    @app.get("/companion/discover")
    def discover_pis(
        sweep: bool | None = Query(default=None),
        _auth: None = Depends(authorize),
    ) -> dict[str, Any]:
        """Search the network now (a few seconds) and list every CANary daemon found."""
        found = link.run_discovery(sweep=sweep)
        return {"found": [item.to_dict() for item in found], "pi": link.pi()}

    @app.post("/companion/pi")
    def select_pi(request: SelectPiRequest, _auth: None = Depends(authorize)) -> dict[str, Any]:
        """Link to one daemon; it must answer now, or the link is refused."""
        return {"pi": link.link(request.base_url, source="selected")}

    @app.delete("/companion/pi")
    def clear_pi(_auth: None = Depends(authorize)) -> dict[str, Any]:
        link.unlink()
        return {"pi": link.pi()}

    @app.post("/companion/sessions/import", status_code=201)
    def import_session_archive_endpoint(
        request: ImportSessionRequest,
        _auth: None = Depends(authorize),
    ) -> dict[str, Any]:
        try:
            archive = base64.b64decode(request.archive_b64, validate=True)
            session = import_archive(
                archive,
                library,
                config=config if request.index_with_config else None,
                source_host=request.source_host,
            )
        except (binascii.Error, ValueError, OSError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"session": _library_session_to_control_dict(session)}

    @app.post("/companion/sync/sessions/{session_id}", status_code=201)
    def sync_session_from_pi(
        session_id: str,
        request: SyncSessionRequest,
        _auth: None = Depends(authorize),
    ) -> dict[str, Any]:
        host = link.pi_base_url(request.pi_host)
        token = default_pi_token(request.token)
        if request.pin:
            request_json(
                host,
                "POST",
                f"/control/sessions/{session_id}/pin",
                token=token,
                body={"reason": request.pin_reason, "metadata": {}},
            )

        def _do_sync() -> dict[str, Any]:
            expected_sha256 = None
            if request.verify_checksum:
                checksum = request_json(
                    host,
                    "GET",
                    f"/control/sessions/{session_id}/export/checksum",
                    token=token,
                )
                expected_sha256 = checksum.get("sha256")
            archive = download_session_export(
                host,
                session_id,
                token=token,
                dest_dir=library.sync_dir,
                expected_sha256=expected_sha256,
            )
            try:
                session = import_archive(
                    archive,
                    library,
                    config=config if request.index_with_config else None,
                    source_host=host,
                )
            finally:
                # The export is imported from the file it was downloaded to,
                # never read into memory (R29.16); it is spent either way.
                if isinstance(archive, Path):
                    archive.unlink(missing_ok=True)
            return {
                "session": _library_session_to_control_dict(session),
                "sha256": expected_sha256,
            }

        # 6.34: queued/retried like model publish (`run_queued_action`): a
        # dropped mid-transfer connection leaves the partial download on
        # disk (`_download_session_export`'s `.part` file) and a `failed`
        # row in `/companion/actions`; calling this same endpoint again
        # resumes instead of restarting from byte 0.
        try:
            return run_queued_action(
                library,
                "sync_session",
                {"session_id": session_id, "host": host},
                _do_sync,
            )
        except (ValueError, OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/companion/models/{model_type}/publish")
    def publish_model_to_pi(
        model_type: str,
        request: PublishModelRequest,
        _auth: None = Depends(authorize),
    ) -> dict[str, Any]:
        if model_type not in MODEL_TYPES:
            raise HTTPException(status_code=400, detail=f"unsupported model type: {model_type}")
        host = link.pi_base_url(request.pi_host)
        token = default_pi_token(request.token)
        artifact_path = (
            Path(request.artifact_path)
            if request.artifact_path
            else http_artifact_path(config, model_type)
        )
        manifest_path = (
            Path(request.manifest_path)
            if request.manifest_path
            else manifest_path_for(artifact_path)
        )

        def _do_publish() -> dict[str, Any]:
            manifest = json.loads(manifest_path.read_text())
            # 6.37: compatibility report against whatever the target already
            # has active *before* pushing bytes, so a schema mismatch is
            # visible to the operator up front instead of surfacing only as
            # a live rejection on the Pi.
            try:
                running = request_json(
                    host, "GET", f"/control/models/{model_type}/manifest", token=token
                )
                running_manifest = running.get("manifest")
            except RuntimeError:
                running_manifest = None  # target unreachable/no manifest yet
            report = compatibility_report(
                manifest if isinstance(manifest, dict) else {}, running_manifest
            )
            artifact_b64 = base64.b64encode(artifact_path.read_bytes()).decode("ascii")
            result = request_json(
                host,
                "POST",
                f"/control/models/{model_type}/artifact",
                token=token,
                body={"artifact_b64": artifact_b64, "manifest": manifest},
            )
            model_id = request.model_id or _model_id(model_type, manifest)
            library.add_model_artifact(
                model_id,
                model_type=model_type,
                artifact_path=artifact_path,
                manifest=manifest if isinstance(manifest, dict) else {},
                version=_manifest_version(manifest if isinstance(manifest, dict) else {}),
                copy=False,
            )
            # Accepted/active confirmation: the Pi only round-trips a manifest
            # once `publish_model_artifact` wrote it. Not per model type today
            # (one daemon-wide "last swap"), so this is the best available
            # signal, not a per-type guarantee.
            confirmed_manifest = request_json(
                host, "GET", f"/control/models/{model_type}/manifest", token=token
            ).get("manifest", {})
            accepted = isinstance(confirmed_manifest, dict) and confirmed_manifest.get(
                "model_version"
            ) == manifest.get("model_version")
            status = "accepted" if accepted else "uploaded"
            try:
                daemon_status = request_json(host, "GET", "/status", token=token)
            except RuntimeError:
                daemon_status = {}
            active_version = (daemon_status.get("health") or {}).get("active_model_version")
            if accepted and active_version == manifest.get("model_version"):
                status = "active"
            library.record_publish(model_id, target=host, status=status, detail=json.dumps(result))
            return {
                "model_id": model_id,
                "target": host,
                "result": result,
                "status": status,
                "compatibility": report,
            }

        # Queue/retry semantics (6.37): re-posting the same publish request
        # after a failure (network drop, Pi rebooting) just re-reads the
        # still-on-disk local artifact and retries the upload; nothing here
        # is one-shot/consumed on failure.
        try:
            publish = run_queued_action(
                library,
                "publish_model",
                {"model_type": model_type, "host": host},
                _do_publish,
            )
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"publish": publish}

    @app.get("/companion/actions")
    def queued_actions(_auth: None = Depends(authorize)) -> dict[str, Any]:
        actions = library.list_actions()
        return {"actions": actions, "count": len(actions)}

    @app.post("/companion/actions/{action_id}/retry")
    def retry_queued_action(
        action_id: int,
        _auth: None = Depends(authorize),
    ) -> dict[str, Any]:
        """Re-run a `failed` `queued_actions` row (6.34/6.37 retry semantics).

        Re-dispatches through the same route handlers used for the original
        request; sync and publish are themselves idempotent on retry
        (resumable download, re-read local artifact).
        """
        action = library.get_action(action_id)
        if action is None:
            raise HTTPException(status_code=404, detail=f"action not found: {action_id}")
        params = json.loads(action["params"] or "{}")
        kind = action["kind"]
        if kind == "sync_session":
            return sync_session_from_pi(
                params["session_id"], SyncSessionRequest(pi_host=params.get("host"))
            )
        if kind == "publish_model":
            return publish_model_to_pi(
                params["model_type"], PublishModelRequest(pi_host=params.get("host"))
            )
        raise HTTPException(status_code=400, detail=f"action kind not retryable: {kind}")

    @app.get("/companion/sync/state")
    def companion_sync_state(_auth: None = Depends(authorize)) -> dict[str, Any]:
        sessions = library.list_sessions()
        actions = library.list_actions()
        queued = [action for action in actions if action.get("status") == "queued"]
        return {
            "source_hosts": sorted({s.source_host for s in sessions if s.source_host}),
            "queued_actions": queued,
            "actions": actions,
            "publish_history": library.publish_history(),
            "pi": link.pi(),
        }

    if settings.discover and link.selection.read() is None and not settings.pi_host:
        link.start_discovery()
    return link


__all__ = ["CompanionLink", "DiscoveryState", "register_companion_link_routes"]
