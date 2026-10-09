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

**Direction on one wire.** Frames carry no direction, but their service byte
does: ISO 14230-3 answers request ``sid`` with ``sid + 0x40`` or ``7F``, and no
request id has bit 6 set (:func:`~src.collection.kline.protocol.is_response_sid`).
It used to be judged against the *pending* request instead, so a reply whose
request the sniffer missed read as a request: 189 of the ABS's replies on
2026-09-24. A reply is paired with its request.

**Which module.** Length-byte frames carry no address either, so each frame is
stamped with the module whose StartCommunication opened the session
(``module``): the addressed target, from the positive reply, until
StopCommunication, the next wake-up or the next StartCommunication. A frame
seen before any StartCommunication (the capture attached mid-session) has
``module = None``: unknown, not guessed. On 2026-09-24 the scan tool's ABS
``22 12 10`` (yaw) and UpRev's ECM ``22 12 10`` (TPS2) were otherwise the same
row.

**Wake-ups (H05.9).** A sniffing port is in mark-parity mode
(:func:`~src.collection.kline.transport.mark_breaks`), so a break arrives as
``FF 00 00``, a framing or parity error as ``FF 00 <byte>`` and a literal
``0xFF`` as ``FF FF``; :class:`FrameDecoder` un-escapes them before framing, so
a frame's bytes are exactly what they were before marking (a byte with a
framing error is still fed as data, as an unmarked port delivered it). Each
break becomes a :class:`SniffedBreak`. What it can and cannot say:

- The UART reports a break once the line has been low for a whole character
  (~1 ms at 10400 baud): ``detected`` is that report, so the falling edge was
  about one character time earlier. The release is **not** reported.
- The next byte's arrival bounds the release: low + high (falling edge to the
  next start bit) is ``span_s = next_byte - detected``, the two stamped the
  same way. How the span splits into low and high is not observable here;
  the low is only bounded, ``[char_s, span max]``.
- Both stamps carry the read-time bounds of :class:`RxChunk` and the UART's
  receive timeout (32 bit times, :data:`UART_RX_TIMEOUT_CHARS`), which can hold
  a lone character before the host sees it; ``timing_uncertainty_s`` adds them.

In an unmarked stream (every capture before H05.9) a ``0x00`` between frames is
still read as a wake-up, as before, but no break is reported: an unmarked zero
cannot be told from data, so it is not presented as a measurement.

**Definitions.** :class:`DefinitionTracker` remembers each acknowledged
``AC`` definition, so later ``21`` replies can be laid out into values —
using catalog widths, refusing a reply it cannot place rather than guessing.
A capture that starts after the definition keeps the raw reply with the
reason.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from src.collection.kline import protocol as kp
from src.collection.kline.transport import RxChunk

INTER_BYTE_GAP_S = 0.025
#: A break with no byte after it for this long is reported without one: the
#: tester gave up, or what follows is not its StartCommunication. ISO 14230's
#: slowest wake-up (5-baud) has its sync byte within W1 = 300 ms.
BREAK_FOLLOW_S = 1.0
#: The PL011's receive timeout, in characters (32 bit times): a lone character
#: can sit in the FIFO this long before the host can read it.
UART_RX_TIMEOUT_CHARS = 3.2

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
    #: The module address this frame belongs to: the other end from the
    #: tester. ``None`` when no StartCommunication has said (see above).
    module: int | None = None
    #: The tester's own address, for either direction: an addressed frame
    #: names it (source of a request, target of a reply), and length-byte
    #: frames inherit it from the StartCommunication reply, like ``module``.
    #: ``None`` when no addressed frame of this session was seen (H06.4).
    tester: int | None = None


@dataclass(frozen=True)
class SniffedBreak:
    """A wake-up low (a UART break) on the line, with what bounds its timing.

    Times are the host clock of the reads (monotonic). See the module
    docstring for what is and is not measurable.
    """

    #: Latest and earliest the UART can have reported the break.
    detected: float
    detected_earliest: float
    char_s: float
    #: The latest bound of the last byte or break before it; ``None`` if none.
    prev_activity: float | None
    #: The next byte's latest and earliest arrival; ``None`` if none came
    #: within :data:`BREAK_FOLLOW_S` (or another break came first).
    next_byte: float | None = None
    next_byte_earliest: float | None = None
    #: When another break followed before any byte (a 5-baud address byte
    #: is a run of breaks).
    next_break: float | None = None

    @property
    def span_s(self) -> float | None:
        """Falling edge to the next byte's start bit: low + high."""
        return None if self.next_byte is None else self.next_byte - self.detected

    @property
    def span_bounds_s(self) -> tuple[float, float] | None:
        if self.next_byte is None or self.next_byte_earliest is None:
            return None
        slack = UART_RX_TIMEOUT_CHARS * self.char_s
        return (
            max(self.char_s, self.next_byte_earliest - self.detected - slack),
            self.next_byte - self.detected_earliest + slack,
        )

    @property
    def low_bounds_s(self) -> tuple[float, float | None]:
        """The low is at least one character; at most the whole span."""
        bounds = self.span_bounds_s
        return (self.char_s, bounds[1] if bounds is not None else None)

    @property
    def idle_before_s(self) -> float | None:
        """Last activity to the falling edge (about one character before
        ``detected``)."""
        if self.prev_activity is None:
            return None
        return max(0.0, self.detected - self.char_s - self.prev_activity)

    @property
    def timing_uncertainty_s(self) -> float:
        """How far the span (or, with no next byte, the falling edge) can be
        from its estimate."""
        bounds = self.span_bounds_s
        span = self.span_s
        if bounds is None or span is None:
            return (
                self.detected - self.detected_earliest + (1.0 + UART_RX_TIMEOUT_CHARS) * self.char_s
            )
        return max(span - bounds[0], bounds[1] - span)


class _Unmarker:
    """PARMRK un-escaping across reads: ``FF FF`` -> byte ``FF``, ``FF 00 00``
    -> break, ``FF 00 xx`` -> byte ``xx`` with a framing/parity error."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self.malformed = 0

    def feed(self, data: bytes) -> list[tuple[Literal["byte", "break", "error"], int]]:
        out: list[tuple[Literal["byte", "break", "error"], int]] = []
        for b in data:
            pending = self._pending
            if not pending:
                if b == 0xFF:
                    pending.append(b)
                else:
                    out.append(("byte", b))
            elif len(pending) == 1:
                if b == 0xFF:
                    out.append(("byte", 0xFF))
                    pending.clear()
                elif b == 0x00:
                    pending.append(b)
                else:
                    # Not a sequence PARMRK produces: keep the bytes as data.
                    self.malformed += 1
                    out += [("byte", 0xFF), ("byte", b)]
                    pending.clear()
            else:
                out.append(("break", 0) if b == 0x00 else ("error", b))
                pending.clear()
        return out


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
    #: Marked streams only: breaks reported, and bytes with a framing or
    #: parity error (still decoded as data).
    breaks: int = 0
    framing_errors: int = 0


class FrameDecoder:
    """Streaming: feed reads (or bytes); get frames as they complete."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._started = 0.0
        self._in_session = False
        self._module: int | None = None
        self._tester: int | None = None
        self._uncertainty = 0.0
        self._unmarker = _Unmarker()
        self._last_activity: float | None = None
        self._break: SniffedBreak | None = None
        self.stats = SniffStats()

    def feed_chunk(self, chunk: RxChunk) -> list[SniffedFrame]:
        """One read's frames (breaks are dropped; :meth:`feed_chunk_events`
        keeps them)."""
        return [e for e in self.feed_chunk_events(chunk) if isinstance(e, SniffedFrame)]

    def feed_chunk_events(self, chunk: RxChunk) -> list[SniffedFrame | SniffedBreak]:
        """One read's frames and completed breaks, in line order. Only the
        silence observed before the read can separate its bytes from what
        came before; within a read, none is known."""
        events: list[tuple[Literal["byte", "break", "error"], int]]
        if chunk.marked:
            events = self._unmarker.feed(chunk.data)
        else:
            events = [("byte", b) for b in chunk.data]
        out: list[SniffedFrame | SniffedBreak] = []
        for k, ((earliest, latest), (kind, byte)) in enumerate(
            zip(chunk.char_bounds(len(events)), events, strict=True)
        ):
            if kind == "break":
                out += self._on_break(earliest, latest, chunk.char_s)
                continue
            if kind == "error":
                self.stats.framing_errors += 1
            if self._break is not None:
                out.append(replace(self._break, next_byte=latest, next_byte_earliest=earliest))
                self._break = None
            out += self.feed(
                latest,
                byte,
                quiet_s=chunk.quiet_s if k == 0 else 0.0,
                uncertainty_s=latest - earliest,
                zero_is_wakeup=not chunk.marked,
            )
            self._last_activity = latest
        return out

    def _on_break(self, earliest: float, latest: float, char_s: float) -> list[SniffedBreak]:
        out: list[SniffedBreak] = []
        if self._break is not None:
            out.append(replace(self._break, next_break=latest))
        self.stats.breaks += 1
        self._wakeup()
        self._break = SniffedBreak(
            detected=latest,
            detected_earliest=earliest,
            char_s=char_s,
            prev_activity=self._last_activity,
        )
        self._last_activity = latest
        return out

    def flush_breaks(self, now: float, *, force: bool = False) -> list[SniffedBreak]:
        """A break that no byte has followed for :data:`BREAK_FOLLOW_S` (or any,
        with ``force``, when sniffing ends), reported without a next byte."""
        pending = self._break
        if pending is None or not (force or now - pending.detected > BREAK_FOLLOW_S):
            return []
        self._break = None
        return [pending]

    def _wakeup(self) -> None:
        """A wake-up ends the frame in flight and the session."""
        if self._buf:
            self.stats.abandoned += 1
            self._buf.clear()
        self.stats.wakeups += 1
        self._in_session = False
        self._module = None
        self._tester = None

    def feed(
        self,
        at: float,
        byte: int,
        *,
        quiet_s: float,
        uncertainty_s: float = 0.0,
        zero_is_wakeup: bool = True,
    ) -> list[SniffedFrame]:
        """One byte that arrived by ``at``, after the line was seen idle for
        ``quiet_s``. ``zero_is_wakeup``: an unmarked stream, whose breaks read
        as ``0x00``; in a marked one a zero is data and breaks come separately."""
        out: list[SniffedFrame] = []
        if self._buf and quiet_s > INTER_BYTE_GAP_S:
            self.stats.abandoned += 1
            self._buf.clear()
        self._uncertainty = uncertainty_s
        if not self._buf:
            if byte == 0x00 and zero_is_wakeup:
                self.stats.wakeups += 1
                self._in_session = False
                self._module = None
                self._tester = None
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
            is_reply = kp.is_response_sid(payload[0])
            named_tester = addressed.target if is_reply else addressed.source
            if payload[:1] == bytes([kp.SID_START_COMMUNICATION + kp.POSITIVE_OFFSET]):
                self._in_session = True
                self._module = addressed.source
                self._tester = named_tester
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
                module=addressed.source if is_reply else addressed.target,
                tester=named_tester,
            )
        payload = kp.decode_length_frame(raw)
        sid = payload[0]
        is_reply = kp.is_response_sid(sid)
        module = self._module
        tester = self._tester
        if sid == kp.SID_STOP_COMMUNICATION + kp.POSITIVE_OFFSET:
            self._in_session = False
            self._module = None
            self._tester = None
        return SniffedFrame(
            self._started,
            at,
            raw,
            "length",
            payload,
            "ecu" if is_reply else "tester",
            timing_uncertainty_s=self._uncertainty,
            module=module,
            tester=tester,
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
    "BREAK_FOLLOW_S",
    "INTER_BYTE_GAP_S",
    "UART_RX_TIMEOUT_CHARS",
    "SniffedBreak",
    "DefinitionTracker",
    "Exchange",
    "FrameDecoder",
    "SniffStats",
    "SniffedFrame",
    "SniffedValues",
    "exchanges",
]
