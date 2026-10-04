"""Wire I/O for the Canable-native OBD loop (roadmap R02.9).

One request out, one exchange back, every received frame through the loop's
tap: the single and batched polls (``_query_with_tap``,
``_query_batched_with_tap``), session control, the calibration-id read, the
tapped receive and drain, and the arrival stamps of a response. The receive
policy itself is ``isotp.await_response`` (R02.5). Moved out of
``can_obd_collector``, which re-exports these names.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import can

from src.collection import tuning_profile, tx_gate
from src.collection.calibration_id import (
    READ_LOCAL_IDENTIFIER_SERVICE,
    CalibrationIdSpec,
    decode_calibration_id,
)
from src.collection.can_obd_client import (
    receive_frame,
)
from src.collection.frame_time import FrameArrival, frame_arrival
from src.collection.isotp import (
    FLOW_CONTROL_CTS,
    Exchange,
    IsoTpResponseAssembler,
    ObdResponse,
    await_response,
    build_batched_did_request_payload,
    build_batched_request_payload,
    build_memory_read_payload,
    build_request_payload,
    split_batched_did_response,
    walk_batched_response,
)
from src.collection.obd_scheduler import (
    CanObdLoopStats,
    DidSessionSpec,
    Mode01WidthLedger,
    PollRequest,
    SessionControlResult,
    mode01_registry_width,
)

log = logging.getLogger(__name__)


#: Identity of this loop's parse-error log throttle (one line per
#: ``DROP_LOG_INTERVAL_SECONDS``). Distinct from the passive sniffer's owners
#: so a storm on one live bus never suppresses the other's warning lines.
_PARSE_ERROR_OWNER = "canable-obd"


def _send_session_control(
    bus: can.BusABC,
    session_spec: DidSessionSpec,
    *,
    sub_function: int | None = None,
    request_id: int,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> SessionControlResult:
    """Send ``10 <session>`` (UDS DiagnosticSessionControl) and await ``50 <session>``.

    Opens or keeps alive the Mode-22 gating session, or — with
    ``sub_function`` — hands the bus back by returning to the default
    session. This helper performs session control only—no memory write or
    actuation—and returns True on a positive response.
    """
    exiting = sub_function is not None
    request = PollRequest(
        kind="session",
        service=0x10,
        identifier=sub_function if sub_function is not None else session_spec.session,
        request_id=request_id,
        channel="UDS_SESSION_EXIT" if exiting else "UDS_SESSION_CONTROL",
    )
    stats.session_controls_sent += 1
    started = time.monotonic()
    result = _query_with_tap(
        bus,
        request,
        timeout_seconds=timeout_seconds,
        on_frame=on_frame,
        stop_event=stop_event,
        stats=stats,
        deadline_monotonic=deadline_monotonic,
    )
    stats.session_control_seconds += max(0.0, time.monotonic() - started)
    if result.response is not None:
        stats.session_control_responses += 1
        return SessionControlResult(ok=True)
    # Session control bypasses the poll-request path, so without these the
    # only trace of a failure was a bare counter — a refusal and a silent bus
    # looked identical in `/status` and neither appeared in the log at all.
    # The shared counters keep every failure visible; the session-specific
    # ones keep it out of the channel-read throughput (C04.8).
    if result.negative_response_code is not None:
        stats.negative_responses += 1
        stats.session_control_negative_responses += 1
    else:
        stats.request_timeouts += 1
        stats.session_control_timeouts += 1
    return SessionControlResult(ok=False, negative_response_code=result.negative_response_code)


def _read_calibration_id(
    bus: can.BusABC,
    calibration: CalibrationIdSpec,
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> str | None:
    """Send ``21 <lid>`` once; cache and return the ASCII id, or ``None``.

    A read (ReadDataByLocalIdentifier). The result goes to
    ``tuning_profile.record_ecu_calibration_id``, which flags a change or a
    registry mismatch (W02.3).
    """
    request = PollRequest(
        kind="identification",
        service=READ_LOCAL_IDENTIFIER_SERVICE,
        identifier=calibration.local_identifier,
        request_id=calibration.request_id,
        channel="ECU_CALIBRATION_ID",
    )
    sent_before = stats.requests_sent
    result = _query_with_tap(
        bus,
        request,
        timeout_seconds=timeout_seconds,
        on_frame=on_frame,
        stop_event=stop_event,
        stats=stats,
        deadline_monotonic=deadline_monotonic,
    )
    stats.identification_requests_sent += stats.requests_sent - sent_before
    calibration_id = decode_calibration_id(
        None if result.response is None else bytes(result.response.payload)
    )
    if calibration_id is not None:
        tuning_profile.record_ecu_calibration_id(calibration_id)
    elif result.negative_response_code is not None:
        log.debug(
            "calibration id read refused",
            extra={"nrc": result.negative_response_code},
        )
    return calibration_id


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
    #: Arrival of the first response frame (C02.7); ``None`` without a response.
    arrival: FrameArrival | None = None
    #: Completion minus first-frame arrival for multi-frame responses, when > 0.
    response_interval_s: float | None = None


def _frame_arrival(msg: can.Message) -> FrameArrival:
    """:func:`~src.collection.frame_time.frame_arrival` on this loop's wall clock."""
    return frame_arrival(msg, now=time.time())


def _response_interval(first: FrameArrival | None, completed: FrameArrival) -> float | None:
    """Reassembly span of a multi-frame response; ``None`` for single-frame."""
    if first is None:
        return None
    interval = completed.at - first.at
    return interval if interval > 0 else None


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
    arrival: FrameArrival | None = None
    response_interval_s: float | None = None
    responder_id: int | None = None
    #: Mode 01 only: the PID the walk blames for a desync (``BatchWalk.suspect``).
    suspect: int | None = None


def _batched_pid_width(pid: int, widths: Mode01WidthLedger | None = None) -> int | None:
    """Mode 01 payload width for splitting a batched response.

    The registry width, unless ``widths`` has measured this PID answering with
    a different one — then ``None``, so a walk that meets it stops rather than
    slicing by a width the vehicle has contradicted. (Such a PID is never put
    in a batch; this is the walk's own refusal, not the batching rule.)
    """
    if widths is not None:
        return widths.batch_width(pid)
    return mode01_registry_width(pid)


def _query_batched_with_tap(
    bus: can.BusABC,
    leader: PollRequest,
    identifiers: list[int],
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
    did_response_widths: Mapping[int, int] | None = None,
    mode01_widths: Mode01WidthLedger | None = None,
) -> BatchedQueryResult:
    """Issue one Mode-01 or Mode-22 batch through the owned receive tap."""
    is_did = leader.kind == "did"
    service = 0x22 if is_did else 0x01
    payload = (
        build_batched_did_request_payload(service, identifiers)
        if is_did
        else build_batched_request_payload(service, identifiers)
    )
    message = can.Message(
        arbitration_id=leader.request_id,
        data=payload,
        is_extended_id=False,
    )
    if not tx_gate.send(bus, message, what=f"batch {len(identifiers)} identifiers"):
        return BatchedQueryResult()
    stats.requests_sent += 1
    if is_did:
        stats.did_batched_requests += 1
        stats.did_batched_members_requested += len(identifiers)
    else:
        stats.batched_requests += 1
        stats.batched_pids_requested += len(identifiers)

    # identifier=None: the payload is a stream of <pid> <data> pairs, so
    # there is no single echo to match on. A physical request (the DID batch
    # on 0x7E0) is pinned to its one responder inside `await_response`.
    exchange = _await_exchange(
        bus,
        IsoTpResponseAssembler(service=service, identifier=None),
        leader.request_id,
        timeout_seconds=timeout_seconds,
        on_frame=on_frame,
        stop_event=stop_event,
        stats=stats,
        deadline_monotonic=deadline_monotonic,
    )
    if exchange.response is None:
        return BatchedQueryResult(negative_response_code=exchange.negative_response_code)
    suspect: int | None = None
    if is_did:
        readings, clean = split_batched_did_response(
            exchange.response.payload,
            lambda did: (did_response_widths or {}).get(did),
        )
    else:
        walk = walk_batched_response(
            exchange.response.payload,
            lambda pid: _batched_pid_width(pid, mode01_widths),
            expected=frozenset(identifiers),
        )
        readings, clean, suspect = walk.readings, walk.clean, walk.suspect
    first, completed = _exchange_arrivals(exchange)
    return BatchedQueryResult(
        readings=readings,
        clean=clean,
        arrival=first or completed,
        response_interval_s=_response_interval(first, completed),
        responder_id=exchange.response.responder_id,
        suspect=suspect,
    )


def _query_with_tap(
    bus: can.BusABC,
    request: PollRequest,
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
    # A service-0x23 read carries an address and a length instead of an
    # identifier, and its positive response echoes neither -- so `identifier`
    # must be None for the assembler too, or it would look for an echo that
    # is never sent and drop every reply.
    if request.kind == "memory":
        assert request.memory_read is not None
        identifier = None
        request_data = build_memory_read_payload(
            request.memory_read.address, request.memory_read.length
        )
    else:
        identifier = None if request.kind == "dtc" else request.identifier
        request_data = build_request_payload(request.service, identifier)
    message = can.Message(
        arbitration_id=request.request_id,
        data=request_data,
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

    exchange = _await_exchange(
        bus,
        IsoTpResponseAssembler(service=request.service, identifier=identifier),
        request.request_id,
        timeout_seconds=timeout_seconds,
        on_frame=on_frame,
        stop_event=stop_event,
        stats=stats,
        deadline_monotonic=deadline_monotonic,
    )
    if exchange.response is None:
        return QueryResult(negative_response_code=exchange.negative_response_code)
    first, completed = _exchange_arrivals(exchange)
    return QueryResult(
        response=exchange.response,
        arrival=first or completed,
        response_interval_s=_response_interval(first, completed),
    )


def _await_exchange(
    bus: can.BusABC,
    assembler: IsoTpResponseAssembler,
    request_id: int,
    *,
    timeout_seconds: float,
    on_frame: Callable[[can.Message], None],
    stop_event: threading.Event,
    stats: CanObdLoopStats,
    deadline_monotonic: float | None,
) -> Exchange[can.Message]:
    """The loop's side of ``isotp.await_response``: its tapped receive, its
    flow-control send, and the capture deadline as the hard cap (R02.5)."""
    request_deadline = time.monotonic() + timeout_seconds
    if deadline_monotonic is not None:
        request_deadline = min(request_deadline, deadline_monotonic)

    def send_flow_control(arbitration_id: int) -> None:
        tx_gate.send(
            bus,
            can.Message(arbitration_id=arbitration_id, data=FLOW_CONTROL_CTS, is_extended_id=False),
            what="flow-control",
        )

    exchange = await_response(
        lambda remaining: _recv_and_tap(
            bus, on_frame, stats, timeout=min(0.05, max(0.001, remaining))
        ),
        send_flow_control,
        assembler,
        request_id,
        request_deadline,
        clock=time.monotonic,
        stop=stop_event.is_set,
        hard_deadline=deadline_monotonic,
    )
    if exchange.continuation_lost:
        stats.continuation_losses += 1
    if exchange.flow_control_resent:
        stats.flow_control_resends += 1
        if exchange.response is not None:
            stats.flow_control_rescues += 1
    return exchange


def _exchange_arrivals(
    exchange: Exchange[can.Message],
) -> tuple[FrameArrival | None, FrameArrival]:
    """``(first-frame arrival or None, completing-frame arrival)`` for a
    positive exchange; the first is the acquisition instant of a multi-frame
    response (C02.7)."""
    assert exchange.final_frame is not None
    first = None if exchange.first_frame is None else _frame_arrival(exchange.first_frame)
    return first, _frame_arrival(exchange.final_frame)


def _recv_and_tap(
    bus: can.BusABC,
    on_frame: Callable[[can.Message], None],
    stats: CanObdLoopStats,
    *,
    timeout: float,
) -> can.Message | None:
    """The collector's receive: ``receive_frame`` plus the loop's accounting.

    Every receive the loop makes — polls, batches, session control, drains —
    comes through here; the client it hands its tap to uses the same
    ``receive_frame``. Nothing else on the loop's thread calls ``bus.recv``.
    Parse errors are ``receive_frame``'s to catch (R02.1); this only counts.
    """
    msg = receive_frame(
        bus,
        timeout=timeout,
        on_frame=on_frame,
        on_parse_error=stats.count_parse_error,
        parse_error_owner=_PARSE_ERROR_OWNER,
    )
    if msg is not None:
        stats.frames_seen += 1
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
