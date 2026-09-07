"""Tests for telemetry queue pressure helpers."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from functools import partial

import pytest

from src.collection.queueing import (
    QueueDepthMonitor,
    QueueDropCounter,
    ThreadHandoff,
    put_nowait_or_drop,
    threadsafe_enqueue,
)
from src.telemetry import TelemetryRecord


def _record() -> TelemetryRecord:
    return TelemetryRecord.obd(1_778_000_000.0, "RPM", 700.0, "rpm")


def test_put_nowait_or_drop_counts_full_queue() -> None:
    queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=1)
    counter = QueueDropCounter("test")

    assert put_nowait_or_drop(queue, _record(), on_drop=counter) is True
    assert put_nowait_or_drop(queue, _record(), on_drop=counter) is False
    assert counter.dropped == 1


def test_queue_depth_monitor_warns_near_capacity(caplog) -> None:
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

    def counting(callback, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal scheduled
        scheduled += 1
        return original(callback, *args, **kwargs)

    loop.call_soon_threadsafe = counting  # type: ignore[method-assign]
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

    async def run(push, label: str) -> tuple[int, float]:  # type: ignore[no-untyped-def]
        scheduled = 0
        original = loop.call_soon_threadsafe

        def counting(callback, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal scheduled
            scheduled += 1
            return original(callback, *args, **kwargs)

        loop.call_soon_threadsafe = counting  # type: ignore[method-assign]
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
