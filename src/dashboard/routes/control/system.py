"""Daemon-level control routes: the sequence-detector backbone, vehicle profiles, UPS power,
preflight and version.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.control.preflight import build_preflight
from src.control.sequence_detector import (
    SequenceDetectorController,
    SequenceDetectorUnavailableError,
)
from src.control.sessions import (
    latest_session,
)
from src.dashboard.routes.control.context import ControlContext, _require
from src.dashboard.schemas import (
    ProfileNameUpdateRequest,
    SequenceDetectorRequest,
    UpsKeepaliveRequest,
)
from src.dashboard.ups import (
    build_power_snapshot,
    read_keepalive_remaining,
    read_watchdog_status,
    write_keepalive,
)
from src.dashboard.version import _canary_version, _git_sha
from src.profiles import load_registry, update_profile_name


def build_router(ctx: ControlContext) -> APIRouter:
    router = ctx.router()
    capture_controller = ctx.capture_controller
    config = ctx.config
    publish_control_event = ctx.publish_control_event
    resolved_control_token = ctx.resolved_control_token
    sequence_detector_controller = ctx.sequence_detector_controller
    ups_keepalive_path = ctx.ups_keepalive_path
    ups_status_path = ctx.ups_status_path

    def _sequence_detector() -> SequenceDetectorController:
        return _require(
            sequence_detector_controller,
            "sequence-detector",
            detail="sequence-detector control is not available",
        )

    @router.get("/control/sequence-detector")
    def get_sequence_detector() -> dict[str, Any]:
        return {"sequence_detector": _sequence_detector().status().to_dict()}

    @router.post("/control/sequence-detector")
    def post_sequence_detector(
        request: SequenceDetectorRequest,
    ) -> dict[str, Any]:
        controller = _sequence_detector()
        try:
            status = controller.switch(request.backbone)
        except SequenceDetectorUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish_control_event("sequence_detector.switch", "ok", status.to_dict())
        return {"sequence_detector": status.to_dict()}

    @router.get("/control/profiles")
    def get_control_profiles() -> dict[str, Any]:
        # The active profile's slug is the scoped storage path's final component
        # (see src.profiles.scope_config).
        active_slug = config.collection.storage_path.name
        profiles = load_registry(config.profiles.registry_path)
        return {
            "profiles": [profile.to_dict() for profile in profiles],
            "active_slug": active_slug,
            "count": len(profiles),
        }

    @router.post("/control/profiles/{slug}/name")
    def post_control_profile_name(
        slug: str,
        request: ProfileNameUpdateRequest,
    ) -> dict[str, Any]:
        try:
            profile = update_profile_name(config.profiles.registry_path, slug, request.name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no profile with slug {slug!r}") from exc
        payload = {"profile": profile.to_dict()}
        publish_control_event("profile.rename", "ok", payload)
        return payload

    @router.get("/control/power")
    def get_control_power() -> dict[str, Any]:
        status = read_watchdog_status(ups_status_path)
        keepalive_remaining_s = read_keepalive_remaining(ups_keepalive_path)
        return {"power": build_power_snapshot(status, keepalive_remaining_s)}

    @router.post("/control/power/keepalive")
    def post_control_power_keepalive(
        request: UpsKeepaliveRequest,
    ) -> dict[str, Any]:
        try:
            expiry = write_keepalive(request.minutes, ups_keepalive_path)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"UPS keep-alive file not writable ({ups_keepalive_path}): {exc}",
            ) from exc
        status = read_watchdog_status(ups_status_path)
        keepalive_remaining_s = read_keepalive_remaining(ups_keepalive_path)
        payload = {
            "power": build_power_snapshot(status, keepalive_remaining_s),
            "expiry": expiry,
        }
        publish_control_event("power.keepalive", "ok", payload)
        return payload

    @router.get("/control/preflight")
    def get_control_preflight() -> dict[str, Any]:
        current_session = (
            capture_controller.active.session_path
            if capture_controller is not None and capture_controller.active is not None
            else latest_session(config.collection.storage_path)
        )
        return build_preflight(
            config,
            control_token_configured=bool(resolved_control_token),
            current_session=current_session,
        )

    @router.get("/control/version")
    def get_control_version() -> dict[str, Any]:
        return {
            "api_version": 1,
            "package_version": _canary_version(),
            "git_sha": _git_sha(),
            "config_token_env": config.daemon.control_token_env,
        }

    return router
