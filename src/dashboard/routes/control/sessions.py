"""Stored sessions: listing, metadata, pinning, deletion and export.

The per-session routes (read, metadata, pin, unpin, delete, export) and the
chart and GPS-track reads are the ones the companion also serves:
:mod:`src.dashboard.routes.stored_sessions` and
:mod:`src.dashboard.routes.session_reads` (R03.7). This module adds the
daemon's store (its storage root, the active-capture delete guard, the audit
publish) and the daemon-only session list.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.dashboard.routes.control.context import ControlContext
from src.dashboard.routes.stored_sessions import (
    SessionStore,
    build_stored_session_router,
    sessions_payload,
)
from src.storage.sessions import filter_sessions, list_sessions, summarize_session


def _guard_active_capture(ctx: ControlContext, session_id: str) -> None:
    capture_controller = ctx.capture_controller
    active = capture_controller.current_capture() if capture_controller is not None else None
    if active is not None and active.session_id == session_id:
        raise HTTPException(status_code=409, detail="cannot delete the active capture session")
    # R03.3: the writer's own directory too. Retention protects it with
    # `protect={storage_writer.paths.root}`; this route only guarded the
    # capture, so with no capture active the plain live session could be
    # rmtree'd under the writer.
    if ctx.session.is_writer_session(session_id):
        raise HTTPException(status_code=409, detail="cannot delete the session being written")


def daemon_session_store(ctx: ControlContext) -> SessionStore:
    """The daemon's :class:`SessionStore`: its storage root, the
    active-capture delete guard and the audit publish."""
    return SessionStore(
        resolve=ctx.resolve_session,
        describe=lambda sid: summarize_session(ctx.resolve_session(sid)).to_dict(),
        export_prefix="canary-",
        guard_delete=lambda sid: _guard_active_capture(ctx, sid),
        on_write=lambda event, payload: ctx.publish_control_event(event, "ok", payload),
        catalog_db=ctx.config.daemon.catalog_db,
    )


def build_store_router(ctx: ControlContext) -> APIRouter:
    """One session's read/metadata/pin/unpin/delete/export: the routes the
    companion also serves, over this daemon's storage root (R03.7)."""
    return build_stored_session_router(daemon_session_store(ctx), ctx.require_auth)


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config

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
        return sessions_payload([session.to_dict() for session in sessions])

    return router
