from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.analysis.gps_tracker import GpsTracker
from src.collection.queueing import QueueDepthMonitor
from src.dashboard.broadcast import Broadcaster
from src.preprocessing.live import LiveFeatureSource
from src.storage.monitor import warn_if_storage_low
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


async def drain_queue_with_broadcast(
    queue: asyncio.Queue[TelemetryRecord],
    writer: ParquetTelemetryWriter,
    stop_event: asyncio.Event,
    *,
    telemetry_broadcaster: Broadcaster | None = None,
    live_source: LiveFeatureSource | None = None,
    gps_tracker: GpsTracker | None = None,
    poll_interval: float = 0.5,
    storage_path: Path | None = None,
    storage_warning_mb: int | None = None,
    queue_warning_fraction: float = 0.8,
) -> None:
    """Drain telemetry into storage + the live broadcaster + the live source.

    Extension of ``src.main._drain_queue``: same Parquet write path, same
    queue-depth / disk-space monitoring, but each record is *also*

    * published onto ``telemetry_broadcaster`` so ``/ws/telemetry``
      subscribers see it without polling Parquet;
    * fed into ``live_source.observe(record)`` so the streaming feature
      pipeline (Milestone B) stays in sync with what's hitting disk.

    Both are optional — pass ``None`` to skip either. Publishing is
    best-effort: a full subscriber queue drops the oldest message and a
    ``live_source.observe`` exception is caught + logged. Neither can
    block the writer.
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)

    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            if writer.flush_due():
                writer.flush()
                if storage_path is not None and storage_warning_mb is not None:
                    warn_if_storage_low(storage_path, storage_warning_mb)
            continue

        try:
            writer.write(record)
            if telemetry_broadcaster is not None:
                telemetry_broadcaster.publish(_record_to_dict(record))
                if live_source is not None:
                    for decoded in live_source.decode_for_telemetry(record):
                        telemetry_broadcaster.publish(_record_to_dict(decoded))
            if live_source is not None:
                try:
                    live_source.observe(record)
                except Exception:  # noqa: BLE001 — live inference is best-effort
                    log.exception("live_source.observe failed; skipping record")
            if gps_tracker is not None:
                try:
                    gps_tracker.observe(record)
                except Exception:  # noqa: BLE001 — odometer is best-effort
                    log.exception("gps_tracker.observe failed; skipping record")
        finally:
            queue.task_done()
            queue_monitor.observe(queue)

    if gps_tracker is not None:
        gps_tracker.persist()
    writer.close()
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)


def _record_to_dict(record: TelemetryRecord) -> dict[str, Any]:
    """Live-stream shape: same {timestamp, source, channel, value, unit}
    the REST ``/telemetry/latest`` endpoint returns, so the dashboard
    can render the two paths identically.

    ``timestamp`` is ISO-8601 *string* (not a ``datetime``): the WS handler
    serializes this via ``websocket.send_json``, and a raw datetime raises
    ``TypeError: Object of type datetime is not JSON serializable`` —
    closing every telemetry socket. The REST path already stringifies; this
    keeps the two paths genuinely identical (and JSON-safe)."""
    return {
        "timestamp": record.timestamp.isoformat(),
        "source": record.source,
        "channel": record.channel,
        "value": record.value,
        "unit": record.unit,
    }


__all__ = [
    "drain_queue_with_broadcast",
    "_record_to_dict",
]
