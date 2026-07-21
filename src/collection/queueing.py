"""Telemetry queue pressure helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

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


def threadsafe_enqueue(
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue[TelemetryRecord],
    *,
    on_drop: DropCallback | None = None,
) -> Callable[[TelemetryRecord], None]:
    """Thread-safe "hop onto the event loop and enqueue" callback (Task 4.7).

    Wraps :func:`put_nowait_or_drop` behind ``loop.call_soon_threadsafe``, so
    a collector's background reader thread can safely push a record without
    touching the asyncio queue directly — the factory duplicated identically
    at three call sites (``obd_collector.py``'s ``queue_reading_callback``,
    ``can_obd_collector.py``'s per-attempt ``on_frame``/``on_reading``
    closures). Callers still do their own reading→``TelemetryRecord``
    conversion before calling the returned callback — that conversion
    genuinely differs per collector (``reading_to_record`` vs
    ``frame_to_record``).
    """

    def _enqueue(record: TelemetryRecord) -> None:
        loop.call_soon_threadsafe(partial(put_nowait_or_drop, queue, record, on_drop=on_drop))

    return _enqueue


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
