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
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

import can

from src.can_ids import parse_standard_can_id
from src.collection import tuning_profile, tx_gate
from src.collection.can_collector import (
    DEFAULT_RECONNECT_POLICY,
    RecordingResult,
    bus_factory,
    frame_to_record,
)
from src.collection.can_collector import _write_manifest as write_recording_manifest
from src.collection.can_obd_client import (
    PID_DECODERS,
    decode_mil_on,
    parse_dtc_payload,
)
from src.collection.custom_pids import evaluate_custom_pid
from src.collection.isotp import (
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    FLOW_CONTROL_CTS,
    IsoTpResponseAssembler,
    ObdResponse,
    build_batched_request_payload,
    build_request_payload,
    split_batched_response,
)
from src.collection.obd_collector import JsonlReadingWriter, ObdReading, reading_to_record
from src.collection.obd_registry import (
    lookup,
    misfire_count_channel,
    misfire_counts_from_results,
    parse_mode06_response,
)
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
    _PollRequestState,
    _scheduler_metrics,
    _select_next_due_request,
    _update_response_state,
    build_poll_requests,
    parse_did_token,
    parse_pid_token,
    resolve_did_session_spec,
    select_batch_companions,
)
from src.collection.queueing import QueueDropCounter, log_dropped_can_frame, threadsafe_enqueue
from src.collection.reconnect import (
    BusReconnector,
    RebuildOutcome,
    ReconnectPolicy,
    run_with_reconnect,
)
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

AUTO_PROBE_TIMEOUT_SECONDS = 0.25
AUTO_BASELINE_PID_NAMES = tuple(name for _pid, (name, _decoder) in sorted(PID_DECODERS.items()))
SCHEDULER_METRICS_INTERVAL_SECONDS = 5.0
#: Consecutive whole-batch failures before the session narrows its batch size.
#: Small on purpose: an ECU that dislikes a given size should cost a handful
#: of request slots to discover, not a whole drive.
MAX_CONSECUTIVE_BATCH_FAILURES = 3


def _next_batch_size(current: int) -> int:
    """Step a failing batch size down: 6 -> 4 -> 2 -> 1 (off).

    Halving rather than disabling outright, because "supports batching" is not
    binary. An ECU that answers four PIDs but refuses six would otherwise lose
    the feature entirely, when it was worth 4x. Reaching 1 disables batching
    for the session.
    """
    if current <= 2:
        return 1
    if current > 4:
        return 4
    return 2


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
    freeze_frame_indices = [
        index for index, request in enumerate(requests) if request.kind == "freeze_frame"
    ]
    controller: DidWindowController | None = None
    if spec.did_session is not None and did_indices:
        controller = DidWindowController(spec.did_session, did_indices, now=time.monotonic())
    if requests:
        log.info(
            "canable obd streaming started",
            extra={
                "pids": [r.channel for r in requests if r.kind == "pid"],
                "dids": [r.channel for r in requests if r.kind == "did"],
                "dtc_channels": [r.channel for r in requests if r.kind == "dtc"],
                "service_channels": [r.channel for r in requests if r.kind == "service"],
                "freeze_frame_channels": [r.channel for r in requests if r.kind == "freeze_frame"],
                "custom_pid_channels": [r.channel for r in requests if r.kind == "custom"],
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
    # Batching is opt-in and self-disabling. An ECU that ignores or refuses
    # multi-PID requests would otherwise fail every batch forever, so a run of
    # consecutive failures falls the session back to single-PID rather than
    # stalling. This doubles as the adaptive form of the startup size probe:
    # we discover what the vehicle actually accepts by using it.
    batching_enabled = spec.batch_max_pids > 1
    active_batch_max = spec.batch_max_pids
    consecutive_batch_failures = 0
    stats.loop_started_at = time.monotonic()
    while not _should_stop(stop_event, deadline_monotonic):
        now = time.monotonic()
        if (
            freeze_frame_indices
            and spec.freeze_frame_trigger is not None
            and spec.freeze_frame_trigger.take()
        ):
            # Task 3.3: a stored DTC just appeared — un-suppress every
            # freeze-frame request for exactly this one shot. They go back
            # to "on_demand" dormancy on their own once each has fired once
            # (`_request_rate_hz` returns 0 for them, see `build_poll_requests`).
            for index in freeze_frame_indices:
                request_states[index].suppressed_until = 0.0
                request_states[index].next_due_at = now
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

        if tx_gate.is_passive():
            # Pure-monitoring mode. Keep receiving and tapping frames so
            # broadcast CAN logging, the anomaly pipeline and the status
            # snapshot all stay live — we simply issue nothing.
            #
            # The pause belongs HERE rather than at the send: blocking only
            # the transmit would leave every request waiting out its full
            # timeout, and `_update_response_state` would escalate each one
            # into the miss-backoff. Passive mode would then hand back a
            # scheduler with every PID suppressed once it lifted.
            _recv_and_tap(bus, on_frame, stats, timeout=0.2)
            continue

        if not requests:
            _recv_and_tap(bus, on_frame, stats, timeout=0.2)
            continue

        eligible: RequestFilter | None = None
        if tuning_profile.is_active():
            # Phase 1.1 tuning capture: concentrate the whole bus budget on a
            # small channel set, and skip the DID controller entirely so the
            # `10 C0` session never blacks out Mode 01. Those blackouts stop
            # Mode 01 for up to 10 s in every 60, which is exactly the gap
            # that makes a steady-state hold unusable for tuning.
            eligible = tuning_profile.request_eligible
        elif controller is not None:
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

        batch_indices = select_batch_companions(
            requests,
            request_states,
            now,
            leader_index=request_index,
            max_pids=active_batch_max if batching_enabled else 1,
            eligible=eligible,
        )
        if len(batch_indices) > 1:
            answered = _issue_batched_request(
                bus,
                requests,
                request_states,
                batch_indices,
                spec=spec,
                on_frame=on_frame,
                on_reading=on_reading,
                stop_event=stop_event,
                stats=stats,
                deadline_monotonic=deadline_monotonic,
            )
            if answered:
                consecutive_batch_failures = 0
            else:
                consecutive_batch_failures += 1
                if consecutive_batch_failures >= MAX_CONSECUTIVE_BATCH_FAILURES:
                    narrowed = _next_batch_size(active_batch_max)
                    consecutive_batch_failures = 0
                    if narrowed <= 1:
                        batching_enabled = False
                        log.warning(
                            "multi-PID batching disabled: no batch size this ECU "
                            "accepts; falling back to single-PID for this session",
                            extra={"configured_batch_max_pids": spec.batch_max_pids},
                        )
                    else:
                        log.warning(
                            "narrowing multi-PID batch size %d -> %d after %d consecutive failures",
                            active_batch_max,
                            narrowed,
                            MAX_CONSECUTIVE_BATCH_FAILURES,
                        )
                        active_batch_max = narrowed
                    stats.batch_size_narrowings += 1
            if controller is not None:
                for index in batch_indices:
                    controller.on_request_attempted(index)
            for index in batch_indices:
                request_states[index].next_due_at = _next_request_due_at(
                    requests[index], request_states[index], spec
                )
            next_budget_at = max(next_budget_at + 1.0 / budget_rps, time.monotonic())
            continue

        timeout_seconds = spec.request_timeout_seconds
        if request.auto_probe and (request.fast_probe or request_state.missed_responses > 0):
            timeout_seconds = min(timeout_seconds, AUTO_PROBE_TIMEOUT_SECONDS)
        request_started = time.monotonic()
        request_state.last_requested_at = request_started
        request_state.requests_sent += 1
        try:
            result = _query_with_tap(
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
        if result.response is not None:
            readings = _response_to_reading(request, result.response)
            if readings:
                emitted = True
                stats.readings_emitted += len(readings)
                for reading in readings:
                    on_reading(reading)
        if emitted:
            request_state.responses += 1
            request_state.last_response_at = time.monotonic()
        elif result.negative_response_code is not None:
            # Formal ECU refusal, not a true timeout — counted separately
            # (see `QueryResult`) so it doesn't masquerade as bus silence.
            request_state.negative_responses += 1
            request_state.last_negative_response_code = result.negative_response_code
            stats.negative_responses += 1
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

    async def _attempt(capture: Callable[[can.BusABC], None]) -> None:
        thread_stop.clear()
        try:
            bus = factory(spec.can)
            capture(bus)

            enqueue = threadsafe_enqueue(loop, queue, on_drop=drop_counter)

            def on_frame(msg: can.Message) -> None:
                enqueue(frame_to_record(msg))

            def on_reading(reading: ObdReading) -> None:
                enqueue(reading_to_record(reading))

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
        finally:
            thread_stop.set()

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


#: Mode 01 PID whose response carries both the continuous MONITOR_STATUS
#: count channel and (Task 1.4) the MIL bit `decode_monitor_status` drops.
_MONITOR_STATUS_PID = 0x01
_MIL_STATUS_CHANNEL = "MIL_STATUS"
#: Service whose "service" kind response carries per-cylinder misfire data
#: (Task 3.1/3.2) in addition to the raw sweep — see the `service` branch
#: below.
_MODE06_SERVICE = 0x06


def _response_to_reading(
    request: _PollRequest,
    response: ObdResponse,
) -> list[ObdReading]:
    """Turn one raw response into zero or more decoded readings.

    Usually one reading per request; PID 0x01 emits a second (MIL_STATUS,
    Task 1.4) derived from the same payload instead of issuing another bus
    request just for the bit ``decode_monitor_status`` doesn't surface.
    """
    timestamp = time.time()
    if request.kind == "did":
        if not response.payload:
            return []
        return [ObdReading(timestamp, request.channel, response.payload.hex(), "")]

    if request.kind == "dtc":
        # Mode 03/07/0A (Task 1.3) — always emit, even with zero codes, so
        # downstream DTC state can tell "polled, none found" from "not
        # polled yet". Comma-joined string keeps the value on the same
        # TelemetryValue contract as every other reading.
        codes = parse_dtc_payload(response.payload)
        return [ObdReading(timestamp, request.channel, ",".join(codes), "")]

    if request.kind == "service":
        # Mode 06 (Task 3.1) — a whole monitor-test-result sweep is a list
        # of 9-byte records, which doesn't fit the scalar TelemetryValue
        # contract. Ship the raw payload as hex (matching the `did` branch
        # above); `src.control.monitor_state.MonitorState` parses it back
        # with `parse_mode06_response`. Always emit, even empty, so "polled,
        # nothing reported" is distinguishable from "not polled yet".
        # The ISO-TP matcher strips the echoed MID. Reattach it so the shared
        # Mode 06 parser receives its documented 9-byte
        # ``MID TID UAS value min max`` record.
        payload = response.payload
        if response.pid is not None:
            payload = bytes([response.pid]) + payload
        readings = [ObdReading(timestamp, request.channel, payload.hex(), "")]
        if request.service == _MODE06_SERVICE:
            # Task 3.2: also land per-cylinder misfire counts as ordinary
            # numeric channels (mirrors the MIL_STATUS derived-reading
            # pattern below) so they flow through storage into the wide
            # telemetry frame the session fingerprint scans — the hex sweep
            # reading above can't be aggregated numerically.
            counts = misfire_counts_from_results(parse_mode06_response(payload))
            for cylinder, count in sorted(counts.items()):
                readings.append(
                    ObdReading(timestamp, misfire_count_channel(cylinder), float(count), "count")
                )
        return readings

    if request.kind == "freeze_frame":
        # Mode 02 (Task 3.3) — same wire formula as Mode 01 for this PID
        # (`obd_registry.lookup(0x02, pid)`'s Mode-01 remap), but the
        # response carries one extra leading frame-number byte ahead of the
        # formula bytes that Mode 01 doesn't have; strip it before decoding.
        entry = PID_DECODERS.get(request.identifier)
        if entry is None or len(response.payload) < 2:
            return []
        _name, decoder = entry
        decoded = decoder(response.payload[1:])
        if decoded is None:
            return []
        value, unit = decoded
        return [ObdReading(timestamp, request.channel, value, unit)]

    if request.kind == "custom":
        # Task 2.6 — decode through the definition's sandboxed formula
        # instead of a PID_DECODERS lookup. `build_poll_requests` only ever
        # builds a "custom" request from a CustomPid, so this is set.
        assert request.custom_pid is not None
        decoded = evaluate_custom_pid(request.custom_pid, response.payload)
        if decoded is None:
            return []
        value, unit = decoded
        return [ObdReading(timestamp, request.channel, value, unit)]

    # Reuse the public PID decoder behavior without issuing another request:
    # synthesize the same DecodedPid logic locally from PID_DECODERS.
    entry = PID_DECODERS.get(request.identifier)
    if entry is None:
        return []
    name, decoder = entry
    decoded = decoder(response.payload)
    if decoded is None:
        return []
    value, unit = decoded
    readings = [ObdReading(timestamp, name, value, unit)]
    if request.identifier == _MONITOR_STATUS_PID:
        mil_on = decode_mil_on(response.payload)
        if mil_on is not None:
            readings.append(ObdReading(timestamp, _MIL_STATUS_CHANNEL, mil_on, "bool"))
    return readings


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
    result = _query_with_tap(
        bus,
        request,
        timeout_seconds=timeout_seconds,
        on_frame=on_frame,
        stop_event=stop_event,
        stats=stats,
        deadline_monotonic=deadline_monotonic,
    )
    return result.response is not None


@dataclass(frozen=True)
class QueryResult:
    """Outcome of one `_query_with_tap` request/response cycle.

    Distinguishes a formal negative response (ECU replied with a reason,
    `0x7F <service> <NRC>`) from a true timeout (no frame at all) so
    callers can count them separately instead of folding both into
    "no response" — the gap that hid whether the Xterra's ECU is silent
    on Mode 0A/06 or promptly refusing them.
    """

    response: ObdResponse | None = None
    negative_response_code: int | None = None


@dataclass
class BatchedQueryResult:
    """Outcome of one multi-PID request/response cycle."""

    #: ``{pid: payload}`` for PIDs present in the response, or ``None`` when
    #: the batch as a whole failed (timeout, refusal, or a desynced walk).
    readings: dict[int, bytes] | None = None
    negative_response_code: int | None = None
    #: True when the payload walk completed without hitting an unknown PID
    #: or a truncated tail.
    clean: bool = True


def _batched_pid_width(pid: int) -> int | None:
    """Mode 01 payload width for splitting a batched response."""
    definition = lookup(0x01, pid)
    if definition is None or definition.byte_length <= 0:
        return None
    return definition.byte_length


def _query_batched_with_tap(
    bus: can.BusABC,
    leader: _PollRequest,
    pids: list[int],
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> BatchedQueryResult:
    """Issue one ``01 <pid> <pid> …`` request and split the response."""
    message = can.Message(
        arbitration_id=leader.request_id,
        data=build_batched_request_payload(0x01, pids),
        is_extended_id=False,
    )
    if not tx_gate.send(bus, message, what=f"batch {len(pids)} PIDs"):
        return BatchedQueryResult()
    stats.requests_sent += 1
    stats.batched_requests += 1
    stats.batched_pids_requested += len(pids)

    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)
    # identifier=None: the payload is a stream of <pid> <data> pairs, so
    # there is no single echo to match on.
    assembler = IsoTpResponseAssembler(service=0x01, identifier=None)

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
        result = assembler.accept(int(msg.arbitration_id), data)
        if result.negative_response:
            return BatchedQueryResult(negative_response_code=result.negative_response_code)
        if result.flow_control_id is not None:
            tx_gate.send(
                bus,
                can.Message(
                    arbitration_id=result.flow_control_id,
                    data=FLOW_CONTROL_CTS,
                    is_extended_id=False,
                ),
                what="flow-control",
            )
            continue
        if result.response is not None:
            readings, clean = split_batched_response(result.response.payload, _batched_pid_width)
            return BatchedQueryResult(readings=readings, clean=clean)
    return BatchedQueryResult()


def _issue_batched_request(
    bus: can.BusABC,
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    indices: list[int],
    *,
    spec: CanObdSpec,
    on_frame: Callable[[can.Message], None],
    on_reading: Callable[[ObdReading], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> bool:
    """Poll several Mode 01 PIDs in one round trip. True if the batch answered.

    Per-member accounting is deliberate: a PID present in the response is
    credited exactly as the single-PID path would credit it, and a PID absent
    from an otherwise-good response takes a miss on its own — so one dead
    channel backs off alone instead of dragging its five neighbours down.

    A *whole-batch* failure (timeout, refusal, desynced walk) credits and
    penalises nobody. We have no evidence about any individual PID in that
    case, and applying six misses would wrongly back off six healthy channels
    on what may be an ECU that simply dislikes batching. The caller counts
    consecutive failures and stops batching instead.
    """
    pids = [requests[index].identifier for index in indices]
    leader = requests[indices[0]]
    started = time.monotonic()
    for index in indices:
        states[index].last_requested_at = started
        states[index].requests_sent += 1

    try:
        result = _query_batched_with_tap(
            bus,
            leader,
            pids,
            timeout_seconds=spec.request_timeout_seconds,
            on_frame=on_frame,
            stop_event=stop_event,
            stats=stats,
            deadline_monotonic=deadline_monotonic,
        )
    except Exception:  # noqa: BLE001 - propagate after recording scheduler state
        for index in indices:
            states[index].bus_errors += 1
        stats.bus_errors += 1
        raise

    if result.readings is None:
        stats.batch_failures += 1
        if result.negative_response_code is not None:
            stats.negative_responses += 1
            for index in indices:
                states[index].last_negative_response_code = result.negative_response_code
        return False
    if not result.clean:
        stats.batch_desyncs += 1

    now = time.monotonic()
    for index in indices:
        request = requests[index]
        state = states[index]
        payload = result.readings.get(request.identifier)
        emitted = False
        if payload is not None:
            # Synthesise the response the single-PID path would have seen, so
            # both paths decode through exactly the same code.
            readings = _response_to_reading(
                request,
                ObdResponse(
                    service=0x41,
                    pid=request.identifier,
                    payload=payload,
                    responder_id=DEFAULT_RESPONSE_ID_MIN,
                ),
            )
            if readings:
                emitted = True
                stats.readings_emitted += len(readings)
                for reading in readings:
                    on_reading(reading)
        if emitted:
            state.responses += 1
            state.last_response_at = now
        else:
            state.timeouts += 1
            stats.request_timeouts += 1
        _update_response_state(state, emitted=emitted, now=now)
    return True


def _query_with_tap(
    bus: can.BusABC,
    request: _PollRequest,
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> QueryResult:
    # Mode 03/07/0A (Task 1.3) take no parameter byte, so their placeholder
    # identifier must not leak onto the wire. Mode 06 is different: it
    # requires a monitor ID (MID), including MID 0x00 for the supported-MID
    # query. Dropping ``service`` identifiers here turned ``06 00`` into a
    # bare ``06`` that this ECM silently ignores, and the assembler then also
    # waited for the wrong response shape.
    identifier = None if request.kind == "dtc" else request.identifier
    message = can.Message(
        arbitration_id=request.request_id,
        data=build_request_payload(request.service, identifier),
        is_extended_id=False,
    )
    if not tx_gate.send(bus, message, what=f"poll {request.channel}"):
        # Passive mode: no frame went out, so no response is coming. Return an
        # empty result WITHOUT counting a request or a timeout — the poll loop
        # normally skips issuing entirely when passive, so reaching here means
        # some other path called through; it must still not pollute the
        # scheduler's backoff state.
        return QueryResult()
    stats.requests_sent += 1

    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)
    assembler = IsoTpResponseAssembler(service=request.service, identifier=identifier)

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
            return QueryResult(negative_response_code=result.negative_response_code)
        if result.flow_control_id is not None:
            tx_gate.send(
                bus,
                can.Message(
                    arbitration_id=result.flow_control_id,
                    data=FLOW_CONTROL_CTS,
                    is_extended_id=False,
                ),
                what="flow-control",
            )
            continue
        if result.response is not None:
            return QueryResult(response=result.response)
    return QueryResult()


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
        log_dropped_can_frame(stats.parse_errors, exc)
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
