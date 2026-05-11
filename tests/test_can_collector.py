"""Tests for the CAN recorder.

Hardware-free: a python-can `VirtualBus` lets a sender feed frames to the
recorder over an in-memory channel. CI runs these without any USB device.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import can
import pytest

from src.collection.can_collector import (
    bus_factory,
    collect_can,
    collect_can_from_config,
    frame_to_record,
    make_session_dir,
    record,
)
from src.collection.reconnect import ReconnectPolicy
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


def test_frame_to_record_maps_can_message():
    msg = can.Message(
        timestamp=1_778_000_000.25,
        arbitration_id=0x123,
        data=b"\x01\x02\x03",
        is_extended_id=False,
    )
    record = frame_to_record(msg)
    assert record.source == "can"
    assert record.channel == "0x123"
    assert record.payload == b"\x01\x02\x03"
    assert record.metadata == {"can_id": 0x123, "dlc": 3}


@pytest.mark.asyncio
async def test_collect_can_enqueues_frames():
    class FakeBus:
        def __init__(self) -> None:
            self.messages = [
                can.Message(
                    timestamp=1_778_000_000.0,
                    arbitration_id=0x456,
                    data=b"\xaa\xbb",
                    is_extended_id=False,
                )
            ]

        def recv(self, timeout: float) -> can.Message | None:
            return self.messages.pop(0) if self.messages else None

    bus = FakeBus()
    queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(collect_can(bus, queue, stop, poll_interval=0.01))  # type: ignore[arg-type]

    record = await asyncio.wait_for(queue.get(), timeout=1.0)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert record.channel == "0x456"
    assert record.payload == b"\xaa\xbb"


@pytest.mark.asyncio
async def test_collect_can_from_config_retries_after_factory_failure():
    class FakeBus:
        def __init__(self) -> None:
            self.shutdown_called = False
            self.messages = [
                can.Message(
                    timestamp=1_778_000_000.0,
                    arbitration_id=0x789,
                    data=b"\x01\x02",
                    is_extended_id=False,
                )
            ]

        def recv(self, timeout: float) -> can.Message | None:
            return self.messages.pop(0) if self.messages else None

        def shutdown(self) -> None:
            self.shutdown_called = True

    attempts = 0
    bus = FakeBus()

    def factory(_spec: CanBusConfig) -> FakeBus:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("can adapter missing")
        return bus

    queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        collect_can_from_config(
            _virtual_spec("canary-test-reconnect"),
            queue,
            stop,
            poll_interval=0.01,
            factory=factory,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(
                initial_delay_seconds=0.01,
                max_delay_seconds=0.01,
            ),
        )
    )

    record = await asyncio.wait_for(queue.get(), timeout=1.0)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert attempts == 2
    assert record.channel == "0x789"
    assert bus.shutdown_called is True


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


class ExplodingBus:
    def recv(self, timeout: float) -> can.Message | None:
        raise RuntimeError("adapter disappeared")


class FlakyBus:
    """A bus that raises a parameterizable exception on every Nth recv to
    simulate slcan parser crashes. Good frames otherwise."""

    def __init__(
        self,
        bad_every: int = 3,
        total_calls: int = 12,
        exception: BaseException | None = None,
    ) -> None:
        self.calls = 0
        self.bad_every = bad_every
        self.total_calls = total_calls
        # Default: the original observed mode (decimal-parse of hex DLC).
        # Tests pin specific failure modes by passing exception=...
        self.exception: BaseException = (
            exception
            if exception is not None
            else ValueError("invalid literal for int() with base 10: 'C'")
        )

    def recv(self, timeout: float) -> can.Message | None:
        self.calls += 1
        if self.calls > self.total_calls:
            # Block to let the recorder's deadline fire cleanly.
            time.sleep(timeout)
            return None
        if self.calls % self.bad_every == 0:
            raise self.exception
        return can.Message(
            arbitration_id=0x100 + self.calls,
            data=bytes([self.calls & 0xFF] * 4),
            is_extended_id=False,
            timestamp=float(self.calls),
        )


def test_record_skips_malformed_frames_without_aborting(tmp_path: Path):
    """A single ValueError from python-can's slcan parser must not kill the run.
    The bad frames are counted and the recording continues."""
    spec = _virtual_spec("canary-test-flaky")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    result = record(
        FlakyBus(bad_every=3, total_calls=12),  # type: ignore[arg-type]
        paths,
        spec=spec,
        vehicle=_vehicle(),
        duration=0.3,
        poll_interval=0.02,
    )

    # 12 calls total, every 3rd is bad → 4 bad, 8 good
    assert result.frames_recorded == 8
    assert result.parse_errors == 4
    assert result.stopped_by == "deadline"

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["frames_recorded"] == 8
    assert manifest["parse_errors"] == 4
    assert manifest["stopped_by"] == "deadline"


def test_record_skips_index_error_from_truncated_slcan(tmp_path: Path):
    """python-can's slcan parser raises IndexError when an extended-frame line
    is truncated before the DLC byte (slcan.py:304). This was the second
    observed crash mode in real driving (commit cf4bca0 only caught the
    ValueError mode). Must be tolerated."""
    spec = _virtual_spec("canary-test-truncated")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    result = record(
        FlakyBus(
            bad_every=4,
            total_calls=12,
            exception=IndexError("string index out of range"),
        ),  # type: ignore[arg-type]
        paths,
        spec=spec,
        vehicle=_vehicle(),
        duration=0.3,
        poll_interval=0.02,
    )

    # Every 4th of 12 calls is bad → 3 bad, 9 good
    assert result.frames_recorded == 9
    assert result.parse_errors == 3
    assert result.stopped_by == "deadline"


def test_record_writes_error_manifest_when_recv_raises(tmp_path: Path):
    spec = _virtual_spec("canary-test-error")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    with pytest.raises(RuntimeError, match="adapter disappeared"):
        record(
            ExplodingBus(),  # type: ignore[arg-type]
            paths,
            spec=spec,
            vehicle=_vehicle(),
            duration=1.0,
            poll_interval=0.05,
        )

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "error"
    assert manifest["frames_recorded"] == 0
    assert manifest["error"] == "RuntimeError: adapter disappeared"
