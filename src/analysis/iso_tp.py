"""ISO-TP (ISO 15765-2) reassembly + UDS service decoding for analysis.

Built for offline analysis of recorded `frames.log` files — not for real-time
inference. The goal is: given a candump capture, produce a per-CAN-ID stream
of fully-reassembled UDS messages (request or response, service ID, payload),
so the RE workflow can correlate them with `tags.jsonl` windows.

Scope (intentionally narrow for v1):
  * candump text format only (what python-can's CanutilsLogWriter emits).
  * Single Frame + First/Consecutive Frame reassembly.
  * Flow Control frames are recognized and skipped (we're a passive observer,
    no need to emit them).
  * UDS services we name explicitly: 0x10, 0x14, 0x19, 0x21, 0x22, 0x27,
    0x2F, 0x3E. Anything else is decoded as "unknown service" with raw bytes.
  * No multi-CAN-ID interleaving worries: each diagnostic ID has its own
    reassembly state machine.

The parser is tolerant: malformed/truncated frames are dropped with a debug
log entry rather than raising, because real captures contain the occasional
corrupt frame at adapter open/close.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

# candump line: "(<timestamp>) <channel> <ID>#<HEX> [flags]"
_CANDUMP_RE = re.compile(
    r"^\((?P<ts>\d+\.\d+)\)\s+(?P<chan>\S+)\s+(?P<id>[0-9A-Fa-f]+)#(?P<hex>[0-9A-Fa-f]+)"
)


@dataclass(frozen=True)
class Frame:
    """One CAN frame parsed from candump format."""

    timestamp: float
    channel: str
    can_id: int
    data: bytes


def parse_candump_line(line: str) -> Frame | None:
    """Parse one candump line, returning None for blanks or malformed input."""
    m = _CANDUMP_RE.match(line)
    if m is None:
        return None
    try:
        return Frame(
            timestamp=float(m["ts"]),
            channel=m["chan"],
            can_id=int(m["id"], 16),
            data=bytes.fromhex(m["hex"]),
        )
    except ValueError:
        return None


def iter_frames(path: Path) -> Iterator[Frame]:
    """Yield every parseable frame in a candump log."""
    with path.open("r") as f:
        for line in f:
            frame = parse_candump_line(line)
            if frame is not None:
                yield frame


# ---------------------------------------------------------------------------
# ISO-TP reassembly
# ---------------------------------------------------------------------------


class IsoTpFrameType(Enum):
    SINGLE = 0x0
    FIRST = 0x1
    CONSECUTIVE = 0x2
    FLOW_CONTROL = 0x3


@dataclass
class _Reassembly:
    """Per-CAN-ID state for an in-flight multi-frame message."""

    expected_total: int
    buffer: bytearray
    next_seq: int  # next consecutive-frame sequence number we expect (0-15)
    started_at: float


@dataclass(frozen=True)
class IsoTpMessage:
    """A fully-reassembled ISO-TP message on one CAN ID."""

    timestamp: float  # timestamp of the first (or only) frame
    can_id: int
    payload: bytes


def reassemble(frames: Iterable[Frame]) -> Iterator[IsoTpMessage]:
    """Stream frames through ISO-TP reassembly, yielding completed messages.

    Drops in-flight messages on sequence-error or restart (FF arriving while
    one is already in flight on the same CAN ID). Tracks state per-CAN-ID so
    interleaved ECM/TCM/etc. exchanges don't corrupt each other.
    """
    in_flight: dict[int, _Reassembly] = {}

    for frame in frames:
        if not frame.data:
            continue
        pci = frame.data[0]
        ftype = pci >> 4

        if ftype == IsoTpFrameType.SINGLE.value:
            length = pci & 0x0F
            if length == 0 or length > len(frame.data) - 1:
                continue  # malformed
            yield IsoTpMessage(frame.timestamp, frame.can_id, bytes(frame.data[1 : 1 + length]))
            in_flight.pop(frame.can_id, None)  # any prior partial is invalidated

        elif ftype == IsoTpFrameType.FIRST.value:
            if len(frame.data) < 8:
                continue
            total = ((pci & 0x0F) << 8) | frame.data[1]
            if total < 8:
                continue  # First Frame with <8 bytes total is illegal; skip
            in_flight[frame.can_id] = _Reassembly(
                expected_total=total,
                buffer=bytearray(frame.data[2:8]),
                next_seq=1,
                started_at=frame.timestamp,
            )

        elif ftype == IsoTpFrameType.CONSECUTIVE.value:
            state = in_flight.get(frame.can_id)
            if state is None:
                continue  # unsolicited CF — drop
            seq = pci & 0x0F
            if seq != (state.next_seq & 0x0F):
                # Sequence error — abandon this message; safer than emitting
                # garbage that downstream code will trust.
                in_flight.pop(frame.can_id, None)
                continue
            remaining = state.expected_total - len(state.buffer)
            chunk_len = min(7, remaining)
            state.buffer.extend(frame.data[1 : 1 + chunk_len])
            state.next_seq += 1
            if len(state.buffer) >= state.expected_total:
                yield IsoTpMessage(state.started_at, frame.can_id, bytes(state.buffer))
                in_flight.pop(frame.can_id, None)

        elif ftype == IsoTpFrameType.FLOW_CONTROL.value:
            # We're a passive observer; FC frames don't carry payload data.
            continue

        else:
            # Unknown PCI type — likely not ISO-TP traffic at all.
            continue


# ---------------------------------------------------------------------------
# UDS decoding
# ---------------------------------------------------------------------------

# Service IDs we name explicitly in output. Request SID; +0x40 = positive resp.
_UDS_SERVICES: dict[int, str] = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x21: "ReadDataByLocalIdentifier",  # KWP2000-style; common on Nissans
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3B: "WriteDataByLocalIdentifier",  # KWP2000
    0x3E: "TesterPresent",
}


@dataclass(frozen=True)
class UdsMessage:
    """A decoded UDS message at the application layer."""

    timestamp: float
    can_id: int
    is_response: bool        # True if SID >= 0x40 (positive response)
    is_negative: bool        # True if 0x7F negative response
    service_id: int          # request SID; for responses, response - 0x40
    service_name: str        # human-readable
    sub_function: int | None # first byte after SID, e.g. session type or DID hi
    did: int | None          # 16-bit data identifier when service is 0x22 / 0x62
    raw_payload: bytes       # full UDS payload (SID + body)
    body: bytes              # everything after SID (for responses, after SID + DID for 0x62)
    nrc: int | None = field(default=None)  # negative response code, if applicable

    @property
    def short(self) -> str:
        """One-line summary for display."""
        if self.is_negative:
            return (
                f"NEG {_UDS_SERVICES.get(self.service_id, f'svc 0x{self.service_id:02X}')} "
                f"NRC=0x{self.nrc:02X}"
            )
        prefix = "←" if self.is_response else "→"
        if self.did is not None:
            did_str = f" DID=0x{self.did:04X}"
            body_str = f" data={self.body.hex().upper()}" if self.body else ""
            return f"{prefix} {self.service_name}{did_str}{body_str}"
        body_str = f" body={self.body.hex().upper()}" if self.body else ""
        return f"{prefix} {self.service_name}{body_str}"


def decode_uds(message: IsoTpMessage) -> UdsMessage | None:
    """Decode one ISO-TP payload as a UDS message. Returns None if empty."""
    payload = message.payload
    if not payload:
        return None
    sid = payload[0]

    # Negative response: 0x7F <requested_sid> <NRC>
    if sid == 0x7F and len(payload) >= 3:
        return UdsMessage(
            timestamp=message.timestamp,
            can_id=message.can_id,
            is_response=True,
            is_negative=True,
            service_id=payload[1],
            service_name=_UDS_SERVICES.get(payload[1], f"svc 0x{payload[1]:02X}"),
            sub_function=None,
            did=None,
            raw_payload=payload,
            body=payload[1:],
            nrc=payload[2],
        )

    is_response = sid >= 0x40 and sid != 0x7F
    request_sid = sid - 0x40 if is_response else sid
    name = _UDS_SERVICES.get(request_sid, f"svc 0x{request_sid:02X}")

    sub = payload[1] if len(payload) >= 2 else None

    # Mode 22 (request) or 0x62 (response): DID is bytes [1:3]
    did: int | None = None
    body_start = 1
    if request_sid == 0x22 and len(payload) >= 3:
        did = (payload[1] << 8) | payload[2]
        body_start = 3
    body = payload[body_start:] if body_start < len(payload) else b""

    return UdsMessage(
        timestamp=message.timestamp,
        can_id=message.can_id,
        is_response=is_response,
        is_negative=False,
        service_id=request_sid,
        service_name=name,
        sub_function=sub,
        did=did,
        raw_payload=payload,
        body=body,
    )


def decode_session(
    frames: Iterable[Frame],
    diag_ids: set[int] | None = None,
) -> Iterator[UdsMessage]:
    """End-to-end: filter to diagnostic IDs, reassemble, decode UDS.

    `diag_ids` defaults to the SAE-standard physical addressing range
    (0x7E0–0x7EF) plus the functional broadcast ID 0x7DF.
    """
    if diag_ids is None:
        diag_ids = {0x7DF, *range(0x7E0, 0x7F0)}
    diag_frames = (f for f in frames if f.can_id in diag_ids)
    for msg in reassemble(diag_frames):
        decoded = decode_uds(msg)
        if decoded is not None:
            yield decoded
