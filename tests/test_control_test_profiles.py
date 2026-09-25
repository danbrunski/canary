"""Tests for the scanned YAML test-profile catalog.

Covers the loader (src/control/test_profiles.py), the /test-profiles
endpoint, and the guided_session.py decoupling (YAML profiles merged in as
protocols, no longer hardcoded).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from scripts.guided_session import PROTOCOLS, available_protocols
from src.config import CanaryConfig
from src.control.test_profiles import load_test_profiles
from src.dashboard.api import create_app
from tests.configs import isolated_config


def _make_config(tmp_path: Path) -> CanaryConfig:
    storage = tmp_path / "storage"
    storage.mkdir()
    # The shape of tests/test_dashboard_api.py's config: both detectors declared.
    return isolated_config(
        tmp_path,
        "daemon",
        storage=storage,
        models={"isolation_forest": {"enabled": True}, "lstm_autoencoder": {"enabled": True}},
        dashboard={"port": 8000},
    )


GOOD = """
id: city
label: "City drive"
description: "Stop-and-go."
protocol: drive
vehicle_state: driving
purpose: "city stop-and-go"
order: 3
events:
  - { label: "City driving", tag: city_stop_go, kind: state, hint: "surface streets" }
  - { label: "Hard accel", tag: hard_accel }
"""

EARLIER = """
id: full_drive
label: "Full drive"
order: 1
events:
  - { label: "Cold idle", tag: cold_start, kind: state }
"""


def test_load_good_profile(tmp_path: Path) -> None:
    (tmp_path / "city.yaml").write_text(GOOD)
    catalog = load_test_profiles(tmp_path)
    assert catalog.errors == []
    assert len(catalog.profiles) == 1
    p = catalog.profiles[0]
    assert p.id == "city"
    assert p.vehicle_state == "driving"
    assert p.events[0].kind == "state"
    assert p.events[1].kind == "event"  # schema default
    assert p.events[1].advisory_duration_s is None


def test_missing_and_empty_directory_are_empty(tmp_path: Path) -> None:
    assert load_test_profiles(tmp_path / "nope").profiles == []
    assert load_test_profiles(tmp_path).profiles == []  # exists, no files


def test_profiles_sorted_by_order_then_label(tmp_path: Path) -> None:
    (tmp_path / "city.yaml").write_text(GOOD)  # order 3
    (tmp_path / "full.yaml").write_text(EARLIER)  # order 1
    ids = [p.id for p in load_test_profiles(tmp_path).profiles]
    assert ids == ["full_drive", "city"]


def test_one_bad_file_does_not_blank_the_catalog(tmp_path: Path) -> None:
    (tmp_path / "good.yaml").write_text(GOOD)
    (tmp_path / "bad.yaml").write_text("id: x\nlabel: X\nunknown_key: 1\nevents: []\n")
    catalog = load_test_profiles(tmp_path)
    assert [p.id for p in catalog.profiles] == ["city"]
    assert len(catalog.errors) == 1
    assert catalog.errors[0].file == "bad.yaml"


def test_events_required(tmp_path: Path) -> None:
    (tmp_path / "noev.yaml").write_text("id: x\nlabel: X\nevents: []\n")
    catalog = load_test_profiles(tmp_path)
    assert catalog.profiles == []
    assert catalog.errors[0].file == "noev.yaml"


def test_duplicate_id_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(GOOD)
    (tmp_path / "b.yaml").write_text(GOOD)  # same id: city
    catalog = load_test_profiles(tmp_path)
    assert len(catalog.profiles) == 1
    assert "duplicate id" in catalog.errors[0].error


def test_empty_file_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "empty.yaml").write_text("")
    catalog = load_test_profiles(tmp_path)
    assert catalog.profiles == []
    assert catalog.errors[0].file == "empty.yaml"


def test_test_profiles_endpoint(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    anomalies_dir = tmp_path / "anomalies"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "city.yaml").write_text(GOOD)
    client = TestClient(
        create_app(config, anomalies_dir=anomalies_dir, test_profiles_dir=profiles_dir)
    )
    body = client.get("/test-profiles").json()
    assert body["profiles"][0]["id"] == "city"
    assert body["errors"] == []

    missing = create_app(config, anomalies_dir=anomalies_dir, test_profiles_dir=tmp_path / "absent")
    r = TestClient(missing).get("/test-profiles")
    assert r.status_code == 200
    assert r.json() == {"profiles": [], "errors": []}


def test_repo_seed_profiles_are_valid() -> None:
    """The committed data/test_profiles/ catalog must load cleanly."""
    catalog = load_test_profiles(Path("data/test_profiles"))
    assert catalog.errors == []
    ids = {p.id for p in catalog.profiles}
    assert {"full_drive", "city", "highway", "load_sweep"}.issubset(ids)


def test_guided_session_merges_yaml_profiles(tmp_path: Path) -> None:
    (tmp_path / "city.yaml").write_text(GOOD)
    merged = available_protocols(tmp_path)
    # Built-in RE walks survive...
    assert "rev" in merged and merged["rev"] is PROTOCOLS["rev"]
    # ...and the YAML profile is now a walkable protocol.
    assert "city" in merged
    steps = merged["city"]
    assert [s.label for s in steps] == ["city_stop_go", "hard_accel"]
    assert steps[0].description == "surface streets"  # hint wins over label


def test_guided_session_yaml_overrides_builtin(tmp_path: Path) -> None:
    (tmp_path / "drive.yaml").write_text(
        "id: drive\nlabel: D\nevents:\n  - { label: X, tag: only_step }\n"
    )
    merged = available_protocols(tmp_path)
    assert [s.label for s in merged["drive"]] == ["only_step"]
