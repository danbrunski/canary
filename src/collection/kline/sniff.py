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
it marks a wake-up. A silence longer than :data:`INTER_BYTE_GAP_S` inside a frame
abandons it (UpRev sends its first request bytes 10 ms apart, so the limit is
well above that), and a bad checksum drops one byte and resyncs.

**Only an observed silence counts.** The host sees bytes when it reads them,
not when they crossed the wire, so a gap between two reads may be the host's
own stall. :class:`~src.collection.kline.transport.RxChunk` carries how long
the reader actually watched the line sit idle (``quiet_s``), and that is the
gap this decoder acts on. Measured between read times instead, every ~350 ms
host stall on 2026-09-24 abandoned the frame in flight — 62 of UpRev's ~650
logging replies, and the definitions of its third start — and the resync
after it twice assembled a "frame" out of pieces whose checksum happened to
match. Each frame also says how well its time is known
(``timing_uncertainty_s``): traffic that waited out a stall arrives in one
read, and its frames are stamped at the latest they can have ended.

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
from src.collection.kline.transport import RxChunk

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
    #: ``finished`` is the latest the frame can have ended; it ended no
    #: earlier than ``finished - timing_uncertainty_s``.
    timing_uncertainty_s: float = 0.0


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
    """Streaming: feed reads (or bytes); get frames as they complete."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._started = 0.0
        self._in_session = False
        self._pending_request: int | None = None
        self._uncertainty = 0.0
        self.stats = SniffStats()

    def feed_chunk(self, chunk: RxChunk) -> list[SniffedFrame]:
        """One read's bytes. Only the silence observed before the read can
        separate them from what came before; within a read, none is known."""
        out: list[SniffedFrame] = []
        for k, ((earliest, latest), byte) in enumerate(
            zip(chunk.byte_bounds(), chunk.data, strict=True)
        ):
            out += self.feed(
                latest,
                byte,
                quiet_s=chunk.quiet_s if k == 0 else 0.0,
                uncertainty_s=latest - earliest,
            )
        return out

    def feed(
        self, at: float, byte: int, *, quiet_s: float, uncertainty_s: float = 0.0
    ) -> list[SniffedFrame]:
        """One byte that arrived by ``at``, after the line was seen idle for
        ``quiet_s``."""
        out: list[SniffedFrame] = []
        if self._buf and quiet_s > INTER_BYTE_GAP_S:
            self.stats.abandoned += 1
            self._buf.clear()
        self._uncertainty = uncertainty_s
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
            if first & 0x3F == 0:
                # ISO 14230's separate length byte, which neither UpRev nor
                # this ECM uses; met here only as resync data. Taken as a
                # 4-byte frame whose checksum matched by chance, it once
                # crashed the sniffer (decode_addressed refuses it).
                return None
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
                timing_uncertainty_s=self._uncertainty,
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
            self._started,
            at,
            raw,
            "length",
            payload,
            "ecu" if is_reply else "tester",
            timing_uncertainty_s=self._uncertainty,
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
