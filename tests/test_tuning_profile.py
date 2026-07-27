"""Focused tuning poll profile — src/collection/tuning_profile.py.

Phase 1.1 of engine-tuning-support.md. The point is concentrating the bus
budget: normal polling lands TIMING_ADVANCE at 0.06 Hz, or ~10 samples per
rpm x load cell over a whole drive, which is not enough to conclude anything.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import can
import pytest

from src.collection import tuning_profile, tx_gate
from src.collection.can_obd_collector import run_can_obd_loop
from src.collection.obd_scheduler import CanObdSpec
from src.config import CanBusConfig


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    """Process-wide state; never leak between tests."""
    tuning_profile.set_active(False, source="test")
    tx_gate.set_passive(False, source="test")
    yield
    tuning_profile.set_active(False, source="test")
    tx_gate.set_passive(False, source="test")


class _FakeBus:
    """Answers every Mode 01 request so the loop keeps cycling."""

    def __init__(self) -> None:
        self.sent: list[can.Message] = []

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.sent.append(msg)

    def recv(self, timeout: float | None = None) -> can.Message | None:
        return None

    def shutdown(self) -> None:  # pragma: no cover - interface completeness
        pass

    def requested_pids(self) -> set[int]:
        """PID byte of each Mode 01 request put on the bus."""
        out = set()
        for m in self.sent:
            d = bytes(m.data)
            if len(d) >= 3 and d[1] == 0x01:
                out.add(d[2])
        return out


def _spec(**kw: Any) -> CanObdSpec:
    base: dict[str, Any] = {
        "can": CanBusConfig(interface="virtual", channel="test", bitrate=500000),
        "poll_rate_hz": 50.0,
        "request_timeout_seconds": 0.02,
    }
    base.update(kw)
    return CanObdSpec(**base)


def test_default_state_is_inactive() -> None:
    assert tuning_profile.is_active() is False
    snap = tuning_profile.snapshot()
    assert snap["active"] is False
    assert "RPM" in snap["channels"]


def test_set_active_returns_previous_state() -> None:
    assert tuning_profile.set_active(True, source="test") is False
    assert tuning_profile.set_active(True, source="test") is True
    assert tuning_profile.set_active(False, source="test") is True


def test_disabling_restores_the_default_channel_set() -> None:
    """A narrow one-off must not silently persist into the next capture."""
    tuning_profile.set_active(True, channel_names=frozenset({"RPM"}), source="test")
    assert tuning_profile.channels() == frozenset({"RPM"})

    tuning_profile.set_active(False, source="test")

    assert tuning_profile.channels() == tuning_profile.DEFAULT_TUNING_CHANNELS


def test_request_eligible_restricts_to_the_tuning_set() -> None:
    tuning_profile.set_active(True, source="test")

    class R:
        def __init__(self, channel: str) -> None:
            self.channel = channel

    assert tuning_profile.request_eligible(0, R("RPM")) is True
    assert tuning_profile.request_eligible(0, R("TIMING_ADVANCE")) is True
    assert tuning_profile.request_eligible(0, R("FUEL_LEVEL")) is False
    assert tuning_profile.request_eligible(0, R("CATALYST_TEMP_B1S1")) is False


def test_poll_loop_only_requests_tuning_channels_when_active() -> None:
    """The whole point: the bus rotation narrows to the tuning set."""
    bus = _FakeBus()
    stop = threading.Event()
    tuning_profile.set_active(True, source="test")

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "TIMING_ADVANCE", "FUEL_LEVEL", "CATALYST_TEMP_B1S1")),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.6,
    )

    pids = bus.requested_pids()
    assert 0x0C in pids, "RPM should be polled"
    assert 0x0E in pids, "TIMING_ADVANCE should be polled"
    assert 0x2F not in pids, "FUEL_LEVEL is outside the tuning set"
    assert 0x3C not in pids, "CATALYST_TEMP_B1S1 is outside the tuning set"


def test_poll_loop_polls_everything_when_inactive() -> None:
    """Symmetry: the restriction lifts cleanly."""
    bus = _FakeBus()
    stop = threading.Event()

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "FUEL_LEVEL")),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.6,
    )

    pids = bus.requested_pids()
    assert 0x0C in pids
    assert 0x2F in pids, "FUEL_LEVEL should be polled when tuning mode is off"


def test_tuning_mode_does_not_defeat_passive_mode() -> None:
    """Passive mode is a safety gate and must win. Tuning mode narrows WHICH
    channels are polled; it must never re-enable transmission."""
    bus = _FakeBus()
    stop = threading.Event()
    tx_gate.set_passive(True, source="test")
    tuning_profile.set_active(True, source="test")

    run_can_obd_loop(
        bus,  # type: ignore[arg-type]
        _spec(pids=("RPM", "TIMING_ADVANCE")),
        on_frame=lambda _m: None,
        on_reading=lambda _r: None,
        stop_event=stop,
        deadline_monotonic=time.monotonic() + 0.4,
    )

    assert bus.sent == []


def test_snapshot_carries_achieved_rate_when_supplied() -> None:
    """Phase 1.1 asks for the achieved per-channel Hz to be recorded."""
    tuning_profile.set_active(True, source="control")

    snap = tuning_profile.snapshot(achieved_hz={"RPM": 4.2})

    assert snap["active"] is True
    assert snap["source"] == "control"
    assert snap["achieved_hz"] == {"RPM": 4.2}
