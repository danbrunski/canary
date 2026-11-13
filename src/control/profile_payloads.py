"""What ``/control/profiles`` answers with, built from the registry (R29.7).

The shaping lived in closures in ``dashboard/routes/control/system.py``, over
``config`` and the active profile's slug, so a test of "which profiles does the
list call deletable" had to build the whole app. These are plain functions of
``(config, active_slug)``; the routes call them and add the control event.

``active_slug`` is the active profile's slug: the scoped storage path's final
component (see :func:`src.vehicle.profiles.scope_config`). It is a parameter,
not read here, so a test names the profile it is asking about.
"""

from __future__ import annotations

from typing import Any

from src.config import CanaryConfig
from src.vehicle.profiles import (
    ProfileEdit,
    delete_refusal,
    deleted_profiles,
    last_resolution,
    load_registry,
    parameter_schema,
    pending_restart,
    profile_overrides,
    vehicle_block_slug,
)

#: Where a profile edit takes effect, in the API response and the UI.
APPLIES_AT_RESTART = (
    "The daemon copied this profile into its configuration when it started, and every "
    "session manifest records that copy. Restart the daemon (scripts/restart_canary.sh, "
    "which refuses during a capture) for the change to reach the derived fuel, trip and "
    "power channels, the gear estimate, the dyno, tire wear and the next session's manifest. "
    "The running session keeps the values it started with. Offline tools that resolve the "
    "profile themselves (MCP analysis tools, scripts) read the new values at once."
)
APPLIES_WHEN_ACTIVE = "Takes effect when this vehicle is next the active profile."


def active_pending_restart(
    config: CanaryConfig, profiles: list[Any], active_slug: str
) -> list[str]:
    """The active profile's fields that differ from what the daemon started with."""
    active = next((p for p in profiles if p.slug == active_slug), None)
    return [] if active is None else pending_restart(config.vehicle, active)


def edit_payload(config: CanaryConfig, edit: ProfileEdit, active_slug: str) -> dict[str, Any]:
    """The answer to a profile update or revert."""
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


def profile_delete_refusal(config: CanaryConfig, slug: str, active_slug: str) -> str | None:
    """Why Delete would refuse now for ``slug``, else ``None``."""
    return delete_refusal(slug, active_slug=active_slug, fallback_slug=vehicle_block_slug(config))


def profiles_payload(config: CanaryConfig, active_slug: str) -> dict[str, Any]:
    """The registry as ``GET /control/profiles`` lists it."""
    profiles = load_registry(config.profiles)
    overrides = profile_overrides(config.profiles)
    pending = active_pending_restart(config, profiles, active_slug)
    resolution = last_resolution()
    return {
        "profiles": [
            {
                **profile.to_dict(),
                "tracked": overrides.get(profile.slug, {"tracked": False})["tracked"],
                "overrides": overrides.get(profile.slug, {"fields": []})["fields"],
                # Why Delete would refuse now (the reason the button shows), else null.
                "delete_refusal": profile_delete_refusal(config, profile.slug, active_slug),
            }
            for profile in profiles
        ],
        # Tracked profiles this machine has deleted: hidden here, in git for
        # everyone else, restorable.
        "deleted": [
            {
                "slug": profile.slug,
                "name": profile.name,
                "make": profile.make,
                "model": profile.model,
                "year": profile.year,
            }
            for profile in deleted_profiles(config.profiles)
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


__all__ = [
    "APPLIES_AT_RESTART",
    "APPLIES_WHEN_ACTIVE",
    "active_pending_restart",
    "edit_payload",
    "profile_delete_refusal",
    "profiles_payload",
]
