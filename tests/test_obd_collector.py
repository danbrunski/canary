"""Tests for the OBD-II parallel-capture collector.

Real ELM327 testing happens at the bench. These tests cover everything
testable without hardware:
  * ObdReading serialization
  * JsonlReadingWriter file output
  * Unknown-PID validation
  * The python-obd `OBDResponse → callback` adapter (with a fake response)
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.collection.obd_collector import (
    JsonlReadingWriter,
    ObdReading,
    ObdSpec,
    _make_callback,
    collect_obd,
    obd_factory,
    queue_reading_callback,
    reading_to_record,
    resolve_pids,
    supervise_obd_capture,
)
from src.collection.queueing import QueueDropCounter
from src.collection.reconnect import ReconnectPolicy

# ---------------------------------------------------------------------------
# Reading + writer
# ---------------------------------------------------------------------------


def test_obd_reading_serializes_to_json_line():
    r = ObdReading(timestamp=1700000000.5, pid="RPM", value=752.0, unit="rpm")
    parsed = json.loads(r.to_json_line())
    assert parsed == {"t": 1700000000.5, "pid": "RPM", "value": 752.0, "unit": "rpm"}


def test_obd_reading_handles_string_value():
    """VIN and similar PIDs return strings, not floats."""
    r = ObdReading(timestamp=1.0, pid="VIN", value="JN8AR05Y...", unit="")
    assert json.loads(r.to_json_line())["value"] == "JN8AR05Y..."


def test_reading_to_record_maps_obd_reading():
    record = reading_to_record(
        ObdReading(timestamp=1_778_000_000.0, pid="RPM", value=752.0, unit="rpm")
    )
    assert record.source == "obd"
    assert record.channel == "RPM"
    assert record.value == 752.0
    assert record.unit == "rpm"


@pytest.mark.asyncio
async def test_queue_reading_callback_enqueues_threadsafe():
    queue = asyncio.Queue()
    callback = queue_reading_callback(queue, asyncio.get_running_loop())

    callback(ObdReading(timestamp=1_778_000_000.0, pid="RPM", value=700.0, unit="rpm"))
    record = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert record.channel == "RPM"
    assert record.value == 700.0


@pytest.mark.asyncio
async def test_queue_reading_callback_counts_full_queue_drop():
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    counter = QueueDropCounter("obd-test")
    callback = queue_reading_callback(queue, asyncio.get_running_loop(), on_drop=counter)

    callback(ObdReading(timestamp=1_778_000_000.0, pid="RPM", value=700.0, unit="rpm"))
    await asyncio.sleep(0)
    callback(ObdReading(timestamp=1_778_000_001.0, pid="RPM", value=701.0, unit="rpm"))
    await asyncio.sleep(0)

    assert counter.dropped == 1


def test_jsonl_writer_appends_one_line_per_reading(tmp_path: Path):
    path = tmp_path / "pids.jsonl"
    w = JsonlReadingWriter(path)
    try:
        w.write(ObdReading(1.0, "RPM", 800.0, "rpm"))
        w.write(ObdReading(1.5, "COOLANT_TEMP", 95.0, "°C"))
    finally:
        w.close()

    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["pid"] == "RPM"
    assert json.loads(lines[1])["pid"] == "COOLANT_TEMP"


def test_jsonl_writer_creates_parent_dirs(tmp_path: Path):
    path = tmp_path / "nested" / "deep" / "pids.jsonl"
    w = JsonlReadingWriter(path)
    try:
        w.write(ObdReading(1.0, "RPM", 800.0, "rpm"))
    finally:
        w.close()
    assert path.exists()


def test_jsonl_writer_flushes_per_write(tmp_path: Path):
    """A crash mid-session should lose at most the last in-flight line."""
    path = tmp_path / "pids.jsonl"
    w = JsonlReadingWriter(path)
    try:
        w.write(ObdReading(1.0, "RPM", 800.0, "rpm"))
        # Without close(), can we still read what was written?
        assert path.read_text().strip(), "writer should flush per write, not buffer"
    finally:
        w.close()


# ---------------------------------------------------------------------------
# resolve_pids
# ---------------------------------------------------------------------------


def test_resolve_pids_known_commands():
    cmds = resolve_pids(("RPM", "COOLANT_TEMP", "MAF"))
    assert len(cmds) == 3
    # python-obd commands have a `name` attr
    assert all(hasattr(c, "name") for c in cmds)


def test_resolve_pids_unknown_command_raises_with_helpful_message():
    with pytest.raises(ValueError, match="unknown OBD command 'RMP'"):
        resolve_pids(("RPM", "RMP"))  # typo in second item


# ---------------------------------------------------------------------------
# _make_callback: pint Quantity → ObdReading
# ---------------------------------------------------------------------------


@dataclass
class _FakeQuantity:
    """Stand-in for pint.Quantity — only the attrs python-obd uses."""

    magnitude: float
    units: Any


@dataclass
class _FakeResponse:
    """Stand-in for obd.OBDResponse with the methods our adapter touches."""

    value: Any
    null: bool = False

    def is_null(self) -> bool:
        return self.null


def test_callback_unwraps_pint_quantity_into_value_and_unit():
    captured: list[ObdReading] = []
    cb = _make_callback("RPM", captured.append)
    cb(_FakeResponse(value=_FakeQuantity(magnitude=752.0, units="rpm")))
    assert len(captured) == 1
    r = captured[0]
    assert r.pid == "RPM"
    assert r.value == 752.0
    assert r.unit == "rpm"
    assert r.timestamp > 0


def test_callback_drops_dimensionless_unit_string():
    """python-obd uses 'dimensionless' for unitless PIDs (e.g. THROTTLE_POS %)
    but we'd rather store an empty string than that token."""
    captured: list[ObdReading] = []
    cb = _make_callback("ENGINE_LOAD", captured.append)
    cb(_FakeResponse(value=_FakeQuantity(magnitude=42.0, units="dimensionless")))
    assert captured[0].unit == ""


def test_callback_skips_null_responses():
    captured: list[ObdReading] = []
    cb = _make_callback("RPM", captured.append)
    cb(_FakeResponse(value=None, null=True))
    cb(None)  # also None response (rare but possible)
    assert captured == []


def test_callback_passes_through_string_values():
    """VIN-style responses come back as strings, not Quantities."""
    captured: list[ObdReading] = []
    cb = _make_callback("VIN", captured.append)
    cb(_FakeResponse(value="JN8AR05Y8XW123456"))
    assert captured[0].value == "JN8AR05Y8XW123456"
    assert captured[0].unit == ""


# ---------------------------------------------------------------------------
# obd_factory: validation runs before connection (so typos fail loud)
# ---------------------------------------------------------------------------


def test_obd_factory_rejects_unknown_pid_before_opening_connection():
    """Don't burn a connect attempt on a session that's going to fail anyway."""
    spec = ObdSpec(port="/dev/ttyUSB0", pids=("RPM", "BOGUS_PID"))
    with patch("src.collection.obd_collector.obd.Async") as fake_async:
        with pytest.raises(ValueError, match="unknown OBD command 'BOGUS_PID'"):
            obd_factory(spec, on_reading=lambda _r: None)
        fake_async.assert_not_called()


def test_obd_factory_raises_when_adapter_does_not_connect():
    spec = ObdSpec(port="/dev/ttyUSB-fake", pids=("RPM",))
    with patch("src.collection.obd_collector.obd.Async") as fake_async:
        instance = fake_async.return_value
        instance.is_connected.return_value = False
        with pytest.raises(RuntimeError, match="did not connect"):
            obd_factory(spec, on_reading=lambda _r: None)


def test_obd_factory_skips_unsupported_pids_and_watches_supported():
    spec = ObdSpec(port="/dev/ttyUSB0", pids=("RPM", "MAF"))
    with patch("src.collection.obd_collector.obd.Async") as fake_async:
        instance = fake_async.return_value
        instance.is_connected.return_value = True
        # Pretend ECU supports RPM but not MAF
        instance.supports.side_effect = lambda cmd: cmd.name == "RPM"

        conn = obd_factory(spec, on_reading=lambda _r: None)

    # Only one watch call (RPM), MAF was skipped.
    assert instance.watch.call_count == 1
    watched_cmd = instance.watch.call_args.args[0]
    assert watched_cmd.name == "RPM"
    assert conn is instance


class _FakeObdConnection:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_collect_obd_starts_and_stops_connection() -> None:
    conn = _FakeObdConnection()
    stop = asyncio.Event()
    queue = asyncio.Queue()

    def factory(_spec: ObdSpec, on_reading: Any) -> _FakeObdConnection:
        on_reading(ObdReading(timestamp=1_778_000_000.0, pid="RPM", value=710.0, unit="rpm"))
        return conn

    task = asyncio.create_task(collect_obd(ObdSpec(port="/dev/fake"), queue, stop, factory=factory))
    record = await asyncio.wait_for(queue.get(), timeout=1.0)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    assert record.channel == "RPM"
    assert conn.started is True
    assert conn.stopped is True


@pytest.mark.asyncio
async def test_collect_obd_retries_after_factory_failure() -> None:
    conn = _FakeObdConnection()
    stop = asyncio.Event()
    queue = asyncio.Queue()
    attempts = 0

    def factory(_spec: ObdSpec, on_reading: Any) -> _FakeObdConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("adapter missing")
        on_reading(ObdReading(timestamp=1_778_000_000.0, pid="RPM", value=710.0, unit="rpm"))
        return conn

    task = asyncio.create_task(
        collect_obd(
            ObdSpec(port="/dev/fake"),
            queue,
            stop,
            factory=factory,
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
    assert record.channel == "RPM"
    assert conn.started is True
    assert conn.stopped is True


# ---------------------------------------------------------------------------
# supervise_obd_capture: survives adapter dropout / reattach
# ---------------------------------------------------------------------------

_ZERO_BACKOFF = ReconnectPolicy(initial_delay_seconds=0.0, max_delay_seconds=0.0)


class _SupervisedFakeConn:
    def __init__(self, *, connected: bool = True, on_start: Callable[[], None] | None = None):
        self.connected = connected
        self.started = False
        self.stopped = False
        self._on_start = on_start

    def start(self) -> None:
        self.started = True
        if self._on_start is not None:
            self._on_start()

    def stop(self) -> None:
        self.stopped = True

    def is_connected(self) -> bool:
        return self.connected


def test_supervise_obd_capture_single_attempt_when_reconnect_disabled() -> None:
    stop = threading.Event()
    calls: list[str] = []

    def factory(spec: ObdSpec, _on_reading: Callable[[ObdReading], None]) -> object:
        calls.append(spec.port)
        raise RuntimeError("adapter not responding")

    supervise_obd_capture(
        ObdSpec(port="/dev/fake"),
        lambda _r: None,
        stop,
        factory=factory,  # type: ignore[arg-type]
        reconnect_policy=None,
        port_exists=lambda _p: True,
    )

    assert calls == ["/dev/fake"]
    assert not stop.is_set()


def test_supervise_obd_capture_retries_factory_failures_until_stop() -> None:
    stop = threading.Event()
    calls: list[str] = []

    def factory(spec: ObdSpec, _on_reading: Callable[[ObdReading], None]) -> object:
        calls.append(spec.port)
        if len(calls) >= 3:
            stop.set()
        raise RuntimeError("transient ELM hiccup")

    supervise_obd_capture(
        ObdSpec(port="/dev/fake"),
        lambda _r: None,
        stop,
        factory=factory,  # type: ignore[arg-type]
        reconnect_policy=_ZERO_BACKOFF,
        port_exists=lambda _p: True,
        health_poll_seconds=0.0,
    )

    assert len(calls) == 3


def test_supervise_obd_capture_reconnects_when_device_disappears() -> None:
    stop = threading.Event()
    conns: list[_SupervisedFakeConn] = []

    def factory(_spec: ObdSpec, _on_reading: Callable[[ObdReading], None]) -> _SupervisedFakeConn:
        if not conns:
            conn = _SupervisedFakeConn(connected=False)  # first link drops immediately
        else:
            conn = _SupervisedFakeConn(connected=True, on_start=stop.set)  # second ends the test
        conns.append(conn)
        return conn

    supervise_obd_capture(
        ObdSpec(port="/dev/fake"),
        lambda _r: None,
        stop,
        factory=factory,
        reconnect_policy=_ZERO_BACKOFF,
        port_exists=lambda _p: True,
        health_poll_seconds=0.0,
    )

    assert len(conns) == 2
    assert all(conn.started and conn.stopped for conn in conns)


def test_supervise_obd_capture_waits_for_device_to_appear() -> None:
    stop = threading.Event()
    presence = iter([False, False, True])

    def port_exists(_p: str) -> bool:
        try:
            return next(presence)
        except StopIteration:
            return True

    started = threading.Event()

    def on_start() -> None:
        started.set()
        stop.set()

    def factory(_spec: ObdSpec, _on_reading: Callable[[ObdReading], None]) -> _SupervisedFakeConn:
        return _SupervisedFakeConn(connected=True, on_start=on_start)

    supervise_obd_capture(
        ObdSpec(port="/dev/fake"),
        lambda _r: None,
        stop,
        factory=factory,
        reconnect_policy=_ZERO_BACKOFF,
        port_exists=port_exists,
        health_poll_seconds=0.0,
    )

    assert started.is_set()


def test_supervise_obd_capture_reraises_unknown_pid_error() -> None:
    stop = threading.Event()

    def factory(_spec: ObdSpec, _on_reading: Callable[[ObdReading], None]) -> object:
        raise ValueError("unknown OBD command 'RMP'")

    with pytest.raises(ValueError, match="unknown OBD command"):
        supervise_obd_capture(
            ObdSpec(port="/dev/fake", pids=("RMP",)),
            lambda _r: None,
            stop,
            factory=factory,  # type: ignore[arg-type]
            reconnect_policy=None,
            port_exists=lambda _p: True,
        )
