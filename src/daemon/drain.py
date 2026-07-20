from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.alerts.emitter import AnomalyEventEmitter
from src.analysis.gps_tracker import GpsTracker
from src.analysis.vehicle_ledger import VehicleLedgerStore
from src.collection.queueing import QueueDepthMonitor
from src.control.dtc_state import DtcState
from src.dashboard.broadcast import Broadcaster
from src.models.health import HealthMonitor
from src.preprocessing.live import LiveFeatureSource
from src.storage.async_writer import AsyncParquetTelemetryWriter, ensure_async_writer
from src.storage.monitor import warn_if_storage_low
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

TelemetryOverflowPolicy = Literal["drop_oldest", "drop_newest"]


@dataclass(frozen=True)
class TelemetrySink:
    """One queue fed by the collector fanout."""

    name: str
    queue: asyncio.Queue[TelemetryRecord]
    overflow: TelemetryOverflowPolicy


async def run_telemetry_fanout(
    ingest_queue: asyncio.Queue[TelemetryRecord],
    sinks: Sequence[TelemetrySink],
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Fan collector records out to independent sink queues.

    All sink puts are non-blocking: live/dashboard sinks drop oldest so the
    freshest state wins; storage drops newest and records explicit data loss if
    its queue fills. The latter should be rare with production queue sizes, but
    it is safer than letting a wedged storage sink starve live inference.
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    while not stop_event.is_set() or not ingest_queue.empty():
        queue_monitor.observe(ingest_queue)
        _observe_queue(health_monitor, "ingest", ingest_queue)
        try:
            record = await asyncio.wait_for(ingest_queue.get(), timeout=poll_interval)
        except TimeoutError:
            continue

        try:
            for sink in sinks:
                _enqueue_sink(sink, record, health_monitor=health_monitor)
        finally:
            ingest_queue.task_done()
            queue_monitor.observe(ingest_queue)
            _observe_queue(health_monitor, "ingest", ingest_queue)
            for sink in sinks:
                _observe_queue(health_monitor, sink.name, sink.queue)


def _enqueue_sink(
    sink: TelemetrySink,
    record: TelemetryRecord,
    *,
    health_monitor: HealthMonitor | None = None,
) -> bool:
    """Best-effort enqueue according to the sink's overflow policy."""
    try:
        sink.queue.put_nowait(record)
        return True
    except asyncio.QueueFull:
        pass

    if sink.overflow == "drop_oldest":
        try:
            sink.queue.get_nowait()
            sink.queue.task_done()
            _note_drop(health_monitor, sink.name)
        except asyncio.QueueEmpty:
            pass
        try:
            sink.queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            _note_drop(health_monitor, sink.name)
            log.warning("telemetry sink queue full; dropping record", extra={"sink": sink.name})
            return False

    _note_drop(health_monitor, sink.name)
    log.warning("telemetry sink queue full; dropping record", extra={"sink": sink.name})
    return False


async def run_storage_sink(
    queue: asyncio.Queue[TelemetryRecord],
    writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter,
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.5,
    storage_path: Path | None = None,
    storage_warning_mb: int | None = None,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Persist telemetry to Parquet from its own queue."""
    async_writer = ensure_async_writer(writer)
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)

    try:
        while not stop_event.is_set() or not queue.empty():
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "storage", queue)
            try:
                record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
            except TimeoutError:
                if await async_writer.flush_due():
                    await _storage_call(
                        async_writer.flush,
                        health_monitor=health_monitor,
                    )
                    if storage_path is not None and storage_warning_mb is not None:
                        warn_if_storage_low(storage_path, storage_warning_mb)
                continue

            try:
                await _storage_call(
                    async_writer.write,
                    record,
                    health_monitor=health_monitor,
                )
            finally:
                queue.task_done()
                queue_monitor.observe(queue)
                _observe_queue(health_monitor, "storage", queue)
    finally:
        try:
            await _storage_call(async_writer.close, health_monitor=health_monitor)
        finally:
            if storage_path is not None and storage_warning_mb is not None:
                warn_if_storage_low(storage_path, storage_warning_mb)


async def run_telemetry_broadcast_sink(
    queue: asyncio.Queue[TelemetryRecord],
    broadcaster: Broadcaster,
    stop_event: asyncio.Event,
    *,
    live_source: LiveFeatureSource | None = None,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Publish telemetry records to the dashboard stream."""
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        _observe_queue(health_monitor, "broadcast", queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            continue

        try:
            broadcaster.publish(_record_to_dict(record))
            if live_source is not None:
                for decoded in live_source.decode_for_telemetry(record):
                    broadcaster.publish(_record_to_dict(decoded))
        except Exception as exc:  # noqa: BLE001 — dashboard stream is best-effort
            _note_error(health_monitor, "broadcast", exc)
            log.exception("telemetry broadcast failed; skipping record")
        finally:
            queue.task_done()
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "broadcast", queue)


async def run_live_source_sink(
    queue: asyncio.Queue[TelemetryRecord],
    live_source: LiveFeatureSource,
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Feed the live feature source without depending on storage progress."""
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        _observe_queue(health_monitor, "live", queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            continue

        try:
            live_source.observe(record)
        except Exception as exc:  # noqa: BLE001 — live inference is best-effort
            _note_error(health_monitor, "live", exc)
            log.exception("live_source.observe failed; skipping record")
        finally:
            queue.task_done()
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "live", queue)


async def run_gps_sink(
    queue: asyncio.Queue[TelemetryRecord],
    gps_tracker: GpsTracker,
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
    on_trip_ended: Callable[[], None] | None = None,
) -> None:
    """Feed GPS trip state from its own best-effort queue.

    ``on_trip_ended`` (Task 5.4) fires exactly once per drive, the tick
    ``gps_tracker.observe`` reports the sustained moving->stationary edge —
    best-effort like every other callback here, a broken digest/notifier
    path must never take down the GPS sink.
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    try:
        while not stop_event.is_set() or not queue.empty():
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "gps", queue)
            try:
                record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
            except TimeoutError:
                continue

            try:
                trip_ended = gps_tracker.observe(record)
            except Exception as exc:  # noqa: BLE001 — odometer is best-effort
                _note_error(health_monitor, "gps", exc)
                log.exception("gps_tracker.observe failed; skipping record")
            else:
                if trip_ended and on_trip_ended is not None:
                    try:
                        on_trip_ended()
                    except Exception:  # noqa: BLE001 — digest/notify is best-effort
                        log.exception("on_trip_ended failed")
            finally:
                queue.task_done()
                queue_monitor.observe(queue)
                _observe_queue(health_monitor, "gps", queue)
    finally:
        try:
            gps_tracker.persist()
        except Exception as exc:  # noqa: BLE001 — shutdown persistence is best-effort
            _note_error(health_monitor, "gps", exc)
            log.exception("gps_tracker.persist failed")


async def run_dtc_sink(
    queue: asyncio.Queue[TelemetryRecord],
    dtc_state: DtcState,
    stop_event: asyncio.Event,
    *,
    emitter: AnomalyEventEmitter | None = None,
    vehicle_ledger: VehicleLedgerStore | None = None,
    active_session_path: Callable[[], Path] | None = None,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Feed DTC poll readings (Task 1.3) into the cached report + sidecar.

    ``active_session_path`` mirrors the retention sweeper's callable
    (``lambda: storage_writer.paths.root``) — read fresh on every record so a
    session rotation mid-drive resets ``DtcState``'s per-session baseline
    (Task 1.7) instead of pinning it to whatever session was active at
    sink-startup.
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        _observe_queue(health_monitor, "dtc", queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            continue

        try:
            session_dir = active_session_path() if active_session_path is not None else None
            dtc_state.observe(
                record,
                session_id=session_dir.name if session_dir is not None else None,
                session_dir=session_dir,
                emitter=emitter,
                vehicle_ledger=vehicle_ledger,
            )
        except Exception as exc:  # noqa: BLE001 — DTC bookkeeping is best-effort
            _note_error(health_monitor, "dtc", exc)
            log.exception("dtc_state.observe failed; skipping record")
        finally:
            queue.task_done()
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "dtc", queue)


async def drain_queue_with_broadcast(
    queue: asyncio.Queue[TelemetryRecord],
    writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter,
    stop_event: asyncio.Event,
    *,
    telemetry_broadcaster: Broadcaster | None = None,
    live_source: LiveFeatureSource | None = None,
    gps_tracker: GpsTracker | None = None,
    dtc_state: DtcState | None = None,
    dtc_emitter: AnomalyEventEmitter | None = None,
    vehicle_ledger: VehicleLedgerStore | None = None,
    active_session_path: Callable[[], Path] | None = None,
    on_trip_ended: Callable[[Path], None] | None = None,
    poll_interval: float = 0.5,
    storage_path: Path | None = None,
    storage_warning_mb: int | None = None,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Run collector fanout plus independent storage/live/dashboard sinks.

    Kept under the historical name for callers. Parquet remains the durable
    recording sink, but it is no longer the dispatcher for live inference.
    """
    sink_queue_size = _sink_queue_size(queue)
    sinks: list[TelemetrySink] = []
    sink_tasks: list[asyncio.Task[None]] = []

    async_writer = ensure_async_writer(writer)
    storage_queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=sink_queue_size)
    sinks.append(TelemetrySink("storage", storage_queue, "drop_newest"))
    sink_tasks.append(
        asyncio.create_task(
            run_storage_sink(
                storage_queue,
                async_writer,
                stop_event,
                poll_interval=poll_interval,
                storage_path=storage_path,
                storage_warning_mb=storage_warning_mb,
                queue_warning_fraction=queue_warning_fraction,
                health_monitor=health_monitor,
            )
        )
    )

    if telemetry_broadcaster is not None:
        broadcast_queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=sink_queue_size)
        sinks.append(TelemetrySink("broadcast", broadcast_queue, "drop_oldest"))
        sink_tasks.append(
            asyncio.create_task(
                run_telemetry_broadcast_sink(
                    broadcast_queue,
                    telemetry_broadcaster,
                    stop_event,
                    live_source=live_source,
                    poll_interval=poll_interval,
                    queue_warning_fraction=queue_warning_fraction,
                    health_monitor=health_monitor,
                )
            )
        )

    if live_source is not None:
        live_queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=sink_queue_size)
        sinks.append(TelemetrySink("live", live_queue, "drop_oldest"))
        sink_tasks.append(
            asyncio.create_task(
                run_live_source_sink(
                    live_queue,
                    live_source,
                    stop_event,
                    poll_interval=poll_interval,
                    queue_warning_fraction=queue_warning_fraction,
                    health_monitor=health_monitor,
                )
            )
        )

    if gps_tracker is not None:
        gps_queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=sink_queue_size)
        sinks.append(TelemetrySink("gps", gps_queue, "drop_oldest"))

        def _trip_ended() -> None:
            if on_trip_ended is not None and active_session_path is not None:
                on_trip_ended(active_session_path())

        sink_tasks.append(
            asyncio.create_task(
                run_gps_sink(
                    gps_queue,
                    gps_tracker,
                    stop_event,
                    poll_interval=poll_interval,
                    queue_warning_fraction=queue_warning_fraction,
                    health_monitor=health_monitor,
                    on_trip_ended=_trip_ended,
                )
            )
        )

    if dtc_state is not None:
        dtc_queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=sink_queue_size)
        sinks.append(TelemetrySink("dtc", dtc_queue, "drop_oldest"))
        sink_tasks.append(
            asyncio.create_task(
                run_dtc_sink(
                    dtc_queue,
                    dtc_state,
                    stop_event,
                    emitter=dtc_emitter,
                    vehicle_ledger=vehicle_ledger,
                    active_session_path=active_session_path,
                    poll_interval=poll_interval,
                    queue_warning_fraction=queue_warning_fraction,
                    health_monitor=health_monitor,
                )
            )
        )

    fanout_task = asyncio.create_task(
        run_telemetry_fanout(
            queue,
            sinks,
            stop_event,
            poll_interval=poll_interval,
            queue_warning_fraction=queue_warning_fraction,
            health_monitor=health_monitor,
        )
    )

    try:
        await fanout_task
        for sink in sinks:
            await sink.queue.join()
        await asyncio.gather(*sink_tasks)
    finally:
        fanout_task.cancel()
        for task in sink_tasks:
            task.cancel()
        await asyncio.gather(fanout_task, *sink_tasks, return_exceptions=True)


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


def _sink_queue_size(queue: asyncio.Queue[TelemetryRecord]) -> int:
    return queue.maxsize if queue.maxsize > 0 else 10_000


async def _storage_call(
    func: Any,
    *args: Any,
    health_monitor: HealthMonitor | None,
) -> Any:
    try:
        return await func(*args)
    except Exception as exc:  # noqa: BLE001 — storage failures must be visible, not fatal
        _note_error(health_monitor, "storage", exc)
        log.exception("telemetry storage sink failed")
        return None


def _observe_queue(
    health_monitor: HealthMonitor | None,
    name: str,
    queue: asyncio.Queue[TelemetryRecord],
) -> None:
    if health_monitor is not None:
        health_monitor.observe_telemetry_queue(name, queue)


def _note_drop(health_monitor: HealthMonitor | None, name: str) -> None:
    if health_monitor is not None:
        health_monitor.note_telemetry_drop(name)


def _note_error(health_monitor: HealthMonitor | None, name: str, exc: BaseException) -> None:
    if health_monitor is not None:
        health_monitor.note_telemetry_error(name, exc)


__all__ = [
    "TelemetrySink",
    "drain_queue_with_broadcast",
    "run_dtc_sink",
    "run_gps_sink",
    "run_live_source_sink",
    "run_storage_sink",
    "run_telemetry_broadcast_sink",
    "run_telemetry_fanout",
    "_record_to_dict",
]
