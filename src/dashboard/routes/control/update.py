"""Software update from the UI (phone-ui P3.2).

``GET /control/update`` reads what is installed, the last check, and the root
runner's status; ``POST /control/update`` files a ``check`` or ``update``
request for ``canary-update.path``. The daemon never updates anything itself
(it has no privileges to): see :mod:`src.control.updater` for the contract.

Every refusal happens here, before a request exists: a capture or ECU backup
an update would end, a run already in progress, no default route, and a
trigger that is not installed. ``canary-update`` keeps its own refusals as
well; these are the ones a phone can be told about at once.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from src.control.updater import (
    UpdatePaths,
    active_work,
    has_default_route,
    in_progress,
    new_request,
    pending_request,
    read_status,
    write_request,
)
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.version import _canary_version, _git_sha


class UpdateActionRequest(BaseModel):
    """``check`` fetches and compares; ``update`` runs ``canary-update``.
    ``full`` (update only) runs every deploy step regardless of the diff."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["check", "update"]
    full: bool = False


def update_snapshot(paths: UpdatePaths, *, now: float | None = None) -> dict[str, Any]:
    """The ``update`` payload both routes return."""
    current = time.time() if now is None else now
    status = read_status(paths.status, now=current)
    last_check = status.pop("last_check", None)
    return {
        "installed": {"version": _canary_version(), "sha": _git_sha()},
        "status": status,
        "last_check": last_check,
        "pending_request": pending_request(paths, now=current),
        "trigger_installed": paths.trigger_unit.exists(),
        "internet": {"default_route": has_default_route(paths.proc_net)},
        # What an update would end right now; the update action refuses on it.
        "blocked_by": active_work(),
    }


def _refusal(paths: UpdatePaths, action: str, now: float) -> tuple[int, str] | None:
    """``(status_code, detail)`` when this request must not be filed."""
    if not paths.trigger_unit.exists():
        return 503, (
            f"{paths.trigger_unit.name} is not installed, so nothing would pick the request "
            "up. Run 'sudo canary-update' once from a shell; install_pi.sh installs it."
        )
    status = read_status(paths.status, now=now)
    if in_progress(status):
        phase = status.get("phase") or "starting"
        return 409, f"an update {status.get('action')} is already running ({phase})"
    pending = pending_request(paths, now=now)
    if pending is not None and not pending["stuck"]:
        return 409, (
            f"a {pending['action']} request is already waiting for canary-update "
            f"({pending['age_s']:.0f} s)"
        )
    if action == "update":
        blockers = active_work()
        if blockers:
            return 409, "refused: " + " ".join(blockers)
    if has_default_route(paths.proc_net) is False:
        return 409, (
            "no internet: the Pi has no default route. Join a WiFi network with internet "
            "access, then try again."
        )
    return None


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    paths = ctx.update_paths

    @router.get("/control/update")
    def get_update() -> dict[str, Any]:
        return {"update": update_snapshot(paths)}

    @router.post("/control/update", status_code=202)
    def post_update(request: UpdateActionRequest) -> dict[str, Any]:
        if request.full and request.action != "update":
            raise HTTPException(status_code=422, detail="'full' applies only to the update action")
        now = time.time()
        refusal = _refusal(paths, request.action, now)
        if refusal is not None:
            raise HTTPException(status_code=refusal[0], detail=refusal[1])
        filed = new_request(request.action, full=request.full, now=now)
        try:
            write_request(filed, paths.request)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"update request not writable ({paths.request}): {exc}",
            ) from exc
        payload = {"request": filed.to_dict(), "update": update_snapshot(paths, now=now)}
        ctx.publish_control_event("update.request", "ok", filed.to_dict())
        return payload

    return router
