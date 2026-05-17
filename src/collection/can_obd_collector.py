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
from typing import Literal, Protocol

import can

from src.collection.can_collector import (
    DEFAULT_RECONNECT_POLICY,
    RecordingResult,
    bus_factory,
    frame_to_record,
)
from src.collection.can_collector import _wait_interruptible as wait_interruptible
from src.collection.can_collector import _write_manifest as write_recording_manifest
from src.collection.can_obd_client import (
    DEFAULT_REQUEST_ID,
    DEFAULT_RESPONSE_ID_MAX,
    DEFAULT_RESPONSE_ID_MIN,
    PID_DECODERS,
    ObdResponse,
)
from src.collection.obd_collector import JsonlReadingWriter, ObdReading, reading_to_record
from src.collection.queueing import QueueDropCounter, put_nowait_or_drop
from src.collection.reconnect import ReconnectPolicy, wait_for_stop_or_timeout
from src.config import CanBusConfig, VehicleConfig
from src.session import SessionPaths
from src.telemetry import TelemetryRecord

log = logging.getLogger(__name__)

AUTO_PIDS = "auto"
DEFAULT_DID_REQUEST_ID = 0x7E0
AUTO_PROBE_TIMEOUT_SECONDS = 0.25
AUTO_MISS_BACKOFF_SECONDS = 30.0
AUTO_BASELINE_PID_NAMES = tuple(name for _pid, (name, _decoder) in sorted(PID_DECODERS.items()))

_PCI_SINGLE_FRAME = 0x0
_PCI_FIRST_FRAME = 0x1
_PCI_CONSECUTIVE_FRAME = 0x2
_FLOW_CONTROL_CTS = bytes([0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

_PID_IDS_BY_NAME: dict[str, int] = {name: pid for pid, (name, _decoder) in PID_DECODERS.items()}


class _PidDiscoveryClient(Protocol):
    def query_supported_mode_01_pids(self) -> tuple[int, ...]: ...


@dataclass(frozen=True)
class CanObdSpec:
    """Streaming OBD settings for the Canable-native path."""

    can: CanBusConfig
    poll_rate_hz: float = 1.0
    pids: tuple[str, ...] = (AUTO_PIDS,)
    dids: tuple[str, ...] = ()
    did_request_id: int = DEFAULT_DID_REQUEST_ID
    request_timeout_seconds: float = 1.0


@dataclass
class CanObdLoopStats:
    frames_seen: int = 0
    parse_errors: int = 0
    requests_sent: int = 0
    readings_emitted: int = 0


@dataclass(frozen=True)
class _PollRequest:
    kind: Literal["pid", "did"]
    service: int
    identifier: int
    request_id: int
    channel: str
    auto_probe: bool = False
    fast_probe: bool = False


@dataclass
class _PollRequestState:
    missed_responses: int = 0
    suppressed_until: float = 0.0


@dataclass
class _ReassemblyBuffer:
    total_length: int
    received: bytearray
    next_sequence: int


def parse_pid_token(token: str) -> int | None:
    """Parse a PID selector.

    Returns ``None`` for the ``auto`` sentinel. Explicit selectors accept either
    decoder names such as ``RPM`` or hex PID numbers such as ``0x0C`` / ``0C``.
    Unknown PIDs are rejected because the streaming path only emits decoded
    telemetry rows.
    """
    normalized = token.strip().upper()
    if not normalized:
        raise ValueError("empty OBD PID token")
    if normalized == AUTO_PIDS.upper():
        return None
    by_name = _PID_IDS_BY_NAME.get(normalized)
    if by_name is not None:
        return by_name
    try:
        pid = int(normalized, 16)
    except ValueError as exc:
        raise ValueError(f"unknown Canable OBD PID {token!r}") from exc
    if pid not in PID_DECODERS:
        raise ValueError(f"no Canable OBD decoder registered for PID 0x{pid:02X}")
    return pid


def parse_did_token(token: str) -> int:
    normalized = token.strip().upper()
    if len(normalized) != 4 or any(c not in "0123456789ABCDEF" for c in normalized):
        raise ValueError(f"DID must be a 4-character hex string, got {token!r}")
    return int(normalized, 16)


def parse_can_id(value: str | int) -> int:
    if isinstance(value, int):
        parsed = value
    else:
        parsed = int(value.strip(), 16)
    if not 0 <= parsed <= 0x7FF:
        raise ValueError(f"CAN arbitration ID must be 11-bit, got 0x{parsed:X}")
    return parsed


def build_poll_requests(client: _PidDiscoveryClient, spec: CanObdSpec) -> list[_PollRequest]:
    """Build the round-robin request plan from config and ECU support."""
    tokens = spec.pids or (AUTO_PIDS,)
    auto = False
    explicit_pids: list[int] = []
    for token in tokens:
        parsed = parse_pid_token(token)
        if parsed is None:
            auto = True
        else:
            explicit_pids.append(parsed)

    pids: list[int] = []
    auto_probe_by_pid: dict[int, bool] = {}
    fast_probe_by_pid: dict[int, bool] = {}

    def add_pid(pid: int, *, auto_probe: bool, fast_probe: bool = False) -> None:
        if pid not in pids:
            pids.append(pid)
            fast_probe_by_pid[pid] = fast_probe
        # Explicit config wins over auto management for the same PID.
        auto_probe_by_pid[pid] = auto_probe_by_pid.get(pid, True) and auto_probe
        fast_probe_by_pid[pid] = fast_probe_by_pid.get(pid, False) and auto_probe

    if auto:
        supported = client.query_supported_mode_01_pids()
        for pid in supported:
            if pid in PID_DECODERS:
                add_pid(pid, auto_probe=True, fast_probe=False)
        # Some ECUs/adapters report sparse or incomplete supported-PID bitmasks
        # even though standard Mode-01 PIDs answer when queried directly. Keep
        # discovery first for fast dashboard values, then probe every decoded
        # standard PID CANary knows about so auto mode can surface the full
        # basic-OBD set. Unsupported auto probes are backed off by the run loop.
        for pid in sorted(PID_DECODERS):
            add_pid(pid, auto_probe=True, fast_probe=True)

    for pid in explicit_pids:
        add_pid(pid, auto_probe=False, fast_probe=False)

    requests = [
        _PollRequest(
            kind="pid",
            service=0x01,
            identifier=pid,
            request_id=DEFAULT_REQUEST_ID,
            channel=PID_DECODERS[pid][0],
            auto_probe=auto_probe_by_pid[pid],
            fast_probe=fast_probe_by_pid[pid],
        )
        for pid in pids
    ]
    for did in [parse_did_token(d) for d in spec.dids]:
        requests.append(
            _PollRequest(
                kind="did",
                service=0x22,
                identifier=did,
                request_id=spec.did_request_id,
                channel=f"DID_{did:04X}",
            )
        )
    return requests


def run_can_obd_loop(
    bus: can.BusABC,
    spec: CanObdSpec,
    *,
    on_frame: Callable[[can.Message], None],
    on_reading: Callable[[ObdReading], None],
    stop_event: threading.Event,
    deadline_monotonic: float | None = None,
    client_factory: Callable[[can.BusABC], _PidDiscoveryClient] | None = None,
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
    if requests:
        initial_request_spacing = 1.0 / (max(spec.poll_rate_hz, 0.001) * len(requests))
        log.info(
            "canable obd streaming started",
            extra={
                "pids": [r.channel for r in requests if r.kind == "pid"],
                "dids": [r.channel for r in requests if r.kind == "did"],
                "initial_request_spacing_seconds": initial_request_spacing,
                "auto_miss_backoff_seconds": AUTO_MISS_BACKOFF_SECONDS,
            },
        )
    else:
        log.warning("canable obd streaming has no PID/DID requests; recording CAN only")

    index = 0
    request_states = [_PollRequestState() for _request in requests]
    next_request_at = time.monotonic()
    while not _should_stop(stop_event, deadline_monotonic):
        if not requests:
            _recv_and_tap(bus, on_frame, stats, timeout=0.2)
            continue

        now = time.monotonic()
        active_request_count = _count_active_requests(requests, request_states, now)
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

        request_spacing = 1.0 / (max(spec.poll_rate_hz, 0.001) * active_request_count)
        if now < next_request_at:
            _drain_frames_until(
                bus,
                on_frame,
                stats,
                stop_event,
                next_request_at,
                deadline_monotonic,
            )
            continue

        selection = _select_next_request(requests, request_states, index, now)
        if selection is None:
            continue
        selected_index, request, request_state = selection

        timeout_seconds = spec.request_timeout_seconds
        if request.auto_probe and (request.fast_probe or request_state.missed_responses > 0):
            timeout_seconds = min(timeout_seconds, AUTO_PROBE_TIMEOUT_SECONDS)
        response = _query_with_tap(
            bus,
            request,
            timeout_seconds=timeout_seconds,
            on_frame=on_frame,
            stop_event=stop_event,
            stats=stats,
            deadline_monotonic=deadline_monotonic,
        )
        emitted = False
        if response is not None:
            reading = _response_to_reading(request, response)
            if reading is not None:
                emitted = True
                stats.readings_emitted += 1
                on_reading(reading)
        if request.auto_probe:
            _update_auto_probe_state(
                request_state,
                emitted=emitted,
                now=time.monotonic(),
            )

        index = (selected_index + 1) % len(requests)
        next_request_at = max(next_request_at + request_spacing, time.monotonic())

    return stats


async def collect_can_obd_from_config(
    spec: CanObdSpec,
    queue: asyncio.Queue[TelemetryRecord],
    stop_event: asyncio.Event,
    *,
    factory: Callable[[CanBusConfig], can.BusABC] = bus_factory,
    reconnect_policy: ReconnectPolicy | None = DEFAULT_RECONNECT_POLICY,
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

                await asyncio.to_thread(
                    run_can_obd_loop,
                    bus,
                    spec,
                    on_frame=on_frame,
                    on_reading=on_reading,
                    stop_event=thread_stop,
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
    reconnects = 0  # successful bus rebuilds via factory()

    reconnect_enabled = factory is not None and reconnect_policy is not None
    current_bus: can.BusABC | None = bus
    reconnect_delay = (
        reconnect_policy.initial_delay_seconds if reconnect_policy is not None else 0.0
    )
    # Consecutive failed rebuilds — resets on success, capped by
    # max_reconnects so a wedged adapter can't trap us in an infinite loop.
    failed_rebuilds = 0

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
                    assert reconnect_enabled  # else we'd have raised already
                    assert factory is not None and reconnect_policy is not None
                    if wait_interruptible(local_stop, reconnect_delay):
                        stop_reason = "signal"
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        stop_reason = "deadline"
                        break
                    try:
                        current_bus = factory(spec.can)
                        reconnects += 1
                        failed_rebuilds = 0  # reset on success
                        reconnect_delay = reconnect_policy.initial_delay_seconds
                        log.info("bus reconnected", extra={"total_reconnects": reconnects})
                    except Exception as build_exc:
                        failed_rebuilds += 1
                        log.warning(
                            "bus rebuild failed; will retry",
                            extra={
                                "error": f"{type(build_exc).__name__}: {build_exc}",
                                "next_delay_seconds": reconnect_delay,
                                "failed_rebuilds": failed_rebuilds,
                                "max_reconnects": max_reconnects,
                            },
                        )
                        if max_reconnects is not None and failed_rebuilds >= max_reconnects:
                            log.error(
                                "reconnect attempts exhausted; stopping recording",
                                extra={
                                    "failed_rebuilds": failed_rebuilds,
                                    "frames_recorded": frames_seen,
                                },
                            )
                            stop_reason = "reconnect_exhausted"
                            error = build_exc
                            break
                        reconnect_delay = reconnect_policy.next_delay(reconnect_delay)
                        continue

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
                    log.warning(
                        "canable obd loop failed; will attempt reconnect",
                        extra={
                            "error": f"{type(exc).__name__}: {exc}",
                            "delay_seconds": reconnect_delay,
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
        result = RecordingResult(
            started_at=started_at,
            ended_at=ended_at,
            frames_recorded=frames_seen,
            stopped_by=stop_reason,
            parse_errors=parse_errors,
            reconnects=reconnects,
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


def _update_auto_probe_state(
    state: _PollRequestState,
    *,
    emitted: bool,
    now: float,
) -> None:
    if emitted:
        state.missed_responses = 0
        state.suppressed_until = 0.0
        return
    state.missed_responses += 1
    state.suppressed_until = now + AUTO_MISS_BACKOFF_SECONDS


def _count_active_requests(
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    now: float,
) -> int:
    return sum(
        1
        for request, state in zip(requests, states, strict=True)
        if not _is_request_suppressed(request, state, now)
    )


def _select_next_request(
    requests: list[_PollRequest],
    states: list[_PollRequestState],
    start_index: int,
    now: float,
) -> tuple[int, _PollRequest, _PollRequestState] | None:
    for offset in range(len(requests)):
        index = (start_index + offset) % len(requests)
        request = requests[index]
        state = states[index]
        if not _is_request_suppressed(request, state, now):
            return index, request, state
    return None


def _is_request_suppressed(
    request: _PollRequest,
    state: _PollRequestState,
    now: float,
) -> bool:
    return request.auto_probe and state.suppressed_until > now


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
        data=_build_request_payload(request.service, request.identifier),
        is_extended_id=False,
    )
    bus.send(message)
    stats.requests_sent += 1

    expected_service = 0x40 | request.service
    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)
    pending: dict[int, _ReassemblyBuffer] = {}

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
        pci_high = (data[0] >> 4) & 0x0F
        responder = int(msg.arbitration_id)

        if pci_high == _PCI_SINGLE_FRAME:
            if _is_negative_response(data, request.service):
                return None
            payload = _handle_single_frame(data, expected_service, request.identifier)
            if payload is None:
                continue
            return ObdResponse(expected_service, request.identifier, payload, responder)

        if pci_high == _PCI_FIRST_FRAME:
            total_len, first_bytes = _parse_first_frame(data)
            if first_bytes is None:
                continue
            pending[responder] = _ReassemblyBuffer(total_len, bytearray(first_bytes), 1)
            bus.send(
                can.Message(
                    arbitration_id=responder - 8,
                    data=_FLOW_CONTROL_CTS,
                    is_extended_id=False,
                )
            )
            continue

        if pci_high == _PCI_CONSECUTIVE_FRAME:
            buf = pending.get(responder)
            if buf is None:
                continue
            sequence = data[0] & 0x0F
            if sequence != (buf.next_sequence & 0x0F):
                pending.pop(responder, None)
                continue
            remaining_bytes = buf.total_length - len(buf.received)
            take = min(7, remaining_bytes)
            buf.received.extend(data[1 : 1 + take])
            buf.next_sequence += 1
            if len(buf.received) >= buf.total_length:
                body = bytes(buf.received[: buf.total_length])
                pending.pop(responder, None)
                payload = _extract_service_payload(body, expected_service, request.identifier)
                if payload is None:
                    continue
                return ObdResponse(expected_service, request.identifier, payload, responder)
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


def _build_request_payload(service: int, identifier: int | None) -> bytes:
    if identifier is None:
        body = bytes([0x01, service])
    elif 0 <= identifier <= 0xFF:
        body = bytes([0x02, service, identifier])
    elif 0 <= identifier <= 0xFFFF:
        body = bytes([0x03, service, (identifier >> 8) & 0xFF, identifier & 0xFF])
    else:
        raise ValueError(f"identifier out of range: 0x{identifier:X}")
    return body + bytes(8 - len(body))


def _handle_single_frame(data: bytes, expected_service: int, identifier: int) -> bytes | None:
    length = data[0] & 0x0F
    if length == 0 or length > 7 or len(data) < 1 + length:
        return None
    body = data[1 : 1 + length]
    return _extract_service_payload(body, expected_service, identifier)


def _is_negative_response(data: bytes, service: int) -> bool:
    length = data[0] & 0x0F
    if length < 3 or len(data) < 1 + length:
        return False
    body = data[1 : 1 + length]
    return len(body) >= 3 and body[0] == 0x7F and body[1] == service


def _extract_service_payload(
    body: bytes,
    expected_service: int,
    identifier: int,
) -> bytes | None:
    if not body or body[0] != expected_service:
        return None
    if 0 <= identifier <= 0xFF:
        if len(body) < 2 or body[1] != identifier:
            return None
        return body[2:]
    if 0 <= identifier <= 0xFFFF:
        if len(body) < 3 or body[1] != (identifier >> 8) & 0xFF or body[2] != identifier & 0xFF:
            return None
        return body[3:]
    return None


def _parse_first_frame(data: bytes) -> tuple[int, bytes | None]:
    if len(data) < 2:
        return 0, None
    total_length = ((data[0] & 0x0F) << 8) | data[1]
    if total_length == 0:
        return 0, None
    return total_length, bytes(data[2:8])


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
    "build_poll_requests",
    "collect_can_obd_from_config",
    "parse_can_id",
    "parse_did_token",
    "parse_pid_token",
    "record_can_obd",
    "run_can_obd_loop",
]
