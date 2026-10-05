"""Tests for the /control/can_activity route (raw CAN activity capture)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import can
import pytest
from fastapi.testclient import TestClient

from src.collection.can_bus import bus_factory
from src.config import CanaryConfig, CanBusConfig
from src.control import can_activity
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


class _SteppedClock:
    """``time.monotonic`` for the capture loop: it moves one poll step per
    ``recv`` and never with the wall clock."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def monotonic(self) -> float:
        return self.now


class _ClockedBus:
    """The real virtual-bus handle the route opened. Each ``recv`` advances
    the stepped clock and takes what is already queued, without waiting."""

    def __init__(self, bus: can.BusABC, clock: _SteppedClock) -> None:
        self._bus = bus
        self._clock = clock

    def recv(self, timeout: float | None = None) -> can.Message | None:
        self._clock.now += self._clock.step
        return self._bus.recv(timeout=0)

    def shutdown(self) -> None:
        self._bus.shutdown()


def test_can_activity_reports_frame_rate_and_changed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 0.3 s window is the subject, so it runs on a clock that moves per
    poll: a sender thread on the wall clock could be starved through the whole
    window under load and deliver fewer than the two differing frames this
    asserts on (T01.15). Four frames are queued once the capture's handle is
    open; six polls of 0.05 s fill the window, so all four are counted."""
    channel = "canactivity-report"
    client = _api(tmp_path, channel=channel)
    clock = _SteppedClock(step=0.05)
    sent = [bytes([counter, 0xAA, 0, 0, 0, 0, 0, 0]) for counter in range(4)]

    def factory(can_config: CanBusConfig) -> _ClockedBus:
        bus = bus_factory(can_config)
        sender = can.interface.Bus(interface="virtual", channel=can_config.channel)
        try:
            for data in sent:
                sender.send(can.Message(arbitration_id=0x123, data=data, is_extended_id=False))
        finally:
            sender.shutdown()
        return _ClockedBus(bus, clock)

    monkeypatch.setattr(can_activity, "bus_factory", factory)
    monkeypatch.setattr(can_activity, "time", SimpleNamespace(monotonic=clock.monotonic))

    resp = client.post("/control/can_activity", json={"seconds": 0.3}, headers=_auth())

    assert resp.status_code == 200
    body = resp.json()["can_activity"]
    assert body["truncated"] is False
    assert body["duration_seconds"] == 0.3
    assert body["frames_captured"] == 4
    ids = {entry["id"]: entry for entry in body["ids"]}
    assert set(ids) == {"0x123"}
    entry = ids["0x123"]
    assert entry["changed_byte_indices"] == [0]
    assert entry["frame_count"] == 4
    assert entry["hz"] == round(4 / 0.3, 3)
    assert entry["first_hex"] == sent[0].hex()
    assert entry["last_hex"] == sent[-1].hex()


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
