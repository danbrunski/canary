"""Runtime replay of a stored session through the live pipeline (``/control/replay``).

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.control.replay_control import (
    ReplayConflictError,
    ReplayController,
    ReplayNotFoundError,
    ReplayNotRunningError,
    ReplayRefusedError,
)
from src.dashboard.routes.control.context import ControlContext, _require
from src.dashboard.schemas import (
    ReplayStartRequest,
)
from src.dashboard.status import active_session_dir, derive_vehicle_state_from_storage


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    config = ctx.config
    publish_control_event = ctx.publish_control_event
    replay_controller = ctx.replay_controller
    storage_writer = ctx.storage_writer

    def _replay() -> ReplayController:
        return _require(replay_controller, "replay")

    @router.post("/control/replay", status_code=201)
    async def post_replay_start(
        request: ReplayStartRequest,
    ) -> dict[str, Any]:
        controller = _replay()
        # 7.2 (operator-approved, see OVERNIGHT-QUEUE.md "Replay vs live"):
        # replay is a bench/desk tool, not something to run while actually
        # driving — refuse outright rather than gating live collectors out
        # of the queue while a replay is active.
        # The writer's live directory, not the newest-session search: capture
        # sessions are `_`-prefixed and invisible to that search, so during a
        # capture this guard would read a stale session, see `no_data`, and
        # let a replay start on a running engine.
        vehicle_state = derive_vehicle_state_from_storage(
            config.collection.storage_path,
            replay_controller=controller,
            session_dir=active_session_dir(storage_writer),
        )
        if vehicle_state.state == "engine_running":
            raise HTTPException(
                status_code=409,
                detail=(
                    "refusing to start replay: vehicle_state=engine_running "
                    "(replay is a bench/desk tool; wait for ignition_only/engine_off "
                    "or key out first)"
                ),
            )
        try:
            status = await controller.start(
                request.session_id,
                speed=request.speed,
                loop=request.loop,
            )
        except ReplayConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ReplayNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ReplayRefusedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        publish_control_event("replay.start", "ok", status.to_dict())
        return {"replay": status.to_dict()}

    @router.post("/control/replay/stop")
    async def post_replay_stop() -> dict[str, Any]:
        controller = _replay()
        try:
            status = await controller.stop()
        except ReplayNotRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        publish_control_event("replay.stop", "ok", status.to_dict())
        return {"replay": status.to_dict()}

    @router.get("/control/replay")
    def get_replay() -> dict[str, Any]:
        controller = _replay()
        return {"replay": controller.status().to_dict()}

    return router
