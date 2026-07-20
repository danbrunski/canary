"""Vehicle-profile resolution, registry I/O, and config scoping."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import CanaryConfig, DaemonConfig
from src.dashboard.api import create_app
from src.profiles import (
    VehicleProfile,
    load_registry,
    resolve_active_profile,
    resolve_profile_for_tooling,
    save_registry,
    scope_config,
    slugify,
    update_profile_name,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _base_config(tmp_path: Path, *, obd_source: str = "disabled") -> CanaryConfig:
    """Example config with the VIN probe disabled and the registry in tmp."""
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    return config.model_copy(
        update={
            "collection": config.collection.model_copy(update={"obd_source": obd_source}),
            "profiles": config.profiles.model_copy(
                update={"registry_path": tmp_path / "profiles.yaml"}
            ),
        }
    )


def test_slugify_is_filesystem_safe():
    assert slugify("2008 Nissan Xterra!") == "2008-nissan-xterra"
    # Never a reserved leading underscore (excluded from training discovery).
    assert not slugify("__weird__").startswith("_")
    assert slugify("") == "vehicle"


def test_registry_roundtrip(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    assert load_registry(path) == []  # missing file → empty
    profiles = [
        VehicleProfile(slug="nissan-xterra-2008", vin="VIN123", name="2008 Xterra", year=2008),
        VehicleProfile(slug="vin999", vin="VIN999", name=""),
    ]
    save_registry(path, profiles)
    loaded = load_registry(path)
    assert [p.slug for p in loaded] == ["nissan-xterra-2008", "vin999"]
    assert loaded[0].name == "2008 Xterra"
    assert loaded[0].year == 2008


def test_update_profile_name(tmp_path: Path):
    path = tmp_path / "profiles.yaml"
    save_registry(path, [VehicleProfile(slug="vin999", vin="VIN999", name="")])
    updated = update_profile_name(path, "vin999", "  Daily Driver  ")
    assert updated.name == "Daily Driver"  # trimmed
    assert load_registry(path)[0].name == "Daily Driver"
    with pytest.raises(KeyError):
        update_profile_name(path, "missing", "x")


def test_read_vin_noop_when_not_canable(tmp_path: Path):
    from src.profiles import read_vin

    config = _base_config(tmp_path, obd_source="disabled")
    assert read_vin(config) is None  # never opens a bus


def test_read_vin_noop_on_virtual_bus(tmp_path: Path):
    from src.profiles import read_vin

    config = _base_config(tmp_path, obd_source="canable")
    config = config.model_copy(
        update={
            "hardware": config.hardware.model_copy(
                update={"can": config.hardware.can.model_copy(update={"interface": "virtual"})}
            )
        }
    )
    assert read_vin(config) is None


def test_resolve_falls_back_to_default_and_registers(tmp_path: Path):
    config = _base_config(tmp_path)  # probe disabled → VIN is None
    profile = resolve_active_profile(config)
    # Default profile derived from the vehicle: block, with a sensible name.
    assert profile.slug == slugify(
        f"{config.vehicle.make}-{config.vehicle.model}-{config.vehicle.year}"
    )
    assert profile.name  # default gets the "year make model" label
    # Persisted so the dashboard can list/rename it.
    registered = load_registry(config.profiles.registry_path)
    assert any(p.slug == profile.slug for p in registered)


def test_resolve_override_returns_registry_entry(tmp_path: Path):
    config = _base_config(tmp_path)
    save_registry(
        config.profiles.registry_path,
        [VehicleProfile(slug="work-truck", vin="VINWORK", name="Work Truck")],
    )
    profile = resolve_active_profile(config, override_slug="work-truck")
    assert profile.name == "Work Truck"


def test_resolve_override_unknown_slug_synthesizes(tmp_path: Path):
    config = _base_config(tmp_path)
    profile = resolve_active_profile(config, override_slug="brand-new")
    assert profile.slug == "brand-new"
    assert any(p.slug == "brand-new" for p in load_registry(config.profiles.registry_path))


def test_resolve_disabled_does_not_write(tmp_path: Path):
    config = _base_config(tmp_path)
    config = config.model_copy(
        update={"profiles": config.profiles.model_copy(update={"enabled": False})}
    )
    resolve_active_profile(config)
    assert not config.profiles.registry_path.exists()


def test_resolve_for_tooling_never_probes(tmp_path: Path):
    # canable + non-virtual would normally probe; tooling must not open a bus.
    config = _base_config(tmp_path, obd_source="canable")
    profile = resolve_profile_for_tooling(config, None)
    assert profile.slug  # returns the default profile without touching the bus


def test_scope_config_nests_every_vehicle_path(tmp_path: Path):
    config = _base_config(tmp_path)
    config = config.model_copy(
        update={
            "collection": config.collection.model_copy(
                update={"storage_path": Path("/data/canary")}
            ),
            "daemon": config.daemon.model_copy(
                update={
                    "models_dir": Path("models"),
                    "anomalies_dir": Path("data/anomalies"),
                    "training_ledger": Path("data/control/training_runs.jsonl"),
                    "behavior_models": Path("data/behavior_models.yaml"),
                }
            ),
        }
    )
    profile = VehicleProfile(slug="xterra", vin="VIN42", name="X", make="Nissan", year=2008)
    scoped = scope_config(config, profile)

    # Directory fields gain the slug as a trailing component.
    assert scoped.collection.storage_path == Path("/data/canary/xterra")
    assert scoped.daemon.models_dir == Path("models/xterra")
    assert scoped.daemon.anomalies_dir == Path("data/anomalies/xterra")
    # File fields gain the slug as a parent of the filename.
    assert scoped.daemon.training_ledger == Path("data/control/xterra/training_runs.jsonl")
    assert scoped.daemon.behavior_models == Path("data/xterra/behavior_models.yaml")
    # Vehicle ledger (Task 3.2) scopes the same way as feedback_log.
    assert scoped.daemon.vehicle_ledger == Path("data/control/xterra/vehicle_ledger.jsonl")
    # Reference epoch (Task 4.3) scopes the same way.
    assert scoped.daemon.reference_epoch == Path("data/control/xterra/reference_epoch.json")
    # Trend-alert idempotency marker (Task 4.6) scopes the same way — a stat
    # name is generic (e.g. "idle_rpm_mean"), so without scoping a session id
    # recorded for one vehicle could suppress a legitimate alert for another.
    assert scoped.daemon.trend_alert_state == Path("data/control/xterra/trend_alert_state.json")
    # Slug is recoverable as the storage path's final component.
    assert scoped.collection.storage_path.name == "xterra"
    # The vehicle block reflects the profile (VIN for reproducible manifests).
    assert scoped.vehicle.vin == "VIN42"
    assert scoped.vehicle.make == "Nissan"
    # The registry path itself is NOT scoped (it indexes all vehicles).
    assert scoped.profiles.registry_path == config.profiles.registry_path


def test_scope_config_covers_every_daemon_path_field(tmp_path: Path):
    """Every ``Path``-typed field on ``DaemonConfig`` must come out with the
    slug inserted as a path component. Exhaustive by construction (iterates
    ``DaemonConfig.model_fields`` rather than naming fields), so a future
    field added to ``DaemonConfig`` but forgotten in ``scope_config`` fails
    this test immediately instead of silently shipping unscoped (as
    ``trend_alert_state`` did)."""
    config = _base_config(tmp_path)
    profile = VehicleProfile(slug="xterra", vin="VIN42", name="X")
    scoped = scope_config(config, profile)

    path_fields = [name for name, f in DaemonConfig.model_fields.items() if f.annotation is Path]
    assert path_fields  # sanity: DaemonConfig still has Path fields to check
    for name in path_fields:
        original = getattr(config.daemon, name)
        result = getattr(scoped.daemon, name)
        assert "xterra" in result.parts, (
            f"DaemonConfig.{name} was not profile-scoped by scope_config "
            f"(original={original!r}, scoped={result!r})"
        )


# ---------------------------------------------------------------------------
# Control endpoints
# ---------------------------------------------------------------------------


def _api(tmp_path: Path) -> tuple[TestClient, CanaryConfig]:
    config = _base_config(tmp_path)
    # The active slug is the scoped storage path's final component.
    storage = tmp_path / "data" / "nissan-xterra-2008"
    storage.mkdir(parents=True)
    config = config.model_copy(
        update={"collection": config.collection.model_copy(update={"storage_path": storage})}
    )
    save_registry(
        config.profiles.registry_path,
        [
            VehicleProfile(slug="nissan-xterra-2008", vin="VINX", name=""),
            VehicleProfile(slug="work-truck", vin="VINW", name="Work Truck"),
        ],
    )
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    return TestClient(app), config


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_get_profiles_requires_auth_and_marks_active(tmp_path: Path):
    client, _config = _api(tmp_path)
    assert client.get("/control/profiles").status_code == 401
    body = client.get("/control/profiles", headers=_auth()).json()
    assert body["active_slug"] == "nissan-xterra-2008"
    assert body["count"] == 2
    assert {p["slug"] for p in body["profiles"]} == {"nissan-xterra-2008", "work-truck"}


def test_post_profile_name_persists(tmp_path: Path):
    client, config = _api(tmp_path)
    resp = client.post(
        "/control/profiles/nissan-xterra-2008/name",
        json={"name": "2008 Xterra"},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["profile"]["name"] == "2008 Xterra"
    persisted = {p.slug: p.name for p in load_registry(config.profiles.registry_path)}
    assert persisted["nissan-xterra-2008"] == "2008 Xterra"


def test_post_profile_name_unknown_slug_404(tmp_path: Path):
    client, _config = _api(tmp_path)
    resp = client.post("/control/profiles/nope/name", json={"name": "x"}, headers=_auth())
    assert resp.status_code == 404
