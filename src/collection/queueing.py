"""Telemetry queue pressure helpers."""

from __future__ import annotations

import asyncio
import logging
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
