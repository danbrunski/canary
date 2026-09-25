"""Shared ISO-TP helpers for Canable-native OBD requests."""

from __future__ import annotations

from collections.abc import Callable, Container, Sequence
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

DEFAULT_REQUEST_ID = 0x7DF
DEFAULT_RESPONSE_ID_MIN = 0x7E8
DEFAULT_RESPONSE_ID_MAX = 0x7EF

PCI_SINGLE_FRAME = 0x0
PCI_FIRST_FRAME = 0x1
PCI_CONSECUTIVE_FRAME = 0x2
PCI_FLOW_CONTROL = 0x3

FLOW_CONTROL_CTS = bytes([0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])


@dataclass(frozen=True)
class ObdResponse:
    """One positive service response, after ISO-TP reassembly."""

    service: int
    pid: int | None
    payload: bytes
    responder_id: int


@dataclass
class ReassemblyBuffer:
    total_length: int
    received: bytearray
    next_sequence: int


@dataclass(frozen=True)
class IsoTpFrameResult:
    response: ObdResponse | None = None
    flow_control_id: int | None = None
    negative_response: bool = False
    #: The NRC byte (e.g. 0x31 requestOutOfRange) when `negative_response`
    #: is True; `None` otherwise, or if the frame is malformed (missing the
    #: third body byte) despite matching the 0x7F <service> prefix.
    negative_response_code: int | None = None
    #: NRC 0x78 (`requestCorrectlyReceived-ResponsePending`). NOT a refusal:
    #: ISO 14229 requires the tester to keep waiting on the P2* timer for the
    #: real answer. Kept distinct from `negative_response` because reading it
    #: as a refusal is how one busy moment got recorded as "this ECU does not
    #: support the request" — see `DidWindowController.on_exit_response`.
    response_pending: bool = False


def positive_response_service(service: int) -> int:
    return 0x40 | service


def build_request_payload(service: int, identifier: int | None) -> bytes:
    """Single-frame request body, padded to 8 bytes."""
    if identifier is None:
        body = bytes([0x01, service])
    elif 0 <= identifier <= 0xFF:
        body = bytes([0x02, service, identifier])
    elif 0 <= identifier <= 0xFFFF:
        body = bytes([0x03, service, (identifier >> 8) & 0xFF, identifier & 0xFF])
    else:
        raise ValueError(f"PID/DID identifier out of range: 0x{identifier:X}")
    return body + bytes(8 - len(body))


#: ReadMemoryByAddress. A read: it cannot alter the ECU. Its write
#: counterpart (0x3D, WriteMemoryByAddress) is deliberately absent from this
#: module -- the no-reflash line is about non-volatile memory, and a memory
#: write is the shortest path across it.
MEMORY_READ_SERVICE = 0x23


def build_memory_read_payload(address: int, length: int) -> bytes:
    """Single-frame service-0x23 request for `length` bytes at `address`.

    Emits the KWP32 body -- a 32-bit address followed by a 16-bit length:

    ``07 23 <addr:4 big-endian> <len:2 big-endian>``

    This is the *only* form verified to work on the 2008 Nissan ECM. The
    KWP24 (24-bit address / 8-bit length) and UDS addressAndLengthFormat-
    Identifier forms were both tried against the live vehicle on 2026-09-20
    and both answered NRC 0x12 (sub-function not supported). That is a
    measured vehicle fact rather than a standards preference, so this
    function builds one shape instead of taking a format argument that
    would let a caller re-derive the same two failures.

    The diagnostic message is seven bytes: one service byte plus the six-byte
    KWP32 body. ISO-TP's first byte therefore MUST be ``07``. ``06`` truncates
    the final length byte from the ECU's point of view and this ECM answers
    NRC 0x12; the standalone ``query_raw`` path caught this on live hardware
    because it computes ``1 + len(body)`` rather than taking a prebuilt frame.
    The resulting eight-byte CAN frame needs no padding.
    """
    if not 0 <= address <= 0xFFFFFFFF:
        raise ValueError(f"memory address out of 32-bit range: 0x{address:X}")
    if not 1 <= length <= 0xFFFF:
        raise ValueError(f"memory read length out of range: {length}")
    body = (
        bytes([0x07, MEMORY_READ_SERVICE]) + address.to_bytes(4, "big") + length.to_bytes(2, "big")
    )
    return body + bytes(8 - len(body))


#: SAE J1979 allows several PIDs in one Mode 01 request. Six is the ceiling
#: that still fits a single CAN frame: 1 PCI byte + 1 service byte + 6 PID
#: bytes = 8. Measured on the Xterra ECM 2026-07-26 at 2, 4 and 6 PIDs.
MAX_BATCHED_PIDS = 6


def build_batched_request_payload(service: int, pids: Sequence[int]) -> bytes:
    """Single-frame multi-PID request body, padded to 8 bytes.

    ``01 0C 0D 04 11 0E 10`` asks for six PIDs in one round trip and the ECU
    answers with ``41 0C <2> 0D <1> 04 <1> 11 <1> 0E <1> 10 <2>`` — one
    request instead of six, which is the whole point: on this bus the cost is
    the round trip, not the bytes.

    Padding to 8 is not cosmetic. This ECM ignores short frames outright — no
    response at all, not a negative one — which is indistinguishable from an
    unsupported service and cost two probe rounds to diagnose.
    """
    if not pids:
        raise ValueError("batched request needs at least one PID")
    if len(pids) > MAX_BATCHED_PIDS:
        raise ValueError(f"at most {MAX_BATCHED_PIDS} PIDs per request, got {len(pids)}")
    if len(set(pids)) != len(pids):
        # A duplicate makes the response ambiguous to walk and wastes a slot.
        raise ValueError(f"duplicate PID in batch: {[f'0x{p:02X}' for p in pids]}")
    for pid in pids:
        if not 0 <= pid <= 0xFF:
            raise ValueError(f"Mode 01 PID out of range: 0x{pid:X}")
    body = bytes([1 + len(pids), service, *pids])
    return body + bytes(8 - len(body))


def split_batched_response(
    payload: bytes,
    byte_length_for: Callable[[int], int | None],
) -> tuple[dict[int, bytes], bool]:
    """Walk a batched Mode 01 payload into ``{pid: data}``.

    ``payload`` is what the assembler yields for a request built with
    ``identifier=None`` — everything after the ``41`` echo, i.e. a stream of
    ``<pid> <data…>`` pairs. ``byte_length_for`` returns a PID's expected
    payload length, or ``None`` when the PID is unknown to the caller.

    Returns ``(readings, clean)``. ``clean`` is False when the walk had to
    stop early, which happens when a PID is unknown (we cannot know its
    width, so we cannot find the next boundary) or the payload is truncated.

    Deliberately keyed off the PIDs *present in the response* rather than the
    ones requested. An ECU may omit a PID it does not support instead of
    rejecting the whole batch, and matching positionally against the request
    would then silently misattribute every subsequent value. Whatever is
    returned before a desync is still trustworthy; everything after is not,
    which is why the flag is returned rather than swallowed.
    """
    walk = walk_batched_response(payload, byte_length_for)
    return walk.readings, walk.clean


@dataclass(frozen=True)
class BatchWalk:
    """A batched Mode 01 walk, with where it broke when it did."""

    #: ``{pid: data}`` in walk order, up to the break.
    readings: dict[int, bytes]
    clean: bool
    #: The PID whose width most plausibly broke the walk, or ``None`` when the
    #: payload gives no candidate (the very first byte was not a member).
    #: A truncated tail blames the PID at the break (its width runs past the
    #: end); a byte at a boundary that is not a member blames the PID parsed
    #: just before it (its width landed the walk off a boundary). A heuristic
    #: attribution, not proof — which is why the caller re-measures it.
    suspect: int | None = None


def walk_batched_response(
    payload: bytes,
    byte_length_for: Callable[[int], int | None],
    *,
    expected: Container[int] | None = None,
) -> BatchWalk:
    """``split_batched_response`` that also says where a desync happened.

    ``expected`` is the set of PIDs the request carried. With it, a byte at a
    boundary that is not a requested PID — or a PID seen twice — ends the walk
    as a desync: the ECU cannot answer a PID nobody asked for, so the previous
    width was wrong and every byte after it is misattributed. Without it, only
    an unknown PID or a truncated tail stops the walk.
    """
    readings: dict[int, bytes] = {}
    last: int | None = None
    index = 0
    while index < len(payload):
        pid = payload[index]
        stray = expected is not None and (pid not in expected or pid in readings)
        width = None if stray else byte_length_for(pid)
        if width is None or width <= 0:
            return BatchWalk(readings, False, last)
        start, end = index + 1, index + 1 + width
        if end > len(payload):
            return BatchWalk(readings, False, pid)
        readings.setdefault(pid, payload[start:end])
        last = pid
        index = end
    return BatchWalk(readings, True)


#: UDS service 0x22 allows several DIDs in one ReadDataByIdentifier request.
#: Three is the ceiling that still fits a single CAN frame: 1 PCI byte +
#: 1 service byte + 3 x 2 DID bytes = 8. (Mode 01 fits six because its
#: identifiers are one byte, not two — see MAX_BATCHED_PIDS.)
#:
#: The ECM's support for batching 0x22 is not an assumption: UpRev's own
#: tester was observed on 2026-08-08 sending ``05 22 11 03 04 01`` — service
#: 0x22 carrying DIDs 0x1103 and 0x0401 in one frame — to this ECM.
MAX_BATCHED_DIDS = 3


def build_batched_did_request_payload(service: int, dids: Sequence[int]) -> bytes:
    """Single-frame multi-DID request body, padded to 8 bytes.

    ``22 11 01 12 19 12 08`` asks for three DIDs in one round trip and the
    ECU answers ``62 1101 <n> 1219 <n> 1208 <n>``.

    The point is NOT throughput — a single-DID sweep already manages ~270
    DIDs/s on this bus. It is that every DID in one request is sampled by the
    ECU at the *same instant*, so batched values can be compared with each
    other. Sequentially polled DIDs cannot: they are rotation-sampled and
    carry an unknown relative age, which is the same defect that skews an
    UpRev K-line log by ~100 ms between channels.

    Padding to 8 is not cosmetic — see build_batched_request_payload.
    """
    if not dids:
        raise ValueError("batched request needs at least one DID")
    if len(dids) > MAX_BATCHED_DIDS:
        raise ValueError(f"at most {MAX_BATCHED_DIDS} DIDs per request, got {len(dids)}")
    if len(set(dids)) != len(dids):
        # A duplicate makes the response ambiguous to walk and wastes a slot.
        raise ValueError(f"duplicate DID in batch: {[f'0x{d:04X}' for d in dids]}")
    for did in dids:
        if not 0 <= did <= 0xFFFF:
            raise ValueError(f"Mode 22 DID out of range: 0x{did:X}")
    body = bytes([1 + 2 * len(dids), service, *(b for d in dids for b in (d >> 8, d & 0xFF))])
    return body + bytes(8 - len(body))


def split_batched_did_response(
    payload: bytes,
    byte_length_for: Callable[[int], int | None],
) -> tuple[dict[int, bytes], bool]:
    """Walk a batched Mode 22 payload into ``{did: data}``.

    ``payload`` is everything after the ``62`` echo, i.e. a stream of
    ``<did_hi> <did_lo> <data…>`` groups. ``byte_length_for`` returns a DID's
    expected payload width, or ``None`` when the width is unknown.

    Returns ``(readings, clean)``. ``clean`` is False when the walk stopped
    early — an unknown width (we cannot find the next boundary) or a
    truncated payload.

    A multi-DID response is NOT self-delimiting: nothing in the wire format
    says how long each value is, so a batch over DIDs of unknown width cannot
    be split at all. That is why blind discovery must stay single-DID and
    batching is only for DIDs already measured once. Keyed off the DIDs
    *present in the response* rather than the ones requested, for the same
    reason as the Mode 01 splitter: an ECU may omit an unsupported DID, and
    positional matching would then misattribute every later value.
    """
    readings: dict[int, bytes] = {}
    index = 0
    while index < len(payload):
        if index + 2 > len(payload):
            return readings, False
        did = (payload[index] << 8) | payload[index + 1]
        width = byte_length_for(did)
        if width is None or width <= 0:
            return readings, False
        start, end = index + 2, index + 2 + width
        if end > len(payload):
            return readings, False
        readings.setdefault(did, payload[start:end])
        index = end
    return readings, True


def pci_type(data: bytes) -> int | None:
    if not data:
        return None
    return (data[0] >> 4) & 0x0F


def single_frame_body(data: bytes) -> bytes:
    if not data:
        return b""
    length = data[0] & 0x0F
    return data[1 : 1 + length] if 1 <= length <= 7 and len(data) >= 1 + length else b""


def is_negative_response(data: bytes, service: int) -> bool:
    body = single_frame_body(data)
    return len(body) >= 3 and body[0] == 0x7F and body[1] == service


def negative_response_code(data: bytes, service: int) -> int | None:
    """The NRC byte of a ``0x7F <service> <NRC>`` frame, or `None` if it isn't one."""
    body = single_frame_body(data)
    if len(body) >= 3 and body[0] == 0x7F and body[1] == service:
        return body[2]
    return None


#: The one NRC that is not a refusal: the ECU acknowledges the request and
#: asks for more time (ISO 14229 P2* timing). Named because treating it as a
#: refusal is a silent-data-loss bug, not a cosmetic one.
RESPONSE_PENDING_NRC = 0x78

#: NRCs that mean the ECU will never answer this request, however many times
#: it is asked — the only grounds for disabling a capability rather than
#: retrying it. Everything else (busy, conditions-not-correct, timeout) is a
#: "try again", and must not latch.
PERMANENT_REFUSAL_NRCS: frozenset[int] = frozenset({0x11, 0x12})

#: ISO 14229-1 Annex A negative response codes actually seen on Mode
#: 01/03/06/07/0A/22 traffic. Not exhaustive by design (AGENTS.md: no
#: premature abstraction) — `nrc_name` falls back to raw hex for anything
#: not listed here.
NRC_NAMES: dict[int, str] = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "requestCorrectlyReceived-ResponsePending",
    # UDS and KWP2000 spell "wrong session" differently; this ECM answers the
    # KWP form (7F 21 80 to a default-session `21 10`, measured 2026-09-22).
    0x7F: "serviceNotSupportedInActiveSession",
    0x80: "serviceNotSupportedInActiveDiagnosticSession",
}


def nrc_name(code: int) -> str:
    """Human-readable NRC label, e.g. for surfacing in `/status`."""
    known = NRC_NAMES.get(code)
    return known if known is not None else f"unknown (0x{code:02X})"


def handle_single_frame(
    data: bytes,
    expected_service: int,
    identifier: int | None,
) -> bytes | None:
    body = single_frame_body(data)
    if not body:
        return None
    return extract_service_payload(body, expected_service, identifier)


def extract_service_payload(
    body: bytes,
    expected_service: int,
    identifier: int | None,
) -> bytes | None:
    if not body or body[0] != expected_service:
        return None
    offset = 1
    if identifier is not None:
        if 0 <= identifier <= 0xFF:
            if len(body) < 2 or body[1] != identifier:
                return None
            offset = 2
        elif 0 <= identifier <= 0xFFFF:
            if len(body) < 3 or body[1] != (identifier >> 8) & 0xFF or body[2] != identifier & 0xFF:
                return None
            offset = 3
        else:
            return None
    return body[offset:]


def parse_first_frame(data: bytes) -> tuple[int, bytes | None]:
    if len(data) < 2:
        return 0, None
    total_length = ((data[0] & 0x0F) << 8) | data[1]
    if total_length == 0:
        return 0, None
    return total_length, bytes(data[2:8])


class IsoTpResponseAssembler:
    """Incrementally assemble one expected OBD/UDS response from CAN frames."""

    def __init__(self, *, service: int, identifier: int | None) -> None:
        self.request_service = service
        self.expected_service = positive_response_service(service)
        self.identifier = identifier
        self._pending: dict[int, ReassemblyBuffer] = {}

    def accept(self, responder_id: int, data: bytes) -> IsoTpFrameResult:
        frame_type = pci_type(data)
        if frame_type == PCI_SINGLE_FRAME:
            if is_negative_response(data, self.request_service):
                nrc = negative_response_code(data, self.request_service)
                if nrc == RESPONSE_PENDING_NRC:
                    # "Received you, still working." The real response — positive
                    # or negative — is still coming; the caller extends its
                    # deadline rather than scoring this as a failure.
                    return IsoTpFrameResult(
                        response_pending=True,
                        negative_response_code=nrc,
                    )
                return IsoTpFrameResult(
                    negative_response=True,
                    negative_response_code=nrc,
                )
            payload = handle_single_frame(data, self.expected_service, self.identifier)
            if payload is None:
                return IsoTpFrameResult()
            return IsoTpFrameResult(
                response=ObdResponse(
                    service=self.expected_service,
                    pid=self.identifier,
                    payload=payload,
                    responder_id=responder_id,
                )
            )

        if frame_type == PCI_FIRST_FRAME:
            total_len, first_bytes = parse_first_frame(data)
            if first_bytes is None:
                return IsoTpFrameResult()
            self._pending[responder_id] = ReassemblyBuffer(
                total_length=total_len,
                received=bytearray(first_bytes),
                next_sequence=1,
            )
            return IsoTpFrameResult(flow_control_id=responder_id - 8)

        if frame_type == PCI_CONSECUTIVE_FRAME:
            buf = self._pending.get(responder_id)
            if buf is None:
                return IsoTpFrameResult()
            sequence = data[0] & 0x0F
            if sequence != (buf.next_sequence & 0x0F):
                self._pending.pop(responder_id, None)
                return IsoTpFrameResult()
            remaining_bytes = buf.total_length - len(buf.received)
            take = min(7, remaining_bytes)
            buf.received.extend(data[1 : 1 + take])
            buf.next_sequence += 1
            if len(buf.received) < buf.total_length:
                return IsoTpFrameResult()
            body = bytes(buf.received[: buf.total_length])
            self._pending.pop(responder_id, None)
            payload = extract_service_payload(body, self.expected_service, self.identifier)
            if payload is None:
                return IsoTpFrameResult()
            return IsoTpFrameResult(
                response=ObdResponse(
                    service=self.expected_service,
                    pid=self.identifier,
                    payload=payload,
                    responder_id=responder_id,
                )
            )

        return IsoTpFrameResult()


#: Extra wait granted each time the ECU answers `0x78 responsePending`
#: (ISO 14229 P2*). The ECU has told us it is working, so scoring this as a
#: timeout throws away an answer that was on its way.
RESPONSE_PENDING_EXTENSION_SECONDS = 0.5
#: Cap on those extensions, so a stuck ECU cannot hold the bus indefinitely.
#: Exceeding it is a real failure and is reported as NRC 0x78.
MAX_RESPONSE_PENDING_EXTENSIONS = 4


class ReceivedFrame(Protocol):
    """What :func:`await_response` needs of a received frame (a ``can.Message``)."""

    @property
    def arbitration_id(self) -> int: ...

    @property
    def data(self) -> bytearray | bytes: ...


FrameT = TypeVar("FrameT", bound=ReceivedFrame)


@dataclass(frozen=True)
class Exchange(Generic[FrameT]):
    """How one request/response exchange ended.

    ``response`` on a positive answer; ``negative_response_code`` on a
    definitive ``7F <service> <NRC>`` (or 0x78 once the pending extensions are
    spent); both ``None`` on silence or a stop. ``first_frame`` is the
    multi-frame First Frame (its arrival is the acquisition instant) and
    ``final_frame`` the frame that completed the response.
    """

    response: ObdResponse | None = None
    negative_response_code: int | None = None
    first_frame: FrameT | None = None
    final_frame: FrameT | None = None


def expected_responder(
    request_id: int,
    response_ids: tuple[int, int] = (DEFAULT_RESPONSE_ID_MIN, DEFAULT_RESPONSE_ID_MAX),
) -> int | None:
    """The one responder a request may be answered by, or ``None`` for any.

    A caller that declares a single response id (``response_ids`` with
    ``lo == hi``, e.g. a vehicle profile's ``ecu_response_id`` from
    ``dids.yaml``) is pinned to exactly that id: the pairing is vehicle data,
    and a module answering ``0x745 -> 0x765`` is not ``request + 8``.
    Otherwise the functional address (``0x7DF``) may be answered by any ECU in
    the window, and a physical request is owned by ``request + 8`` (``0x7E0``
    -> ``0x7E8``): accepting ``0x7E9`` would attach another module's bytes to
    the engine ECU's request.
    """
    lo, hi = response_ids
    if lo == hi:
        return lo
    return None if request_id == DEFAULT_REQUEST_ID else request_id + 8


def await_response(
    recv: Callable[[float], FrameT | None],
    send_flow_control: Callable[[int], None],
    assembler: IsoTpResponseAssembler,
    request_id: int,
    deadline: float,
    *,
    clock: Callable[[], float],
    stop: Callable[[], bool] | None = None,
    hard_deadline: float | None = None,
    response_ids: tuple[int, int] = (DEFAULT_RESPONSE_ID_MIN, DEFAULT_RESPONSE_ID_MAX),
) -> Exchange[FrameT]:
    """The one receive-loop policy for a request just sent (R02.5).

    Every OBD/UDS exchange — the collector's single and batched polls and the
    standalone client (``/control/obd_request``, the tools) — waits here, so
    none can differ on:

    * the response window, and **responder pinning** (:func:`expected_responder`):
      a declared single response id, else ``request_id + 8`` for a physical
      request — not optional, so no caller can forget it;
    * ``0x78`` responsePending: each one extends ``deadline`` by
      :data:`RESPONSE_PENDING_EXTENSION_SECONDS`, at most
      :data:`MAX_RESPONSE_PENDING_EXTENSIONS` times and never past
      ``hard_deadline`` (a shutdown or capture deadline);
    * an NRC ends the wait at once;
    * a First Frame is answered with flow control and remembered as the
      acquisition instant.

    ``recv(remaining_seconds)`` returns the next frame or ``None``; the caller
    bounds each slice and taps the frame. ``stop()`` ends the wait early.
    """
    lo, hi = response_ids
    owner = expected_responder(request_id, response_ids)
    extensions = 0
    first_frame: FrameT | None = None
    while True:
        now = clock()
        if now >= deadline or (stop is not None and stop()):
            return Exchange(first_frame=first_frame)
        frame = recv(deadline - now)
        if frame is None:
            continue
        responder = int(frame.arbitration_id)
        if not lo <= responder <= hi:
            continue
        if owner is not None and responder != owner:
            continue
        data = bytes(frame.data)
        if not data:
            continue
        result = assembler.accept(responder, data)
        if result.response_pending:
            if extensions >= MAX_RESPONSE_PENDING_EXTENSIONS:
                return Exchange(negative_response_code=RESPONSE_PENDING_NRC)
            extensions += 1
            deadline += RESPONSE_PENDING_EXTENSION_SECONDS
            if hard_deadline is not None:
                deadline = min(deadline, hard_deadline)
            continue
        if result.negative_response:
            return Exchange(negative_response_code=result.negative_response_code)
        if result.flow_control_id is not None:
            # Multi-frame response: the FIRST frame's arrival is the
            # acquisition instant. Flow control goes to the responder's
            # tester address (responder - 8, e.g. 0x7E8 -> 0x7E0).
            first_frame = frame
            send_flow_control(result.flow_control_id)
            continue
        if result.response is not None:
            return Exchange(response=result.response, first_frame=first_frame, final_frame=frame)


__all__ = [
    "MAX_RESPONSE_PENDING_EXTENSIONS",
    "RESPONSE_PENDING_EXTENSION_SECONDS",
    "Exchange",
    "ReceivedFrame",
    "await_response",
    "expected_responder",
    "split_batched_response",
    "walk_batched_response",
    "BatchWalk",
    "MAX_BATCHED_PIDS",
    "DEFAULT_REQUEST_ID",
    "DEFAULT_RESPONSE_ID_MAX",
    "DEFAULT_RESPONSE_ID_MIN",
    "FLOW_CONTROL_CTS",
    "NRC_NAMES",
    "PERMANENT_REFUSAL_NRCS",
    "RESPONSE_PENDING_NRC",
    "ObdResponse",
    "IsoTpResponseAssembler",
    "build_batched_request_payload",
    "build_request_payload",
    "extract_service_payload",
    "handle_single_frame",
    "is_negative_response",
    "negative_response_code",
    "nrc_name",
    "parse_first_frame",
    "pci_type",
    "positive_response_service",
    "single_frame_body",
]
