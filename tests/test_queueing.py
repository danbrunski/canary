"""Tests for telemetry queue pressure helpers."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import pytest

from src.collection.queueing import (
    DROP_LOG_BREAKDOWN_LIMIT,
    DROP_LOG_INTERVAL_SECONDS,
    DropLogThrottle,
    QueueDepthMonitor,
    QueueDropCounter,
    ThreadHandoff,
    flush_dropped_can_frame_log,
    log_dropped_can_frame,
    put_nowait_or_drop,
    threadsafe_enqueue,
)
from src.telemetry import TelemetryRecord


def _record() -> TelemetryRecord:
    return TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm")


class _FakeClock:
    """Injected monotonic clock: the rate limiters are never tested with sleep."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _lines(caplog: pytest.LogCaptureFixture, message: str) -> list[Any]:
    # Records carry the throttle's ``extra`` fields as attributes.
    return [record for record in caplog.records if record.getMessage() == message]


_HANDOFF_DROP_MESSAGE = "telemetry handoff buffer full; dropping record"
_QUEUE_DROP_MESSAGE = "telemetry queue full; dropping record"


def test_put_nowait_or_drop_counts_full_queue() -> None:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=1)
    counter = QueueDropCounter("test")

    assert put_nowait_or_drop(queue, _record(), on_drop=counter) is True
    assert put_nowait_or_drop(queue, _record(), on_drop=counter) is False
    assert counter.dropped == 1


def test_queue_depth_monitor_warns_near_capacity(caplog: pytest.LogCaptureFixture) -> None:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=2)
    monitor = QueueDepthMonitor(warning_fraction=0.5)
    queue.put_nowait(_record())

    caplog.set_level(logging.WARNING)
    monitor.observe(queue)

    assert "telemetry queue near capacity" in caplog.text


# ---------------------------------------------------------------------------
# C07.4/C07.5 — ThreadHandoff: bounded, accounted, coalescing thread handoff
# ---------------------------------------------------------------------------


def _can(i: int) -> TelemetryRecord:
    return TelemetryRecord.can(1_778_000_000.0 + i, 0x100, bytes([i & 0xFF]))


async def _drain_all(queue: asyncio.Queue[TelemetryRecord]) -> list[TelemetryRecord]:
    return [queue.get_nowait() for _ in range(queue.qsize())]


async def test_thread_handoff_preserves_fifo_order_from_producer_thread() -> None:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    handoff = ThreadHandoff(asyncio.get_running_loop(), queue, capacity=10_000)
    records = [_can(i) for i in range(2000)]

    thread = threading.Thread(target=lambda: [handoff.push(r) for r in records])
    thread.start()
    deadline = time.perf_counter() + 5.0
    while queue.qsize() < len(records) and time.perf_counter() < deadline:
        await asyncio.sleep(0.001)
    thread.join()

    assert await _drain_all(queue) == records
    stats = handoff.stats()
    assert stats.accepted == len(records)
    assert stats.dropped == 0


async def test_thread_handoff_buffer_is_bounded_and_counts_drops() -> None:
    """C07.5 — the producer buffer has a visible bound; overflow is counted,
    ordering of what survived is intact, accepted + dropped == pushed."""
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    handoff = ThreadHandoff(asyncio.get_running_loop(), queue, capacity=4)

    # Pushing from the loop thread without yielding: the drain cannot run,
    # so the buffer fills deterministically.
    results = [handoff.push(_can(i)) for i in range(6)]
    assert results == [True, True, True, True, False, False]
    stats = handoff.stats()
    assert stats.accepted == 4
    assert stats.dropped == 2
    assert stats.high_water == 4
    assert stats.capacity == 4
    assert stats.accepted + stats.dropped == 6

    await asyncio.sleep(0)
    assert [r.payload for r in await _drain_all(queue)] == [bytes([i]) for i in range(4)]


async def test_thread_handoff_close_flushes_pending_in_order_then_refuses() -> None:
    """C07.3/C07.8 — a boundary while a chunk is pending: close() delivers
    the pending records in order; a late push is a counted no-op and a late
    drain callback does not deliver anything twice."""
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    handoff = ThreadHandoff(asyncio.get_running_loop(), queue)
    for i in range(3):
        handoff.push(_can(i))  # drain scheduled but not yet run

    assert handoff.close() == 3
    assert [r.payload for r in await _drain_all(queue)] == [b"\x00", b"\x01", b"\x02"]
    assert handoff.closed

    assert handoff.push(_can(9)) is False
    await asyncio.sleep(0)  # the originally scheduled drain runs now
    await asyncio.sleep(0)
    assert queue.empty()
    stats = handoff.stats()
    assert stats.accepted == 3
    assert stats.dropped == 1
    assert handoff.close() == 0  # idempotent


async def test_thread_handoff_max_batch_bounds_each_drain_and_reschedules() -> None:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    handoff = ThreadHandoff(asyncio.get_running_loop(), queue, max_batch=2)
    for i in range(5):
        handoff.push(_can(i))

    await asyncio.sleep(0)
    assert queue.qsize() == 2
    await asyncio.sleep(0)
    assert queue.qsize() == 4
    await asyncio.sleep(0)
    assert queue.qsize() == 5
    assert handoff.stats().drains == 3
    assert [r.payload for r in await _drain_all(queue)] == [bytes([i]) for i in range(5)]


async def test_thread_handoff_queue_full_goes_to_on_drop_not_buffer_drops() -> None:
    """Two loss causes stay attributable: buffer-full is the handoff's own
    counter, queue-full is the on_drop callback's."""
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=1)
    counter = QueueDropCounter("c07")
    handoff = ThreadHandoff(asyncio.get_running_loop(), queue, on_drop=counter)
    for i in range(3):
        handoff.push(_can(i))
    await asyncio.sleep(0)

    assert queue.qsize() == 1
    assert counter.dropped == 2
    stats = handoff.stats()
    assert stats.accepted == 3
    assert stats.dropped == 0


def test_thread_handoff_rejects_nonpositive_bounds() -> None:
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError):
            ThreadHandoff(loop, asyncio.Queue(), capacity=0)
        with pytest.raises(ValueError):
            ThreadHandoff(loop, asyncio.Queue(), max_batch=0)
    finally:
        loop.close()


async def test_threadsafe_enqueue_is_still_a_plain_callable() -> None:
    """Call sites (`obd_collector`, `can_obd_collector`) keep calling
    ``enqueue(record)``; a record lands after one loop turn as before."""
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    enqueue = threadsafe_enqueue(asyncio.get_running_loop(), queue)
    enqueue(_record())
    await asyncio.sleep(0)
    assert queue.qsize() == 1


async def test_thread_handoff_one_wakeup_per_burst_vs_one_per_record() -> None:
    """C07.4 — deterministic shape: N pushes before the loop turns cost one
    scheduled callback with the handoff, N with the old per-record path."""
    loop = asyncio.get_running_loop()
    scheduled = 0
    original = loop.call_soon_threadsafe

    def counting(callback: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal scheduled
        scheduled += 1
        return original(callback, *args, **kwargs)

    loop.call_soon_threadsafe = counting  # type: ignore[assignment]
    try:
        queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
        handoff = ThreadHandoff(loop, queue, capacity=1000)
        for i in range(1000):
            handoff.push(_can(i))
        chunked = scheduled

        scheduled = 0
        for i in range(1000):
            loop.call_soon_threadsafe(partial(put_nowait_or_drop, queue, _can(i)))
        per_record = scheduled
    finally:
        loop.call_soon_threadsafe = original  # type: ignore[method-assign]

    assert chunked == 1
    assert per_record == 1000
    await asyncio.sleep(0)


#: C07.4 acceptance threshold for keeping the chunked handoff: it must cut
#: loop wake-ups (scheduled callbacks) by more than this fraction versus one
#: ``call_soon_threadsafe`` per record, from a real producer thread.
_HANDOFF_MIN_CALLBACK_REDUCTION = 0.25


async def test_thread_handoff_benchmark_reduces_loop_callbacks_from_a_thread() -> None:
    """C07.4 — 20k records from a producer thread: (a) per-record
    ``call_soon_threadsafe`` vs (b) ``ThreadHandoff``. Asserts the callback
    reduction threshold; wall times are logged for the handoff record
    (docs/collection-receive-path.md), not asserted."""
    loop = asyncio.get_running_loop()
    n = 20_000
    records = [_can(i) for i in range(n)]

    async def run(push: Callable[[TelemetryRecord], Any], label: str) -> tuple[int, float]:
        scheduled = 0
        original = loop.call_soon_threadsafe

        def counting(callback: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal scheduled
            scheduled += 1
            return original(callback, *args, **kwargs)

        loop.call_soon_threadsafe = counting  # type: ignore[assignment]
        try:
            started = time.perf_counter()
            thread = threading.Thread(target=lambda: [push(r) for r in records], name=label)
            thread.start()
            deadline = started + 10.0
            while queue.qsize() < n and time.perf_counter() < deadline:
                await asyncio.sleep(0.001)
            wall = time.perf_counter() - started
            thread.join()
        finally:
            loop.call_soon_threadsafe = original  # type: ignore[method-assign]
        assert queue.qsize() == n, f"{label}: {queue.qsize()} of {n} delivered"
        await _drain_all(queue)
        return scheduled, wall

    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    per_record_callbacks, per_record_wall = await run(
        lambda r: loop.call_soon_threadsafe(partial(put_nowait_or_drop, queue, r)),
        "per-record",
    )
    handoff = ThreadHandoff(loop, queue, capacity=n)
    chunked_callbacks, chunked_wall = await run(handoff.push, "chunked")

    logging.getLogger(__name__).info(
        "handoff benchmark n=%d per_record: callbacks=%d wall=%.1fms chunked: "
        "callbacks=%d drains=%d wall=%.1fms",
        n,
        per_record_callbacks,
        per_record_wall * 1000,
        chunked_callbacks,
        handoff.stats().drains,
        chunked_wall * 1000,
    )
    assert per_record_callbacks == n
    assert chunked_callbacks <= (1 - _HANDOFF_MIN_CALLBACK_REDUCTION) * per_record_callbacks


# ---------------------------------------------------------------------------
# Drop-log rate limiting — the log volume must not track the loss volume
# ---------------------------------------------------------------------------


def test_drop_log_first_line_is_immediate_then_one_per_interval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first drop of a burst is the signal that loss started, so it logs
    at once; after that the interval — not the drop count — sets the rate."""
    clock = _FakeClock()
    throttle = DropLogThrottle("dropped", clock=clock, interval=1.0)
    caplog.set_level(logging.WARNING)

    assert throttle.record("obd:RPM") is True
    assert throttle.record("obd:RPM") is False
    clock.advance(0.99)
    assert throttle.record("obd:RPM") is False
    clock.advance(0.01)
    assert throttle.record("obd:RPM") is True
    assert len(_lines(caplog, "dropped")) == 2


def test_drop_log_volume_does_not_grow_with_drop_volume(caplog: pytest.LogCaptureFixture) -> None:
    """The 2026-09-17 defect: 150,114 dropped records produced 1,502 warning
    lines (one per 100 drops) from inside the producer path. Within one
    interval, any number of drops is one line."""
    clock = _FakeClock()
    caplog.set_level(logging.WARNING)

    small = DropLogThrottle("dropped", clock=clock)
    for _ in range(10):
        small.record("can:0x100")
    ten = len(_lines(caplog, "dropped"))

    caplog.clear()
    huge = DropLogThrottle("dropped", clock=clock)
    for _ in range(150_114):
        huge.record("can:0x100")
    many = len(_lines(caplog, "dropped"))

    assert ten == many == 1


def test_drop_log_breakdown_is_bounded_and_counts_add_up(caplog: pytest.LogCaptureFixture) -> None:
    """Each line carries the interval's shape — distinct source/channel pairs
    with counts — capped so the line itself cannot grow without bound."""
    clock = _FakeClock()
    throttle = DropLogThrottle("dropped", clock=clock)
    caplog.set_level(logging.WARNING)

    total = 0
    for channel in range(9):
        for _ in range(channel + 1):
            total += 1
            throttle.record(f"obd:CH{channel}")
    assert total == 45
    assert len(_lines(caplog, "dropped")) == 1  # clock frozen: one line so far

    assert throttle.flush(force=True) is True
    lines = _lines(caplog, "dropped")
    assert len(lines) == 2
    first, last = lines

    assert first.dropped_since_last == 1
    assert first.dropped_by == {"obd:CH0": 1}

    assert len(last.dropped_by) == DROP_LOG_BREAKDOWN_LIMIT
    assert last.dropped_by == {"obd:CH8": 9, "obd:CH7": 8, "obd:CH6": 7, "obd:CH5": 6, "obd:CH4": 5}
    assert last.dropped_by_others == 2 + 3 + 4  # CH1..CH3; CH0's single drop is in line 1
    assert last.dropped_by_others_keys == 3
    assert sum(last.dropped_by.values()) + last.dropped_by_others == last.dropped_since_last
    # Every drop is accounted for in exactly one line, and the cumulative
    # total on the last line is the exact number dropped.
    assert sum(line.dropped_since_last for line in lines) == total
    assert last.dropped_total == total


def test_drop_log_flush_is_a_no_op_without_a_pending_tail(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    throttle = DropLogThrottle("dropped", clock=_FakeClock())
    assert throttle.flush() is False
    assert throttle.flush(force=True) is False
    assert _lines(caplog, "dropped") == []


def test_drop_log_rejects_nonpositive_bounds() -> None:
    with pytest.raises(ValueError):
        DropLogThrottle("dropped", interval=0)
    with pytest.raises(ValueError):
        DropLogThrottle("dropped", breakdown_limit=0)


def test_queue_drop_counter_stays_exact_under_rate_limiting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Requirement 2: only lines are rate-limited. ``dropped`` is what
    ``/status`` and the loss reconciliation read, and it counts every drop."""
    clock = _FakeClock()
    counter = QueueDropCounter("gps", clock=clock)
    caplog.set_level(logging.WARNING)

    for _ in range(1000):
        counter(_record())

    assert counter.dropped == 1000
    assert len(_lines(caplog, _QUEUE_DROP_MESSAGE)) == 1
    clock.advance(DROP_LOG_INTERVAL_SECONDS)
    counter.flush_drop_log()
    lines = _lines(caplog, _QUEUE_DROP_MESSAGE)
    assert sum(line.dropped_since_last for line in lines) == 1000
    assert lines[-1].dropped_total == 1000
    assert lines[-1].queue == "gps"


async def test_handoff_drop_burst_is_bounded_and_fully_accounted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 3-second burst at 100 drops/s: at most one line per interval (plus
    the immediate first), the counts sum to the exact number dropped, and
    ``HandoffStats.dropped`` is untouched by any of it."""
    clock = _FakeClock()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    handoff = ThreadHandoff(
        asyncio.get_running_loop(),
        queue,
        capacity=1,
        clock=clock,
        log_interval_seconds=1.0,
    )
    caplog.set_level(logging.WARNING)

    # Pushing from the loop thread without yielding: the drain cannot run, so
    # the buffer stays full and every further push is a drop.
    assert handoff.push(_can(0)) is True
    assert handoff.push(_can(1)) is False
    assert len(_lines(caplog, _HANDOFF_DROP_MESSAGE)) == 1  # immediate

    drops = 300
    window = 3.0
    for i in range(drops - 1):
        clock.advance(window / drops)
        assert handoff.push(_can(i)) is False

    lines = _lines(caplog, _HANDOFF_DROP_MESSAGE)
    assert len(lines) <= 1 + math.ceil(window / 1.0)
    assert lines[0].handoff == "handoff"
    assert lines[0].capacity == 1

    # Loss stops: the buffer drains on the next loop turn and the burst's
    # final summary is emitted there — losing the tail is how "it dropped
    # 150k records" becomes invisible.
    clock.advance(1.0)
    await asyncio.sleep(0)
    lines = _lines(caplog, _HANDOFF_DROP_MESSAGE)
    assert sum(line.dropped_since_last for line in lines) == drops
    assert lines[-1].dropped_total == drops
    assert handoff.stats().dropped == drops


async def test_handoff_close_forces_the_final_drop_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The producer can die mid-burst before any drain: close() is the last
    chance to emit the tail, and it ignores the interval."""
    clock = _FakeClock()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue()
    handoff = ThreadHandoff(asyncio.get_running_loop(), queue, capacity=1, clock=clock)
    caplog.set_level(logging.WARNING)

    handoff.push(_can(0))
    for i in range(50):
        handoff.push(_can(i))
    assert len(_lines(caplog, _HANDOFF_DROP_MESSAGE)) == 1

    handoff.close()  # same instant: no interval has elapsed
    lines = _lines(caplog, _HANDOFF_DROP_MESSAGE)
    assert len(lines) == 2
    assert sum(line.dropped_since_last for line in lines) == 50
    assert lines[-1].dropped_total == 50
    assert lines[-1].closed is True


async def test_handoff_flushes_the_queue_drop_counters_tail_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both loss causes on this path hold a tail: buffer-full in the handoff,
    ingest-queue-full in the ``on_drop`` counter. Draining flushes both."""
    clock = _FakeClock()
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=1)
    counter = QueueDropCounter("c07", clock=clock)
    handoff = ThreadHandoff(asyncio.get_running_loop(), queue, on_drop=counter, clock=clock)
    caplog.set_level(logging.WARNING)

    for i in range(5):
        handoff.push(_can(i))
    await asyncio.sleep(0)  # drain: 1 record lands, 4 hit the full queue
    assert counter.dropped == 4
    assert len(_lines(caplog, _QUEUE_DROP_MESSAGE)) == 1

    clock.advance(DROP_LOG_INTERVAL_SECONDS)
    handoff.push(_can(9))  # a drop-free observation closes out the burst
    await asyncio.sleep(0)
    lines = _lines(caplog, _QUEUE_DROP_MESSAGE)
    assert sum(line.dropped_since_last for line in lines) == counter.dropped
    assert lines[-1].dropped_total == counter.dropped


_CAN_PARSE_MESSAGE = "dropped malformed CAN frame"


def test_can_frame_parse_error_storm_is_bounded_and_keeps_the_tail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A parse-error storm is proportional to a failure rate too (63M errors
    in 27 minutes, session 2026-05-15T020322): 10k errors inside one second
    are one line, not 100, and the final summary still lands."""
    caplog.set_level(logging.WARNING)
    owner = "test-storm"
    errors = 10_000
    for _ in range(errors):
        log_dropped_can_frame(ValueError("bad slcan line"), owner=owner)
    # Real monotonic clock here (the registry owns it); a second boundary
    # inside the loop may allow one extra line, never one per 100 errors.
    assert len(_lines(caplog, _CAN_PARSE_MESSAGE)) <= 2

    flush_dropped_can_frame_log(owner=owner, force=True)
    lines = _lines(caplog, _CAN_PARSE_MESSAGE)
    assert sum(line.dropped_since_last for line in lines) == errors
    assert lines[-1].dropped_total == errors
    assert lines[-1].owner == owner
    assert lines[0].dropped_by == {"ValueError": 1}


def test_can_frame_parse_error_throttles_are_per_owner(caplog: pytest.LogCaptureFixture) -> None:
    """Two live buses must not suppress each other's lines — a shared
    throttle would also make ``dropped_total`` flip between two counters."""
    caplog.set_level(logging.WARNING)
    log_dropped_can_frame(ValueError("a"), owner="test-bus-a")
    log_dropped_can_frame(IndexError("b"), owner="test-bus-b")

    lines = _lines(caplog, _CAN_PARSE_MESSAGE)
    assert [line.owner for line in lines] == ["test-bus-a", "test-bus-b"]
    assert [line.dropped_total for line in lines] == [1, 1]


def test_flush_can_frame_parse_error_log_is_a_no_op_for_an_unknown_owner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    flush_dropped_can_frame_log(owner="never-logged", force=True)
    assert _lines(caplog, _CAN_PARSE_MESSAGE) == []


def test_dropped_total_accumulates_across_a_collector_restart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Found by verification, 2026-09-17. A ``CanReader`` is rebuilt on every
    reconnect attempt with ``parse_errors`` back at 0, and
    ``run_can_obd_loop`` gets a fresh ``CanObdLoopStats`` per attempt, while
    the throttle registry is keyed by an owner string that outlives both.
    Echoing the owner's counter therefore undercounted: 5,000 drops then
    3,000 more reported a total of 5,000. ``dropped_total`` must equal the sum
    of the lines' own ``dropped_since_last`` values.
    """
    clock = _FakeClock()
    caplog.set_level(logging.WARNING)
    throttle = DropLogThrottle("dropped", clock=clock, interval=1.0)

    for _ in range(5000):
        throttle.record("ValueError")
    throttle.flush(force=True)
    clock.advance(5.0)
    # the reconnect: the owner's own counter restarts at zero here
    for _ in range(3000):
        throttle.record("ValueError")
    throttle.flush(force=True)

    lines = _lines(caplog, "dropped")
    assert sum(line.dropped_since_last for line in lines) == 8000
    assert lines[-1].dropped_total == 8000
