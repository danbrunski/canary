"""Tests for telemetry queue pressure helpers."""

from __future__ import annotations

import asyncio
import logging

from src.collection.queueing import QueueDepthMonitor, QueueDropCounter, put_nowait_or_drop
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
