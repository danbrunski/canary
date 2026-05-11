"""Tests for the CAN recorder.

Hardware-free: a python-can `VirtualBus` lets a sender feed frames to the
recorder over an in-memory channel. CI runs these without any USB device.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import can
import pytest

from src.collection.can_collector import (
    bus_factory,
    make_session_dir,
    record,
)
from src.config import CanBusConfig, VehicleConfig

VIRTUAL_CHANNEL = "canary-test"


def _virtual_spec(channel: str = VIRTUAL_CHANNEL) -> CanBusConfig:
    return CanBusConfig(interface="virtual", channel=channel, bitrate=500_000)


def _vehicle() -> VehicleConfig:
    return VehicleConfig(make="Nissan", model="Xterra", year=2008)


def test_bus_factory_virtual_constructs_bus():
    bus = bus_factory(_virtual_spec())
    try:
        assert isinstance(bus, can.BusABC)
    finally:
        bus.shutdown()


def test_make_session_dir_creates_layout(tmp_path: Path):
    started = datetime(2026, 5, 10, 20, 30, 45, tzinfo=UTC)
    paths = make_session_dir(tmp_path, started)
    assert paths.root == tmp_path / "2026-05-10T203045"
    assert paths.root.is_dir()
    assert paths.frames == paths.root / "frames.log"
    assert paths.manifest == paths.root / "manifest.json"


def test_make_session_dir_refuses_collision(tmp_path: Path):
    started = datetime(2026, 5, 10, 20, 30, 45, tzinfo=UTC)
    make_session_dir(tmp_path, started)
    # second call at the same instant must not silently reuse — that would
    # mix frames from two recordings into one log.
    with pytest.raises(FileExistsError):
        make_session_dir(tmp_path, started)


def test_record_captures_frames_and_writes_manifest(tmp_path: Path):
    """End-to-end: a sender thread pumps frames; the recorder captures them
    into frames.log and writes a manifest with correct counts."""
    spec = _virtual_spec("canary-test-capture")
    rx_bus = bus_factory(spec)
    tx_bus = bus_factory(spec)
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    sender_stop = threading.Event()
    sent_count = 0

    def sender() -> None:
        nonlocal sent_count
        i = 0
        while not sender_stop.is_set():
            msg = can.Message(
                arbitration_id=0x100 + (i % 4),
                data=bytes([(i + j) & 0xFF for j in range(8)]),
                is_extended_id=False,
            )
            tx_bus.send(msg)
            sent_count += 1
            i += 1
            time.sleep(0.005)

    sender_thread = threading.Thread(target=sender, daemon=True)
    try:
        sender_thread.start()
        # Small lead-in so a few frames are queued by the time recording starts.
        time.sleep(0.05)
        result = record(
            rx_bus,
            paths,
            spec=spec,
            vehicle=_vehicle(),
            duration=0.5,
            poll_interval=0.05,
        )
    finally:
        sender_stop.set()
        sender_thread.join(timeout=2.0)
        rx_bus.shutdown()
        tx_bus.shutdown()

    assert result.frames_recorded > 0
    assert result.stopped_by == "deadline"
    assert paths.frames.exists()
    assert paths.manifest.exists()

    log_lines = [line for line in paths.frames.read_text().splitlines() if line.strip()]
    assert len(log_lines) == result.frames_recorded
    # candump format: "(timestamp) channel id#hex"
    assert all("#" in line for line in log_lines)

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["frames_recorded"] == result.frames_recorded
    assert manifest["bus"]["interface"] == "virtual"
    assert manifest["bus"]["channel"] == "canary-test-capture"
    assert manifest["vehicle"]["make"] == "Nissan"
    assert manifest["stopped_by"] == "deadline"
    assert manifest["duration_seconds"] >= 0.4


def test_record_stops_on_event_before_deadline(tmp_path: Path):
    """stop_event takes precedence over duration; manifest reflects 'signal'."""
    spec = _virtual_spec("canary-test-stop")
    bus = bus_factory(spec)
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    stop = threading.Event()

    # Fire the stop event after a short delay; duration is intentionally long
    # so the deadline can't win.
    threading.Timer(0.2, stop.set).start()

    try:
        result = record(
            bus,
            paths,
            spec=spec,
            vehicle=_vehicle(),
            duration=10.0,
            stop_event=stop,
            poll_interval=0.05,
        )
    finally:
        bus.shutdown()

    assert result.stopped_by == "signal"
    assert (result.ended_at - result.started_at).total_seconds() < 1.0

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "signal"
    assert manifest["frames_recorded"] == 0  # nothing was sending


def test_record_writes_manifest_even_with_zero_frames(tmp_path: Path):
    spec = _virtual_spec("canary-test-empty")
    bus = bus_factory(spec)
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    try:
        result = record(
            bus,
            paths,
            spec=spec,
            vehicle=None,  # also exercises the "no vehicle metadata" path
            duration=0.2,
            poll_interval=0.05,
        )
    finally:
        bus.shutdown()

    assert result.frames_recorded == 0
    manifest = json.loads(paths.manifest.read_text())
    assert manifest["frames_recorded"] == 0
    assert manifest["vehicle"] is None
    assert manifest["vehicle_state"] is None  # default when not passed


def test_record_persists_vehicle_state_in_manifest(tmp_path: Path):
    spec = _virtual_spec("canary-test-state")
    bus = bus_factory(spec)
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    try:
        record(
            bus,
            paths,
            spec=spec,
            vehicle=_vehicle(),
            vehicle_state="engine_running_idle",
            duration=0.1,
            poll_interval=0.05,
        )
    finally:
        bus.shutdown()

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["vehicle_state"] == "engine_running_idle"
