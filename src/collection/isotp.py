"""Shared ISO-TP helpers for Canable-native OBD requests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

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
    readings: dict[int, bytes] = {}
    index = 0
    while index < len(payload):
        pid = payload[index]
        width = byte_length_for(pid)
        if width is None or width <= 0:
            return readings, False
        start, end = index + 1, index + 1 + width
        if end > len(payload):
            return readings, False
        readings.setdefault(pid, payload[start:end])
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


__all__ = [
    "split_batched_response",
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
