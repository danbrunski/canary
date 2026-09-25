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
    record,
)
from src.collection.reconnect import ReconnectPolicy
from src.config import CanBusConfig, VehicleConfig
from src.session import make_session_dir

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
    # Generous bound: it only needs to prove the 0.2 s stop event won over
    # the 10 s deadline. A tight (< 1.0 s) bound flaked under `-n auto` when
    # the parallel torch-training tests starved this worker of CPU.
    assert (result.ended_at - result.started_at).total_seconds() < 5.0

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


def test_record_treats_can_operation_error_as_bus_failure_not_parse_error(tmp_path: Path):
    """Regression: session 2026-05-15T020322 had `CanOperationError: Could not
    read from serial device` from a Canable USB drop on a 28-min drive. The
    `except (ValueError, IndexError, can.CanError)` trap caught it as a parse
    error, so reconnect never fired and we burned 27 min spinning at 200K
    errors/sec (63,400,211 total). After narrowing the trap to
    `(ValueError, IndexError)`, CanOperationError must fall through to the
    bus-level handler — counted as a reconnect, not a parse error."""
    ReconnectableBus.instances_built = 0
    ReconnectableBus.instances_shutdown = 0

    spec = _virtual_spec("canary-test-can-op-error")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    # Bus that raises CanOperationError on recv (matches the slcan dead-pipe path).
    class _DeadSerialBus:
        def __init__(self, good_frames: int = 2) -> None:
            self.calls = 0
            self.budget = good_frames
            self.shutdown_called = False

        def recv(self, timeout: float) -> can.Message | None:
            self.calls += 1
            if self.calls > self.budget:
                raise can.CanOperationError("Could not read from serial device")
            return can.Message(
                arbitration_id=0x100 + self.calls,
                data=bytes([self.calls & 0xFF] * 4),
                is_extended_id=False,
                timestamp=float(self.calls),
            )

        def shutdown(self) -> None:
            self.shutdown_called = True

    rebuild_count = 0

    def factory(_spec: CanBusConfig) -> _DeadSerialBus:
        nonlocal rebuild_count
        rebuild_count += 1
        return _DeadSerialBus(good_frames=2)

    result = record(
        _DeadSerialBus(good_frames=2),  # type: ignore[arg-type]
        paths,
        spec=spec,
        vehicle=_vehicle(),
        duration=0.3,
        poll_interval=0.02,
        factory=factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.01,
            max_delay_seconds=0.01,
        ),
    )

    # The whole point: CanOperationError is NOT a parse error.
    assert result.parse_errors == 0, (
        f"CanOperationError leaked into parse-error path: {result.parse_errors}"
    )
    # And IS a bus-level failure that triggers reconnect.
    assert result.reconnects >= 1, "reconnect path should have fired"


class ReconnectableBus:
    """Bus that fails with OSError after N good frames, simulating a USB
    disconnect. Each instance counts how many times it's been shut down so
    tests can confirm the factory built fresh ones."""

    instances_built = 0
    instances_shutdown = 0

    def __init__(self, good_frames_before_failure: int = 3) -> None:
        type(self).instances_built += 1
        self.calls = 0
        self.budget = good_frames_before_failure
        self.shutdown_called = False

    def recv(self, timeout: float) -> can.Message | None:
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
        self.shutdown_called = True
        type(self).instances_shutdown += 1


def test_record_auto_reconnects_on_bus_failure(tmp_path: Path):
    """OSError from a USB disconnect should trigger reconnect via factory,
    not kill the recording. Mirrors what collect_can_from_config does for
    the daemon path."""
    ReconnectableBus.instances_built = 0
    ReconnectableBus.instances_shutdown = 0

    spec = _virtual_spec("canary-test-reconnect-record")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    def factory(_spec: CanBusConfig) -> ReconnectableBus:
        return ReconnectableBus(good_frames_before_failure=3)

    initial_bus = factory(spec)
    # We expect: 3 good frames from initial bus → fails → rebuild → 3 more → fails
    # → rebuild → 3 more → deadline. So 9 frames total across 3 buses.

    result = record(
        initial_bus,  # type: ignore[arg-type]
        paths,
        spec=spec,
        vehicle=_vehicle(),
        duration=0.6,
        poll_interval=0.02,
        factory=factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.01,
            max_delay_seconds=0.01,
        ),
    )

    assert result.reconnects >= 1, "factory should have been called for reconnect"
    assert result.frames_recorded >= 3, "at least the pre-failure frames should land"
    assert result.stopped_by == "deadline"
    # Failed buses should have been shut down — no USB handle leaks.
    assert ReconnectableBus.instances_shutdown >= result.reconnects

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["reconnects"] == result.reconnects
    assert manifest["dropped_writes"] == 0


def test_record_bails_after_max_consecutive_reconnect_failures(tmp_path: Path):
    """If the factory fails to rebuild N times in a row, record() stops with
    stopped_by='reconnect_exhausted' rather than spinning forever. Protects
    against wedged-USB-adapter scenarios where every rebuild attempt blocks
    in kernel I/O — a real failure mode observed in WSL2."""
    ReconnectableBus.instances_built = 0
    ReconnectableBus.instances_shutdown = 0

    spec = _virtual_spec("canary-test-bail")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    factory_calls = 0

    def failing_factory(_spec: CanBusConfig) -> ReconnectableBus:
        nonlocal factory_calls
        factory_calls += 1
        raise RuntimeError(f"adapter wedged (attempt {factory_calls})")

    initial_bus = ReconnectableBus(good_frames_before_failure=2)

    result = record(
        initial_bus,  # type: ignore[arg-type]
        paths,
        spec=spec,
        vehicle=_vehicle(),
        duration=10.0,  # generous; we expect to bail well before this
        poll_interval=0.02,
        factory=failing_factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.005,
            max_delay_seconds=0.005,
        ),
        max_reconnects=3,
    )

    # First bus dies after 2 frames, then 3 failed rebuild attempts → bail.
    assert result.stopped_by == "reconnect_exhausted"
    assert result.frames_recorded == 2
    assert factory_calls == 3  # exactly max_reconnects attempts

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "reconnect_exhausted"
    assert "adapter wedged" in manifest["error"]


def test_record_resets_failure_count_on_successful_reconnect(tmp_path: Path):
    """A successful reconnect must reset the failed-rebuild counter so
    intermittent USB flaps don't accumulate toward the cap."""
    spec = _virtual_spec("canary-test-reset")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    # Pattern: bus dies → fail, fail, succeed → bus dies → fail, fail, succeed
    # Without reset: 4 cumulative failures → bail at 3. With reset: never bails.
    sequence: list[str] = []

    def varied_factory(_spec: CanBusConfig) -> ReconnectableBus:
        # Two failures then a success, repeating.
        sequence.append("call")
        n = len(sequence)
        if n in (1, 2, 4, 5):
            raise RuntimeError(f"transient failure {n}")
        return ReconnectableBus(good_frames_before_failure=1)

    initial_bus = ReconnectableBus(good_frames_before_failure=1)

    result = record(
        initial_bus,  # type: ignore[arg-type]
        paths,
        spec=spec,
        vehicle=_vehicle(),
        duration=0.5,
        poll_interval=0.01,
        factory=varied_factory,  # type: ignore[arg-type]
        reconnect_policy=ReconnectPolicy(
            initial_delay_seconds=0.005,
            max_delay_seconds=0.005,
        ),
        max_reconnects=3,
    )

    # Should reach deadline cleanly, not bail. At least 2 successful reconnects.
    assert result.stopped_by == "deadline"
    assert result.reconnects >= 2


def test_record_without_factory_still_propagates_bus_errors(tmp_path: Path):
    """Backward compatibility: when factory/reconnect_policy are not provided,
    bus-level OSError should propagate as before (caller decides what to do)."""

    class BrokenBus:
        def recv(self, timeout: float) -> can.Message | None:
            raise OSError(5, "Input/output error")

    spec = _virtual_spec("canary-test-no-reconnect")
    paths = make_session_dir(tmp_path, datetime.now(UTC))

    with pytest.raises(OSError, match="Input/output error"):
        record(
            BrokenBus(),  # type: ignore[arg-type]
            paths,
            spec=spec,
            vehicle=_vehicle(),
            duration=0.3,
            poll_interval=0.02,
        )
    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "error"


def test_record_stops_cleanly_on_disk_write_failure(tmp_path: Path):
    """OSError from writer.on_message_received should end the loop cleanly,
    set stopped_by='disk_error', and still flush the manifest."""

    class GoodBus:
        def __init__(self) -> None:
            self.calls = 0

        def recv(self, timeout: float) -> can.Message | None:
            self.calls += 1
            return can.Message(
                arbitration_id=0x100,
                data=b"\x01\x02",
                is_extended_id=False,
                timestamp=float(self.calls),
            )

    class FlakyWriter:
        """Wraps CanutilsLogWriter and fails after N writes to simulate disk full."""

        def __init__(self, real: object, fail_after: int = 5) -> None:
            self.real = real
            self.fail_after = fail_after
            self.writes = 0

        def __enter__(self) -> FlakyWriter:
            self.real.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> None:
            self.real.__exit__(*args)  # type: ignore[attr-defined]

        def on_message_received(self, msg: can.Message) -> None:
            self.writes += 1
            if self.writes > self.fail_after:
                raise OSError(28, "No space left on device")
            self.real.on_message_received(msg)  # type: ignore[attr-defined]

    # Patch the CanutilsLogWriter constructor to return a flaky wrapper.
    import unittest.mock as mock

    spec = _virtual_spec("canary-test-disk-error")
    paths = make_session_dir(tmp_path, datetime.now(UTC))
    real_writer_cls = can.io.CanutilsLogWriter

    def flaky_writer_cls(path: str) -> FlakyWriter:
        return FlakyWriter(real_writer_cls(path), fail_after=5)

    with mock.patch.object(can.io, "CanutilsLogWriter", flaky_writer_cls):
        result = record(
            GoodBus(),  # type: ignore[arg-type]
            paths,
            spec=spec,
            vehicle=_vehicle(),
            duration=1.0,
            poll_interval=0.005,
        )

    assert result.stopped_by == "disk_error"
    assert result.frames_recorded == 5  # exactly the writes that succeeded
    assert result.dropped_writes == 1  # the one that triggered the stop

    manifest = json.loads(paths.manifest.read_text())
    assert manifest["stopped_by"] == "disk_error"
    assert manifest["frames_recorded"] == 5
    assert manifest["dropped_writes"] == 1
    assert "No space left on device" in manifest["error"]


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


# ---------------------------------------------------------------------------
# C07 — owned receiver thread: event-loop isolation, ownership, accounting
# ---------------------------------------------------------------------------


class _SnifferBus:
    """Scripted fake bus. ``recv`` hands out queued frames, then idles by
    sleeping the full timeout (the real blocking shape). ``send`` must never
    be called — the passive path is a sniffer (C07.9)."""

    def __init__(self, frames: list[can.Message] | None = None, *, idle: bool = True) -> None:
        self.frames = list(frames or [])
        self.idle = idle
        self.recv_calls = 0
        # Thread objects, not idents: an ident is reused once a thread exits,
        # which made "reader replaced" flaky across a reconnect.
        self.recv_threads: set[threading.Thread] = set()
        self.send_calls = 0
        self.shutdown_called = False
        self.recv_after_shutdown = 0

    def recv(self, timeout: float) -> can.Message | None:
        self.recv_calls += 1
        self.recv_threads.add(threading.current_thread())
        if self.shutdown_called:
            self.recv_after_shutdown += 1
        if self.frames:
            return self.frames.pop(0)
        if self.idle:
            time.sleep(timeout)
        return None

    def send(self, msg: can.Message, timeout: float | None = None) -> None:
        self.send_calls += 1
        raise AssertionError("the passive collector must never send")

    def shutdown(self) -> None:
        self.shutdown_called = True


def _frames(n: int, start: int = 0) -> list[can.Message]:
    # Fresh driver stamps: one further than KERNEL_STAMP_MAX_SKEW_SECONDS from
    # the wall clock is replaced by the receipt time (R02.4).
    base = time.time()
    return [
        can.Message(
            timestamp=base + i * 0.0001,
            arbitration_id=0x100 + (i % 8),
            data=bytes([i & 0xFF] * 8),
            is_extended_id=False,
        )
        for i in range(start, start + n)
    ]


def _reader_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("can-recv:") and t.is_alive()]


async def _heartbeat_gaps(duration: float, period: float = 0.01) -> list[float]:
    """Delay beyond ``period`` of an unrelated ticking task, per tick."""
    gaps: list[float] = []
    last = time.perf_counter()
    end = last + duration
    while time.perf_counter() < end:
        await asyncio.sleep(period)
        now = time.perf_counter()
        gaps.append(now - last - period)
        last = now
    return gaps


async def _blocking_reference_collector(
    bus: _SnifferBus,
    queue: asyncio.Queue,
    stop: asyncio.Event,
    *,
    poll_interval: float,
) -> None:
    """The pre-C07 shape of ``collect_can``: blocking ``recv`` in a coroutine."""
    while not stop.is_set():
        msg = bus.recv(timeout=poll_interval)
        if msg is not None:
            queue.put_nowait(frame_to_record(msg))
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_blocking_receive_reference_stalls_event_loop_heartbeat() -> None:
    """C07.1 — the regression being fixed: an idle blocking recv inside the
    coroutine delays an unrelated 10 ms ticker by the whole receive timeout.
    Kept as a reference implementation so the contrast stays measurable."""
    bus = _SnifferBus()
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(_blocking_reference_collector(bus, queue, stop, poll_interval=0.2))
    gaps = await _heartbeat_gaps(0.6)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    assert max(gaps) >= 0.1, f"reference did not block the loop: max gap {max(gaps):.3f}s"


@pytest.mark.asyncio
async def test_collect_can_idle_bus_does_not_stall_event_loop() -> None:
    """C07.1/C07.11 — with the receiver in its own thread, an idle 200 ms
    recv cannot delay unrelated asyncio work for the receive timeout
    (measured ~1 ms max; the bound is loose for a loaded CI worker)."""
    bus = _SnifferBus()
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(collect_can(bus, queue, stop, poll_interval=0.2))  # type: ignore[arg-type]
    gaps = await _heartbeat_gaps(0.6)
    stop.set()
    stats = await asyncio.wait_for(task, timeout=2.0)

    assert max(gaps) < 0.1, f"receive thread stalled the loop: max gap {max(gaps):.3f}s"
    assert queue.empty()
    assert stats.frames == 0
    assert bus.send_calls == 0
    assert not _reader_threads()


@pytest.mark.asyncio
async def test_collect_can_sparse_frame_reaches_queue_promptly() -> None:
    """C07.11 — a single frame on an otherwise idle bus does not wait for a
    batch: handoff latency is one loop turn, not a timer."""

    class _EventBus(_SnifferBus):
        """recv blocks on an event so an injected frame returns immediately."""

        def __init__(self) -> None:
            super().__init__()
            self.arrived = threading.Event()

        def recv(self, timeout: float) -> can.Message | None:
            self.recv_calls += 1
            if self.arrived.wait(timeout):
                self.arrived.clear()
                return self.frames.pop(0)
            return None

    bus = _EventBus()
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(collect_can(bus, queue, stop, poll_interval=0.2))  # type: ignore[arg-type]
    await asyncio.sleep(0.1)  # bus is idle, reader is parked inside recv

    bus.frames.append(_frames(1)[0])
    injected_at = time.perf_counter()
    bus.arrived.set()
    record = await asyncio.wait_for(queue.get(), timeout=1.0)
    latency = time.perf_counter() - injected_at

    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert record.channel == "0x100"
    assert latency < 0.05, f"sparse frame waited {latency:.3f}s"


@pytest.mark.asyncio
async def test_collect_can_busy_bus_preserves_order_and_timestamps() -> None:
    """C07.3/C07.9 — single producer -> FIFO: raw order and ``msg.timestamp``
    survive the thread handoff; sniffer never sends."""
    frames = _frames(5000)
    bus = _SnifferBus(list(frames))
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    # The fake bus returns instantly and never paces, so an unpaced 5000-frame
    # burst can outrun the loop's drain and legitimately hit the default 4096
    # buffer bound (that bound is asserted elsewhere). This test is about
    # order and timestamps, so size the buffer to the burst.
    task = asyncio.create_task(
        collect_can(bus, queue, stop, poll_interval=0.01, handoff_capacity=len(frames))  # type: ignore[arg-type]
    )

    deadline = time.perf_counter() + 5.0
    while queue.qsize() < len(frames) and time.perf_counter() < deadline:
        await asyncio.sleep(0.005)
    stop.set()
    stats = await asyncio.wait_for(task, timeout=2.0)

    records = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [r.channel for r in records] == [f"0x{m.arbitration_id:X}" for m in frames]
    assert [r.timestamp.timestamp() for r in records] == pytest.approx(
        [m.timestamp for m in frames]
    )
    assert stats.frames == len(frames)
    assert stats.handoff.accepted == len(frames)
    assert stats.handoff.dropped == 0
    assert stats.queue_dropped == 0
    assert stats.handoff.high_water <= stats.handoff.capacity
    assert bus.send_calls == 0


@pytest.mark.asyncio
async def test_collect_can_stop_during_receive_joins_reader_before_returning() -> None:
    """C07.3/C07.8 — stop while the reader is parked in a 200 ms recv: the
    coroutine returns only after the thread exited, and no recv happens on
    the bus afterwards."""
    bus = _SnifferBus()
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(collect_can(bus, queue, stop, poll_interval=0.2))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)  # mid-recv
    assert _reader_threads()

    stopped_at = time.perf_counter()
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert time.perf_counter() - stopped_at < 1.0

    assert not _reader_threads()
    calls_at_return = bus.recv_calls
    await asyncio.sleep(0.3)
    assert bus.recv_calls == calls_at_return, "reader kept reading after stop"
    assert bus.send_calls == 0


@pytest.mark.asyncio
async def test_collect_can_from_config_reconnect_replaces_reader_and_never_reads_closed_bus(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C07.3/C07.8 — bus failure: the reader is joined, the bus shut down,
    a fresh bus gets a fresh reader thread, and the dead bus is never read
    again. Stats reported to the callback are cumulative across attempts."""

    class _DyingBus(_SnifferBus):
        def recv(self, timeout: float) -> can.Message | None:
            if not self.frames:
                raise OSError(5, "Input/output error")
            return super().recv(timeout)

    first = _DyingBus(_frames(3))
    second = _SnifferBus(_frames(3, start=3))
    buses = [first, second]

    def factory(_spec: CanBusConfig) -> _SnifferBus:
        return buses.pop(0)

    reports = []
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(
        collect_can_from_config(
            _virtual_spec("canary-test-c07-reconnect"),
            queue,
            stop,
            poll_interval=0.01,
            factory=factory,  # type: ignore[arg-type]
            reconnect_policy=ReconnectPolicy(initial_delay_seconds=0.01, max_delay_seconds=0.01),
            on_receive_stats=reports.append,
        )
    )
    deadline = time.perf_counter() + 5.0
    while queue.qsize() < 6 and time.perf_counter() < deadline:
        await asyncio.sleep(0.005)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)

    records = [queue.get_nowait() for _ in range(queue.qsize())]
    assert [r.channel for r in records] == [f"0x{m.arbitration_id:X}" for m in _frames(6)]
    assert first.shutdown_called and second.shutdown_called
    assert first.recv_after_shutdown == 0
    assert second.recv_after_shutdown == 0
    assert len(first.recv_threads) == 1 and len(second.recv_threads) == 1
    assert first.recv_threads.isdisjoint(second.recv_threads), "reader thread was not replaced"
    assert not _reader_threads()
    assert reports, "stats callback never fired"
    assert reports[-1].frames == 6
    assert reports[-1].reconnects == 1
    assert reports[-1].handoff.accepted == 6


@pytest.mark.asyncio
async def test_collect_can_counts_malformed_frames_without_aborting() -> None:
    """C07.3/C07.8 — parser errors are counted (``parse_errors``), skipped,
    and never end the reader."""
    bus = FlakyBus(bad_every=3, total_calls=12)
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(collect_can(bus, queue, stop, poll_interval=0.01))  # type: ignore[arg-type]
    deadline = time.perf_counter() + 5.0
    while queue.qsize() < 8 and time.perf_counter() < deadline:
        await asyncio.sleep(0.005)
    stop.set()
    stats = await asyncio.wait_for(task, timeout=2.0)

    assert stats.frames == 8
    assert stats.parse_errors == 4
    assert queue.qsize() == 8


@pytest.mark.asyncio
async def test_collect_can_queue_overflow_is_counted_and_reconciles() -> None:
    """C07.8/C07.9 — accepted + explicitly dropped == injected, at every
    stage: frames -> handoff (accepted/dropped) -> queue (queued/queue_dropped)."""
    frames = _frames(500)
    bus = _SnifferBus(list(frames))
    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    stop = asyncio.Event()
    task = asyncio.create_task(collect_can(bus, queue, stop, poll_interval=0.01))  # type: ignore[arg-type]
    deadline = time.perf_counter() + 5.0
    while bus.frames and time.perf_counter() < deadline:
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)  # let the last drain run
    stop.set()
    stats = await asyncio.wait_for(task, timeout=2.0)

    assert stats.frames == len(frames)
    assert stats.handoff.accepted + stats.handoff.dropped == stats.frames
    assert queue.qsize() + stats.queue_dropped == stats.handoff.accepted
    assert stats.queue_dropped == len(frames) - 10
    assert queue.qsize() == 10


@pytest.mark.asyncio
async def test_collect_can_late_reader_after_join_timeout_is_a_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C07.3/C07.8 — a wedged recv that outlives the bounded join: the
    coroutine still returns (warning logged), and when the thread finally
    produces a frame it is a counted no-op — nothing reaches the queue."""

    class _WedgedBus(_SnifferBus):
        def recv(self, timeout: float) -> can.Message | None:
            self.recv_calls += 1
            time.sleep(0.4)  # ignores the timeout, like a stuck USB read
            return _frames(1)[0]

    bus = _WedgedBus()
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    caplog.set_level("WARNING")
    task = asyncio.create_task(
        collect_can(bus, queue, stop, poll_interval=0.05, join_timeout=0.05)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert "did not stop within join timeout" in caplog.text

    await asyncio.sleep(0.6)  # the wedged recv returns its frame now
    assert queue.empty(), "late frame reached the queue after shutdown"
    assert not _reader_threads()


@pytest.mark.asyncio
async def test_collect_can_reports_stats_periodically_and_on_exit() -> None:
    """C07.5 — the producer buffer's bound, high-water and drops are visible
    through the stats callback while running and once more at exit."""
    bus = _SnifferBus(_frames(20))
    queue: asyncio.Queue = asyncio.Queue()
    stop = asyncio.Event()
    reports = []
    task = asyncio.create_task(
        collect_can(bus, queue, stop, poll_interval=0.02, on_receive_stats=reports.append)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0.15)
    stop.set()
    final = await asyncio.wait_for(task, timeout=2.0)

    assert len(reports) >= 2
    assert reports[-1] == final
    assert final.handoff.capacity > 0
    assert 0 < final.handoff.high_water <= final.handoff.capacity
    assert final.handoff.accepted == 20
    assert final.handoff.dropped == 0
