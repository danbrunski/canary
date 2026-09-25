"""R05.17 — the tracked and operational flash registries are merged on read.

Before: once ``append_flash`` created the per-vehicle operational registry it
won outright, so a revision committed to the tracked
``data/<profile>/tune_revisions.yaml`` afterwards (the established hand-edit
workflow) was invisible to every ``active_registry_path`` reader. Making the
tracked file the only source instead would have dropped the dashboard-recorded
flashes. Both directions are pinned here, and a name recorded as two different
flashes refuses naming both files.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from src.analysis.tune_revisions import (
    RegistryConflictError,
    TuneRevision,
    TuneRevisionRegistry,
    load_tune_revisions,
    merge_tune_registries,
)
from src.collection import tuning_profile
from src.config import CanaryConfig
from src.control.calibration import append_flash, calibration_payload, calibration_status
from src.profiles import (
    active_registry,
    active_registry_sources,
    operational_registry_path,
    resolve_profile_for_tooling,
    scope_config,
    source_registry_path,
)

STOCK = TuneRevision(
    revision="stock", calibration_id="1ZS04C", flashed_at=datetime(2026, 7, 1, tzinfo=UTC)
)
DASHBOARD = TuneRevision(
    revision="dash-flash", calibration_id="1ZS44A", flashed_at=datetime(2026, 9, 20, tzinfo=UTC)
)
HAND = TuneRevision(
    revision="hand-added", calibration_id="1ZS44B", flashed_at=datetime(2026, 9, 25, tzinfo=UTC)
)


@pytest.fixture(autouse=True)
def _no_running_revision_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tuning_profile, "_running_tune_revision", None)
    monkeypatch.setattr(tuning_profile, "_registry_calibration_id", None)


def _config(tmp_path: Path) -> CanaryConfig:
    raw = yaml.safe_load(Path("canary.example.yaml").read_text())
    raw.setdefault("collection", {})["storage_path"] = str(tmp_path / "sessions")
    raw.setdefault("daemon", {})["jobs_dir"] = str(tmp_path / "control" / "jobs")
    raw["daemon"]["afr_calibration"] = str(tmp_path / "data" / "afr_calibration.yaml")
    raw.setdefault("profiles", {})["registry_path"] = str(tmp_path / "profiles.yaml")
    base = CanaryConfig.model_validate(raw)
    return scope_config(base, resolve_profile_for_tooling(base, None))


def _write(path: Path, *revisions: TuneRevision) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "revisions": [r.model_dump(mode="json") for r in revisions]},
            sort_keys=False,
        )
    )


def _names(registry: TuneRevisionRegistry | None) -> list[str]:
    assert registry is not None
    return [r.revision for r in registry.revisions]


def test_a_flash_hand_added_to_the_tracked_file_after_migration_is_seen(tmp_path: Path) -> None:
    """The R05.17 defect: the operational copy used to hide it."""
    config = _config(tmp_path)
    _write(source_registry_path(config), STOCK)
    append_flash(config, DASHBOARD)  # seeds the operational copy from the tracked file
    _write(source_registry_path(config), STOCK, HAND)  # later, a committed hand edit

    merged = active_registry(config)

    assert _names(merged) == ["stock", "dash-flash", "hand-added"]
    assert merged is not None and merged.at(datetime(2026, 9, 26, tzinfo=UTC)) == HAND
    assert calibration_status(config)["running_revision"] == "hand-added"
    assert active_registry_sources(config) == (
        operational_registry_path(config),
        source_registry_path(config),
    )


def test_dashboard_flashes_survive_a_tracked_file_that_never_heard_of_them(
    tmp_path: Path,
) -> None:
    """The other direction: the tracked file must not become the only source."""
    config = _config(tmp_path)
    _write(source_registry_path(config), STOCK)
    append_flash(config, DASHBOARD)
    _write(source_registry_path(config), STOCK, HAND)

    assert "dash-flash" in _names(active_registry(config))
    history = [entry["revision"] for entry in calibration_payload(config)["history"]]
    assert history == ["hand-added", "dash-flash", "stock"]  # newest first


def test_merge_orders_by_flashed_at_not_by_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    earlier = TuneRevision(revision="earlier", flashed_at=datetime(2026, 8, 1, tzinfo=UTC))
    _write(operational_registry_path(config), STOCK, DASHBOARD)
    _write(source_registry_path(config), STOCK, earlier)

    assert _names(active_registry(config)) == ["stock", "earlier", "dash-flash"]


def test_one_name_recorded_as_two_flashes_refuses_naming_both_files(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write(operational_registry_path(config), STOCK, DASHBOARD)
    retyped = DASHBOARD.model_copy(update={"calibration_id": "1ZS99Z"})
    _write(source_registry_path(config), STOCK, retyped)

    with pytest.raises(RegistryConflictError) as caught:
        active_registry(config)

    err = caught.value
    assert err.revision == "dash-flash"
    assert err.fields == ("calibration_id",)
    assert err.paths == (operational_registry_path(config), source_registry_path(config))
    assert str(operational_registry_path(config)) in str(err)
    assert str(source_registry_path(config)) in str(err)


def test_a_conflict_is_a_loud_status_alert_not_a_broken_status(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write(operational_registry_path(config), STOCK, DASHBOARD)
    moved = DASHBOARD.model_copy(update={"flashed_at": datetime(2026, 9, 21, tzinfo=UTC)})
    _write(source_registry_path(config), STOCK, moved)

    status = calibration_status(config)

    assert status["calibration_alert"] is True
    assert "flash history refused" in status["calibration_alert_reason"]
    assert str(source_registry_path(config)) in status["calibration_alert_reason"]
    assert status["running_revision"] is None
    assert calibration_payload(config)["history"] == []


def test_a_notes_edit_in_the_tracked_file_is_not_a_conflict(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write(operational_registry_path(config), STOCK, DASHBOARD)
    _write(source_registry_path(config), STOCK.model_copy(update={"notes": "reworded"}))

    merged = active_registry(config)

    assert merged is not None and merged.revisions[0].notes == "reworded"


def test_append_flash_refuses_a_name_only_the_tracked_file_holds(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write(source_registry_path(config), STOCK)
    append_flash(config, DASHBOARD)
    _write(source_registry_path(config), STOCK, HAND)

    with pytest.raises(ValueError, match="already exists"):
        append_flash(config, HAND)


def test_append_flash_writes_the_merged_history_through(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write(source_registry_path(config), STOCK)
    append_flash(config, DASHBOARD)
    _write(source_registry_path(config), STOCK, HAND)
    later = TuneRevision(revision="later", flashed_at=datetime(2026, 10, 1, tzinfo=UTC))

    append_flash(config, later)

    written = load_tune_revisions(operational_registry_path(config))
    assert _names(written) == ["stock", "dash-flash", "hand-added", "later"]


def test_merge_with_one_side_absent_is_the_other_side() -> None:
    only = TuneRevisionRegistry(revisions=[STOCK])
    path = Path("x.yaml")
    assert merge_tune_registries(None, path, only, path) is only
    assert merge_tune_registries(only, path, None, path) is only
    assert merge_tune_registries(None, path, None, path) is None


def test_no_operational_copy_reads_the_tracked_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write(source_registry_path(config), STOCK, HAND)

    assert _names(active_registry(config)) == ["stock", "hand-added"]
    assert active_registry_sources(config) == (source_registry_path(config),)


def test_labelled_flash_events_come_from_the_merged_history(tmp_path: Path) -> None:
    """The detector evaluation's flash boundaries used to read one file."""
    from src.analysis.labelled_events import load_labelled_events

    config = _config(tmp_path)
    _write(source_registry_path(config), STOCK)
    append_flash(config, DASHBOARD)
    _write(source_registry_path(config), STOCK, HAND)

    registry = load_labelled_events(
        tmp_path / "no-labels.yaml",
        tune_registry=active_registry(config),
        tune_sources=active_registry_sources(config),
    )

    assert [event.id for event in registry.events] == ["flash-dash-flash", "flash-hand-added"]
    assert str(source_registry_path(config)) in registry.events[0].provenance
    assert str(operational_registry_path(config)) in registry.events[0].provenance


def test_the_memory_channel_payload_survives_a_registry_conflict(tmp_path: Path) -> None:
    from src.control.calibration import memory_channels_payload
    from src.paths import DIDS_YAML

    config = _config(tmp_path)
    _write(operational_registry_path(config), STOCK, DASHBOARD)
    retyped = DASHBOARD.model_copy(update={"calibration_id": "1ZS99Z"})
    _write(source_registry_path(config), STOCK, retyped)

    payload = memory_channels_payload(config, DIDS_YAML)

    assert payload["running_revision"] is None
    assert payload["count"] == len(payload["channels"])
