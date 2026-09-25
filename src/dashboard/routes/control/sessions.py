"""Stored sessions: listing, metadata, pinning, deletion and export.

The chart and GPS-track reads the companion also serves are
:mod:`src.dashboard.routes.session_reads` (R03.7).

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import FileResponse

from src.companion.services import (
    delete_session_response,
    export_session_checksum_response,
    export_session_response,
    get_session_response,
    pin_session_response,
    sessions_list_response,
    unpin_session_response,
    update_session_metadata_response,
)
from src.control.sessions import (
    filter_sessions,
    list_sessions,
    summarize_session,
)
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import (
    SessionMetadataRequest,
    SessionPinRequest,
)


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    _resolve_session = ctx.resolve_session
    capture_controller = ctx.capture_controller
    config = ctx.config
    publish_control_event = ctx.publish_control_event
    session = ctx.session

    def _guard_active_capture(session_id: str) -> None:
        active = capture_controller.current_capture() if capture_controller is not None else None
        if active is not None and active.session_id == session_id:
            raise HTTPException(status_code=409, detail="cannot delete the active capture session")
        # R03.3: the writer's own directory too. Retention protects it with
        # `protect={storage_writer.paths.root}`; this route only guarded the
        # capture, so with no capture active the plain live session could be
        # rmtree'd under the writer.
        if session.is_writer_session(session_id):
            raise HTTPException(status_code=409, detail="cannot delete the session being written")

    @router.get("/control/sessions")
    def get_control_sessions(
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = Query(default=None, ge=1, le=1000),
        captures: bool = False,
    ) -> dict[str, Any]:
        sessions = filter_sessions(
            list_sessions(config.collection.storage_path, captures=captures),
            since=since,
            until=until,
            limit=limit,
        )
        return sessions_list_response([session.to_dict() for session in sessions])

    @router.get("/control/sessions/{session_id}")
    def get_control_session(
        session_id: str,
    ) -> dict[str, Any]:
        return get_session_response(
            session_id,
            resolve_and_serialize=lambda sid: summarize_session(_resolve_session(sid)).to_dict(),
        )

    @router.post("/control/sessions/{session_id}/metadata")
    def post_control_session_metadata(
        session_id: str,
        request: SessionMetadataRequest,
    ) -> dict[str, Any]:
        payload = update_session_metadata_response(
            session_id,
            request.metadata,
            merge=request.merge,
            resolve=_resolve_session,
        )
        publish_control_event("session.metadata", "ok", payload)
        return payload

    @router.post("/control/sessions/{session_id}/pin")
    def post_control_session_pin(
        session_id: str,
        request: SessionPinRequest,
    ) -> dict[str, Any]:
        payload = pin_session_response(
            session_id,
            reason=request.reason,
            extra_metadata=request.metadata,
            resolve=_resolve_session,
            serialize=lambda session: summarize_session(session).to_dict(),
            catalog_db=config.daemon.catalog_db,
        )
        publish_control_event("session.pin", "ok", payload)
        return payload

    @router.post("/control/sessions/{session_id}/unpin")
    def post_control_session_unpin(
        session_id: str,
    ) -> dict[str, Any]:
        payload = unpin_session_response(
            session_id,
            resolve=_resolve_session,
            serialize=lambda session: summarize_session(session).to_dict(),
            catalog_db=config.daemon.catalog_db,
        )
        publish_control_event("session.unpin", "ok", payload)
        return payload

    @router.delete("/control/sessions/{session_id}")
    def delete_control_session(
        session_id: str,
        force: bool = Query(default=False),
    ) -> dict[str, Any]:
        payload = delete_session_response(
            session_id,
            force=force,
            resolve=_resolve_session,
            guard=_guard_active_capture,
        )
        publish_control_event("session.delete", "ok", payload)
        return payload

    @router.get("/control/sessions/{session_id}/export")
    def export_control_session(
        session_id: str,
        background_tasks: BackgroundTasks,
    ) -> FileResponse:
        return export_session_response(
            session_id,
            background_tasks,
            resolve=_resolve_session,
            tmp_prefix="canary-",
        )

    @router.get("/control/sessions/{session_id}/export/checksum")
    def export_control_session_checksum(
        session_id: str,
    ) -> dict[str, Any]:
        """6.34: sha256 of what `/export` would produce right now, so the
        companion's sync can verify a downloaded archive without trusting
        the transport."""
        return export_session_checksum_response(session_id, resolve=_resolve_session)

    return router
