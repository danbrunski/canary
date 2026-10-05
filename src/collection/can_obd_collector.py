"""CAN-native (python-can) OBD streaming collector: the daemon and recorder entry points.

One ``python-can`` Bus records every received CAN frame and interleaves
active OBD requests on it. That matters for slcan/USB adapters: opening the
same serial adapter twice is not reliable, and two independent ``recv`` loops
would race each other for diagnostic response frames. It is the path for any
adapter python-can drives — the in-vehicle rig is SocketCAN on the HAT, a
Canable over slcan works the same — and it sits beside, not in place of, the
ELM327 collector (``obd_collector``) and the K-line collector.

The public surfaces mirror the ELM327 collector:

* daemon path: ``collect_can_obd_from_config`` emits ``TelemetryRecord`` rows
  for both raw CAN frames and decoded OBD readings;
* recorder path: ``record_can_obd`` writes ``frames.log`` plus ``pids.jsonl``.

The loop itself is ``can_obd_loop``, its wire I/O ``can_obd_io`` and the
response decoder ``obd_response_decode`` (R02.9). Mode 01 PIDs are decoded
through ``pid_decoders.PID_DECODERS``; Mode 22 DIDs are emitted as
``DID_<hex>`` with the response payload hex string, matching the
``record.py --obd-dids`` sidecar shape.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

import can

from src.collection import private_traffic
from src.collection.can_bus import bus_factory, frame_to_record
from src.collection.can_obd_client import (
    PID_DECODERS,
)

# The loop, its wire I/O and the response decoder live in their own modules
# (R02.9); the names below stay importable from here. Monkeypatch the loop's
# ``next_wake_at`` / ``_drain_frames_until`` on ``can_obd_loop``, not here.
from src.collection.can_obd_io import (
    BatchedQueryResult,
    QueryResult,
    _batched_pid_width,
    _drain_frames_until,
    _frame_arrival,
    _query_batched_with_tap,
    _query_with_tap,
    _read_calibration_id,
    _recv_and_tap,
    _send_session_control,
)
from src.collection.can_obd_loop import (
    MAX_CONSECUTIVE_BATCH_FAILURES,
    BatchGovernor,
    BatchOutcome,
    _cycle_plan_for,
    _issue_batched_request,
    _loop_request_verdict,
    _next_batch_size,
    run_can_obd_loop,
)
from src.collection.frame_time import KERNEL_STAMP_MAX_SKEW_SECONDS, FrameArrival
from src.collection.isotp import (
    MAX_RESPONSE_PENDING_EXTENSIONS,
    RESPONSE_PENDING_EXTENSION_SECONDS,
)
from src.collection.obd_reading import (
    JsonlReadingWriter,
    ObdReading,
    reading_to_record,
)
from src.collection.obd_response_decode import (
    _decode_response,
    _response_to_reading,
)
from src.collection.obd_scheduler import (
    AUTO_PIDS,
    DEFAULT_DID_REQUEST_ID,
    CanObdLoopStats,
    CanObdSpec,
    DidSessionSpec,
    ObdSchedulerMetrics,
    PollRequest,
    PollRequestState,
    _initial_request_states,
    _PollRequest,
    build_poll_requests,
    parse_did_token,
    parse_pid_token,
    resolve_did_session_spec,
)
from src.collection.queueing import (
    CollectorHandoffStats,
    HandoffStats,
    QueueDropCounter,
    threadsafe_enqueue,
)
from src.collection.reconnect import (
    DEFAULT_RECONNECT_POLICY,
    ReconnectPolicy,
    run_with_reconnect,
    wait_for_stop_or_timeout,
)
from src.collection.recording import DiskWriteError, RecorderRun, RecordingResult
from src.collection.vehicle_power import VEHICLE_POWER, VehiclePower
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)


AUTO_BASELINE_PID_NAMES = tuple(name for _pid, (name, _decoder) in sorted(PID_DECODERS.items()))


#: How often a collector waiting on the bench looks at the supply again.
BENCH_RECHECK_SECONDS = 2.0


#: How long a collector waits for a configured 12 V sense's first reading.
FIRST_READING_TIMEOUT_SECONDS = 5.0


async def wait_off_the_bench(stop_event: asyncio.Event, power: VehiclePower) -> None:
    """Return once the port is not ``bench`` (or ``stop_event`` is set).

    With a 12 V sense configured but no reading yet, waits up to
    :data:`FIRST_READING_TIMEOUT_SECONDS` for one first."""
    if power.expected and power.latest() is None:
        deadline = asyncio.get_running_loop().time() + FIRST_READING_TIMEOUT_SECONDS
        while (
            power.latest() is None
            and not stop_event.is_set()
            and asyncio.get_running_loop().time() < deadline
        ):
            await wait_for_stop_or_timeout(stop_event, 0.1)
    if power.connection() != "bench":
        return
    log.info(
        "no vehicle on the OBD port (12 V sense reads %.2f V): OBD polling waits",
        power.latest() or 0.0,
    )
    while power.connection() == "bench" and not stop_event.is_set():
        await wait_for_stop_or_timeout(stop_event, BENCH_RECHECK_SECONDS)
    if not stop_event.is_set():
        log.info("vehicle power on the OBD port: OBD polling starts")


async def collect_can_obd_from_config(
    spec: CanObdSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[CanBusConfig], can.BusABC] = bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    on_scheduler_metrics: Callable[[ObdSchedulerMetrics], None] | None = None,
    on_handoff_stats: Callable[[CollectorHandoffStats], None] | None = None,
    power: VehiclePower = VEHICLE_POWER,
) -> None:
    """Daemon producer for the Canable-native path.

    On the bench (the HAT's 12 V sense reads no battery: USB-C or UPS power,
    no vehicle) it does not open the bus: nothing would answer, and every
    request only filled the transmit queue until ``ENOBUFS`` and a reconnect
    warning, once a minute (2026-10-05). It waits, says so once, and starts
    when a battery appears. Without a 12 V reading it runs as it always did.

    ``on_handoff_stats`` (C03.6) receives the collector's cumulative
    thread-handoff and ingest-queue drop counters each time scheduler metrics
    are published, so loss between the poll thread and the ingest queue is
    attributable in ``/status`` (``telemetry_sinks.obd_collect``) instead of
    only in a log line.
    """
    loop = asyncio.get_running_loop()
    drop_counter = QueueDropCounter("canable-obd")
    thread_stop = threading.Event()
    #: Handoff counters from attempts that already ended; the live attempt's
    #: are added on each report so the total is cumulative across reconnects.
    finished_handoffs = HandoffStats(0, 0, 0, 0, 0, 0)
    attempts = 0

    async def _mirror_stop() -> None:
        await stop_event.wait()
        thread_stop.set()

    mirror_task = asyncio.create_task(_mirror_stop())

    def _report_handoff(enqueue_stats: HandoffStats, metrics: ObdSchedulerMetrics) -> None:
        if on_handoff_stats is None:
            return
        raw = sum(int(row.get("responses") or 0) for row in metrics.by_tier.values())
        on_handoff_stats(
            CollectorHandoffStats(
                handoff=finished_handoffs + enqueue_stats,
                queue_dropped=drop_counter.dropped,
                derived_records=metrics.readings_emitted,
                raw_responses=raw,
                reconnects=max(0, attempts - 1),
            )
        )

    async def _attempt(capture: Callable[[can.BusABC], None]) -> None:
        nonlocal finished_handoffs, attempts
        await wait_off_the_bench(stop_event, power)
        if stop_event.is_set():
            return
        thread_stop.clear()
        attempts += 1
        # One bounded, coalescing handoff per attempt (C07.4); closed once the
        # worker has returned so a late callback can never touch the queue.
        enqueue = threadsafe_enqueue(loop, queue, on_drop=drop_counter, name="canable-obd")
        try:
            bus = factory(spec.can)
            capture(bus)

            def on_frame(msg: can.Message) -> None:
                if private_traffic.exclude(msg):
                    # W08.7: acquired while another exchange in this process
                    # held its id (the ECU backup's flash read); not telemetry.
                    return
                enqueue(frame_to_record(msg))

            def on_reading(reading: ObdReading) -> None:
                enqueue(reading_to_record(reading))

            metrics_callback = None
            if on_scheduler_metrics is not None or on_handoff_stats is not None:

                def _publish(metrics: ObdSchedulerMetrics) -> None:
                    if on_scheduler_metrics is not None:
                        on_scheduler_metrics(metrics)
                    _report_handoff(enqueue.stats(), metrics)

                def metrics_callback(metrics: ObdSchedulerMetrics) -> None:
                    loop.call_soon_threadsafe(_publish, metrics)

            await asyncio.to_thread(
                run_can_obd_loop,
                bus,
                spec,
                on_frame=on_frame,
                on_reading=on_reading,
                stop_event=thread_stop,
                on_scheduler_metrics=metrics_callback,
            )
        finally:
            thread_stop.set()
            enqueue.close()
            finished_handoffs = finished_handoffs + enqueue.stats()

    try:
        await run_with_reconnect(
            _attempt,
            stop_event=stop_event,
            policy=reconnect_policy,
            teardown=lambda bus: bus.shutdown(),
            log=log,
            reconnect_message="canable obd collector failed; reconnecting",
            log_extra=lambda: {"interface": spec.can.interface, "channel": spec.can.channel},
        )
    finally:
        mirror_task.cancel()
        try:
            await mirror_task
        except asyncio.CancelledError:
            pass


def record_can_obd(
    bus: can.BusABC,
    paths: SessionPaths,
    *,
    spec: CanObdSpec,
    vehicle: VehicleConfig | None,
    duration: float | None,
    obd_writer: JsonlReadingWriter,
    vehicle_state: str | None = None,
    stop_event: threading.Event | None = None,
    factory: Callable[[CanBusConfig], can.BusABC] | None = None,
    reconnect_policy: ReconnectPolicy | None = None,
    max_reconnects: int | None = 10,
) -> RecordingResult:
    """Record ``frames.log`` and ``pids.jsonl`` from one Canable.

    When ``factory`` AND ``reconnect_policy`` are provided, a bus-level
    failure mid-capture (USB disconnect / re-enumeration, adapter reset,
    ``OSError`` / ``can.CanError`` out of ``bus.recv``/``bus.send``) triggers
    an auto-reconnect instead of ending the drive: the dead bus is shut down,
    we wait per the policy, and ``factory(spec.can)`` rebuilds it. The SAME
    ``CanutilsLogWriter`` stays open across reconnects so ``frames.log``
    remains one continuous file. Without both params, bus-level exceptions
    propagate as before (backward-compatible) — this is the behavior the
    daemon path ``collect_can_obd_from_config`` has always had, now also on
    the recorder path used by ``scripts/record.py --obd-source canable`` for
    the drive backlog.

    ``max_reconnects`` caps CONSECUTIVE failed rebuilds (default 10); a
    successful rebuild resets the counter. Once exhausted the recording stops
    with ``stopped_by = "reconnect_exhausted"``, mirroring ``record()``. This
    matters because a wedged USB adapter can leave the rebuild call in
    uninterruptible kernel I/O (the WSL2 failure mode that motivated the cap
    on the CAN-only path); the in-vehicle analogue is a brown-out on the
    Canable's USB power during a drive.

    ``frames_recorded`` counts frames written to ``frames.log``, across
    reconnects. A full disk ends the run as ``disk_error`` on the first
    failed write (R02.6): the writer runs inside the loop's frame tap, and its
    ``OSError`` used to be taken for a bus fault and "fixed" by rebuilding the
    bus until the deadline. The shared skeleton is ``recording.RecorderRun``.
    """
    run = RecorderRun(
        bus,
        paths,
        spec=spec.can,
        vehicle=vehicle,
        duration=duration,
        vehicle_state=vehicle_state,
        stop_event=stop_event,
        factory=factory,
        reconnect_policy=reconnect_policy,
        max_reconnects=max_reconnects,
        log=log,
    )
    log.info(
        "canable obd recording started",
        extra={
            "frames_path": str(paths.frames),
            "interface": spec.can.interface,
            "channel": spec.can.channel,
            "duration_seconds": duration,
            "auto_reconnect": run.reconnect_enabled,
        },
    )

    with run, can.io.CanutilsLogWriter(str(paths.frames)) as writer:
        on_frame = run.frame_writer(writer)
        on_reading = run.reading_writer(obd_writer.write)
        while not run.should_stop():
            current_bus = run.ensure_bus()
            if current_bus is None:
                if run.finished:
                    break
                continue
            try:
                segment = run_can_obd_loop(
                    current_bus,
                    spec,
                    on_frame=on_frame,
                    on_reading=on_reading,
                    stop_event=run.stop_event,
                    deadline_monotonic=run.deadline,
                )
            except DiskWriteError as exc:
                run.on_disk_error(exc)
                break
            except ValueError:
                # Config error (bad PID/DID token) — not a transient bus
                # fault. Mirrors collect_can_obd_from_config.
                raise
            except Exception as exc:
                run.on_bus_failure(exc, "canable obd loop failed; will attempt reconnect")
                continue
            # Loop returned normally → stop_event or deadline ended it.
            run.parse_errors += segment.parse_errors
            run.settle_stop_reason()
            break
    assert run.result is not None
    return run.result


__all__ = [
    "AUTO_BASELINE_PID_NAMES",
    "AUTO_PIDS",
    "DEFAULT_DID_REQUEST_ID",
    "KERNEL_STAMP_MAX_SKEW_SECONDS",
    "MAX_CONSECUTIVE_BATCH_FAILURES",
    "MAX_RESPONSE_PENDING_EXTENSIONS",
    "RESPONSE_PENDING_EXTENSION_SECONDS",
    "BatchGovernor",
    "BatchOutcome",
    "BatchedQueryResult",
    "CanObdLoopStats",
    "CanObdSpec",
    "DidSessionSpec",
    "FrameArrival",
    "ObdSchedulerMetrics",
    "QueryResult",
    "RecordingResult",
    "build_poll_requests",
    "collect_can_obd_from_config",
    "parse_did_token",
    "parse_pid_token",
    "record_can_obd",
    "resolve_did_session_spec",
    "run_can_obd_loop",
    "PollRequest",
    "PollRequestState",
    "_PollRequest",
    "_batched_pid_width",
    "_cycle_plan_for",
    "_decode_response",
    "_drain_frames_until",
    "_frame_arrival",
    "_initial_request_states",
    "_issue_batched_request",
    "_loop_request_verdict",
    "_next_batch_size",
    "_query_batched_with_tap",
    "_query_with_tap",
    "_read_calibration_id",
    "_recv_and_tap",
    "_response_to_reading",
    "_send_session_control",
]
