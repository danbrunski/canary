"""R05.15 — the operational calibration directory is per vehicle.

Before: ``operational_dir`` was ``jobs_dir.parent / "calibration"``. A scoped
``jobs_dir`` is ``<control>/jobs/<slug>``, so every profile resolved the same
``<control>/jobs/calibration``: a second vehicle's dashboard-recorded flash
landed in the Xterra's registry and ``active_registry_path`` served it to
both. The Xterra's existing file at that shared location must keep being
found, and must not be found by anyone else.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from src.collection import tuning_profile
from src.collection.vehicle_probe import probe_vehicle_info
from src.config import CanaryConfig
from src.control.calibration import (
    active_memory_path,
    append_flash,
    calibration_status,
    memory_verify_summary,
    operational_memory_path,
    record_verify_results,
    seed_operational_memory,
)
from src.paths import DIDS_YAML
from src.vehicle.profiles import (
    MIGRATION_MARKER,
    VehicleProfile,
    active_registry_path,
    legacy_adoption,
    legacy_operational_dir,
    load_registry,
    operational_dir,
    operational_registry_path,
    resolve_active_profile,
    resolve_profile_for_tooling,
    save_registry,
    scope_config,
    source_registry_path,
)
from src.vehicle.tune_revisions import TuneRevision, load_tune_revisions

XTERRA = "nissan-xterra-2008"


def _after_real_flashes() -> datetime:
    """A day after the newest real flash, so synthetic flashes stay newest.

    The registry refuses revisions out of flashed_at order, and the tracked
    file gains real flashes over time (2026-10-04 broke fixed dates)."""
    real = load_tune_revisions(Path("data") / XTERRA / "tune_revisions.yaml")
    assert real is not None
    return max(r.flashed_at for r in real.revisions) + timedelta(days=1)


AFTER = _after_real_flashes()
FLASH = TuneRevision(revision="R05-15-flash", flashed_at=AFTER + timedelta(days=1))


@pytest.fixture(autouse=True)
def _no_running_revision_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuning_profile, "_running_tune_revision", None)
    monkeypatch.setattr(tuning_profile, "_registry_calibration_id", None)


def _base(tmp_path: Path) -> CanaryConfig:
    raw = yaml.safe_load(Path("canary.example.yaml").read_text())
    raw.setdefault("collection", {})["storage_path"] = str(tmp_path / "sessions")
    raw.setdefault("daemon", {})["jobs_dir"] = str(tmp_path / "control" / "jobs")
    raw.setdefault("profiles", {})["registry_path"] = str(tmp_path / "profiles.yaml")
    raw["profiles"]["registry_overlay_path"] = str(tmp_path / "profiles.overlay.yaml")
    return CanaryConfig.model_validate(raw)


def _scoped(base: CanaryConfig, slug: str | None = None) -> CanaryConfig:
    return scope_config(base, resolve_profile_for_tooling(base, slug))


def _legacy_dir(base: CanaryConfig) -> Path:
    """Where every scoped profile resolved before R05.15."""
    return base.daemon.jobs_dir / "calibration"


def _write_legacy_registry(base: CanaryConfig, source: Path, *extra: TuneRevision) -> Path:
    registry = load_tune_revisions(source)
    assert registry is not None
    revisions = [*registry.revisions, *extra]
    path = _legacy_dir(base) / "tune_revisions.yaml"
    path.parent.mkdir(parents=True)
    payload = {"schema_version": 1, "revisions": [r.model_dump(mode="json") for r in revisions]}
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def test_each_profile_gets_its_own_operational_directory(tmp_path: Path) -> None:
    base = _base(tmp_path)
    xterra, truck = _scoped(base), _scoped(base, "work-truck")
    assert xterra.collection.storage_path.name == XTERRA
    assert operational_dir(xterra) == tmp_path / "control" / "calibration" / XTERRA
    assert operational_dir(truck) == tmp_path / "control" / "calibration" / "work-truck"
    # An unscoped config keeps its pre-R05.15 location beside jobs_dir.
    assert operational_dir(base) == tmp_path / "control" / "calibration"


def test_pi_paths_stay_under_the_data_root() -> None:
    """The Pi pins ``jobs_dir`` under /data/canary; calibration state follows it."""
    base = CanaryConfig.model_validate(yaml.safe_load(Path("canary.pi.example.yaml").read_text()))
    scoped = _scoped(base)
    assert operational_dir(scoped) == Path("/data/canary/control/calibration") / XTERRA


def test_a_second_profiles_flash_does_not_land_in_the_first_registry(tmp_path: Path) -> None:
    base = _base(tmp_path)
    xterra, truck = _scoped(base), _scoped(base, "work-truck")
    append_flash(xterra, FLASH)
    other = FLASH.model_copy(update={"revision": "work-truck-flash"})
    append_flash(truck, other)

    ours = load_tune_revisions(active_registry_path(xterra))
    theirs = load_tune_revisions(active_registry_path(truck))
    assert ours is not None and theirs is not None
    assert active_registry_path(xterra) != active_registry_path(truck)
    assert "work-truck-flash" not in {r.revision for r in ours.revisions}
    assert [r.revision for r in theirs.revisions] == ["work-truck-flash"]


def test_the_xterras_shared_location_registry_is_still_read(tmp_path: Path) -> None:
    base = _base(tmp_path)
    xterra, truck = _scoped(base), _scoped(base, "work-truck")
    legacy = _write_legacy_registry(base, source_registry_path(xterra), FLASH)

    # Read for the Xterra: its tracked registry is a prefix of the shared file.
    assert legacy_operational_dir(xterra) == legacy.parent
    assert active_registry_path(xterra) == legacy
    # Never for another vehicle: nothing proves the file is its.
    assert legacy_operational_dir(truck) is None
    assert active_registry_path(truck) == source_registry_path(truck)


def test_the_first_flash_migrates_the_shared_registry(tmp_path: Path) -> None:
    base = _base(tmp_path)
    xterra = _scoped(base)
    legacy = _write_legacy_registry(base, source_registry_path(xterra), FLASH)
    before = legacy.read_text()

    later = TuneRevision(revision="R05-15-later", flashed_at=AFTER + timedelta(days=2))
    append_flash(xterra, later)
    migrated = load_tune_revisions(active_registry_path(xterra))
    assert active_registry_path(xterra) == operational_registry_path(xterra)
    assert migrated is not None
    assert [r.revision for r in migrated.revisions][-2:] == [FLASH.revision, later.revision]
    assert legacy.read_text() == before  # read, never written


def test_a_legacy_registry_from_another_vehicle_is_not_adopted(tmp_path: Path) -> None:
    base = _base(tmp_path)
    xterra = _scoped(base)
    path = _legacy_dir(base) / "tune_revisions.yaml"
    path.parent.mkdir(parents=True)
    foreign = {"schema_version": 1, "revisions": [FLASH.model_dump(mode="json")]}
    path.write_text(yaml.safe_dump(foreign))
    assert legacy_operational_dir(xterra) is None
    assert active_registry_path(xterra) == source_registry_path(xterra)


def test_the_xterras_shared_memory_catalog_and_verify_record_are_still_read(
    tmp_path: Path,
) -> None:
    base = _base(tmp_path)
    xterra, truck = _scoped(base), _scoped(base, "work-truck")
    legacy = _write_legacy_registry(base, source_registry_path(xterra)).parent
    legacy_dids = legacy / "dids.yaml"
    legacy_dids.write_text(DIDS_YAML.read_text())
    channel = yaml.safe_load(DIDS_YAML.read_text())["memory"][0]["name"]
    (legacy / "memory_verify.json").write_text(
        json.dumps({channel: {"outcome": "pass", "at": "2026-09-20T00:00:00+00:00"}})
    )

    assert active_memory_path(xterra, DIDS_YAML) == legacy_dids
    assert active_memory_path(truck, DIDS_YAML) == DIDS_YAML
    assert memory_verify_summary(xterra, DIDS_YAML)["passed"] == 1
    assert memory_verify_summary(truck, DIDS_YAML)["passed"] == 0

    # The first verify migrates both, merged over the legacy record.
    assert seed_operational_memory(xterra, DIDS_YAML) == operational_memory_path(xterra)
    record_verify_results(xterra, {"SOMETHING_ELSE": "fail"})
    assert active_memory_path(xterra, DIDS_YAML) == operational_memory_path(xterra)
    own = json.loads((operational_dir(xterra) / "memory_verify.json").read_text())
    assert own[channel]["outcome"] == "pass"
    assert own["SOMETHING_ELSE"]["outcome"] == "fail"


# ---------------------------------------------------------------------------
# Eager migration at profile resolution, and never a silent orphan
# ---------------------------------------------------------------------------


def _verify_only_legacy(base: CanaryConfig) -> tuple[Path, str]:
    """What a dashboard memory *verify* leaves: dids.yaml + record, no registry."""
    legacy = _legacy_dir(base)
    legacy.mkdir(parents=True)
    (legacy / "dids.yaml").write_text(DIDS_YAML.read_text())
    channel = yaml.safe_load(DIDS_YAML.read_text())["memory"][0]["name"]
    (legacy / "memory_verify.json").write_text(
        json.dumps({channel: {"outcome": "pass", "at": "2026-09-20T00:00:00+00:00"}})
    )
    return legacy, channel


def test_a_verify_only_legacy_dir_is_migrated_for_the_only_registered_profile(
    tmp_path: Path,
) -> None:
    base = _base(tmp_path)
    legacy, _channel = _verify_only_legacy(base)
    # Registers it, alone.
    profile = resolve_active_profile(base, probe=probe_vehicle_info, override_slug=XTERRA)
    xterra = scope_config(base, profile)

    own = operational_dir(xterra)
    assert (own / "dids.yaml").read_text() == (legacy / "dids.yaml").read_text()
    assert (own / "memory_verify.json").exists()
    marker = json.loads((own / MIGRATION_MARKER).read_text())
    assert marker["source"] == str(legacy)
    assert active_memory_path(xterra, DIDS_YAML) == own / "dids.yaml"
    assert memory_verify_summary(xterra, DIDS_YAML)["passed"] == 1
    assert calibration_status(xterra)["legacy_calibration_unadopted"] is None
    assert (legacy / "dids.yaml").exists()  # left intact

    # A second vehicle registering later cannot orphan it: the marker holds.
    save_registry(
        base.profiles,
        [*load_registry(base.profiles), VehicleProfile(slug="work-truck", vin="W")],
    )
    assert legacy_adoption(xterra).adopted
    assert legacy_adoption(_scoped(base, "work-truck")).adopted is False
    assert calibration_status(_scoped(base, "work-truck"))["legacy_calibration_unadopted"] is None


def test_an_unowned_legacy_dir_warns_and_is_surfaced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = _base(tmp_path)
    legacy, _channel = _verify_only_legacy(base)
    save_registry(
        base.profiles,
        [VehicleProfile(slug=XTERRA, vin="X"), VehicleProfile(slug="work-truck", vin="W")],
    )
    with caplog.at_level("WARNING", logger="src.vehicle.profiles"):
        profile = resolve_active_profile(base, probe=probe_vehicle_info, override_slug=XTERRA)
    xterra = scope_config(base, profile)

    assert not (operational_dir(xterra) / "dids.yaml").exists()
    assert any("not adopted" in record.getMessage() for record in caplog.records)
    reason = calibration_status(xterra)["legacy_calibration_unadopted"]
    assert reason is not None and str(legacy) in reason and "2 profiles" in reason


def test_a_hand_added_tracked_flash_does_not_orphan_dashboard_flashes(tmp_path: Path) -> None:
    """The tracked registry gains a flash by hand; the legacy one had its own."""
    raw = yaml.safe_load(Path("canary.example.yaml").read_text())
    raw.setdefault("collection", {})["storage_path"] = str(tmp_path / "sessions")
    raw.setdefault("daemon", {})["jobs_dir"] = str(tmp_path / "control" / "jobs")
    raw["daemon"]["afr_calibration"] = str(tmp_path / "data" / "afr_calibration.yaml")
    raw.setdefault("profiles", {})["registry_path"] = str(tmp_path / "profiles.yaml")
    raw["profiles"]["registry_overlay_path"] = str(tmp_path / "profiles.overlay.yaml")
    base = CanaryConfig.model_validate(raw)
    xterra = _scoped(base)
    tracked = source_registry_path(xterra)
    tracked.parent.mkdir(parents=True)
    real = Path("data") / XTERRA / "tune_revisions.yaml"
    hand = TuneRevision(revision="hand-added", flashed_at=AFTER)

    def write(path: Path, *extra: TuneRevision) -> None:
        registry = load_tune_revisions(real)
        assert registry is not None
        revisions = [r.model_dump(mode="json") for r in (*registry.revisions, *extra)]
        path.write_text(yaml.safe_dump({"schema_version": 1, "revisions": revisions}))

    legacy = _legacy_dir(base) / "tune_revisions.yaml"
    legacy.parent.mkdir(parents=True)
    write(legacy, FLASH)  # the dashboard flash, Pi-side
    write(tracked, hand)  # a flash added to the tracked file by hand
    assert legacy_adoption(xterra).adopted

    resolve_active_profile(base, probe=probe_vehicle_info, override_slug=XTERRA)
    migrated = load_tune_revisions(active_registry_path(xterra))
    assert active_registry_path(xterra) == operational_registry_path(xterra)
    assert migrated is not None and FLASH.revision in {r.revision for r in migrated.revisions}

    # Later, even an unrelated rewrite of the tracked file cannot orphan it.
    tracked.write_text(
        yaml.safe_dump({"schema_version": 1, "revisions": [hand.model_dump(mode="json")]})
    )
    assert legacy_adoption(xterra).adopted
    assert active_registry_path(xterra) == operational_registry_path(xterra)


def test_offline_tools_read_the_legacy_dir_without_migrating(tmp_path: Path) -> None:
    base = _base(tmp_path)
    xterra = _scoped(base)
    _write_legacy_registry(base, source_registry_path(xterra), FLASH)
    assert active_registry_path(xterra) == _legacy_dir(base) / "tune_revisions.yaml"
    assert not operational_dir(xterra).exists()
