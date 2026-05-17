"""Tests for the Canable-native OBD streaming collector."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import can
import pytest

from src.collection.can_collector import make_session_dir
from src.collection.can_obd_collector import (
    CanObdSpec,
    build_poll_requests,
    parse_can_id,
    parse_did_token,
    parse_pid_token,
    record_can_obd,
    run_can_obd_loop,
)
from src.collection.obd_collector import JsonlReadingWriter
from src.collection.reconnect import ReconnectPolicy
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


# --- record_can_obd reconnect resilience -----------------------------------
#
# The Canable-OBD recorder path used by `scripts/record.py --obd-source
# canable` for the drive backlog must survive an in-vehicle USB
# re-enumeration the same way the CAN-only record() path does. Before the
# fix it had no reconnect at all, so a single OSError ended the drive
# silently mid-capture. These mirror the test_can_collector reconnect cases.

_SPEC = CanObdSpec(
    can=CanBusConfig(interface="virtual", channel="recon", bitrate=500000),
    poll_rate_hz=50.0,
    pids=("RPM",),  # explicit → no supported-PID walk, deterministic
    request_timeout_seconds=0.05,
)


class _ReconnectableObdBus:
    """Serves one CAN frame then raises OSError on recv, simulating a USB
    drop. Class counters let tests confirm the factory built fresh buses."""

    instances_built = 0
    instances_shutdown = 0

    def __init__(self, good_frames: int = 1) -> None:
        type(self).instances_built += 1
        self.calls = 0
        self.budget = good_frames

    def send(self, msg: can.Message) -> None:  # read-only OBD request; ignore
        pass

    def recv(self, timeout: float = 0.0) -> can.Message | None:
        self.calls += 1
        if self.calls > self.budget:
            raise OSError(5, "Input/output error")
        return can.Message(
            arbitration_id=0x100 + self.calls,
            data=bytes([self.calls & 0xFF] * 4),
            is_extended_id=False,
            timestamp=float(self.calls),
        )

    def shutdown(self) -> None:
        type(self).instances_shutdown += 1


def test_record_can_obd_auto_reconnects_on_bus_failure(tmp_path: Path) -> None:
    """OSError from a USB disconnect rebuilds the bus via factory instead of
    ending the recording; frames.log stays one continuous file."""
    _ReconnectableObdBus.instances_built = 0
    _ReconnectableObdBus.instances_shutdown = 0

    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    def factory(_spec: CanBusConfig) -> _ReconnectableObdBus:
        return _ReconnectableObdBus(good_frames=1)

    result = record_can_obd(
        factory(_SPEC.can),  # type: ignore[arg-type]
        paths,
        spec=_SPEC,
        vehicle=None,
        duration=0.4,
        obd_writer=writer,
        factory=factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.01,
            max_delay_seconds=0.01,
        ),
    )
    writer.close()

    assert result.reconnects >= 1, "factory should have been called for reconnect"
    assert result.stopped_by == "deadline"
    # Frames written across rebuilds land in one continuous file (data
    # integrity preserved even though the per-segment tally undercounts).
    assert paths.frames.exists()
    assert paths.frames.stat().st_size > 0
    # Failed buses shut down — no USB handle leaks.
    assert _ReconnectableObdBus.instances_shutdown >= result.reconnects

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["reconnects"] == result.reconnects
    assert manifest["stopped_by"] == "deadline"


def test_record_can_obd_bails_after_max_consecutive_reconnect_failures(
    tmp_path: Path,
) -> None:
    """A persistently broken adapter must stop with reconnect_exhausted
    rather than spin forever (the wedged-USB / brown-out failure mode)."""
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    factory_calls = 0

    def failing_factory(_spec: CanBusConfig) -> _ReconnectableObdBus:
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError(f"adapter wedged (attempt {factory_calls})")

    result = record_can_obd(
        _ReconnectableObdBus(good_frames=1),  # type: ignore[arg-type]
        paths,
        spec=_SPEC,
        vehicle=None,
        duration=10.0,  # generous; we expect to bail well before this
        obd_writer=writer,
        factory=failing_factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.005,
            max_delay_seconds=0.005,
        ),
        max_reconnects=3,
    )
    writer.close()

    assert result.stopped_by == "reconnect_exhausted"
    assert factory_calls == 3  # exactly max_reconnects attempts
    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "reconnect_exhausted"
    assert "adapter wedged" in manifest["error"]


def test_record_can_obd_without_factory_propagates(tmp_path: Path) -> None:
    """Backward-compat: with no factory/policy a bus failure propagates as
    before (the daemon path's long-standing behavior), and the manifest is
    still written with stopped_by='error'."""
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    with pytest.raises(OSError):
        record_can_obd(
            _ReconnectableObdBus(good_frames=1),  # type: ignore[arg-type]
            paths,
            spec=_SPEC,
            vehicle=None,
            duration=0.4,
            obd_writer=writer,
        )
    writer.close()

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "error"
    assert manifest["reconnects"] == 0


def test_record_can_obd_reraises_config_value_error(tmp_path: Path) -> None:
    """A config error (unknown PID) must propagate even with reconnect
    enabled — it is not a transient bus fault to retry forever."""
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    writer = JsonlReadingWriter(paths.pids)

    bad_spec = CanObdSpec(
        can=CanBusConfig(interface="virtual", channel="recon", bitrate=500000),
        pids=("NOPE",),
    )

    with pytest.raises(ValueError, match="unknown"):
        record_can_obd(
            _ReconnectableObdBus(good_frames=1),  # type: ignore[arg-type]
            paths,
            spec=bad_spec,
            vehicle=None,
            duration=0.4,
            obd_writer=writer,
            factory=lambda _s: _ReconnectableObdBus(),  # type: ignore[arg-type,return-value]
            reconnect_policy=ReconnectPolicy(
                initial_delay_seconds=0.01, max_delay_seconds=0.01
            ),
        )
    writer.close()
