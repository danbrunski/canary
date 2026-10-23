"""Deleting a vehicle profile (operator request 2026-10-23).

The tracked ``profiles.yaml`` is never written at runtime, so deleting a tracked
profile is a per-machine tombstone in the overlay (``deleted:``) and deleting a
machine-only profile removes its overlay row. Neither touches recorded data. The
traps this pins: a later save that passes the filtered entry list must not
silently un-delete, a start that resolves to a hidden profile must restore it
rather than register a duplicate, and the profile the daemon (or the
``vehicle:`` block) depends on cannot be deleted.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import CanaryConfig, ProfilesConfig
from src.control.audit import ControlAuditLogger
from src.dashboard.api import create_app
from src.vehicle.profiles import (
    SCOPED_DAEMON_DIRS,
    SCOPED_DAEMON_FILES,
    ProfileDeleteRefusedError,
    ProfileResolution,
    VehicleInfoProbe,
    VehicleProfile,
    delete_profile,
    delete_refusal,
    deleted_profiles,
    deleted_slugs,
    load_registry,
    profile_data_paths,
    record_resolution,
    resolve_active_profile,
    resolve_profile_for_tooling,
    restore_profile,
    revert_profile,
    save_registry,
    scope_config,
    slugify,
    update_profile,
    update_profile_name,
    vehicle_block_slug,
)
from tests.configs import isolated_config

NA_VIN = "1M8GDM9AXKP042788"
OTHER_NA_VIN = "1HGCM82633A004352"


@pytest.fixture(autouse=True)
def _no_resolution() -> Iterator[None]:
    record_resolution(None)
    yield
    record_resolution(None)


def _registry(tmp_path: Path) -> ProfilesConfig:
    return ProfilesConfig(
        registry_path=tmp_path / "profiles.yaml",
        registry_overlay_path=tmp_path / "state" / "profiles.yaml",
    )


def _truck(slug: str, *, vin: str = "", name: str | None = None) -> VehicleProfile:
    return VehicleProfile(
        slug=slug,
        vin=vin,
        name=slug.title() if name is None else name,
        make="Nissan",
        model="Xterra",
        year=2008,
    )


def _write_tracked(registry: ProfilesConfig, *profiles: VehicleProfile) -> None:
    registry.registry_path.write_text(
        yaml.safe_dump({"schema_version": 1, "profiles": [p.to_dict() for p in profiles]})
    )


def _overlay_file(registry: ProfilesConfig) -> dict[str, Any]:
    return dict(yaml.safe_load(registry.registry_overlay_path.read_text()))


def _slugs(registry: ProfilesConfig) -> list[str]:
    return [p.slug for p in load_registry(registry)]


def _delete(registry: ProfilesConfig, slug: str, **kw: Any) -> Any:
    kw.setdefault("active_slug", "active")
    kw.setdefault("fallback_slug", "fallback")
    return delete_profile(registry, slug, **kw)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_deleting_a_machine_only_profile_removes_its_overlay_row(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("tracked"))
    save_registry(registry, [*load_registry(registry), _truck("stray")])
    assert _slugs(registry) == ["tracked", "stray"]

    deletion = _delete(registry, "stray")

    assert (deletion.slug, deletion.tracked) == ("stray", False)
    assert _slugs(registry) == ["tracked"]
    # No tombstone: nothing tracked is hidden, so the overlay stays as a machine's
    # that never deleted anything.
    assert "deleted" not in _overlay_file(registry)
    assert deleted_slugs(registry) == []
    assert deleted_profiles(registry) == []


def test_deleting_a_tracked_profile_hides_it_and_leaves_the_tracked_file_alone(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone"))
    tracked_before = registry.registry_path.read_bytes()

    deletion = _delete(registry, "gone")

    assert (deletion.slug, deletion.tracked) == ("gone", True)
    assert registry.registry_path.read_bytes() == tracked_before
    assert _overlay_file(registry)["deleted"] == ["gone"]
    assert _slugs(registry) == ["keep"]
    assert [p.slug for p in deleted_profiles(registry)] == ["gone"]
    # Resolution (and only resolution) can still see it.
    assert [p.slug for p in load_registry(registry, include_deleted=True)] == ["keep", "gone"]


def test_a_tracked_profiles_machine_edits_are_dropped_by_the_delete(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone", name="Tracked name"))
    update_profile_name(registry, "gone", "My own name")
    assert load_registry(registry)[1].name == "My own name"

    _delete(registry, "gone")
    assert [row["slug"] for row in _overlay_file(registry)["profiles"]] == []
    restored = restore_profile(registry, "gone")

    assert restored.name == "Tracked name"
    assert _slugs(registry) == ["keep", "gone"]


def _rename_other(registry: ProfilesConfig) -> None:
    update_profile_name(registry, "keep", "Renamed")


def _edit_other(registry: ProfilesConfig) -> None:
    update_profile(registry, "keep", {"make": "Datsun"})


def _revert_other(registry: ProfilesConfig) -> None:
    update_profile(registry, "keep", {"make": "Datsun"})
    revert_profile(registry, "keep")


def _save_visible(registry: ProfilesConfig) -> None:
    """What a calibration-probe save and ``_maybe_persist`` do: save the entry
    list ``load_registry`` returned, which does not hold the hidden profile."""
    save_registry(registry, load_registry(registry))


def _register_a_new_vin(registry: ProfilesConfig) -> None:
    config = isolated_config(registry.registry_path.parent, "example").model_copy(
        update={"profiles": registry}
    )
    resolve_active_profile(config, probe=_probe(NA_VIN))


@pytest.mark.parametrize(
    "later_save",
    [_rename_other, _edit_other, _revert_other, _save_visible, _register_a_new_vin],
)
def test_a_later_save_never_drops_the_tombstone(tmp_path: Path, later_save: Any) -> None:
    """The ``save_registry`` trap: every save passes the *filtered* entry list,
    so a tombstone kept only by being passed back would vanish on the first
    rename, edit, revert, auto-register or calibration-probe save."""
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone"))
    _delete(registry, "gone")

    later_save(registry)

    assert deleted_slugs(registry) == ["gone"]
    assert "gone" not in _slugs(registry)


def test_the_tombstone_list_is_written_sorted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("zulu"), _truck("alpha"))
    _delete(registry, "zulu")
    _delete(registry, "alpha")
    assert _overlay_file(registry)["deleted"] == ["alpha", "zulu"]


def test_an_unknown_or_already_deleted_slug_is_a_key_error(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone"))
    with pytest.raises(KeyError):
        _delete(registry, "nope")
    _delete(registry, "gone")
    with pytest.raises(KeyError):
        _delete(registry, "gone")


def test_the_active_profile_cannot_be_deleted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("active"), _truck("other"))
    with pytest.raises(ProfileDeleteRefusedError, match="the daemon is running on it"):
        _delete(registry, "active")
    assert not registry.registry_overlay_path.exists()
    assert _slugs(registry) == ["active", "other"]


def test_the_vehicle_block_profile_cannot_be_deleted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("active"), _truck("fallback"))
    with pytest.raises(ProfileDeleteRefusedError) as raised:
        _delete(registry, "fallback")
    assert "vehicle: block" in str(raised.value)
    assert "would recreate it" in str(raised.value)
    assert "change the vehicle: block first" in str(raised.value)
    assert not registry.registry_overlay_path.exists()


def test_the_refusal_is_one_function_for_the_button_and_the_delete() -> None:
    assert delete_refusal("a", active_slug="a", fallback_slug="b") is not None
    assert delete_refusal("b", active_slug="a", fallback_slug="b") is not None
    assert delete_refusal("c", active_slug="a", fallback_slug="b") is None
    # Unscoped (no active profile): only the vehicle: block's is protected.
    assert delete_refusal("a", active_slug=None, fallback_slug="b") is None


def test_restore_brings_a_tracked_profile_back(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone"))
    _delete(registry, "gone")

    profile = restore_profile(registry, "gone")

    assert profile.slug == "gone"
    assert _slugs(registry) == ["keep", "gone"]
    assert deleted_slugs(registry) == []
    assert "deleted" not in _overlay_file(registry)


def test_restore_refuses_a_profile_that_is_not_deleted(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"))
    with pytest.raises(KeyError):
        restore_profile(registry, "keep")
    with pytest.raises(KeyError):
        restore_profile(registry, "nope")


def test_a_tombstone_for_a_slug_git_no_longer_has_hides_nothing(tmp_path: Path) -> None:
    """A tracked profile removed from git later leaves a stale tombstone; a
    machine-only profile that reuses the slug must not be hidden by it."""
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"))
    save_registry(registry, [*load_registry(registry), _truck("reused")], deleted=["reused"])
    assert _slugs(registry) == ["keep", "reused"]
    with pytest.raises(KeyError):
        restore_profile(registry, "reused")


def test_a_vin_held_by_a_hidden_profile_cannot_be_given_to_another(tmp_path: Path) -> None:
    from src.vehicle.profiles import DuplicateVinError

    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone", vin=NA_VIN))
    _delete(registry, "gone")
    with pytest.raises(DuplicateVinError, match=r"'gone'.*hidden on this machine"):
        update_profile(registry, "keep", {"vin": NA_VIN})


# ---------------------------------------------------------------------------
# Resolution never runs on a hidden profile
# ---------------------------------------------------------------------------


def _probe(vin: str | None):  # type: ignore[no-untyped-def]
    def probe(_config: CanaryConfig) -> VehicleInfoProbe:
        return VehicleInfoProbe(vin=vin, calibration_id=None, cvn=None)

    return probe


def _config(tmp_path: Path, registry: ProfilesConfig, **vehicle: Any) -> CanaryConfig:
    config = isolated_config(tmp_path, "example")
    config = config.model_copy(update={"profiles": registry})
    if vehicle:
        config = config.model_copy(update={"vehicle": config.vehicle.model_copy(update=vehicle)})
    return config


def test_a_vin_matching_a_deleted_tracked_profile_restores_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone", vin=NA_VIN))
    _delete(registry, "gone")
    config = _config(tmp_path, registry)

    with caplog.at_level(logging.WARNING, logger="src.vehicle.profiles"):
        profile = resolve_active_profile(config, probe=_probe(NA_VIN))

    assert profile.slug == "gone"
    # Restored, not duplicated under a VIN-derived slug.
    assert _slugs(registry) == ["keep", "gone"]
    assert deleted_slugs(registry) == []
    messages = [r.getMessage() for r in caplog.records]
    assert any("'gone'" in m and "deleted on this machine" in m and NA_VIN in m for m in messages)


def test_the_fallback_resolving_to_a_deleted_profile_restores_it(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("nissan-xterra-2008"))
    # Hidden by hand: the API refuses to delete the vehicle: block's profile.
    save_registry(registry, [], deleted=["nissan-xterra-2008"])
    config = _config(tmp_path, registry)

    profile = resolve_active_profile(config, probe=_probe(None))

    assert profile.slug == "nissan-xterra-2008"
    assert _slugs(registry) == ["nissan-xterra-2008"]
    assert deleted_slugs(registry) == []


def test_a_profile_override_naming_a_deleted_profile_restores_it(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone"))
    _delete(registry, "gone")
    config = _config(tmp_path, registry)

    profile = resolve_active_profile(config, probe=_probe(None), override_slug="gone")

    assert profile.slug == "gone"
    assert "gone" in _slugs(registry)
    assert deleted_slugs(registry) == []


def test_a_start_that_resolves_elsewhere_leaves_the_tombstone(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep", vin=OTHER_NA_VIN), _truck("gone", vin=NA_VIN))
    _delete(registry, "gone")
    config = _config(tmp_path, registry)

    profile = resolve_active_profile(config, probe=_probe(OTHER_NA_VIN))

    assert profile.slug == "keep"
    assert deleted_slugs(registry) == ["gone"]


def test_a_new_vin_never_takes_a_hidden_profiles_slug(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    vin_slug = slugify(NA_VIN)
    _write_tracked(registry, _truck(vin_slug))
    save_registry(registry, [], deleted=[vin_slug])
    config = _config(tmp_path, registry)

    profile = resolve_active_profile(config, probe=_probe(NA_VIN))

    assert profile.slug != vin_slug
    assert deleted_slugs(registry) == [vin_slug]


def test_offline_tooling_sees_a_hidden_profile_without_restoring_it(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("keep"), _truck("gone", name="Tracked gone"))
    _delete(registry, "gone")
    config = _config(tmp_path, registry)

    assert resolve_profile_for_tooling(config, "gone").name == "Tracked gone"
    assert deleted_slugs(registry) == ["gone"]


def test_the_resolution_record_carries_the_vehicle_blocks_slug(tmp_path: Path) -> None:
    """A scoped config's ``vehicle:`` block is the profile's own, so the slug
    the file names has to be read before scoping."""
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("other-truck", vin=NA_VIN))
    config = _config(tmp_path, registry, make="Datsun", model="240Z", year=1972)
    profile = resolve_active_profile(config, probe=_probe(NA_VIN))
    scoped = scope_config(config, profile)
    assert (scoped.vehicle.make, scoped.vehicle.model) == ("Nissan", "Xterra")
    assert vehicle_block_slug(scoped) == "datsun-240z-1972"


def test_vehicle_block_slug_without_a_record_is_the_configs_own(tmp_path: Path) -> None:
    config = _config(tmp_path, _registry(tmp_path), make="Datsun", model="240Z", year=1972)
    assert vehicle_block_slug(config) == "datsun-240z-1972"
    record_resolution(ProfileResolution("x", "vin_matched", None, "from-the-record"))
    assert vehicle_block_slug(config) == "from-the-record"


# ---------------------------------------------------------------------------
# What a deleted profile leaves behind
# ---------------------------------------------------------------------------


def test_profile_data_paths_lists_the_existing_sibling_directories(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    config = _config(tmp_path, registry)
    scoped = scope_config(config, _truck("active"))
    sessions = scoped.collection.storage_path
    models = scoped.daemon.models_dir
    calibration = scoped.daemon.calibration_dir
    tuning_file_dir = scoped.daemon.tuning_config.parent
    for path in (
        sessions.parent / "gone",
        models.parent / "gone",
        calibration.parent / "gone",
        tuning_file_dir.parent / "gone",
        # The active profile's own, and another profile's: not "gone"'s.
        sessions,
        sessions.parent / "elsewhere",
    ):
        path.mkdir(parents=True, exist_ok=True)

    found = profile_data_paths(scoped, "gone")

    assert set(found) == {
        sessions.parent / "gone",
        models.parent / "gone",
        calibration.parent / "gone",
        tuning_file_dir.parent / "gone",
    }
    assert len(found) == len(set(found))
    assert all(path.is_dir() for path in found)
    # Nothing was removed.
    assert sessions.exists() and (sessions.parent / "elsewhere").exists()


def test_profile_data_paths_is_empty_when_nothing_was_recorded(tmp_path: Path) -> None:
    scoped = scope_config(_config(tmp_path, _registry(tmp_path)), _truck("active"))
    assert profile_data_paths(scoped, "never-existed") == []


def test_profile_data_paths_is_empty_for_an_unscoped_config(tmp_path: Path) -> None:
    config = _config(tmp_path, _registry(tmp_path))
    (config.collection.storage_path.parent / "gone").mkdir(parents=True)
    assert config.scoped_profile is None
    assert profile_data_paths(config, "gone") == []


def test_every_scoped_field_is_found_by_the_data_listing(tmp_path: Path) -> None:
    """``scope_config`` and ``profile_data_paths`` share their field lists, so a
    path scoped under the slug cannot be missed when the profile is deleted."""
    config = _config(tmp_path, _registry(tmp_path))
    scoped = scope_config(config, _truck("active"))
    changed = {
        name
        for name in type(config.daemon).model_fields
        if getattr(scoped.daemon, name) != getattr(config.daemon, name)
    }
    assert changed == {*SCOPED_DAEMON_DIRS, *SCOPED_DAEMON_FILES, "calibration_dir"}
    expected: set[Path] = {scoped.collection.storage_path.parent / "gone"}
    for name in (*SCOPED_DAEMON_DIRS, "calibration_dir"):
        expected.add(getattr(scoped.daemon, name).parent / "gone")
    for name in SCOPED_DAEMON_FILES:
        expected.add(getattr(scoped.daemon, name).parent.parent / "gone")
    for path in expected:
        path.mkdir(parents=True, exist_ok=True)
    assert set(profile_data_paths(scoped, "gone")) == expected


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

AUTH = {"Authorization": "Bearer secret"}


def _api(tmp_path: Path) -> tuple[TestClient, CanaryConfig, Path]:
    registry = _registry(tmp_path)
    _write_tracked(registry, _truck("xterra"), _truck("other"), _truck("fallback-truck"))
    save_registry(registry, [*load_registry(registry), _truck("stray")])
    config = _config(tmp_path, registry)
    scoped = scope_config(config, load_registry(registry)[0])
    audit = tmp_path / "audit.jsonl"
    app = create_app(
        scoped,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        audit_logger=ControlAuditLogger(audit),
    )
    return TestClient(app), scoped, audit


def _actions(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [json.loads(line)["action"] for line in path.read_text().splitlines()]


def test_the_listing_says_why_delete_would_refuse_and_lists_the_hidden(tmp_path: Path) -> None:
    record_resolution(ProfileResolution("xterra", "no_vin_read", None, "fallback-truck"))
    client, _scoped, _audit = _api(tmp_path)

    body = client.get("/control/profiles", headers=AUTH).json()

    refusals = {row["slug"]: row["delete_refusal"] for row in body["profiles"]}
    assert "daemon is running on it" in refusals["xterra"]
    assert "vehicle: block" in refusals["fallback-truck"]
    assert refusals["other"] is None
    assert refusals["stray"] is None
    assert body["deleted"] == []

    assert client.delete("/control/profiles/other", headers=AUTH).status_code == 200
    body = client.get("/control/profiles", headers=AUTH).json()
    assert [row["slug"] for row in body["profiles"]] == ["xterra", "fallback-truck", "stray"]
    assert body["deleted"] == [
        {"slug": "other", "name": "Other", "make": "Nissan", "model": "Xterra", "year": 2008}
    ]


def test_delete_a_machine_only_profile(tmp_path: Path) -> None:
    client, _scoped, audit = _api(tmp_path)
    resp = client.delete("/control/profiles/stray", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": "stray", "tracked": False, "data_left": []}
    assert _actions(audit) == ["profile.delete"]


def test_delete_a_tracked_profile_reports_the_data_it_leaves(tmp_path: Path) -> None:
    client, scoped, audit = _api(tmp_path)
    left = scoped.collection.storage_path.parent / "other"
    left.mkdir(parents=True)

    resp = client.delete("/control/profiles/other", headers=AUTH)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": "other", "tracked": True, "data_left": [str(left)]}
    assert left.is_dir()
    assert _actions(audit) == ["profile.delete"]


def test_delete_refusals_are_409_with_the_reason_and_write_nothing(tmp_path: Path) -> None:
    record_resolution(ProfileResolution("xterra", "no_vin_read", None, "fallback-truck"))
    client, _scoped, audit = _api(tmp_path)

    active = client.delete("/control/profiles/xterra", headers=AUTH)
    fallback = client.delete("/control/profiles/fallback-truck", headers=AUTH)

    assert active.status_code == 409
    assert "the daemon is running on it" in active.json()["detail"]
    assert fallback.status_code == 409
    assert "change the vehicle: block first" in fallback.json()["detail"]
    assert _actions(audit) == []
    listing = client.get("/control/profiles", headers=AUTH).json()
    assert listing["deleted"] == []
    assert listing["count"] == 4


def test_delete_an_unknown_profile_is_404(tmp_path: Path) -> None:
    client, _scoped, audit = _api(tmp_path)
    assert client.delete("/control/profiles/nope", headers=AUTH).status_code == 404
    assert _actions(audit) == []


def test_delete_needs_the_control_token(tmp_path: Path) -> None:
    client, _scoped, _audit = _api(tmp_path)
    assert client.delete("/control/profiles/other").status_code in (401, 403)
    assert client.post("/control/profiles/other/restore").status_code in (401, 403)


def test_restore_route_brings_the_tracked_profile_back(tmp_path: Path) -> None:
    client, _scoped, audit = _api(tmp_path)
    client.delete("/control/profiles/other", headers=AUTH)

    resp = client.post("/control/profiles/other/restore", headers=AUTH)

    assert resp.status_code == 200, resp.text
    assert resp.json()["profile"]["slug"] == "other"
    listing = client.get("/control/profiles", headers=AUTH).json()
    assert "other" in [row["slug"] for row in listing["profiles"]]
    assert listing["deleted"] == []
    assert _actions(audit) == ["profile.delete", "profile.restore"]


def test_restore_route_is_404_when_nothing_is_deleted(tmp_path: Path) -> None:
    client, _scoped, audit = _api(tmp_path)
    assert client.post("/control/profiles/other/restore", headers=AUTH).status_code == 404
    assert client.post("/control/profiles/nope/restore", headers=AUTH).status_code == 404
    assert _actions(audit) == []
