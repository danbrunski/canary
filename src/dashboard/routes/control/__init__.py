"""The ``/control/*`` routes, one ``APIRouter`` per domain (R03.6).

``register_control_routes`` takes the one :class:`~src.dashboard.app_context.AppContext`
``create_app`` built (R29.6) and includes each domain's router. Auth is
router-level: every router is built by ``AppContext.router()``, whose one
dependency is the shared ``security.control_auth_dependency``, so a route cannot
be registered here without it (``tests/test_route_table_snapshot.py`` pins the
result).

Include order is registration order, which Starlette matches in; the snapshot
test also asserts no route is shadowed by an earlier one.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, FastAPI

from src.control import profile_capture as profile_capture_module
from src.dashboard.app_context import AppContext
from src.dashboard.routes.session_reads import build_session_read_router
from src.dashboard.status_errors import register_status_error_handler

from . import (
    brain,
    bus,
    captures,
    classification,
    custom_pids,
    diagnostics,
    feedback,
    jobs,
    models,
    network,
    profile_capture,
    replay,
    sessions,
    system,
    tires,
    tuning,
    update,
)

#: How often the capture-autostart tick classifies the vehicle; the one
#: definition is ``src.control.profile_capture`` (R03.5). Kept here because an
#: app built without an injected autostart reads it per tick (``create_app``),
#: which is how a test shortens it.
AUTOSTART_TICK_SECONDS = profile_capture_module.AUTOSTART_TICK_SECONDS


def _session_reads(ctx: AppContext) -> APIRouter:
    """The chart/GPS reads shared with the companion (R03.7)."""
    return build_session_read_router(lambda: ctx.config.collection.storage_path, ctx.require_auth)


#: Domain router builders in registration order.
_ROUTER_BUILDERS: tuple[Callable[[AppContext], APIRouter], ...] = (
    system.build_router,
    network.build_router,
    captures.build_router,
    replay.build_router,
    sessions.build_router,
    sessions.build_store_router,
    _session_reads,
    bus.build_router,
    diagnostics.build_router,
    profile_capture.build_router,
    classification.build_router,
    jobs.build_router,
    models.build_router,
    feedback.build_router,
    tuning.build_router,
    custom_pids.build_router,
    tires.build_router,
    brain.build_router,
    update.build_router,
)


def register_control_routes(app: FastAPI, ctx: AppContext) -> None:
    # A refusal any of these routes lets escape answers with its own status and body.
    register_status_error_handler(app)
    for build in _ROUTER_BUILDERS:
        app.include_router(build(ctx))


__all__ = [
    "AUTOSTART_TICK_SECONDS",
    "register_control_routes",
]
