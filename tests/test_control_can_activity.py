"""Tests for the /control/can_activity route (raw CAN activity capture)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import can
from fastapi.testclient import TestClient

from src.config import CanaryConfig, CanBusConfig
from src.dashboard.api import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _api(tmp_path: Path, *, channel: str) -> TestClient:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    config.hardware.can = CanBusConfig(interface="virtual", channel=channel, bitrate=500000)
    app = create_app(
        config,
        anomalies_dir=tmp_path / "anomalies",
        control_token="secret",
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer secret"}


def _sender(channel: str, *, stop: threading.Event) -> None:
    bus = can.interface.Bus(interface="virtual", channel=channel)
    try:
        counter = 0
        while not stop.is_set():
            bus.send(
                can.Message(
                    arbitration_id=0x123,
                    data=bytes([counter % 256, 0xAA, 0, 0, 0, 0, 0, 0]),
                    is_extended_id=False,
                )
            )
            counter += 1
            time.sleep(0.01)
    finally:
        bus.shutdown()


def test_can_activity_requires_auth(tmp_path: Path) -> None:
    client = _api(tmp_path, channel="canactivity-auth")
    resp = client.post("/control/can_activity", json={"seconds": 0.1})
    assert resp.status_code == 401


def test_can_activity_rejects_seconds_above_cap(tmp_path: Path) -> None:
    client = _api(tmp_path, channel="canactivity-cap")
    resp = client.post("/control/can_activity", json={"seconds": 31}, headers=_auth())
    assert resp.status_code == 422


def test_can_activity_reports_frame_rate_and_changed_bytes(tmp_path: Path) -> None:
    channel = "canactivity-report"
    client = _api(tmp_path, channel=channel)
    stop = threading.Event()
    t = threading.Thread(target=_sender, args=(channel,), kwargs={"stop": stop}, daemon=True)
    t.start()
    try:
        resp = client.post("/control/can_activity", json={"seconds": 0.3}, headers=_auth())
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert resp.status_code == 200
    body = resp.json()["can_activity"]
    assert body["truncated"] is False
    ids = {entry["id"]: entry for entry in body["ids"]}
    assert "0x123" in ids
    assert ids["0x123"]["changed_byte_indices"] == [0]
    assert ids["0x123"]["frame_count"] > 0


def test_can_activity_filters_by_id(tmp_path: Path) -> None:
    channel = "canactivity-filter"
    client = _api(tmp_path, channel=channel)
    stop = threading.Event()
    t = threading.Thread(target=_sender, args=(channel,), kwargs={"stop": stop}, daemon=True)
    t.start()
    try:
        resp = client.post(
            "/control/can_activity",
            json={"seconds": 0.2, "ids": [0x456]},
            headers=_auth(),
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert resp.status_code == 200
    assert resp.json()["can_activity"]["ids"] == []
