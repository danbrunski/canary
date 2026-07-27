"""Tests for the /control/passive routes (CAN transmit gate)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.collection import tx_gate
from src.config import CanaryConfig
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


@pytest.fixture(autouse=True)
def _reset_gate() -> Iterator[None]:
    tx_gate.set_passive(False, source="test")
    tx_gate.reset_suppressed()
    yield
    tx_gate.set_passive(False, source="test")
    tx_gate.reset_suppressed()


def _api(tmp_path: Path) -> TestClient:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def test_passive_routes_require_auth(tmp_path: Path) -> None:
    client = _api(tmp_path)
    assert client.get("/control/passive").status_code == 401
    assert client.post("/control/passive", json={"enabled": True}).status_code == 401


def test_get_passive_reports_disarmed_by_default(tmp_path: Path) -> None:
    client = _api(tmp_path)
    body = client.get("/control/passive", headers=_auth()).json()["passive"]
    assert body["enabled"] is False
    assert body["suppressed_frames"] == 0


def test_post_passive_arms_and_disarms(tmp_path: Path) -> None:
    client = _api(tmp_path)

    armed = client.post("/control/passive", json={"enabled": True}, headers=_auth()).json()
    assert armed["passive"]["enabled"] is True
    assert armed["passive"]["source"] == "control"
    assert armed["previous_enabled"] is False
    assert tx_gate.is_passive() is True

    disarmed = client.post("/control/passive", json={"enabled": False}, headers=_auth()).json()
    assert disarmed["passive"]["enabled"] is False
    assert disarmed["previous_enabled"] is True
    assert tx_gate.is_passive() is False


def test_status_surfaces_passive_state(tmp_path: Path) -> None:
    """An operator who armed passive mode and forgot sees stale OBD everywhere
    else; /status is where the explanation has to live."""
    client = _api(tmp_path)
    client.post("/control/passive", json={"enabled": True}, headers=_auth())

    status = client.get("/status").json()

    assert status["passive"]["enabled"] is True


def test_tuning_mode_routes_require_auth(tmp_path: Path) -> None:
    client = _api(tmp_path)
    assert client.get("/control/tuning-mode").status_code == 401
    assert client.post("/control/tuning-mode", json={"enabled": True}).status_code == 401


def test_tuning_mode_arms_and_disarms(tmp_path: Path) -> None:
    from src.collection import tuning_profile

    client = _api(tmp_path)
    try:
        armed = client.post("/control/tuning-mode", json={"enabled": True}, headers=_auth()).json()
        assert armed["tuning_mode"]["active"] is True
        assert "RPM" in armed["tuning_mode"]["channels"]
        assert tuning_profile.is_active() is True

        # Status must surface it — a narrowed rotation looks exactly like a
        # collection fault from every other panel.
        assert client.get("/status").json()["tuning_mode"]["active"] is True

        off = client.post("/control/tuning-mode", json={"enabled": False}, headers=_auth()).json()
        assert off["tuning_mode"]["active"] is False
        assert off["previous_active"] is True
    finally:
        tuning_profile.set_active(False, source="test")


def test_tuning_mode_accepts_a_channel_override(tmp_path: Path) -> None:
    from src.collection import tuning_profile

    client = _api(tmp_path)
    try:
        body = {"enabled": True, "channels": ["RPM", "MAF"]}
        got = client.post("/control/tuning-mode", json=body, headers=_auth()).json()
        assert got["tuning_mode"]["channels"] == ["MAF", "RPM"]
    finally:
        tuning_profile.set_active(False, source="test")
