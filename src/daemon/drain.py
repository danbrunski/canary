from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from src.alerts.emitter import AnomalyEventEmitter
from src.analysis.gps_tracker import GpsTracker, TripEnd
from src.collection.queueing import QueueDepthMonitor
from src.control.dtc_state import DtcState
from src.control.monitor_state import MonitorState
from src.daemon.broadcast import Broadcaster
from src.daemon.health import HealthMonitor
from src.decode.dids import DID_SOURCE, DecodedDid
from src.storage.async_writer import AsyncParquetTelemetryWriter, ensure_async_writer
from src.storage.monitor import warn_if_storage_low
from src.storage.parquet_writer import ParquetTelemetryWriter
from src.telemetry import DECODED_SOURCE, TelemetryRecord
from src.vehicle.vehicle_ledger import VehicleLedgerStore

if TYPE_CHECKING:
    # Annotation only: it loads the feature pipeline, and this module is on
    # the daemon's path to recording (S01.3).
    from src.preprocessing.live import LiveFeatureSource

log = logging.getLogger(__name__)

TelemetryOverflowPolicy = Literal["drop_oldest", "drop_newest"]

# One worker-thread handoff per raw record made the storage sink unusually
# sensitive to event-loop pressure. Keep batches bounded so the actor remains
# responsive to mode transitions and shutdown while amortizing that handoff.
STORAGE_WRITE_BATCH_SIZE = 1024
STORAGE_WRITE_BATCH_MAX_WAIT_SECONDS = 0.01


#: Records the fanout moves per event-loop turn once one is available. Bounds
#: loop occupancy per turn while amortizing the per-record ``wait_for`` (a task
#: plus a timer handle each) that dominated fanout cost under a busy bus.
FANOUT_MAX_BATCH = 256


@dataclass(frozen=True)
class TelemetrySink:
    """One queue fed by the collector fanout.

    ``accepts`` (C07.6) is an optional per-sink subscription filter applied
    in the fanout: a record it rejects is never enqueued to this sink. Only
    for sinks whose consumer would discard the record on sight anyway
    (GPS/DTC/monitor reject by ``source`` before doing any work), so it
    changes nothing but the wasted enqueue/dequeue; storage, broadcast and
    live sinks take every record.
    """

    name: str
    queue: asyncio.Queue[TelemetryRecord]
    overflow: TelemetryOverflowPolicy
    accepts: Callable[[TelemetryRecord], bool] | None = None


def storable(record: TelemetryRecord) -> bool:
    """The storage sink's subscription: every record but a ``decoded`` one.

    Decoded rows are never stored (P02.8, operator decision 2026-09-26). They
    were 28 % of every session's rows, 95 % repeats, and every one is
    recomputable from the raw frame stored beside it; the session readers
    decode them on read (``src.preprocessing.decode.decode_on_read``). No
    config key or capture profile reaches this: a replay of an old session
    that emits its stored decoded rows, or anything else that puts one on the
    ingest queue, still stores none
    (``tests/test_decoded_rows_never_stored.py``).
    """
    return record.source != DECODED_SOURCE


def accepts_source(source: str) -> Callable[[TelemetryRecord], bool]:
    """Sink filter: only records from one telemetry ``source``."""

    def _accepts(record: TelemetryRecord) -> bool:
        return record.source == source

    return _accepts


#: Replay output (``src.control.replay_control``); the trip sink suspends the
#: live trip while the writer records here.
_REPLAY_DIR_PREFIX = "_replay-"


def _trip_sink_filter(gps_tracker: GpsTracker) -> Callable[[TelemetryRecord], bool]:
    """The trip sink's subscription: whatever the tracker says it reads.

    The tracker owns the filter because it owns the road-speed binding and
    the decimation. A stand-in without ``accepts`` gets the pre-D03.29
    ``gps``-only subscription rather than every record on the bus.
    """
    accepts = getattr(gps_tracker, "accepts", None)
    if callable(accepts):
        return cast(Callable[[TelemetryRecord], bool], accepts)
    return accepts_source("gps")


def _resolved_live_source(
    ready: asyncio.Future[LiveFeatureSource | None] | None,
) -> LiveFeatureSource | None:
    """The live source once ``ready`` has resolved to one, else ``None``."""
    if ready is None or not ready.done() or ready.cancelled() or ready.exception() is not None:
        return None
    return ready.result()


class PipelineBarrier:
    """Wait until every record already in the pipeline has been handled (D02.9).

    A replay boundary switches the writer's output directory and resets the
    live in-memory state. Records still queued at that instant belong to the
    side they were produced on: live records queued before a replay starts go
    to the live session, the replay's own tail goes to ``_replay-*`` before the
    writer switches back. ``settle`` joins the ingest queue and then every
    sink queue (the fanout and every sink call ``task_done`` per record, and
    the storage sink only after the writer accepted the batch).

    ``run_daemon`` constructs it over the ingest queue and hands it to
    :func:`drain_queue_with_broadcast`, which attaches its live sink list (a
    reference, so a sink added later is joined too).
    """

    def __init__(self, ingest_queue: asyncio.Queue[TelemetryRecord]) -> None:
        self._ingest = ingest_queue
        self._sinks: Sequence[TelemetrySink] = ()

    def attach(self, sinks: Sequence[TelemetrySink]) -> None:
        self._sinks = sinks

    async def _join_all(self) -> None:
        await self._ingest.join()
        for sink in list(self._sinks):
            await sink.queue.join()

    async def settle(self, timeout: float) -> bool:
        """Join the pipeline, bounded. ``False`` (and a warning) on timeout —
        a wedged sink must not hold a replay boundary open forever."""
        try:
            await asyncio.wait_for(self._join_all(), timeout=timeout)
        except TimeoutError:
            log.warning("telemetry pipeline did not settle within %.1f s", timeout)
            return False
        return True


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
            first = await asyncio.wait_for(ingest_queue.get(), timeout=poll_interval)
        except TimeoutError:
            continue

        # One awaited get, then whatever is already queued (bounded) without
        # yielding — order is preserved, and a busy bus no longer costs one
        # task + timer per record.
        batch = [first]
        while len(batch) < FANOUT_MAX_BATCH:
            try:
                batch.append(ingest_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        try:
            for record in batch:
                for sink in sinks:
                    if sink.accepts is None or sink.accepts(record):
                        _enqueue_sink(sink, record, health_monitor=health_monitor)
        finally:
            for _ in batch:
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
            _note_drop(health_monitor, sink.name, queue=sink.queue)
        except asyncio.QueueEmpty:
            pass
        try:
            sink.queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            _note_drop(health_monitor, sink.name, queue=sink.queue)
            return False

    _note_drop(health_monitor, sink.name, queue=sink.queue)
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
    write_batch_size: int = STORAGE_WRITE_BATCH_SIZE,
    write_batch_max_wait_seconds: float = STORAGE_WRITE_BATCH_MAX_WAIT_SECONDS,
) -> None:
    """Persist telemetry to Parquet from its own queue.

    Writes each dequeued record as it arrived, except a ``decoded`` one
    (:func:`storable`, P02.8): the raw frames are the source of truth and the
    readers decode them on read. Until 2026-09-26 this sink also expanded
    every raw CAN frame into stored ``decoded:*`` rows through a storage-side
    ``FrameDecoder``; that expansion is gone, and nothing can pass one in.
    """
    if write_batch_size <= 0:
        raise ValueError("write_batch_size must be positive")
    if write_batch_max_wait_seconds < 0:
        raise ValueError("write_batch_max_wait_seconds must be non-negative")
    async_writer = ensure_async_writer(writer)
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    if storage_path is not None and storage_warning_mb is not None:
        warn_if_storage_low(storage_path, storage_warning_mb)

    try:
        while not stop_event.is_set() or not queue.empty():
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "storage", queue)
            try:
                first = await asyncio.wait_for(queue.get(), timeout=poll_interval)
            except TimeoutError:
                if await async_writer.flush_due():
                    await _storage_call(
                        async_writer.flush,
                        health_monitor=health_monitor,
                    )
                    if storage_path is not None and storage_warning_mb is not None:
                        warn_if_storage_low(storage_path, storage_warning_mb)
                continue

            records = [first]
            batch_deadline = asyncio.get_running_loop().time() + write_batch_max_wait_seconds
            while len(records) < write_batch_size:
                try:
                    records.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    remaining = batch_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        records.append(await asyncio.wait_for(queue.get(), timeout=remaining))
                    except TimeoutError:
                        break

            # The fanout never enqueues a decoded row here (``storable`` is
            # this sink's subscription); the refusal is repeated for any other
            # way onto this queue. A per-id stage (P02.10's change-only CAN
            # frames) belongs here too: it sees every dequeued record, in
            # arrival order, before the writer does.
            to_write = [record for record in records if storable(record)]

            try:
                started_at = time.perf_counter()
                files = await _storage_call(
                    async_writer.write_many,
                    to_write,
                    health_monitor=health_monitor,
                )
                if health_monitor is not None:
                    if files is None:
                        # ``_storage_call`` reports and swallows the exception
                        # so the sink can continue. The failed batch is no
                        # longer queued, therefore count it as explicit loss.
                        health_monitor.note_telemetry_drop("storage", count=len(records))
                    else:
                        health_monitor.observe_telemetry_write(
                            "storage",
                            records=len(to_write),
                            elapsed_seconds=time.perf_counter() - started_at,
                            flushes=len(files),
                        )
                        # S01.1: the writer has accepted these, so this, not
                        # a collector, is when recording began.
                        health_monitor.startup.note_stored(records)
            finally:
                for _ in records:
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
    live_source_getter: Callable[[], LiveFeatureSource | None] | None = None,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Publish telemetry records to the dashboard stream.

    ``live_source_getter`` is for a source that is built after this sink
    starts (see ``drain_queue_with_broadcast``'s ``live_source_ready``);
    until it returns one, records are published without their decodes.
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        _observe_queue(health_monitor, "broadcast", queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            continue

        try:
            # The stream is live-only: there is no backlog to preserve for a
            # future subscriber.  Avoid serialising every raw record and
            # decoding CAN/DID rows when nobody has the dashboard open.  At
            # bus rate that otherwise duplicates work already performed by
            # the storage and live-feature sinks for no observable result.
            if broadcaster.subscriber_count == 0:
                continue
            broadcaster.publish(_record_to_dict(record))
            source = live_source
            if source is None and live_source_getter is not None:
                source = live_source_getter()
            if source is not None:
                for decoded in source.decode_for_telemetry(record):
                    broadcaster.publish(_record_to_dict(decoded))
                did = source.decode_did_for_telemetry(record)
                if did is not None:
                    broadcaster.publish(_did_to_dict(record, did))
        except Exception as exc:  # noqa: BLE001 — dashboard stream is best-effort
            _note_error(health_monitor, "broadcast", exc)
            log.exception("telemetry broadcast failed; skipping record")
        finally:
            queue.task_done()
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "broadcast", queue)


async def _run_record_sink(
    name: str,
    queue: asyncio.Queue[TelemetryRecord],
    handle: Callable[[TelemetryRecord], None],
    stop_event: asyncio.Event,
    *,
    failure: str,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """The per-record sink loop: drain ``queue`` into ``handle`` until stopped.

    Best-effort per record: a failing ``handle`` is counted against ``name``
    and logged as ``"<failure> failed; skipping record"``, and the loop keeps
    going. Drains what is queued after ``stop_event`` before returning. The
    live, DTC and monitor sinks were three copies of this loop (R03.8).
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    while not stop_event.is_set() or not queue.empty():
        queue_monitor.observe(queue)
        _observe_queue(health_monitor, name, queue)
        try:
            record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
        except TimeoutError:
            continue

        try:
            handle(record)
        except Exception as exc:  # noqa: BLE001 — every record sink is best-effort
            _note_error(health_monitor, name, exc)
            log.exception("%s failed; skipping record", failure)
        finally:
            queue.task_done()
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, name, queue)


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
    await _run_record_sink(
        "live",
        queue,
        live_source.observe,
        stop_event,
        failure="live_source.observe",
        poll_interval=poll_interval,
        queue_warning_fraction=queue_warning_fraction,
        health_monitor=health_monitor,
    )


async def run_gps_sink(  # noqa: PLR0915 — one loop, three event kinds
    queue: asyncio.Queue[TelemetryRecord],
    gps_tracker: GpsTracker,
    stop_event: asyncio.Event,
    *,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
    on_trip_ended: Callable[[Path | None, TripEnd], None] | None = None,
    active_session_path: Callable[[], Path] | None = None,
    on_trip_unobservable: Callable[[Path, TripEnd], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Feed GPS and vehicle-speed trip state from its own best-effort queue.

    ``on_trip_ended`` (Task 5.4) fires exactly once per drive, the tick
    ``gps_tracker.observe`` reports the sustained moving->stationary edge —
    best-effort like every other callback here, a broken digest/notifier
    path must never take down the GPS sink. It is called with the session the
    trip ended in — :attr:`TripEnd.session`, where the stop was seen, which is
    often *not* the session active when the edge fires 300 s later — and the
    end itself (its ``sessions`` name every session the trip spanned).
    ``None`` only when no ``active_session_path`` is wired.

    Sessions are followed, not looked up at fire time (D03.29).
    ``active_session_path`` is read before every record and every idle tick;
    on a rotation the tracker's :meth:`~GpsTracker.close_session` runs first
    and its answer goes to the session that *ended*. Looking the session up
    when the edge fired digested whichever session was active by then: a stop
    followed by silence ended the trip on the next key-on's first record, gave
    the finished drive no digest, and gave the new session a near-empty one.

    ``on_trip_unobservable`` (D03.29) receives the session and the
    :class:`~src.analysis.gps_tracker.TripEnd` whenever the tracker could not
    see a drive end: speed lost while moving for the whole window, or a
    session that rotated away with no edge and a blind stretch that long. The
    caller records it as the reason there is no digest, so an absent digest is
    never silent.

    An idle queue (``poll_interval`` with no record) advances the edge's clock
    via ``GpsTracker.tick``, so key-off silence ends a stopped trip on time even
    when nothing at all arrives. The tick's time is the last live record's
    timestamp plus ``clock()`` (monotonic) elapsed since it arrived — never the
    wall clock, whose steps (chrony, a GPS time fix) would otherwise end or
    split a trip; there is no tick before the first live record. Replay output (``_replay-*``)
    suspends the live trip and resumes it afterwards; nothing is reported for
    a replay directory.
    """
    queue_monitor = QueueDepthMonitor(warning_fraction=queue_warning_fraction)
    session_dir: Path | None = None
    # Stand-ins without the D03.29 surface (tests' sink-filter fakes) still run.
    tick = getattr(gps_tracker, "tick", None)
    enter_session = getattr(gps_tracker, "enter_session", None)
    #: (last live record's timestamp, ``clock()`` when it arrived).
    anchor: tuple[float, float] | None = None

    def _is_replay(path: Path | None) -> bool:
        return path is not None and path.name.startswith(_REPLAY_DIR_PREFIX)

    def _route(end: TripEnd | None, current: Path | None) -> None:
        if not end:
            return
        # The end names its own session; "current" is only the fallback.
        path = end.session if end.session is not None else current
        if _is_replay(path):
            return
        if end.observed:
            if on_trip_ended is None:
                return
            try:
                on_trip_ended(path, end)
            except Exception:  # noqa: BLE001 — digest/notify is best-effort
                log.exception("on_trip_ended failed")
        elif path is not None and on_trip_unobservable is not None:
            try:
                on_trip_unobservable(path, end)
            except Exception:  # noqa: BLE001 — the refusal record is best-effort
                log.exception("on_trip_unobservable failed")

    def _follow_session() -> None:
        nonlocal session_dir
        if active_session_path is None:
            return
        current = active_session_path()
        previous = session_dir
        session_dir = current
        if previous is None:
            if callable(enter_session):
                enter_session(current)
            return
        if current == previous:
            return
        if _is_replay(previous):
            # Leaving a replay: its trip state is another drive's; drop it.
            if not _is_replay(current):
                gps_tracker.resume()
        else:
            # A real session ended (the writer never returns to it — a replay
            # exits into a fresh live session too). Explain it if it was blind;
            # an open trip carries on and names its own session when it ends.
            _route(gps_tracker.close_session(), previous)
            if _is_replay(current):
                gps_tracker.suspend()
        if callable(enter_session):
            enter_session(current)

    try:
        while not stop_event.is_set() or not queue.empty():
            queue_monitor.observe(queue)
            _observe_queue(health_monitor, "gps", queue)
            try:
                record = await asyncio.wait_for(queue.get(), timeout=poll_interval)
            except TimeoutError:
                if callable(tick) and anchor is not None:
                    try:
                        _follow_session()
                        _route(tick(anchor[0] + (clock() - anchor[1])), session_dir)
                    except Exception as exc:  # noqa: BLE001 — trip state is best-effort
                        _note_error(health_monitor, "gps", exc)
                        log.exception("gps_tracker.tick failed")
                continue

            try:
                _follow_session()
                if not _is_replay(session_dir):
                    anchor = (record.timestamp.timestamp(), clock())
                trip_end = gps_tracker.observe(record)
            except Exception as exc:  # noqa: BLE001 — odometer is best-effort
                _note_error(health_monitor, "gps", exc)
                log.exception("gps_tracker.observe failed; skipping record")
            else:
                _route(trip_end, session_dir)
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

    def _observe(record: TelemetryRecord) -> None:
        session_dir = active_session_path() if active_session_path is not None else None
        dtc_state.observe(
            record,
            session_id=session_dir.name if session_dir is not None else None,
            session_dir=session_dir,
            emitter=emitter,
            vehicle_ledger=vehicle_ledger,
        )

    await _run_record_sink(
        "dtc",
        queue,
        _observe,
        stop_event,
        failure="dtc_state.observe",
        poll_interval=poll_interval,
        queue_warning_fraction=queue_warning_fraction,
        health_monitor=health_monitor,
    )


async def run_monitor_sink(
    queue: asyncio.Queue[TelemetryRecord],
    monitor_state: MonitorState,
    stop_event: asyncio.Event,
    *,
    emitter: AnomalyEventEmitter | None = None,
    active_session_path: Callable[[], Path] | None = None,
    poll_interval: float = 0.5,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Feed Mode 06 monitor-poll readings (Task 3.1) into the cached sweep.

    Mirrors ``run_dtc_sink`` — ``active_session_path`` is read fresh on every
    record so a session rotation mid-drive resets ``MonitorState``'s
    per-session misfire baseline instead of pinning it to whatever session
    was active at sink-startup.
    """

    def _observe(record: TelemetryRecord) -> None:
        session_dir = active_session_path() if active_session_path is not None else None
        monitor_state.observe(
            record,
            session_id=session_dir.name if session_dir is not None else None,
            emitter=emitter,
        )

    await _run_record_sink(
        "monitor",
        queue,
        _observe,
        stop_event,
        failure="monitor_state.observe",
        poll_interval=poll_interval,
        queue_warning_fraction=queue_warning_fraction,
        health_monitor=health_monitor,
    )


async def drain_queue_with_broadcast(
    queue: asyncio.Queue[TelemetryRecord],
    writer: ParquetTelemetryWriter | AsyncParquetTelemetryWriter,
    stop_event: asyncio.Event,
    *,
    telemetry_broadcaster: Broadcaster | None = None,
    live_source: LiveFeatureSource | None = None,
    live_source_ready: asyncio.Future[LiveFeatureSource | None] | None = None,
    gps_tracker: GpsTracker | None = None,
    dtc_state: DtcState | None = None,
    dtc_emitter: AnomalyEventEmitter | None = None,
    monitor_state: MonitorState | None = None,
    monitor_emitter: AnomalyEventEmitter | None = None,
    vehicle_ledger: VehicleLedgerStore | None = None,
    active_session_path: Callable[[], Path] | None = None,
    on_trip_ended: Callable[[Path], None] | None = None,
    on_trip_complete: Callable[[Path, TripEnd], None] | None = None,
    on_trip_unobservable: Callable[[Path, TripEnd], None] | None = None,
    poll_interval: float = 0.5,
    storage_path: Path | None = None,
    storage_warning_mb: int | None = None,
    queue_warning_fraction: float = 0.8,
    health_monitor: HealthMonitor | None = None,
    barrier: PipelineBarrier | None = None,
) -> None:
    """Run collector fanout plus independent storage/live/dashboard sinks.

    Kept under the historical name for callers. Parquet remains the durable
    recording sink, but it is no longer the dispatcher for live inference.

    Sink subscriptions (C07.6): broadcast and live receive every record, and
    storage every record but a ``decoded`` one (:func:`storable`, P02.8: never
    stored, whatever a config or capture profile says). The GPS (trip) sink
    only receives what ``GpsTracker.accepts``
    names — ``gps`` records, Mode 01 vehicle speed, and the declared
    road-speed frame decimated to 2 Hz (D03.29) — and the DTC and monitor sinks only ``obd``
    records (``DtcState.observe`` / ``MonitorState.observe`` return on any
    other source) — so raw CAN frames, the bulk of ingest on a live bus, are
    not enqueued to three queues that would discard them unread. Loss
    semantics are unchanged: the storage sink still drops newest and counts.

    Trip ends (D03.29): ``on_trip_complete(session_dir, trip_end)`` receives
    the session the trip ended in plus the :class:`TripEnd` (spanned sessions,
    start and stop times) and is what the daemon wires; ``on_trip_ended``
    (session only) is used when ``on_trip_complete`` is not given.

    ``live_source_ready`` stands in for ``live_source`` when the source is
    still being built: storage and the other sinks run at once, and the live
    sink joins the fanout when the future resolves. Records before that are
    not observed — the same as when the daemon had not started yet — and are
    not counted as live-sink drops, because no live queue exists to drop
    them from.
    """
    if live_source is not None and live_source_ready is not None:
        raise ValueError("pass live_source or live_source_ready, not both")
    sink_queue_size = _sink_queue_size(queue)
    sinks: list[TelemetrySink] = []
    sink_tasks: list[asyncio.Task[None]] = []
    if barrier is not None:
        barrier.attach(sinks)
    #: What every sink loop takes besides its queue, target and stop event.
    common: dict[str, Any] = {
        "poll_interval": poll_interval,
        "queue_warning_fraction": queue_warning_fraction,
        "health_monitor": health_monitor,
    }

    def add_sink(
        name: str,
        overflow: TelemetryOverflowPolicy,
        runner: Callable[[asyncio.Queue[TelemetryRecord]], Coroutine[Any, Any, None]],
        accepts: Callable[[TelemetryRecord], bool] | None = None,
    ) -> None:
        """One queue, its fanout subscription and its task (R03.8).

        The fanout reads ``sinks`` afresh for every batch, and the shutdown
        path below joins and cancels ``sink_tasks`` by reference, so a sink
        added while the drain is running is fed and torn down like any other.
        """
        sink_queue: asyncio.Queue[TelemetryRecord] = asyncio.Queue(maxsize=sink_queue_size)
        sinks.append(TelemetrySink(name, sink_queue, overflow, accepts=accepts))
        sink_tasks.append(asyncio.create_task(runner(sink_queue)))

    async_writer = ensure_async_writer(writer)
    add_sink(
        "storage",
        "drop_newest",
        lambda q: run_storage_sink(
            q,
            async_writer,
            stop_event,
            storage_path=storage_path,
            storage_warning_mb=storage_warning_mb,
            **common,
        ),
        accepts=storable,
    )

    if telemetry_broadcaster is not None:
        broadcaster = telemetry_broadcaster
        add_sink(
            "broadcast",
            "drop_oldest",
            lambda q: run_telemetry_broadcast_sink(
                q,
                broadcaster,
                stop_event,
                live_source=live_source,
                live_source_getter=(
                    (lambda: _resolved_live_source(live_source_ready))
                    if live_source_ready is not None
                    else None
                ),
                **common,
            ),
        )

    def _add_live_sink(source: LiveFeatureSource) -> None:
        add_sink(
            "live",
            "drop_oldest",
            lambda q: run_live_source_sink(q, source, stop_event, **common),
        )

    if live_source is not None:
        _add_live_sink(live_source)
    elif live_source_ready is not None:

        def _on_live_source_ready(ready: asyncio.Future[LiveFeatureSource | None]) -> None:
            source = _resolved_live_source(ready)
            if source is not None and not stop_event.is_set():
                _add_live_sink(source)

        live_source_ready.add_done_callback(_on_live_source_ready)

    if gps_tracker is not None:
        tracker = gps_tracker

        def _trip_ended(session_dir: Path | None, end: TripEnd) -> None:
            if session_dir is None:
                if active_session_path is None:
                    return
                session_dir = active_session_path()
            if on_trip_complete is not None:
                on_trip_complete(session_dir, end)
            elif on_trip_ended is not None:
                on_trip_ended(session_dir)

        add_sink(
            "gps",
            "drop_oldest",
            lambda q: run_gps_sink(
                q,
                tracker,
                stop_event,
                on_trip_ended=_trip_ended,
                active_session_path=active_session_path,
                on_trip_unobservable=on_trip_unobservable,
                **common,
            ),
            accepts=_trip_sink_filter(tracker),
        )

    if dtc_state is not None:
        dtc = dtc_state
        add_sink(
            "dtc",
            "drop_oldest",
            lambda q: run_dtc_sink(
                q,
                dtc,
                stop_event,
                emitter=dtc_emitter,
                vehicle_ledger=vehicle_ledger,
                active_session_path=active_session_path,
                **common,
            ),
            accepts=accepts_source("obd"),
        )

    if monitor_state is not None:
        monitors = monitor_state
        add_sink(
            "monitor",
            "drop_oldest",
            lambda q: run_monitor_sink(
                q,
                monitors,
                stop_event,
                emitter=monitor_emitter,
                active_session_path=active_session_path,
                **common,
            ),
            accepts=accepts_source("obd"),
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


def _did_to_dict(record: TelemetryRecord, decoded: DecodedDid) -> dict[str, Any]:
    """Live-stream row for a decoded Mode-22 DID: ``did:<NAME>`` in
    engineering units, carrying the raw poll's timestamp.

    Built by hand rather than via ``_record_to_dict`` because ``did`` is a
    pseudo-source that never rides a ``TelemetryRecord`` (see
    :data:`src.decode.dids.DID_SOURCE`) — the raw ``obd:DID_<hex>`` poll is
    what gets persisted, and preprocessing re-decodes it into the wide
    frame. This row exists only on the live stream, so the dashboard can
    show a gauge instead of a hex string.
    """
    return {
        "timestamp": record.timestamp.isoformat(),
        "source": DID_SOURCE,
        "channel": decoded.entry.name,
        "value": decoded.value,
        "unit": decoded.unit,
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


#: A stalled sink drops every record that arrives until it drains, and the
#: naive one-log-line-per-drop produced 3,570 identical journal lines inside a
#: single second on 2026-07-27 — amplifying a stall on a Pi that was already
#: failing to keep up, while saying nothing about how long it lasted. Drops are
#: coalesced into *bursts* instead: consecutive drops on one sink separated by
#: less than ``_DROP_BURST_GAP_SECONDS``.
_DROP_BURST_GAP_SECONDS = 1.0
#: Progress cadence inside a long burst, so a sink that never recovers still
#: reports rather than going silent after its opening line.
_DROP_BURST_REPORT_SECONDS = 10.0


@dataclass
class _DropBurst:
    """Running tally for one sink's current run of drops."""

    started_at: float
    last_at: float
    count: int = 1
    reported_at: float = field(default=0.0)


_drop_bursts: dict[str, _DropBurst] = {}


def _log_burst_total(name: str, burst: _DropBurst) -> None:
    log.warning(
        "telemetry sink drop burst ended: %d record(s) over %.2fs",
        burst.count,
        burst.last_at - burst.started_at,
        extra={
            "sink": name,
            "dropped": burst.count,
            "duration_seconds": round(burst.last_at - burst.started_at, 3),
        },
    )


def _note_drop(
    health_monitor: HealthMonitor | None,
    name: str,
    *,
    queue: asyncio.Queue[TelemetryRecord] | None = None,
) -> None:
    """Count the drop and log it burst-aware rather than once per record."""
    if health_monitor is not None:
        health_monitor.note_telemetry_drop(name)

    now = time.monotonic()
    burst = _drop_bursts.get(name)
    if burst is not None and now - burst.last_at <= _DROP_BURST_GAP_SECONDS:
        burst.count += 1
        burst.last_at = now
        if now - burst.reported_at >= _DROP_BURST_REPORT_SECONDS:
            burst.reported_at = now
            log.warning(
                "telemetry sink still dropping: %d record(s) over %.1fs",
                burst.count,
                now - burst.started_at,
                extra={"sink": name, "dropped": burst.count},
            )
        return

    # A new burst. Report the previous one's total first — a burst's size is
    # only known once it ends, and this avoids needing a background timer.
    if burst is not None:
        _log_burst_total(name, burst)
    _drop_bursts[name] = _DropBurst(started_at=now, last_at=now, reported_at=now)
    log.warning(
        "telemetry sink queue full; dropping record",
        extra={
            "sink": name,
            "queue_maxsize": queue.maxsize if queue is not None else None,
        },
    )


def reset_drop_bursts() -> None:
    """Clear burst state — for tests, which reuse sink names across cases."""
    _drop_bursts.clear()


def _note_error(health_monitor: HealthMonitor | None, name: str, exc: BaseException) -> None:
    if health_monitor is not None:
        health_monitor.note_telemetry_error(name, exc)


__all__ = [
    "PipelineBarrier",
    "TelemetrySink",
    "accepts_source",
    "drain_queue_with_broadcast",
    "run_dtc_sink",
    "run_gps_sink",
    "run_live_source_sink",
    "run_monitor_sink",
    "run_storage_sink",
    "run_telemetry_broadcast_sink",
    "run_telemetry_fanout",
    "storable",
    "_record_to_dict",
]
