"""R29.7: what ``/control/profiles`` and the profile edits answer, without an app.

The shaping was closures in ``dashboard/routes/control/system.py`` over ``config``
and the active slug. These are :mod:`src.control.profile_payloads` functions of
``(config, active_slug)``; ``test_profile_edit`` / ``test_profile_delete`` still
drive the same payloads through HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from src.config import CanaryConfig, ProfilesConfig, VehicleParameters
from src.control.profile_payloads import (
    APPLIES_AT_RESTART,
    APPLIES_WHEN_ACTIVE,
    active_pending_restart,
    edit_payload,
    profile_delete_refusal,
    profiles_payload,
)
from src.paths import REPO_ROOT
from src.vehicle.profiles import (
    ProfileEdit,
    ProfileResolution,
    VehicleProfile,
    load_registry,
    parameter_schema,
    record_resolution,
    save_registry,
    scope_config,
)

NA_VIN = "1M8GDM9AXKP042788"


@pytest.fixture(autouse=True)
def _no_resolution() -> Iterator[None]:
    record_resolution(None)
    yield
    record_resolution(None)


def _parameters(**overrides: Any) -> VehicleParameters:
    base: dict[str, Any] = {
        "mass_kg": 2184.4,
        "mass_evidence": "itemised estimate",
        "usual_payload_kg": 127.0,
        "final_drive": 4.08,
        "gearbox_ratios": (4.368, 2.518, 1.743, 1.283, 1.0, 0.769),
        "tire_loaded_circumference_m": 2.559,
        "driveline_evidence": "published ratios; GPS cruise",
    }
    return VehicleParameters(**{**base, **overrides})


def _rig(tmp_path: Path) -> tuple[CanaryConfig, ProfilesConfig]:
    """A config scoped to ``xterra``, with a second profile in the registry."""
    profiles = ProfilesConfig(
        registry_path=tmp_path / "profiles.yaml",
        registry_overlay_path=tmp_path / "state" / "profiles.yaml",
    )
    base = CanaryConfig.from_yaml(REPO_ROOT / "canary.example.yaml")
    config = base.model_copy(update={"profiles": profiles})
    xterra = VehicleProfile(
        slug="xterra",
        vin="",
        name="Xterra",
        make="Nissan",
        model="Xterra",
        year=2008,
        parameters=_parameters(),
    )
    other = VehicleProfile(slug="work-truck", name="Work truck", vin=NA_VIN)
    save_registry(profiles, [xterra, other])
    return scope_config(config, xterra), profiles


def test_the_list_names_why_delete_would_refuse_for_each_profile(tmp_path: Path) -> None:
    config, _profiles = _rig(tmp_path)
    body = profiles_payload(config, "xterra")
    by_slug = {entry["slug"]: entry for entry in body["profiles"]}
    assert set(by_slug) == {"xterra", "work-truck"}
    assert body["count"] == 2
    refusal = by_slug["xterra"]["delete_refusal"]
    assert refusal is not None and refusal.startswith("the daemon is running on it")
    assert by_slug["work-truck"]["delete_refusal"] is None


def test_the_list_carries_what_the_daemon_runs_and_how_an_edit_applies(tmp_path: Path) -> None:
    config, _profiles = _rig(tmp_path)
    body = profiles_payload(config, "xterra")
    assert body["active_slug"] == "xterra"
    assert body["in_use"] == config.vehicle.model_dump(mode="json")
    assert body["applies"] == APPLIES_AT_RESTART
    assert body["parameter_fields"] == parameter_schema()
    assert body["deleted"] == []
    assert body["resolution"] is None
    assert body["pending_restart"] == [] and body["restart_required"] is False


def test_a_profile_without_a_tracked_file_is_untracked_with_no_overrides(tmp_path: Path) -> None:
    config, _profiles = _rig(tmp_path)
    entry = profiles_payload(config, "xterra")["profiles"][0]
    assert entry["tracked"] is False and entry["overrides"] == []


def test_the_resolution_this_process_recorded_is_reported(tmp_path: Path) -> None:
    config, _profiles = _rig(tmp_path)
    record_resolution(
        ProfileResolution(slug="xterra", how="vin", vin_read=NA_VIN, fallback_slug="x")
    )
    body = profiles_payload(config, "xterra")
    assert body["resolution"] == {"slug": "xterra", "how": "vin", "vin_read": NA_VIN}


def test_a_registry_edit_the_daemon_has_not_restarted_for_is_pending(tmp_path: Path) -> None:
    config, profiles = _rig(tmp_path)
    edited = [
        replace(p, parameters=_parameters(usual_payload_kg=95.0)) if p.slug == "xterra" else p
        for p in load_registry(profiles)
    ]
    save_registry(profiles, edited)
    body = profiles_payload(config, "xterra")
    assert body["pending_restart"] == ["parameters.usual_payload_kg"]
    assert body["restart_required"] is True


def test_the_pending_fields_are_the_active_profiles_only(tmp_path: Path) -> None:
    config, profiles = _rig(tmp_path)
    registry = load_registry(profiles)
    assert active_pending_restart(config, registry, "xterra") == []
    assert active_pending_restart(config, registry, "no-such-profile") == []


def test_delete_refusal_for_the_active_and_the_fallback_profile(tmp_path: Path) -> None:
    config, _profiles = _rig(tmp_path)
    record_resolution(
        ProfileResolution(slug="xterra", how="vin", vin_read=None, fallback_slug="work-truck")
    )
    assert (profile_delete_refusal(config, "xterra", "xterra") or "").startswith(
        "the daemon is running on it"
    )
    assert (profile_delete_refusal(config, "work-truck", "xterra") or "").startswith(
        "canary.yaml's vehicle: block"
    )
    assert profile_delete_refusal(config, "elsewhere", "xterra") is None


def _edit(profile: VehicleProfile) -> ProfileEdit:
    return ProfileEdit(
        profile=profile,
        changed=("name",),
        warnings=("a warning",),
        vin_resolution="unchanged",
    )


def test_an_edit_of_the_active_profile_says_it_waits_for_a_restart(tmp_path: Path) -> None:
    config, profiles = _rig(tmp_path)
    xterra = next(p for p in load_registry(profiles) if p.slug == "xterra")
    payload = edit_payload(config, _edit(xterra), "xterra")
    assert payload["active"] is True
    assert payload["applies"] == APPLIES_AT_RESTART
    assert payload["changed"] == ["name"] and payload["warnings"] == ["a warning"]
    assert payload["vin_resolution"] == "unchanged"
    assert payload["profile"] == xterra.to_dict()
    assert payload["pending_restart"] == [] and payload["restart_required"] is False


def test_an_edit_of_another_profile_applies_when_it_is_next_active(tmp_path: Path) -> None:
    config, profiles = _rig(tmp_path)
    other = next(p for p in load_registry(profiles) if p.slug == "work-truck")
    payload = edit_payload(config, _edit(other), "xterra")
    assert payload["active"] is False
    assert payload["applies"] == APPLIES_WHEN_ACTIVE
    # Another profile's differences from the running config are not "pending": it is not running.
    assert payload["pending_restart"] == [] and payload["restart_required"] is False


def test_the_route_answers_exactly_what_the_payload_function_builds(tmp_path: Path) -> None:
    """``GET /control/profiles`` is a wrapper: same body, nothing added or dropped."""
    from fastapi.testclient import TestClient

    from src.dashboard.api import create_app

    config, _profiles = _rig(tmp_path)
    client = TestClient(
        create_app(config, anomalies_dir=tmp_path / "an", control_token="secret"),
    )
    response = client.get("/control/profiles", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    expected = profiles_payload(config, config.collection.storage_path.name)
    assert response.json() == json.loads(json.dumps(expected))  # tuples travel as lists
    assert response.json()["active_slug"] == "xterra"
