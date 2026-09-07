"""Telemetry queue pressure helpers."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

DropCallback = Callable[[TelemetryRecord], None]


@dataclass
class QueueDropCounter:
    """Count dropped telemetry records and log at a bounded rate."""

    name: str
    dropped: int = 0
    log_every: int = 100

    def __call__(self, record: TelemetryRecord) -> None:
        self.dropped += 1
        if self.dropped == 1 or self.dropped % self.log_every == 0:
            log.warning(
                "telemetry queue full; dropping record",
                extra={
                    "queue": self.name,
                    "source": record.source,
                    "channel": record.channel,
                    "dropped_total": self.dropped,
                },
            )


def log_dropped_can_frame(count: int, exc: Exception, *, log_every: int = 100) -> None:
    """Bounded-rate warning for a malformed CAN frame parse error (Task 4.7).

    Logs on the first occurrence and every ``log_every``-th one after —
    mirroring :class:`QueueDropCounter`'s bounded-log shape, applied to the
    "dropped malformed CAN frame" warning that was hand-rolled identically
    at three call sites (``can_collector.py``'s slcan and socketcan read
    loops, ``can_obd_collector.py``'s shared frame-read helper). ``count``
    is the caller's own running total (already incremented) — each site
    keeps its own counter storage (a local ``int``, or a ``CanObdLoopStats``
    field summed elsewhere), so this owns only the shared logging policy.
    """
    if count == 1 or count % log_every == 0:
        log.warning(
            "dropped malformed CAN frame",
            extra={"error": f"{type(exc).__name__}: {exc}", "parse_errors_total": count},
        )


@dataclass
class QueueDepthMonitor:
    """Warn when an asyncio queue remains near capacity."""

    warning_fraction: float = 0.8
    clear_fraction: float = 0.5
    warned: bool = False

    def observe(self, queue: asyncio.Queue[TelemetryRecord]) -> None:
        maxsize = queue.maxsize
        if maxsize <= 0:
            return
        depth = queue.qsize()
        fraction = depth / maxsize
        if fraction >= self.warning_fraction and not self.warned:
            self.warned = True
            log.warning(
                "telemetry queue near capacity",
                extra={
                    "queue_depth": depth,
                    "queue_maxsize": maxsize,
                    "queue_fraction": round(fraction, 3),
                },
            )
        elif self.warned and fraction <= self.clear_fraction:
            self.warned = False


#: Producer-side buffer bound (records) between a collector thread and the
#: event loop. Sized for a few seconds of a flooded CAN bus (~1 kHz) while the
#: loop is busy; overflow is counted, never silent.
DEFAULT_HANDOFF_CAPACITY = 4096
#: Records one scheduled drain callback moves per loop turn. Bounds loop
#: occupancy per callback; leftovers reschedule with ``call_soon`` so the loop
#: interleaves other work between chunks.
DEFAULT_HANDOFF_MAX_BATCH = 256


@dataclass(frozen=True)
class HandoffStats:
    """Snapshot of one :class:`ThreadHandoff`'s counters (C07.5).

    ``accepted + dropped`` equals every record ever pushed, so C03-style loss
    attribution can reconcile against the producer's own count. ``dropped``
    is loss *before* the asyncio queue (buffer full, or pushed after
    :meth:`ThreadHandoff.close`); queue-full drops are the ``on_drop``
    callback's to count, they are a different cause.
    """

    accepted: int
    dropped: int
    high_water: int
    capacity: int
    drains: int
    max_batch: int

    def __add__(self, other: HandoffStats) -> HandoffStats:
        """Cumulate across reconnect attempts: counters sum, marks take the max."""
        return HandoffStats(
            accepted=self.accepted + other.accepted,
            dropped=self.dropped + other.dropped,
            high_water=max(self.high_water, other.high_water),
            capacity=max(self.capacity, other.capacity),
            drains=self.drains + other.drains,
            max_batch=max(self.max_batch, other.max_batch),
        )


class ThreadHandoff:
    """Bounded producer buffer that hops records onto the event loop in chunks.

    C07.4/C07.5 — replaces one ``loop.call_soon_threadsafe`` per record. A
    push from any thread appends to a bounded FIFO and schedules **one**
    drain callback only when none is already pending; every record pushed
    before that drain runs rides the same wake-up. On an idle loop the drain
    runs on the very next turn, so sparse telemetry pays exactly the old
    per-record latency (no timer, no waiting for a full batch); under load the
    loop's own backlog is what coalesces pushes. ``max_latency`` is therefore
    structurally one loop turn rather than a tunable wait.

    Ordering: single producer -> FIFO buffer -> single drain owner (the loop),
    so source order is preserved end to end. The drain moves at most
    ``max_batch`` records per callback and reschedules itself for the rest.

    Ownership: :meth:`close` (loop thread only) flushes what is buffered into
    the queue in order and then refuses further pushes — a late push or a late
    drain callback after close is a counted no-op, never a queue write.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[TelemetryRecord],
        *,
        on_drop: DropCallback | None = None,
        capacity: int = DEFAULT_HANDOFF_CAPACITY,
        max_batch: int = DEFAULT_HANDOFF_MAX_BATCH,
        name: str = "handoff",
        log_every: int = 100,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if max_batch <= 0:
            raise ValueError("max_batch must be positive")
        self._loop = loop
        self._queue = queue
        self._on_drop = on_drop
        self._capacity = capacity
        self._max_batch = max_batch
        self._name = name
        self._log_every = log_every
        self._lock = threading.Lock()
        self._buffer: deque[TelemetryRecord] = deque()
        self._scheduled = False
        self._closed = False
        self._accepted = 0
        self._dropped = 0
        self._high_water = 0
        self._drains = 0

    @property
    def closed(self) -> bool:
        return self._closed

    def push(self, record: TelemetryRecord) -> bool:
        """Buffer ``record`` (any thread); False when it was dropped instead."""
        with self._lock:
            if self._closed or len(self._buffer) >= self._capacity:
                self._dropped += 1
                dropped_total = self._dropped
            else:
                self._buffer.append(record)
                self._accepted += 1
                depth = len(self._buffer)
                if depth > self._high_water:
                    self._high_water = depth
                dropped_total = 0
                schedule = not self._scheduled
                self._scheduled = True
        if dropped_total:
            self._log_drop(dropped_total, record)
            return False
        if schedule:
            self._schedule_drain()
        return True

    __call__ = push

    def close(self) -> int:
        """Flush buffered records in order, then refuse further pushes.

        Loop-thread only. Returns how many records were flushed. Safe to
        call more than once.
        """
        with self._lock:
            self._closed = True
            pending = list(self._buffer)
            self._buffer.clear()
            self._scheduled = False
        for record in pending:
            put_nowait_or_drop(self._queue, record, on_drop=self._on_drop)
        return len(pending)

    def stats(self) -> HandoffStats:
        with self._lock:
            return HandoffStats(
                accepted=self._accepted,
                dropped=self._dropped,
                high_water=self._high_water,
                capacity=self._capacity,
                drains=self._drains,
                max_batch=self._max_batch,
            )

    def _schedule_drain(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._drain)
        except RuntimeError:
            # Loop already closed: nothing will ever drain. Leave the flag set
            # so the producer stops paying for wake-ups; close() reconciles.
            log.warning("telemetry handoff loop closed; buffered records stranded")

    def _drain(self) -> None:
        """Move one bounded chunk onto the queue (loop thread only)."""
        with self._lock:
            if self._closed:
                self._scheduled = False
                return
            take = min(self._max_batch, len(self._buffer))
            batch = [self._buffer.popleft() for _ in range(take)]
            remaining = len(self._buffer)
            self._scheduled = remaining > 0
            self._drains += 1
        for record in batch:
            put_nowait_or_drop(self._queue, record, on_drop=self._on_drop)
        if remaining:
            self._loop.call_soon(self._drain)

    def _log_drop(self, dropped_total: int, record: TelemetryRecord) -> None:
        if dropped_total == 1 or dropped_total % self._log_every == 0:
            log.warning(
                "telemetry handoff buffer full; dropping record",
                extra={
                    "handoff": self._name,
                    "source": record.source,
                    "channel": record.channel,
                    "capacity": self._capacity,
                    "closed": self._closed,
                    "dropped_total": dropped_total,
                },
            )


def threadsafe_enqueue(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[TelemetryRecord],
    *,
    on_drop: DropCallback | None = None,
    capacity: int = DEFAULT_HANDOFF_CAPACITY,
    max_batch: int = DEFAULT_HANDOFF_MAX_BATCH,
    name: str = "handoff",
) -> ThreadHandoff:
    """Thread-safe "hop onto the event loop and enqueue" callback (Task 4.7).

    Returns a :class:`ThreadHandoff` — callable exactly like the plain closure
    it replaced (``enqueue(record)``), so a collector's background reader
    thread can push a record without touching the asyncio queue directly —
    but backed by one bounded, accounted buffer and one coalesced drain
    callback instead of one ``call_soon_threadsafe`` per record (C07.4).
    Callers still do their own reading->``TelemetryRecord`` conversion before
    calling it — that conversion genuinely differs per collector
    (``reading_to_record`` vs ``frame_to_record``). Owners that know when
    their producer thread has exited should call :meth:`ThreadHandoff.close`
    so late callbacks become no-ops.
    """
    return ThreadHandoff(
        loop, queue, on_drop=on_drop, capacity=capacity, max_batch=max_batch, name=name
    )


def put_nowait_or_drop(
    queue: asyncio.Queue[TelemetryRecord],
    record: TelemetryRecord,
    *,
    on_drop: DropCallback | None = None,
) -> bool:
    """Put a record without blocking; return False and count when the queue is full."""
    try:
        queue.put_nowait(record)
    except asyncio.QueueFull:
        if on_drop is None:
            log.warning(
                "telemetry queue full; dropping record",
                extra={"source": record.source, "channel": record.channel},
            )
        else:
            on_drop(record)
        return False
    return True
