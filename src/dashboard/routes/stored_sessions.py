"""Stored-session routes both apps serve: one session's read, metadata, pin,
unpin, delete, export and export checksum (R03.7).

The daemon (``/control/sessions/{id}…`` over its storage root) and the laptop
companion (the same paths over its synced library) registered these seven
routes twice, each calling a hook-parameterized ``companion.services.*_response``
function. The routes are declared once here; what genuinely differs between
the two apps is a :class:`SessionStore` — where a session id resolves, how a
session is described, and the one side effect each app adds around a write:

* the companion re-indexes a written session into its library (and its
  metadata response carries the re-indexed ``session``), and drops a deleted
  one from the index;
* the daemon refuses to delete the session being captured or written, and
  publishes every write on its audit bus.

The session *list* stays per app: the daemon's filters (``since``/``until``/
``limit``/``captures``) have no counterpart over the companion's index.
"""

# No `from __future__ import annotations`, as in `session_reads`: FastAPI
# resolves these route signatures when they are declared.
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from src.artifact_hash import sha256_file
from src.dashboard.schemas import SessionMetadataRequest, SessionPinRequest
from src.storage.sessions import (
    delete_session,
    pin_session,
    summarize_session,
    unpin_session,
    write_session_metadata,
    write_session_zip,
)


@dataclass(frozen=True)
class SessionStore:
    """Where an app's stored sessions live, and what it does around a write."""

    #: Session id -> its directory; raises ``HTTPException(404)`` when unknown.
    resolve: Callable[[str], Path]
    #: Session id -> the ``session`` object ``GET …/{id}`` returns.
    describe: Callable[[str], dict[str, Any]]
    #: Prefix of the temporary zip ``…/export`` streams from.
    export_prefix: str
    #: The companion's library re-index of a just-written session. When set it
    #: serializes the session after pin/unpin, and the metadata response
    #: carries its result as ``session``; unset, a written session is
    #: re-summarized from disk and the metadata response has no ``session``.
    reindex: Callable[[Path], dict[str, Any]] | None = None
    #: Refuses a delete (``HTTPException``) before any file is touched.
    guard_delete: Callable[[str], None] | None = None
    #: Runs after the files are gone (the companion's index drop).
    after_delete: Callable[[str], None] | None = None
    #: ``(event, payload)`` after every successful write — the daemon's audit
    #: publish (``session.metadata`` / ``.pin`` / ``.unpin`` / ``.delete``).
    on_write: Callable[[str, dict[str, Any]], None] | None = None
    #: The session catalog pin state is mirrored into (daemon only).
    catalog_db: Path | None = None

    def serialize_written(self, path: Path) -> dict[str, Any]:
        if self.reindex is not None:
            return self.reindex(path)
        return summarize_session(path).to_dict()

    def written(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.on_write is not None:
            self.on_write(event, payload)
        return payload


def sessions_payload(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``GET /control/sessions`` body both apps return."""
    return {"sessions": sessions, "count": len(sessions)}


def session_export_checksum(session_id: str, path: Path) -> dict[str, Any]:
    """sha256 of what ``…/export`` would stream for ``path`` right now (6.34).

    ``write_session_zip`` is a deterministic function of the session's files,
    so zipping to a throwaway file and hashing it gives the digest a
    same-moment export produces, without keeping a second copy. The
    companion's sync checks its download against it.
    """
    tmp = tempfile.NamedTemporaryFile(prefix="canary-checksum-", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    try:
        write_session_zip(path, tmp_path)
        digest = sha256_file(tmp_path)
        size_bytes = tmp_path.stat().st_size
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"session_id": session_id, "sha256": digest, "size_bytes": size_bytes}


def build_stored_session_router(store: SessionStore, auth: Callable[..., Any]) -> APIRouter:
    """The seven per-session routes over ``store``, every one behind ``auth``."""
    router = APIRouter(dependencies=[Depends(auth)])

    @router.get("/control/sessions/{session_id}")
    def get_stored_session(session_id: str) -> dict[str, Any]:
        return {"session": store.describe(session_id)}

    @router.post("/control/sessions/{session_id}/metadata")
    def post_stored_session_metadata(
        session_id: str, request: SessionMetadataRequest
    ) -> dict[str, Any]:
        path = store.resolve(session_id)
        written = write_session_metadata(path, request.metadata, merge=request.merge)
        payload: dict[str, Any] = {"session_id": session_id, "metadata": written}
        if store.reindex is not None:
            payload["session"] = store.reindex(path)
        return store.written("session.metadata", payload)

    @router.post("/control/sessions/{session_id}/pin")
    def post_stored_session_pin(session_id: str, request: SessionPinRequest) -> dict[str, Any]:
        path = store.resolve(session_id)
        metadata = pin_session(path, reason=request.reason, catalog_db=store.catalog_db)
        if request.metadata:
            metadata = write_session_metadata(path, request.metadata)
        return store.written(
            "session.pin", {"session": store.serialize_written(path), "metadata": metadata}
        )

    @router.post("/control/sessions/{session_id}/unpin")
    def post_stored_session_unpin(session_id: str) -> dict[str, Any]:
        path = store.resolve(session_id)
        metadata = unpin_session(path, catalog_db=store.catalog_db)
        return store.written(
            "session.unpin", {"session": store.serialize_written(path), "metadata": metadata}
        )

    @router.delete("/control/sessions/{session_id}")
    def delete_stored_session(
        session_id: str, force: bool = Query(default=False)
    ) -> dict[str, Any]:
        path = store.resolve(session_id)
        if store.guard_delete is not None:
            store.guard_delete(session_id)
        try:
            delete_session(path, force=force)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if store.after_delete is not None:
            store.after_delete(session_id)
        return store.written(
            "session.delete", {"session_id": session_id, "deleted": True, "force": force}
        )

    @router.get("/control/sessions/{session_id}/export")
    def export_stored_session(session_id: str, background_tasks: BackgroundTasks) -> FileResponse:
        path = store.resolve(session_id)
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"{store.export_prefix}{session_id}-", suffix=".zip", delete=False
        )
        tmp_path = Path(tmp.name)
        tmp.close()
        write_session_zip(path, tmp_path)
        background_tasks.add_task(tmp_path.unlink, missing_ok=True)
        return FileResponse(tmp_path, media_type="application/zip", filename=f"{session_id}.zip")

    @router.get("/control/sessions/{session_id}/export/checksum")
    def export_stored_session_checksum(session_id: str) -> dict[str, Any]:
        """6.34: sha256 of what ``/export`` would produce right now, so the
        companion's sync can verify a downloaded archive without trusting
        the transport."""
        return session_export_checksum(session_id, store.resolve(session_id))

    return router


__all__ = [
    "SessionStore",
    "build_stored_session_router",
    "session_export_checksum",
    "sessions_payload",
]
