"""Tests for the /control/trigger_poll route (force-poll on demand)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from src.collection.obd_scheduler import ForcePollTrigger
from src.config import CanaryConfig
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_trigger_poll_requires_auth(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    client = TestClient(app)
    resp = client.post("/control/trigger_poll", json={"channels": ["RPM"]})
    assert resp.status_code == 401


def test_trigger_poll_requires_at_least_one_channel(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    trigger = ForcePollTrigger()
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        force_poll_trigger=trigger,
    )
    client = TestClient(app)
    resp = client.post("/control/trigger_poll", json={"channels": []}, headers=_auth())
    assert resp.status_code == 422


def test_trigger_poll_rejects_non_canable_obd_source(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.collection.obd_source = "disabled"
    trigger = ForcePollTrigger()
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        force_poll_trigger=trigger,
    )
    client = TestClient(app)
    resp = client.post("/control/trigger_poll", json={"channels": ["RPM"]}, headers=_auth())
    assert resp.status_code == 409


def test_trigger_poll_returns_503_when_collector_not_running(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    app = create_app(config, anomalies_dir=tmp_path / "anomalies", control_token="secret")
    client = TestClient(app)
    resp = client.post("/control/trigger_poll", json={"channels": ["RPM"]}, headers=_auth())
    assert resp.status_code == 503


def test_trigger_poll_queues_the_requested_channels(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    trigger = ForcePollTrigger()
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        force_poll_trigger=trigger,
    )
    client = TestClient(app)
    resp = client.post(
        "/control/trigger_poll",
        json={"channels": ["RPM", "DTC_STORED"]},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["trigger_poll"]["queued_channels"] == ["RPM", "DTC_STORED"]
    assert trigger.take() == frozenset({"RPM", "DTC_STORED"})
