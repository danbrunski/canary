"""Labelled capture windows (``/control/captures``): start, tag and stop a window on the live
session.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.control.capture import (
    CaptureConflictError,
    NoActiveCaptureError,
    no_active_capture_detail,
)
from src.dashboard.routes.control.context import ControlContext
from src.dashboard.schemas import (
    CaptureStartRequest,
    CaptureStopRequest,
    CaptureTagRequest,
)


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    _controller = ctx.require_capture_controller
    publish_control_event = ctx.publish_control_event

    @router.post("/control/captures", status_code=201)
    def post_capture(
        request: CaptureStartRequest,
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            capture = controller.start_capture(
                purpose=request.purpose,
                protocol=request.protocol,
                vehicle_state=request.vehicle_state,
                fields=request.fields,
            )
        except CaptureConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        publish_control_event("capture.start", "ok", capture.to_dict())
        return {"capture": capture.to_dict()}

    @router.get("/control/captures/current")
    def get_current_capture() -> dict[str, Any]:
        """The active capture window, or an explicitly-named absence.

        A read, so "there is no window" is a legitimate answer and stays a
        200 — unlike the tag/stop siblings below, where it means a command
        did nothing and is a 409. But the bare ``{"capture": null}`` this
        used to return told a caller nothing about *which* kind of empty it
        was, and "a restart dropped the window" is the one that actually
        happens. ``active`` makes the check explicit and ``detail`` names
        the cause, with the same wording the 409s carry.
        """
        controller = _controller()
        capture = controller.current_capture()
        if capture is None:
            return {
                "capture": None,
                "active": False,
                "detail": no_active_capture_detail("there is nothing to tag or stop"),
            }
        return {"capture": capture.to_dict(), "active": True, "detail": None}

    @router.post("/control/captures/current/tags")
    def post_capture_tag(
        request: CaptureTagRequest,
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            tag = controller.add_tag(
                request.label,
                kind=request.kind,
                timestamp=request.timestamp,
                fields=request.fields,
            )
        except NoActiveCaptureError as exc:
            # 409, never a 200 with a null `tag`: "tagged" and "there was
            # nothing to tag" must not look alike to a caller that only
            # checks the status code. The detail is
            # `capture.no_active_capture_detail`, which names the restart as
            # the usual cause.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        capture = controller.current_capture()
        payload = {
            "tag": tag,
            "capture": None if capture is None else capture.to_dict(),
        }
        publish_control_event("capture.tag", "ok", payload)
        return payload

    @router.post("/control/captures/current/stop")
    def post_capture_stop(
        request: CaptureStopRequest,
    ) -> dict[str, Any]:
        controller = _controller()
        try:
            capture = controller.stop_capture(reason=request.reason, fields=request.fields)
        except NoActiveCaptureError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        publish_control_event("capture.stop", "ok", capture.to_dict())
        return {"capture": capture.to_dict()}

    return router
