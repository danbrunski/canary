"""Daemon-level control routes: the sequence-detector backbone, vehicle profiles, UPS power,
preflight and version.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.collection import vin_read
from src.control.preflight import build_preflight
from src.control.profile_payloads import edit_payload, profiles_payload
from src.control.rom_backup import VehicleReading
from src.control.sequence_detector import (
    SequenceDetectorController,
    SequenceDetectorUnavailableError,
)
from src.control.vin_refusal import vin_refusal
from src.daemon.ups import (
    build_power_snapshot,
    read_keepalive_remaining,
    read_watchdog_status,
    write_keepalive,
)
from src.dashboard.readers import read_vehicle_reading
from src.dashboard.routes.control.context import ControlContext, _require
from src.dashboard.schemas import (
    ProfileNameUpdateRequest,
    ProfileRevertRequest,
    ProfileUpdateRequest,
    SequenceDetectorRequest,
    UpsKeepaliveRequest,
)
from src.dashboard.version import _canary_version, _git_sha
from src.storage.sessions import latest_session
from src.vehicle.profiles import (
    ActiveVinMismatchError,
    DuplicateVinError,
    NoTrackedProfileError,
    ProfileDeleteRefusedError,
    ProfileEditError,
    delete_profile,
    load_registry,
    profile_data_paths,
    restore_profile,
    revert_profile,
    update_profile,
    update_profile_name,
    vehicle_block_slug,
)
from src.vehicle.vin import inspect_vin

#: How long the VIN read waits for the collector to take the request: it is
#: served between polls, after any open Mode 22 window closes.
VIN_PICKUP_SECONDS = 5.0


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

    active_slug = config.profile_slug

    @router.get("/control/profiles")
    def get_control_profiles() -> dict[str, Any]:
        return profiles_payload(config, active_slug)

    @router.patch("/control/profiles/{slug}")
    def patch_control_profile(slug: str, request: ProfileUpdateRequest) -> dict[str, Any]:
        changes = {name: getattr(request, name) for name in request.model_fields_set}
        try:
            edit = update_profile(config.profiles, slug, changes, active_slug=active_slug)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no profile with slug {slug!r}") from exc
        except (DuplicateVinError, ActiveVinMismatchError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProfileEditError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = edit_payload(config, edit, active_slug)
        if edit.changed:
            publish_control_event("profile.update", "ok", payload)
        return payload

    @router.post("/control/profiles/{slug}/revert")
    def post_control_profile_revert(slug: str, request: ProfileRevertRequest) -> dict[str, Any]:
        try:
            edit = revert_profile(config.profiles, slug, request.fields)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no profile with slug {slug!r}") from exc
        except NoTrackedProfileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProfileEditError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = edit_payload(config, edit, active_slug)
        publish_control_event("profile.revert", "ok", payload)
        return payload

    @router.delete("/control/profiles/{slug}")
    def delete_control_profile(slug: str) -> dict[str, Any]:
        """Remove a profile from this machine's registry. A tracked profile is
        hidden here (a tombstone in the overlay) and stays in git; recorded data
        is never deleted, and ``data_left`` says where it is."""
        try:
            deletion = delete_profile(
                config.profiles,
                slug,
                active_slug=active_slug,
                fallback_slug=vehicle_block_slug(config),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no profile with slug {slug!r}") from exc
        except ProfileDeleteRefusedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = {
            "deleted": deletion.slug,
            "tracked": deletion.tracked,
            "data_left": [str(path) for path in profile_data_paths(config, slug)],
        }
        publish_control_event("profile.delete", "ok", payload)
        return payload

    @router.post("/control/profiles/{slug}/restore")
    def post_control_profile_restore(slug: str) -> dict[str, Any]:
        """Bring back a tracked profile this machine deleted, as the tracked file has it."""
        try:
            profile = restore_profile(config.profiles, slug)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"profile {slug!r} is not deleted on this machine"
            ) from exc
        payload = {"profile": profile.to_dict()}
        publish_control_event("profile.restore", "ok", payload)
        return payload

    def _vehicle_reading() -> VehicleReading:
        return read_vehicle_reading(
            config, live_source=ctx.live_source, replay_controller=ctx.replay_controller
        )

    @router.post("/control/vin/read")
    def post_control_vin_read() -> dict[str, Any]:
        """Read the connected vehicle's VIN (Mode 09 0x02) through the running
        collector. Returned for the operator to confirm; never saved here."""
        outcome = vin_refusal(config, _vehicle_reading) or vin_read.request_vin(
            timeout_seconds=config.profiles.vin_probe_timeout_seconds,
            pickup_seconds=VIN_PICKUP_SECONDS,
        )
        result = outcome.to_dict()
        if outcome.vin is not None:
            check, problem = inspect_vin(outcome.vin)
            result["check"] = check.to_dict() if check is not None else None
            result["check_problem"] = problem
            # A profile this machine hides still keys that VIN: the next start restores it.
            everyone = load_registry(config.profiles, include_deleted=True)
            holder = next((p for p in everyone if p.vin == outcome.vin), None)
            result["held_by"] = holder.slug if holder is not None else None
        payload = {"vin_read": result}
        publish_control_event("vin.read", "ok" if outcome.ok else str(outcome.refusal), payload)
        return payload

    @router.post("/control/profiles/{slug}/name")
    def post_control_profile_name(
        slug: str,
        request: ProfileNameUpdateRequest,
    ) -> dict[str, Any]:
        try:
            profile = update_profile_name(config.profiles, slug, request.name)
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
