"""Capture profiles and the profile-capture lifecycle (start/stop/pause/autostart/quality); the
lifecycle itself is :mod:`src.control.profile_capture`.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.collection import tuning_profile
from src.control.capture_autostart import CaptureAutostartRule
from src.control.capture_finalize import (
    CaptureQualityUnavailableError,
    recompute_quality_off_loop,
)
from src.control.profile_capture import (
    ProfileCaptureError,
    ProfileCaptureStart,
)
from src.dashboard.routes.control.context import ControlContext, SessionDir
from src.dashboard.schemas import (
    CaptureAutostartArmRequest,
    CapturePauseRequest,
    CaptureProfileSaveRequest,
    CaptureProfileStartRequest,
)
from src.features.capture_inputs import model_input_warnings


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    _resolve_session = ctx.resolve_session
    autostart = ctx.autostart
    profile_store = ctx.profile_store
    publish_control_event = ctx.publish_control_event
    session = ctx.session

    @router.get("/control/capture/profiles")
    def get_capture_profiles() -> dict[str, Any]:
        catalog = profile_store.load()
        return {
            "schema_version": catalog.schema_version,
            "profiles": [profile.model_dump(mode="json") for profile in catalog.profiles],
            "resolved_plans": {
                profile.name: profile_store.resolved_plan(profile) for profile in catalog.profiles
            },
        }

    @router.put("/control/capture/profiles")
    def put_capture_profile(
        request: CaptureProfileSaveRequest,
    ) -> dict[str, Any]:
        try:
            profile_store.save(request.profile, previous_name=request.previous_name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if request.profile.name == "default" and not tuning_profile.is_active():
            tuning_profile.activate(request.profile, source="capture-profile-save")
        warnings = model_input_warnings(request.profile, profile_store.behavior_models_path)
        payload = {"profile": request.profile.model_dump(mode="json"), "warnings": warnings}
        publish_control_event("capture_profile.save", "ok", payload)
        return payload

    @router.delete("/control/capture/profiles/{name}")
    def delete_capture_profile(
        name: str,
    ) -> dict[str, Any]:
        # Normalize the same way the store does, so a case variant cannot slip
        # past the guard and delete the profile that is currently running.
        if tuning_profile.active_profile().name == name.strip().lower():
            raise HTTPException(status_code=409, detail="cannot delete the active capture profile")
        try:
            profile_store.delete(name)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"capture profile not found: {name}"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = {"deleted": name}
        publish_control_event("capture_profile.delete", "ok", payload)
        return payload

    @router.post("/control/capture")
    async def start_profile_capture(
        request: CaptureProfileStartRequest,
    ) -> dict[str, Any]:
        try:
            payload = await session.start(
                ProfileCaptureStart(
                    profile=request.profile,
                    tune_revision=request.tune_revision,
                    rate_hz=request.rate_hz,
                    bus_budget_requests_per_second=request.bus_budget_requests_per_second,
                    bus_budget_ttl_seconds=request.bus_budget_ttl_seconds,
                )
            )
        except ProfileCaptureError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        if not payload.get("idempotent"):
            publish_control_event("capture_profile.start", "ok", payload)
        return payload

    @router.post("/control/capture/autostart")
    async def arm_capture_autostart(
        request: CaptureAutostartArmRequest,
    ) -> dict[str, Any]:
        """Arm a capture the daemon starts when the vehicle qualifies.

        Nothing starts here. The daemon starts the capture on the first tick
        the engine is running at or above ``coolant_min_c``, and stops it
        when the engine stops — the operator is driving and cannot do either.
        """
        try:
            payload = autostart.arm(
                CaptureAutostartRule(
                    profile=request.profile,
                    coolant_min_c=request.coolant_min_c,
                    rate_hz=request.rate_hz,
                    bus_budget_requests_per_second=request.bus_budget_requests_per_second,
                    tune_revision=request.tune_revision,
                    start_after_ticks=request.start_after_ticks,
                    stop_after_seconds=request.stop_after_seconds,
                ),
                publish=publish_control_event,
            )
        except ProfileCaptureError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        publish_control_event("capture_autostart.arm", "ok", payload)
        return payload

    @router.get("/control/capture/autostart")
    def get_capture_autostart() -> dict[str, Any]:
        return autostart.snapshot()

    @router.delete("/control/capture/autostart")
    def disarm_capture_autostart() -> dict[str, Any]:
        """Disarm. A capture already running is left running, by design:
        disarming is "stop deciding for me", not "throw away my data"."""
        payload = autostart.disarm()
        publish_control_event("capture_autostart.disarm", "ok", payload)
        return payload

    @router.post("/control/capture/pause")
    def pause_profile_capture(
        request: CapturePauseRequest,
    ) -> dict[str, Any]:
        """Suspend or resume polling while keeping the capture session open."""
        try:
            payload = session.pause(request.paused)
        except ProfileCaptureError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        publish_control_event(
            "capture_profile.pause" if request.paused else "capture_profile.resume", "ok", payload
        )
        return payload

    @router.delete("/control/capture")
    @router.post("/control/capture/stop")
    async def stop_profile_capture() -> dict[str, Any]:
        try:
            payload = await session.stop("operator_stop")
        except ProfileCaptureError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        if not payload.get("idempotent"):
            publish_control_event("capture_profile.stop", "ok", payload)
        return payload

    @router.get("/control/capture/quality")
    async def get_live_capture_quality() -> dict[str, Any]:
        """The active capture scored on what has been persisted so far.

        Same evaluator and same tuple arithmetic as the stop-time sidecar
        (``scope: live`` rather than ``stop``), so the arm-time check the MCP
        tool runs cannot disagree with the verdict written at stop. Flushes
        the writer first so the rows read back include the last few seconds.
        """
        try:
            quality = await session.live_quality()
        except ProfileCaptureError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        except CaptureQualityUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        result: dict[str, Any] = quality.to_dict()
        return result

    @router.get("/control/capture/quality/{session_id}")
    async def get_recomputed_capture_quality(
        session_id: str,
        session_dir: SessionDir,
    ) -> dict[str, Any]:
        """Recompute a historic capture's coverage and tuple yield from its rows.

        Never rewrites the session: the stored sidecar (any version) is
        returned beside the recomputation under ``stored``, with its scope
        explicit. Loss counters are unknown unless the session recorded
        epochs (``capture_epoch.json`` / a v2 sidecar).
        """
        from src.analysis.capture_coverage import read_quality_with_scope

        try:
            # A child process, not this route's threadpool: the row read-back
            # holds the GIL and would starve the daemon's loop (2026-09-17).
            quality = await recompute_quality_off_loop(session_dir)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CaptureQualityUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "session_id": session_id,
            "recomputed": quality.to_dict(),
            "stored": read_quality_with_scope(session_dir),
        }

    return router
