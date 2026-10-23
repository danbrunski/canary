"""S03.1: one state root puts everything the daemon writes outside the checkout."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import (
    DAEMON_STATE_LAYOUT,
    DAEMON_TRACKED_PATHS,
    CanaryConfig,
    _daemon_path_fields,
)
from src.paths import REPO_ROOT
from src.vehicle.profiles import VehicleProfile, operational_dir, scope_config
from src.yaml_load import load_yaml

EXAMPLE = REPO_ROOT / "canary.example.yaml"
#: Not a location the daemon writes on its own: the root itself, and the
#: calibration directory, which is derived from ``jobs_dir`` when unset.
UNCLASSIFIED_BY_DESIGN = frozenset({"state_dir", "calibration_dir"})


def _config(**daemon: Any) -> CanaryConfig:
    raw = load_yaml(EXAMPLE.read_text())
    raw.setdefault("daemon", {}).update(daemon)
    return CanaryConfig.model_validate(raw)


def test_every_daemon_path_is_classified_exactly_once() -> None:
    fields = set(_daemon_path_fields())
    state = set(DAEMON_STATE_LAYOUT)
    assert not state & DAEMON_TRACKED_PATHS
    assert fields == state | DAEMON_TRACKED_PATHS | UNCLASSIFIED_BY_DESIGN


def test_unset_runtime_paths_resolve_under_state_dir(tmp_path: Path) -> None:
    config = _config(state_dir=str(tmp_path))
    for name, relative in DAEMON_STATE_LAYOUT.items():
        assert getattr(config.daemon, name) == tmp_path / relative, name
    assert config.profiles.registry_overlay_path == tmp_path / "control/profiles.yaml"
    assert config.llm_assist.cards_path == tmp_path / "analyses/llm_assist_cards.jsonl"


def test_an_explicit_path_wins_over_state_dir(tmp_path: Path) -> None:
    config = _config(state_dir=str(tmp_path), audit_log="/elsewhere/audit.jsonl")
    assert config.daemon.audit_log == Path("/elsewhere/audit.jsonl")
    assert config.daemon.jobs_dir == tmp_path / "control/jobs"


def test_tracked_inputs_stay_in_the_checkout(tmp_path: Path) -> None:
    config = _config(state_dir=str(tmp_path))
    for name in DAEMON_TRACKED_PATHS:
        assert getattr(config.daemon, name).is_relative_to(REPO_ROOT), name


def test_scoped_state_paths_never_name_the_checkout(tmp_path: Path) -> None:
    """After vehicle scoping too: the calibration dir follows jobs_dir."""
    config = scope_config(
        _config(state_dir=str(tmp_path)),
        VehicleProfile(slug="truck", vin="", name="Truck"),
    )
    for name in DAEMON_STATE_LAYOUT:
        path = getattr(config.daemon, name)
        assert path.is_relative_to(tmp_path), (name, path)
        assert "truck" in path.parts, (name, path)
    assert operational_dir(config) == tmp_path / "control/calibration/truck"
    assert config.daemon.behavior_models == tmp_path / "models/truck/behavior_models.yaml"


def test_investigation_memory_is_per_vehicle_state(tmp_path: Path) -> None:
    """E01: the memory ledger is runtime state, under ``state_dir``, per vehicle."""
    from src.evidence import memory_ledger_for

    assert DAEMON_STATE_LAYOUT["memory_ledger"] == Path("control/memory.jsonl")
    config = scope_config(
        _config(state_dir=str(tmp_path)),
        VehicleProfile(slug="truck", vin="", name="Truck"),
    )
    assert config.daemon.memory_ledger == tmp_path / "control/truck/memory.jsonl"
    assert memory_ledger_for(config).path == config.daemon.memory_ledger
    # E01.3: a replay's memory is derived beside it, isolated under ``_replay/``.
    replay = memory_ledger_for(config, replay_id="2026-10-04T120000").path
    assert replay == tmp_path / "control/truck/_replay/2026-10-04T120000.memory.jsonl"


def test_without_state_dir_the_laptop_defaults_are_unchanged() -> None:
    config = _config()
    assert config.daemon.state_dir is None
    assert config.daemon.models_dir == Path("models")
    assert config.daemon.behavior_models == REPO_ROOT / "data/behavior_models.yaml"


# --- S03.3: behavior models are Pi-owned, governed by tracked files ---------


def test_policy_beside_an_artifact_in_the_checkout_or_a_test_dir(tmp_path: Path) -> None:
    from src.behavior.behavior_models import BEHAVIOR_POLICY_FILENAME, behavior_policy_path
    from src.paths import DATA_DIR

    tracked = DATA_DIR / "nissan-xterra-2008" / "behavior_models.yaml"
    assert behavior_policy_path(tracked) == tracked.parent / BEHAVIOR_POLICY_FILENAME
    beside = tmp_path / "behavior_models.yaml"
    (tmp_path / BEHAVIOR_POLICY_FILENAME).write_text("entries: []\n")
    assert behavior_policy_path(beside) == tmp_path / BEHAVIOR_POLICY_FILENAME


def test_a_machine_copy_is_governed_by_the_tracked_profile_files(tmp_path: Path) -> None:
    """The Pi's /data/canary/models/<slug>/behavior_models.yaml has no policy or
    validation document beside it; the laptop-authored ones in data/<slug>/ apply."""
    from src.analysis.behavior_validation import validation_document_path
    from src.behavior.behavior_models import behavior_policy_path
    from src.paths import DATA_DIR

    machine_copy = tmp_path / "models" / "nissan-xterra-2008" / "behavior_models.yaml"
    profile = DATA_DIR / "nissan-xterra-2008"
    assert behavior_policy_path(machine_copy) == profile / "behavior_policy.yaml"
    assert validation_document_path(machine_copy) == profile / "behavior_validation.yaml"
    assert behavior_policy_path(machine_copy).exists()


def test_seeding_copies_the_tracked_artifact_once_and_never_overwrites(tmp_path: Path) -> None:
    from src.vehicle.profiles import profile_dir, seed_machine_owned_files

    state = tmp_path / "state"
    config = scope_config(
        _config(state_dir=str(state), afr_calibration=str(tmp_path / "data/afr_calibration.yaml")),
        VehicleProfile(slug="truck", vin="", name="Truck"),
    )
    tracked = profile_dir(config) / "behavior_models.yaml"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("schema_version: 1\nmodels: []\n")

    assert seed_machine_owned_files(config) == [config.daemon.behavior_models]
    assert config.daemon.behavior_models.read_text() == tracked.read_text()

    config.daemon.behavior_models.write_text("schema_version: 1\nmodels: []  # refit on the Pi\n")
    assert seed_machine_owned_files(config) == []
    assert "refit on the Pi" in config.daemon.behavior_models.read_text()


def test_retention_footer_ledger_is_per_vehicle_state(tmp_path: Path) -> None:
    """S01.5: the sweeper's verified-footer cache is written at runtime, so it
    lives under ``state_dir``, per vehicle, never in the checkout."""
    assert DAEMON_STATE_LAYOUT["retention_footer_ledger"] == Path("control/retention_footers.json")
    config = scope_config(
        _config(state_dir=str(tmp_path)),
        VehicleProfile(slug="truck", vin="", name="Truck"),
    )
    assert config.daemon.retention_footer_ledger == (
        tmp_path / "control/truck/retention_footers.json"
    )
