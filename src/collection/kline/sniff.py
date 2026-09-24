"""Decode someone else's K-line session, byte by byte, as it happens.

The HAT listens with the pull-up off and never transmits (``Mode.SNIFF``);
this module turns the UART's bytes into what the other tester and the ECU
said. Proven against UpRev's session on the Xterra (2026-09-23, fixture
``tests/fixtures/kline/uprev_session_bytes.json.gz``): every frame recovered,
every checksum good.

**Framing follows the protocol's own state.** Before StartCommunication has
been answered, frames are *addressed* (format byte, target, source); after
it, this ECM uses *length-byte* frames until StopCommunication or the next
wake-up. A wake-up low pulse reaches a Linux UART as one ``0x00`` break byte;
between frames a ``0x00`` can be nothing else (no frame has length zero), so
it marks a wake-up. A byte gap longer than :data:`INTER_BYTE_GAP_S` inside a
frame abandons it (UpRev sends its first request bytes 10 ms apart, so the
limit is well above that), and a bad checksum drops one byte and resyncs.

**Direction on one wire.** Addressed frames carry their source. Length-byte
frames do not, so a frame is the *reply* when it is ``7F ...`` or when its
service byte is the pending request's plus ``0x40``; otherwise it is a
request. A reply is paired with its request.

**Definitions.** :class:`DefinitionTracker` remembers each acknowledged
``AC`` definition, so later ``21`` replies can be laid out into values —
using catalog widths, refusing a reply it cannot place rather than guessing.
A capture that starts after the definition keeps the raw reply with the
reason.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from src.collection.kline import protocol as kp

INTER_BYTE_GAP_S = 0.025

Direction = Literal["tester", "ecu"]


@dataclass(frozen=True)
class SniffedFrame:
    started: float
    finished: float
    raw: bytes
    kind: Literal["addressed", "length"]
    payload: bytes
    direction: Direction
    source: int | None = None  # addressed frames only
    target: int | None = None


@dataclass(frozen=True)
class Exchange:
    """A request and, when one came, its reply."""

    request: SniffedFrame
    reply: SniffedFrame | None


@dataclass
class SniffStats:
    frames: int = 0
    wakeups: int = 0
    checksum_resyncs: int = 0
    abandoned: int = 0


class FrameDecoder:
    """Streaming: feed ``(time, byte)``; get frames as they complete."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._started = 0.0
        self._last = 0.0
        self._in_session = False
        self._pending_request: int | None = None
        self.stats = SniffStats()

    def feed(self, at: float, byte: int) -> list[SniffedFrame]:
        out: list[SniffedFrame] = []
        if self._buf and at - self._last > INTER_BYTE_GAP_S:
            self.stats.abandoned += 1
            self._buf.clear()
        self._last = at
        if not self._buf:
            if byte == 0x00:
                self.stats.wakeups += 1
                self._in_session = False
                self._pending_request = None
                return out
            self._started = at
        self._buf.append(byte)
        while self._buf:
            total = self._expected_length()
            if total is None:
                # A zero can start no frame. Only between frames is it a
                # wake-up (handled above); here it is mid-resync data, so skip
                # it rather than wait on it forever.
                self.stats.checksum_resyncs += 1
                del self._buf[:1]
                continue
            if len(self._buf) < total:
                break
            candidate = bytes(self._buf[:total])
            if kp.checksum(candidate[:-1]) != candidate[-1]:
                self.stats.checksum_resyncs += 1
                del self._buf[:1]
                continue
            del self._buf[:total]
            out.append(self._frame(candidate, at))
            self._started = at
        return out

    def _expected_length(self) -> int | None:
        first = self._buf[0]
        if not self._in_session and first & 0xC0 in (0x80, 0xC0):
            return (first & 0x3F) + 4
        if first == 0:
            return None
        return first + 2

    def _frame(self, raw: bytes, at: float) -> SniffedFrame:
        self.stats.frames += 1
        if not self._in_session and raw[0] & 0xC0 in (0x80, 0xC0):
            addressed = kp.decode_addressed(raw)
            payload = addressed.payload
            is_reply = payload[:1] == bytes([kp.SID_START_COMMUNICATION + kp.POSITIVE_OFFSET])
            if is_reply:
                self._in_session = True
            return SniffedFrame(
                self._started,
                at,
                raw,
                "addressed",
                payload,
                "ecu" if is_reply else "tester",
                source=addressed.source,
                target=addressed.target,
            )
        payload = kp.decode_length_frame(raw)
        sid = payload[0]
        pending = self._pending_request
        is_reply = sid == kp.SID_NEGATIVE_RESPONSE or (
            pending is not None and sid == (pending + kp.POSITIVE_OFFSET) & 0xFF
        )
        if is_reply:
            self._pending_request = None
            if sid == kp.SID_STOP_COMMUNICATION + kp.POSITIVE_OFFSET:
                self._in_session = False
        else:
            self._pending_request = sid
        return SniffedFrame(
            self._started, at, raw, "length", payload, "ecu" if is_reply else "tester"
        )


def exchanges(frames: Sequence[SniffedFrame]) -> Iterator[Exchange]:
    """Pair each tester frame with the ECU frame that answered it."""
    pending: SniffedFrame | None = None
    for frame in frames:
        if frame.direction == "tester":
            if pending is not None:
                yield Exchange(pending, None)
            pending = frame
        elif pending is not None:
            yield Exchange(pending, frame)
            pending = None
    if pending is not None:
        yield Exchange(pending, None)


@dataclass(frozen=True)
class SniffedValues:
    at: float
    local_id: int
    values: tuple[tuple[kp.ValueDescriptor, bytes], ...] = ()
    #: Why the reply could not be laid out, when it could not.
    unresolved: str | None = None
    raw: bytes = b""


@dataclass
class DefinitionTracker:
    """Remembers acknowledged ``AC`` definitions and lays out ``21`` replies."""

    did_widths: Mapping[int, int]
    definitions: dict[int, tuple[kp.Descriptor, ...]] = field(default_factory=dict)

    def observe(self, exchange: Exchange) -> SniffedValues | None:
        request, reply = exchange.request, exchange.reply
        if reply is None or reply.payload[:1] == bytes([kp.SID_NEGATIVE_RESPONSE]):
            return None
        sid = request.payload[0]
        if sid == kp.SID_DEFINE_LOCAL_ID:
            local_id, descriptors = kp.parse_define_local_id(request.payload)
            self.definitions[local_id] = descriptors
            return None
        if sid != kp.SID_READ_DATA_BY_LOCAL_ID:
            return None
        local_id = request.payload[1]
        params = reply.payload[1:]
        try:
            values = kp.expand_local_id(local_id, self.definitions)
            widths = kp.value_widths(values, self.did_widths)
            split = kp.split_local_id_reply(local_id, params, widths)
        except kp.KlineProtocolError as exc:
            return SniffedValues(reply.finished, local_id, unresolved=str(exc), raw=reply.raw)
        return SniffedValues(
            reply.finished, local_id, tuple(zip(values, split, strict=True)), raw=reply.raw
        )


__all__ = [
    "INTER_BYTE_GAP_S",
    "DefinitionTracker",
    "Exchange",
    "FrameDecoder",
    "SniffStats",
    "SniffedFrame",
    "SniffedValues",
    "exchanges",
]
