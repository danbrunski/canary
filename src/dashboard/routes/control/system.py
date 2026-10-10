"""Daemon-level control routes: the sequence-detector backbone, vehicle profiles, UPS power,
preflight and version.

Part of ``/control/*``; every route here requires the control token
(router-level, R03.6).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from src.collection import tuning_profile, tx_gate, vin_read
from src.control.preflight import build_preflight
from src.control.sequence_detector import (
    SequenceDetectorController,
    SequenceDetectorUnavailableError,
)
from src.daemon.ups import (
    build_power_snapshot,
    read_keepalive_remaining,
    read_watchdog_status,
    write_keepalive,
)
from src.dashboard.routes.control.context import ControlContext, _require
from src.dashboard.routes.rom_library import vehicle_reading
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
    ProfileEdit,
    ProfileEditError,
    last_resolution,
    load_registry,
    parameter_schema,
    pending_restart,
    profile_overrides,
    revert_profile,
    update_profile,
    update_profile_name,
)
from src.vehicle.vin import inspect_vin

#: How long the VIN read waits for the collector to take the request: it is
#: served between polls, after any open Mode 22 window closes.
VIN_PICKUP_SECONDS = 5.0
#: vehicle_state values where the OBD collector deliberately stays silent or
#: nothing answers: the key is off, or no vehicle is on the port.
_KEY_OFF_STATES = {
    "parked": "the vehicle is parked (12 V on the port, every module asleep)",
    "engine_off": "the key is off (broadcast CAN only; the ECM does not answer OBD)",
    "no_data": "neither bus is fresh: the key is off or nothing is connected",
    "bench": "no vehicle on the OBD port (bench power)",
}
#: Where a profile edit takes effect, in the API response and the UI.
APPLIES_AT_RESTART = (
    "The daemon copied this profile into its configuration when it started, and every "
    "session manifest records that copy. Restart the daemon (scripts/restart_canary.sh, "
    "which refuses during a capture) for the change to reach the derived fuel, trip and "
    "power channels, the gear estimate, the dyno, tyre wear and the next session's manifest. "
    "The running session keeps the values it started with. Offline tools that resolve the "
    "profile themselves (MCP analysis tools, scripts) read the new values at once."
)
APPLIES_WHEN_ACTIVE = "Takes effect when this vehicle is next the active profile."


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

    # The active profile's slug is the scoped storage path's final component
    # (see src.vehicle.profiles.scope_config).
    active_slug = config.collection.storage_path.name

    def _pending(profiles: list[Any]) -> list[str]:
        active = next((p for p in profiles if p.slug == active_slug), None)
        return [] if active is None else pending_restart(config.vehicle, active)

    def _edit_payload(edit: ProfileEdit) -> dict[str, Any]:
        active = edit.profile.slug == active_slug
        pending = pending_restart(config.vehicle, edit.profile) if active else []
        return {
            "profile": edit.profile.to_dict(),
            "changed": list(edit.changed),
            "warnings": list(edit.warnings),
            "vin_resolution": edit.vin_resolution,
            "active": active,
            "pending_restart": pending,
            "restart_required": bool(pending),
            "applies": APPLIES_AT_RESTART if active else APPLIES_WHEN_ACTIVE,
        }

    @router.get("/control/profiles")
    def get_control_profiles() -> dict[str, Any]:
        profiles = load_registry(config.profiles)
        overrides = profile_overrides(config.profiles)
        pending = _pending(profiles)
        resolution = last_resolution()
        return {
            "profiles": [
                {
                    **profile.to_dict(),
                    "tracked": overrides.get(profile.slug, {"tracked": False})["tracked"],
                    "overrides": overrides.get(profile.slug, {"fields": []})["fields"],
                }
                for profile in profiles
            ],
            "active_slug": active_slug,
            "count": len(profiles),
            # What the daemon runs on: the active profile as it was at startup.
            "in_use": config.vehicle.model_dump(mode="json"),
            "pending_restart": pending,
            "restart_required": bool(pending),
            "applies": APPLIES_AT_RESTART,
            "resolution": resolution.to_dict() if resolution is not None else None,
            "parameter_fields": parameter_schema(),
        }

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
        payload = _edit_payload(edit)
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
        payload = _edit_payload(edit)
        publish_control_event("profile.revert", "ok", payload)
        return payload

    def _vin_refusal() -> vin_read.VinReadOutcome | None:
        """The named reason not to ask the ECU for its VIN now, if there is one."""
        if config.collection.obd_source != "canable":
            return vin_read.VinReadOutcome(
                None,
                "unsupported",
                "reading the VIN needs collection.obd_source=canable "
                f"(currently {config.collection.obd_source!r}); type it in instead",
            )
        if tx_gate.passive_armed():
            return vin_read.VinReadOutcome(
                None,
                "passive_armed",
                "passive armed: CANary is not transmitting; disarm it to read the VIN",
            )
        owner = tx_gate.ecm_session_owner()
        if owner != "can_diagnostics":
            return vin_read.VinReadOutcome(
                None,
                "ecm_held",
                f"another tool holds the ECM ({owner}); CANary does not transmit to it until "
                "that ends",
            )
        if tuning_profile.is_paused():
            return vin_read.VinReadOutcome(
                None,
                "capture_paused",
                "the capture is paused: the OBD collector is not polling, so it does not "
                "transmit the VIN request either; resume the capture to read the VIN",
            )
        reading = vehicle_reading(
            config,
            live_source=ctx.live_source,
            replay_controller=ctx.replay_controller,
        )
        if reading.state == "replay":
            return vin_read.VinReadOutcome(
                None,
                "replay",
                "a replay is running: the vehicle on the dashboard is a stored session",
            )
        if reading.state in _KEY_OFF_STATES:
            return vin_read.VinReadOutcome(
                None,
                "key_off",
                f"key off: {_KEY_OFF_STATES[reading.state]} (vehicle_state "
                f"{reading.state}); turn the key on, or type the VIN in",
            )
        return None

    @router.post("/control/vin/read")
    def post_control_vin_read() -> dict[str, Any]:
        """Read the connected vehicle's VIN (Mode 09 0x02) through the running
        collector. Returned for the operator to confirm; never saved here."""
        outcome = _vin_refusal() or vin_read.request_vin(
            timeout_seconds=config.profiles.vin_probe_timeout_seconds,
            pickup_seconds=VIN_PICKUP_SECONDS,
        )
        result = outcome.to_dict()
        if outcome.vin is not None:
            check, problem = inspect_vin(outcome.vin)
            result["check"] = check.to_dict() if check is not None else None
            result["check_problem"] = problem
            holder = next((p for p in load_registry(config.profiles) if p.vin == outcome.vin), None)
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
