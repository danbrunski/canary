"""Canable-native OBD streaming collector.

This is the replacement path for the dead ELM327 adapter. It owns one
``python-can`` Bus, records every received CAN frame, and interleaves read-only
OBD requests on that same bus. That matters for slcan/USB Canables: opening the
same serial adapter twice is not reliable, and two independent ``recv`` loops
would race each other for diagnostic response frames.

The public surfaces mirror the legacy ELM327 collector:

* daemon path: ``collect_can_obd_from_config`` emits ``TelemetryRecord`` rows
  for both raw CAN frames and decoded OBD readings;
* recorder path: ``record_can_obd`` writes ``frames.log`` plus ``pids.jsonl``.

Mode 01 PIDs are decoded through ``can_obd_client.PID_DECODERS``. Mode 22 DIDs
are emitted as ``DID_<hex>`` with the response payload hex string, matching the
legacy ``record.py --obd-dids`` sidecar shape.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial

import can

from src.can_ids import parse_standard_can_id
from src.collection.can_collector import (
    DEFAULT_RECONNECT_POLICY,
    RecordingResult,
    bus_factory,
    frame_to_record,
)
from src.collection.can_collector import _write_manifest as write_recording_manifest
from src.collection.can_obd_client import (
    PID_DECODERS,
)
from src.collection.isotp import (
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    FLOW_CONTROL_CTS,
    IsoTpResponseAssembler,
    ObdResponse,
    build_request_payload,
)
from src.collection.obd_collector import JsonlReadingWriter, ObdReading, reading_to_record
from src.collection.obd_scheduler import (
    AUTO_MISS_BACKOFF_SECONDS,
    AUTO_PIDS,
    DEFAULT_DID_REQUEST_ID,
    CanObdLoopStats,
    CanObdSpec,
    DidSessionSpec,
    DidWindowController,
    ObdSchedulerMetrics,
    RequestFilter,
    _bus_budget_requests_per_second,
    _count_active_requests,
    _initial_request_states,
    _next_due_at,
    _next_request_due_at,
    _PidDiscoveryClient,
    _PollRequest,
    _scheduler_metrics,
    _select_next_due_request,
    _update_response_state,
    build_poll_requests,
    parse_did_token,
    parse_pid_token,
    resolve_did_session_spec,
)
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import (
    BusReconnector,
    RebuildOutcome,
    ReconnectPolicy,
    wait_for_stop_or_timeout,
)
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

AUTO_PROBE_TIMEOUT_SECONDS = 0.25
AUTO_BASELINE_PID_NAMES = tuple(name for _pid, (name, _decoder) in sorted(PID_DECODERS.items()))
SCHEDULER_METRICS_INTERVAL_SECONDS = 5.0


def parse_can_id(value: str | int) -> int:
    return parse_standard_can_id(value)


def run_can_obd_loop(
    bus: can.BusABC,
    spec: CanObdSpec,
    *,
    on_frame: Callable[[can.Message], None],
    on_reading: Callable[[ObdReading], None],
    stop_event: threading.Event,
    deadline_monotonic: float | None = None,
    client_factory: Callable[[can.BusABC], _PidDiscoveryClient] | None = None,
    on_scheduler_metrics: Callable[[ObdSchedulerMetrics], None] | None = None,
    scheduler_metrics_interval_seconds: float = SCHEDULER_METRICS_INTERVAL_SECONDS,
) -> CanObdLoopStats:
    """Run a single-bus CAN + OBD loop until stopped.

    ``on_frame`` sees every received frame, including diagnostic responses.
    ``on_reading`` sees decoded Mode 01 PID readings and raw Mode 22 DID
    payloads. The function is synchronous by design so it can run either in the
    foreground recorder or inside ``asyncio.to_thread`` for the daemon.
    """
    from src.collection.can_obd_client import CanObdClient

    stats = CanObdLoopStats()
    client = (
        CanObdClient(bus, timeout_seconds=spec.request_timeout_seconds)
        if client_factory is None
        else client_factory(bus)
    )
    requests = build_poll_requests(client, spec)
    budget_rps = _bus_budget_requests_per_second(spec)
    did_indices = [index for index, request in enumerate(requests) if request.kind == "did"]
    controller: DidWindowController | None = None
    if spec.did_session is not None and did_indices:
        controller = DidWindowController(spec.did_session, did_indices, now=time.monotonic())
    if requests:
        log.info(
            "canable obd streaming started",
            extra={
                "pids": [r.channel for r in requests if r.kind == "pid"],
                "dids": [r.channel for r in requests if r.kind == "did"],
                "poll_tiers": {r.channel: r.poll_tier for r in requests},
                "bus_budget_requests_per_second": budget_rps,
                "auto_miss_backoff_seconds": AUTO_MISS_BACKOFF_SECONDS,
                "did_session_windows": (
                    controller.snapshot(time.monotonic()) if controller is not None else None
                ),
            },
        )
    else:
        log.warning("canable obd streaming has no PID/DID requests; recording CAN only")

    request_states = _initial_request_states(requests, spec, time.monotonic())
    next_budget_at = time.monotonic()
    next_metrics_at = time.monotonic()
    while not _should_stop(stop_event, deadline_monotonic):
        now = time.monotonic()
        if on_scheduler_metrics is not None and now >= next_metrics_at:
            on_scheduler_metrics(
                _scheduler_metrics(
                    requests,
                    request_states,
                    spec,
                    stats,
                    now,
                    did_profile=controller.snapshot(now) if controller is not None else None,
                )
            )
            stats.scheduler_snapshots += 1
            next_metrics_at = now + max(0.1, scheduler_metrics_interval_seconds)

        if not requests:
            _recv_and_tap(bus, on_frame, stats, timeout=0.2)
            continue

        eligible: RequestFilter | None = None
        if controller is not None:
            controller.advance(request_states, now)
            if controller.session_control_due(request_states, now) and now >= next_budget_at:
                ok = _send_session_control(
                    bus,
                    spec.did_session,  # type: ignore[arg-type]  # controller implies non-None
                    request_id=spec.did_request_id,
                    timeout_seconds=spec.request_timeout_seconds,
                    on_frame=on_frame,
                    stop_event=stop_event,
                    stats=stats,
                    deadline_monotonic=deadline_monotonic,
                )
                controller.on_session_response(ok=ok, states=request_states, now=time.monotonic())
                next_budget_at = max(next_budget_at + 1.0 / budget_rps, time.monotonic())
                continue
            eligible = partial(controller.request_eligible, now=now)

        active_request_count = _count_active_requests(
            requests, request_states, now, eligible=eligible
        )
        if active_request_count == 0:
            _drain_frames_until(
                bus,
                on_frame,
                stats,
                stop_event,
                now + 0.2,
                deadline_monotonic,
            )
            continue

        if now < next_budget_at:
            _drain_frames_until(
                bus,
                on_frame,
                stats,
                stop_event,
                next_budget_at,
                deadline_monotonic,
            )
            continue

        selection = _select_next_due_request(requests, request_states, now, eligible=eligible)
        if selection is None:
            next_due = _next_due_at(requests, request_states, now, eligible=eligible)
            if controller is not None:
                transition_at = controller.next_transition_at()
                next_due = transition_at if next_due is None else min(next_due, transition_at)
            if next_due is not None:
                wait_until = next_due if next_budget_at <= now else min(next_due, next_budget_at)
                _drain_frames_until(
                    bus,
                    on_frame,
                    stats,
                    stop_event,
                    wait_until,
                    deadline_monotonic,
                )
            continue
        request_index, request, request_state = selection

        timeout_seconds = spec.request_timeout_seconds
        if request.auto_probe and (request.fast_probe or request_state.missed_responses > 0):
            timeout_seconds = min(timeout_seconds, AUTO_PROBE_TIMEOUT_SECONDS)
        request_started = time.monotonic()
        request_state.last_requested_at = request_started
        request_state.requests_sent += 1
        try:
            response = _query_with_tap(
                bus,
                request,
                timeout_seconds=timeout_seconds,
                on_frame=on_frame,
                stop_event=stop_event,
                stats=stats,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception:  # noqa: BLE001 - propagate after recording scheduler state
            request_state.bus_errors += 1
            stats.bus_errors += 1
            raise
        emitted = False
        if response is not None:
            reading = _response_to_reading(request, response)
            if reading is not None:
                emitted = True
                stats.readings_emitted += 1
                on_reading(reading)
        if emitted:
            request_state.responses += 1
            request_state.last_response_at = time.monotonic()
        else:
            request_state.timeouts += 1
            stats.request_timeouts += 1
        _update_response_state(request_state, emitted=emitted, now=time.monotonic())
        if controller is not None:
            controller.on_request_attempted(request_index)

        request_state.next_due_at = _next_request_due_at(request, request_state, spec)
        next_budget_at = max(next_budget_at + 1.0 / budget_rps, time.monotonic())

    if on_scheduler_metrics is not None:
        on_scheduler_metrics(
            _scheduler_metrics(
                requests,
                request_states,
                spec,
                stats,
                time.monotonic(),
                did_profile=(
                    controller.snapshot(time.monotonic()) if controller is not None else None
                ),
            )
        )
        stats.scheduler_snapshots += 1
    return stats


async def collect_can_obd_from_config(
    spec: CanObdSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[CanBusConfig], can.BusABC] = bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
    on_scheduler_metrics: Callable[[ObdSchedulerMetrics], None] | None = None,
) -> None:
    """Daemon producer for the Canable-native path."""
    loop = asyncio.get_running_loop()
    drop_counter = QueueDropCounter("canable-obd")
    thread_stop = threading.Event()

    async def _mirror_stop() -> None:
        await stop_event.wait()
        thread_stop.set()

    mirror_task = asyncio.create_task(_mirror_stop())
    delay = reconnect_policy.initial_delay_seconds if reconnect_policy is not None else 0.0
    try:
        while not stop_event.is_set():
            bus: can.BusABC | None = None
            thread_stop.clear()
            try:
                bus = factory(spec.can)

                def on_frame(msg: can.Message) -> None:
                    record = frame_to_record(msg)
                    loop.call_soon_threadsafe(
                        partial(put_nowait_or_drop, queue, record, on_drop=drop_counter)
                    )

                def on_reading(reading: ObdReading) -> None:
                    record = reading_to_record(reading)
                    loop.call_soon_threadsafe(
                        partial(put_nowait_or_drop, queue, record, on_drop=drop_counter)
                    )

                metrics_callback = None
                if on_scheduler_metrics is not None:

                    def metrics_callback(metrics: ObdSchedulerMetrics) -> None:
                        loop.call_soon_threadsafe(on_scheduler_metrics, metrics)

                await asyncio.to_thread(
                    run_can_obd_loop,
                    bus,
                    spec,
                    on_frame=on_frame,
                    on_reading=on_reading,
                    stop_event=thread_stop,
                    on_scheduler_metrics=metrics_callback,
                )
                return
            except ValueError:
                raise
            except Exception as exc:
                if stop_event.is_set():
                    return
                if reconnect_policy is None:
                    raise
                log.warning(
                    "canable obd collector failed; reconnecting",
                    extra={
                        "interface": spec.can.interface,
                        "channel": spec.can.channel,
                        "error": f"{type(exc).__name__}: {exc}",
                        "reconnect_delay_seconds": delay,
                    },
                )
                if await wait_for_stop_or_timeout(stop_event, delay):
                    return
                delay = reconnect_policy.next_delay(delay)
            finally:
                thread_stop.set()
                if bus is not None:
                    bus.shutdown()
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

    Frames captured in a segment that ends in a bus exception are written to
    ``frames.log`` before the failure but not counted (``run_can_obd_loop``
    does not return its stats when it raises). Data integrity is preserved;
    only the manifest's ``frames_recorded`` tally is a small undercount across
    a reconnect.
    """
    started_at = datetime.now(UTC)
    deadline = time.monotonic() + duration if duration is not None else None
    local_stop = stop_event or threading.Event()
    error: BaseException | None = None
    stop_reason = "signal"

    frames_seen = 0
    parse_errors = 0

    reconnect_enabled = factory is not None and reconnect_policy is not None
    current_bus: can.BusABC | None = bus
    # Owns the bounded-backoff rebuild accounting (delay, failed_rebuilds,
    # reconnects); the wedged-adapter cap lives in BusReconnector.
    reconnector = (
        BusReconnector(reconnect_policy, max_reconnects, log)
        if reconnect_enabled and reconnect_policy is not None
        else None
    )

    log.info(
        "canable obd recording started",
        extra={
            "frames_path": str(paths.frames),
            "interface": spec.can.interface,
            "channel": spec.can.channel,
            "duration_seconds": duration,
            "auto_reconnect": reconnect_enabled,
        },
    )

    try:
        with can.io.CanutilsLogWriter(str(paths.frames)) as writer:

            def on_frame(msg: can.Message) -> None:
                writer.on_message_received(msg)

            while True:
                if local_stop.is_set():
                    stop_reason = "signal"
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stop_reason = "deadline"
                    break

                # Lost the bus on a previous iteration: rebuild before recv.
                if current_bus is None:
                    assert reconnector is not None and factory is not None
                    outcome, current_bus = reconnector.rebuild(
                        factory,
                        spec.can,
                        stop_event=local_stop,
                        deadline_monotonic=deadline,
                        frames_recorded=frames_seen,
                    )
                    if outcome is RebuildOutcome.STOP_SIGNAL:
                        stop_reason = "signal"
                        break
                    if outcome is RebuildOutcome.STOP_DEADLINE:
                        stop_reason = "deadline"
                        break
                    if outcome is RebuildOutcome.EXHAUSTED:
                        stop_reason = "reconnect_exhausted"
                        error = reconnector.last_error
                        break
                    if outcome is RebuildOutcome.RETRY:
                        continue
                    # Only REBUILT falls through — it always returns a bus.
                    assert current_bus is not None

                try:
                    segment = run_can_obd_loop(
                        current_bus,
                        spec,
                        on_frame=on_frame,
                        on_reading=obd_writer.write,
                        stop_event=local_stop,
                        deadline_monotonic=deadline,
                    )
                except ValueError:
                    # Config error (bad PID/DID token) — not a transient bus
                    # fault. Mirrors collect_can_obd_from_config.
                    raise
                except Exception as exc:
                    if not reconnect_enabled:
                        raise
                    assert reconnector is not None
                    log.warning(
                        "canable obd loop failed; will attempt reconnect",
                        extra={
                            "error": f"{type(exc).__name__}: {exc}",
                            "delay_seconds": reconnector.delay,
                        },
                    )
                    try:
                        current_bus.shutdown()
                    except Exception:
                        # Bus may already be dead; shutdown is best-effort.
                        pass
                    current_bus = None
                    continue

                # Loop returned normally → stop_event or deadline ended it.
                frames_seen += segment.frames_seen
                parse_errors += segment.parse_errors
                if local_stop.is_set():
                    stop_reason = "signal"
                elif deadline is not None and time.monotonic() >= deadline:
                    stop_reason = "deadline"
                break
    except BaseException as exc:
        error = exc
        stop_reason = "error"
        raise
    finally:
        ended_at = datetime.now(UTC)
        # The bus may have been rebuilt; close whatever we currently hold so
        # we don't leak a USB handle. The original bus (if unchanged) is the
        # caller's to close.
        if reconnect_enabled and current_bus is not None and current_bus is not bus:
            try:
                current_bus.shutdown()
            except Exception:
                pass
        reconnects_total = reconnector.reconnects if reconnector is not None else 0
        result = RecordingResult(
            started_at=started_at,
            ended_at=ended_at,
            frames_recorded=frames_seen,
            stopped_by=stop_reason,
            parse_errors=parse_errors,
            reconnects=reconnects_total,
        )
        try:
            write_recording_manifest(
                paths.manifest,
                spec=spec.can,
                vehicle=vehicle,
                result=result,
                vehicle_state=vehicle_state,
                error=error,
            )
        except Exception:
            if error is None:
                raise
            log.exception("failed to write manifest after canable OBD recording error")
    return result


def _response_to_reading(
    request: _PollRequest,
    response: ObdResponse,
) -> ObdReading | None:
    timestamp = time.time()
    if request.kind == "did":
        if not response.payload:
            return None
        return ObdReading(timestamp, request.channel, response.payload.hex(), "")

    # Reuse the public PID decoder behavior without issuing another request:
    # synthesize the same DecodedPid logic locally from PID_DECODERS.
    entry = PID_DECODERS.get(request.identifier)
    if entry is None:
        return None
    name, decoder = entry
    decoded = decoder(response.payload)
    if decoded is None:
        return None
    value, unit = decoded
    return ObdReading(timestamp, name, value, unit)


def _send_session_control(
    bus: can.BusABC,
    session_spec: DidSessionSpec,
    *,
    request_id: int,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> bool:
    """Send ``10 <session>`` (UDS DiagnosticSessionControl) and await ``50 <session>``.

    Opens or keeps alive the Mode-22 gating session. Session *control* only —
    no memory write, no actuation; the read-only posture of the collector is
    unchanged. Returns True on a positive response.
    """
    request = _PollRequest(
        kind="session",
        service=0x10,
        identifier=session_spec.session,
        request_id=request_id,
        channel="UDS_SESSION_CONTROL",
    )
    stats.session_controls_sent += 1
    response = _query_with_tap(
        bus,
        request,
        timeout_seconds=timeout_seconds,
        on_frame=on_frame,
        stop_event=stop_event,
        stats=stats,
        deadline_monotonic=deadline_monotonic,
    )
    return response is not None


def _query_with_tap(
    bus: can.BusABC,
    request: _PollRequest,
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> ObdResponse | None:
    message = can.Message(
        arbitration_id=request.request_id,
        data=build_request_payload(request.service, request.identifier),
        is_extended_id=False,
    )
    bus.send(message)
    stats.requests_sent += 1

    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)
    assembler = IsoTpResponseAssembler(service=request.service, identifier=request.identifier)

    while not _should_stop(stop_event, request_deadline):
        remaining = max(0.001, request_deadline - time.monotonic())
        msg = _recv_and_tap(bus, on_frame, stats, timeout=min(0.05, remaining))
        if msg is None:
            continue
        if not (DEFAULT_RESPONSE_ID_MIN <= msg.arbitration_id <= DEFAULT_RESPONSE_ID_MAX):
            continue
        data = bytes(msg.data)
        if not data:
            continue
        responder = int(msg.arbitration_id)
        result = assembler.accept(responder, data)
        if result.negative_response:
            return None
        if result.flow_control_id is not None:
            bus.send(
                can.Message(
                    arbitration_id=result.flow_control_id,
                    data=FLOW_CONTROL_CTS,
                    is_extended_id=False,
                )
            )
            continue
        if result.response is not None:
            return result.response
    return None


def _recv_and_tap(
    bus: can.BusABC,
    on_frame: Callable[[can.Message], None],
    stats: CanObdLoopStats,
    *,
    timeout: float,
) -> can.Message | None:
    try:
        msg = bus.recv(timeout=timeout)
    except (ValueError, IndexError) as exc:
        stats.parse_errors += 1
        if stats.parse_errors == 1 or stats.parse_errors % 100 == 0:
            log.warning(
                "dropped malformed CAN frame",
                extra={
                    "error": f"{type(exc).__name__}: {exc}",
                    "parse_errors_total": stats.parse_errors,
                },
            )
        return None
    if msg is None:
        return None
    stats.frames_seen += 1
    on_frame(msg)
    return msg


def _drain_frames_until(
    bus: can.BusABC,
    on_frame: Callable[[can.Message], None],
    stats: CanObdLoopStats,
    stop_event: threading.Event,
    until: float,
    deadline_monotonic: float | None,
) -> None:
    target = until if deadline_monotonic is None else min(until, deadline_monotonic)
    while not _should_stop(stop_event, target):
        remaining = max(0.001, target - time.monotonic())
        _recv_and_tap(bus, on_frame, stats, timeout=min(0.05, remaining))


def _should_stop(stop_event: threading.Event, deadline_monotonic: float | None) -> bool:
    if stop_event.is_set():
        return True
    return deadline_monotonic is not None and time.monotonic() >= deadline_monotonic


__all__ = [
    "AUTO_PIDS",
    "AUTO_BASELINE_PID_NAMES",
    "DEFAULT_DID_REQUEST_ID",
    "CanObdLoopStats",
    "CanObdSpec",
    "DidSessionSpec",
    "ObdSchedulerMetrics",
    "build_poll_requests",
    "collect_can_obd_from_config",
    "parse_can_id",
    "parse_did_token",
    "parse_pid_token",
    "record_can_obd",
    "resolve_did_session_spec",
    "run_can_obd_loop",
]
