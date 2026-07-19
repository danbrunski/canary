"""Tests for the /control/power routes (UPS status + keep-alive)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.config import CanaryConfig
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _api(
    tmp_path: Path,
) -> tuple[TestClient, Path, Path]:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    status_path = tmp_path / "ups_status"
    keepalive_path = tmp_path / "ups_keepalive"
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        ups_status_path=status_path,
        ups_keepalive_path=keepalive_path,
    )
    return TestClient(app), status_path, keepalive_path


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_get_power_requires_auth(tmp_path: Path) -> None:
    client, _status_path, _keepalive_path = _api(tmp_path)
    assert client.get("/control/power").status_code == 401


def test_get_power_no_watchdog_running(tmp_path: Path) -> None:
    client, _status_path, _keepalive_path = _api(tmp_path)
    body = client.get("/control/power", headers=_auth()).json()["power"]
    assert body["watchdog_running"] is False
    assert body["keepalive_remaining_s"] == 0.0


def test_get_power_reflects_watchdog_heartbeat(tmp_path: Path) -> None:
    client, status_path, _keepalive_path = _api(tmp_path)
    status_path.write_text(
        json.dumps(
            {
                "armed": True,
                "input_lost": False,
                "on_battery": False,
                "soc": 88.5,
                "vcell": 4.12,
                "seconds_lost": 0.0,
                "grace_s": 300.0,
                "ts": 0.0,
            }
        )
    )
    body = client.get("/control/power", headers=_auth()).json()["power"]
    assert body["watchdog_running"] is True
    assert body["armed"] is True
    assert body["soc"] == 88.5
    assert body["vcell"] == 4.12


def test_post_keepalive_sets_hold_and_get_reflects_it(tmp_path: Path) -> None:
    client, _status_path, keepalive_path = _api(tmp_path)
    resp = client.post("/control/power/keepalive", json={"minutes": 10}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["expiry"] > 0
    assert body["power"]["keepalive_remaining_s"] > 0
    assert keepalive_path.exists()

    follow_up = client.get("/control/power", headers=_auth()).json()["power"]
    assert follow_up["keepalive_remaining_s"] > 0


def test_post_keepalive_zero_cancels(tmp_path: Path) -> None:
    client, _status_path, _keepalive_path = _api(tmp_path)
    client.post("/control/power/keepalive", json={"minutes": 10}, headers=_auth())
    resp = client.post("/control/power/keepalive", json={"minutes": 0}, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["expiry"] == 0.0
    assert resp.json()["power"]["keepalive_remaining_s"] == 0.0


def test_post_keepalive_rejects_out_of_range(tmp_path: Path) -> None:
    client, _status_path, _keepalive_path = _api(tmp_path)
    assert (
        client.post("/control/power/keepalive", json={"minutes": -1}, headers=_auth()).status_code
        == 422
    )
    assert (
        client.post("/control/power/keepalive", json={"minutes": 9999}, headers=_auth()).status_code
        == 422
    )


def test_post_keepalive_rejects_extra_fields(tmp_path: Path) -> None:
    client, _status_path, _keepalive_path = _api(tmp_path)
    resp = client.post(
        "/control/power/keepalive", json={"minutes": 5, "bogus": True}, headers=_auth()
    )
    assert resp.status_code == 422


def test_post_keepalive_missing_dir_returns_503(tmp_path: Path) -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    unwritable = tmp_path / "does" / "not" / "exist" / "ups_keepalive"
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
        ups_status_path=tmp_path / "ups_status",
        ups_keepalive_path=unwritable,
    )
    client = TestClient(app)
    resp = client.post("/control/power/keepalive", json={"minutes": 5}, headers=_auth())
    assert resp.status_code == 503
