"""Tests for `capture_can_activity` (the `can_activity` RE tool)."""

from __future__ import annotations

import threading
import time

import can
import pytest

from src.config import CanBusConfig
from src.control.can_activity import CanActivityBusError, capture_can_activity


def _sender(
    channel: str, frames: list[tuple[int, bytes]], *, gap: float, stop: threading.Event
) -> None:
    bus = can.interface.Bus(interface="virtual", channel=channel)
    try:
        while not stop.is_set():
            for arb_id, data in frames:
                bus.send(can.Message(arbitration_id=arb_id, data=data, is_extended_id=False))
                time.sleep(gap)
    finally:
        bus.shutdown()


def test_capture_reports_per_id_frame_rate_and_changed_bytes() -> None:
    channel = "canary-activity-test"
    # 0x100: byte 0 counts up (changes), byte 1 is constant.
    frames = [
        (0x100, bytes([0x00, 0xAA, 0, 0, 0, 0, 0, 0])),
        (0x100, bytes([0x01, 0xAA, 0, 0, 0, 0, 0, 0])),
        (0x200, bytes([0x7F, 0x7F, 0, 0, 0, 0, 0, 0])),
    ]
    stop = threading.Event()
    t = threading.Thread(
        target=_sender, args=(channel, frames), kwargs={"gap": 0.01, "stop": stop}, daemon=True
    )
    t.start()
    try:
        result = capture_can_activity(
            CanBusConfig(interface="virtual", channel=channel, bitrate=500000),
            seconds=0.3,
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    by_id = {entry.arbitration_id: entry for entry in result.ids}
    assert 0x100 in by_id and 0x200 in by_id
    assert by_id[0x100].frame_count > 0
    assert by_id[0x100].changed_byte_indices == (0,)
    # 0x200 only ever sends one distinct frame — nothing changed.
    assert by_id[0x200].changed_byte_indices == ()
    assert result.frames_captured == by_id[0x100].frame_count + by_id[0x200].frame_count
    assert result.truncated is False


def test_capture_filters_by_id() -> None:
    channel = "canary-activity-filter-test"
    frames = [(0x100, bytes(8)), (0x200, bytes(8))]
    stop = threading.Event()
    t = threading.Thread(
        target=_sender, args=(channel, frames), kwargs={"gap": 0.01, "stop": stop}, daemon=True
    )
    t.start()
    try:
        result = capture_can_activity(
            CanBusConfig(interface="virtual", channel=channel, bitrate=500000),
            seconds=0.2,
            ids=[0x100],
        )
    finally:
        stop.set()
        t.join(timeout=1.0)

    assert {entry.arbitration_id for entry in result.ids} == {0x100}


def test_capture_raises_on_unopenable_bus() -> None:
    with pytest.raises(CanActivityBusError):
        capture_can_activity(
            CanBusConfig(
                interface="socketcan", channel="__canary_test_nonexistent__", bitrate=500000
            ),
            seconds=0.1,
        )
