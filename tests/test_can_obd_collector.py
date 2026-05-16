"""Tests for the Canable-native OBD streaming collector."""

from __future__ import annotations

import threading
import time
from typing import Any

import can
import pytest

from src.collection.can_obd_collector import (
    CanObdSpec,
    build_poll_requests,
    parse_can_id,
    parse_did_token,
    parse_pid_token,
    run_can_obd_loop,
)
from src.config import CanBusConfig


class _StaticDiscovery:
    def query_supported_mode_01_pids(self) -> tuple[int, ...]:
        return (0x0C, 0x05, 0x99)  # RPM + coolant + unsupported-by-decoder


class _FakeBus:
    def __init__(self) -> None:
        self.sent: list[can.Message] = []
        self._responses: list[can.Message] = []

    def send(self, msg: can.Message) -> None:
        self.sent.append(msg)
        data = bytes(msg.data)
        if msg.arbitration_id == 0x7DF and data[:3] == bytes([0x02, 0x01, 0x0C]):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x04, 0x41, 0x0C, 0x0A, 0xF0, 0x00, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )
        elif msg.arbitration_id == 0x7E0 and data[:4] == bytes([0x03, 0x22, 0x11, 0x01]):
            self._responses.append(
                can.Message(
                    arbitration_id=0x7E8,
                    data=bytes([0x05, 0x62, 0x11, 0x01, 0x12, 0x34, 0x00, 0x00]),
                    is_extended_id=False,
                )
            )

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        if self._responses:
            return self._responses.pop(0)
        if timeout:
            time.sleep(min(timeout, 0.005))
        return None


def test_parse_pid_token_accepts_auto_names_and_hex() -> None:
    assert parse_pid_token("auto") is None
    assert parse_pid_token("RPM") == 0x0C
    assert parse_pid_token("0x0c") == 0x0C
    assert parse_pid_token("0C") == 0x0C
    with pytest.raises(ValueError, match="unknown"):
        parse_pid_token("NOPE")


def test_parse_did_and_can_id() -> None:
    assert parse_did_token("1101") == 0x1101
    assert parse_can_id("7E0") == 0x7E0
    assert parse_can_id("0x7e1") == 0x7E1
    with pytest.raises(ValueError, match="4-character"):
        parse_did_token("110")


def test_build_poll_requests_combines_auto_explicit_and_dids() -> None:
    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        pids=("auto", "SPEED"),
        dids=("1101",),
    )
    requests = build_poll_requests(_StaticDiscovery(), spec)
    assert [r.channel for r in requests] == ["RPM", "COOLANT_TEMP", "SPEED", "DID_1101"]


def test_run_loop_emits_can_frames_pid_readings_and_did_readings() -> None:
    bus = _FakeBus()
    stop = threading.Event()
    frames: list[can.Message] = []
    readings: list[Any] = []

    def on_reading(reading: Any) -> None:
        readings.append(reading)
        if {r.pid for r in readings} >= {"RPM", "DID_1101"}:
            stop.set()

    spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        poll_rate_hz=20.0,
        pids=("RPM",),
        dids=("1101",),
        request_timeout_seconds=0.05,
    )

    stats = run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        spec,
        on_frame=frames.append,
        on_reading=on_reading,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 1.0,
    )

    by_pid = {reading.pid: reading for reading in readings}
    assert by_pid["RPM"].value == 700.0
    assert by_pid["DID_1101"].value == "1234"
    assert len(frames) >= 2
    assert stats.requests_sent >= 2
    assert stats.readings_emitted >= 2
