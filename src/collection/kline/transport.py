"""The CANary-HAT's K-line transceiver: the serial port, EN and the pull-up.

One class owns all three, so the TLIN1027's start-up order
(``docs/CANary-HAT-Spec.md`` §11, proven on the HAT 2026-09-23) cannot be
done out of order by a caller:

1. the tty opens **before** EN (GPIO26) rises — R8 only holds TXD recessive
   once the host drives it;
2. whatever arrives across the mode change is discarded;
3. every wake-up pulse is timed with the host clock and refused if the host
   could not make it (a scheduling stall stretches a 25 ms pulse);
4. every transmitted byte must come back as echo — K-line is one wire, so a
   mismatch means another tester is talking. A module whose profile declares
   an inter-byte gap (ISO P4, :meth:`KlineProfile.p4_ms`) gets its request
   bytes one at a time, that far apart, each echo checked before the next:
   the airbag module ignores the same bytes sent back to back (2026-10-10);
5. every request passes :func:`src.collection.kline.protocol.check_request`,
   the read-only allowlist;
6. EN and the pull-up are driven low **before** the tty closes, on every exit
   path, because a released GPIO line keeps its level on the Pi 5.

``sniff`` mode listens with the pull-up off and cannot transmit at all; that
is the mode for watching UpRev or a scan tool (H06). ``command`` mode is the
tester: pull-up on, wake-ups, requests.

**Sniffing sees wake-ups (H05.9).** A wake-up pulse reaches the UART as a
break, which a tty in its default mode delivers as a bare ``0x00`` — the same
byte as a data zero — or drops. :meth:`KlineTransport.open` therefore puts a
sniffing port in mark-parity mode (:func:`mark_breaks`: ``PARMRK | INPCK`` on,
``IGNBRK``, ``BRKINT``, ``IGNPAR``, ``ISTRIP`` off) and reads the flags back,
refusing to sniff if they did not stick; every :class:`RxChunk` says whether
its bytes are marked. The kernel then reports a break as ``FF 00 00``, a
framing or parity error as ``FF 00 <byte>`` and a literal ``0xFF`` as
``FF FF``; :class:`~src.collection.kline.sniff.FrameDecoder` un-escapes them.
Command mode is unchanged (its echo checks compare raw bytes).

Every wake-up CANary sends is kept as a :class:`WakePulse` (host-measured low
and high, what the port read back, whether the module answered), so the
collector can store it beside a tester's sniffed wake-ups.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

from src.collection import tx_gate
from src.collection.kline import protocol as kp
from src.collection.kline.profile import KlineProfile, Wakeup

#: ISO 14230 W5: the bus must be idle this long before a wake-up.
IDLE_BEFORE_WAKEUP_S = 0.3
#: A wake-up whose measured low time is off by more than this is refused.
WAKEUP_TOLERANCE_MS = 2.0
WAKEUP_TOLERANCE_FRACTION = 0.02
#: TLIN1027 mode change (tMODE_CHANGE + tNOMINT is ~50 µs); generous.
MODE_CHANGE_S = 0.005
#: How long a responsePending (NRC 0x78) may keep a request open (ISO P2*).
RESPONSE_PENDING_MAX_S = 5.0
#: ISO 14230-2's longest inter-byte gap: past it a module drops the frame.
P4_MAX_S = 0.020


class KlineTransportError(RuntimeError):
    """The K-line exchange failed at the transport level."""


class KlineTimeoutError(KlineTransportError):
    pass


class EchoMismatchError(KlineTransportError):
    """What came back is not what was sent: another tester is on the line."""


class NoEchoError(KlineTransportError):
    """Nothing came back at all: the transceiver is unpowered or disabled."""


class BusBusyError(KlineTransportError):
    """The line was not idle before a wake-up."""


class WakeupTimingError(KlineTransportError):
    """The host could not produce the wake-up pulse the profile asks for."""


class ByteTimingError(KlineTransportError):
    """The host stretched a gap between request bytes past ISO P4max: the
    module drops such a frame, so silence after it says nothing about it."""


class TransmitRefusedError(KlineTransportError):
    """Sniff mode never transmits."""


class PassiveArmedError(TransmitRefusedError):
    """The operator armed passive mode: nothing of CANary's transmits, on any
    bus. Passive hands the one K-line wire to another tool (a scan tool, a
    reflash); a late StopCommunication or wake-up from CANary would garble its
    wake-up or StartCommunication. Checked on every byte CANary would drive."""


@dataclass(frozen=True)
class RxChunk:
    """The bytes one read returned, and what the reader knows of their timing.

    A read only says the bytes arrived by ``at``; a host that stopped reading
    for 300 ms gets that whole stretch of traffic back in one read. So the
    chunk carries bounds, not guesses: every byte arrived after ``after`` (the
    last read that found nothing), and the line was *observed* silent for
    ``quiet_s`` before the first of them — the only gap a sniffer may act on.
    (An empty read can miss a byte still in the UART's FIFO, which holds it for
    its ~32-bit receive timeout, ~3 ms at 10400 baud: small against the 25 ms
    the sniffer needs before it abandons a frame.)
    On 2026-09-24 inferring gaps from read times instead abandoned the frame in
    flight at each ~350 ms host stall: ~10 % of UpRev's replies.
    """

    at: float
    data: bytes
    quiet_s: float
    after: float
    char_s: float
    #: The port was in mark-parity mode (:func:`mark_breaks`): ``data`` holds
    #: ``FF 00 00`` for a break, ``FF 00 xx`` for a framing/parity error and
    #: ``FF FF`` for a literal ``0xFF``. ``False``: raw bytes, as every
    #: capture before H05.9 was read.
    marked: bool = False

    def byte_bounds(self) -> list[tuple[float, float]]:
        """``(earliest, latest)`` arrival of each byte: bytes take ``char_s``
        each on the wire, the last arrived by ``at``, the first after ``after``."""
        return self.char_bounds(len(self.data))

    def char_bounds(self, n: int) -> list[tuple[float, float]]:
        """:meth:`byte_bounds` for ``n`` wire characters: a marked chunk holds
        fewer characters than bytes (``FF 00 00`` is one break)."""
        return [
            (
                min(self.after + (k + 1) * self.char_s, self.at - (n - 1 - k) * self.char_s),
                self.at - (n - 1 - k) * self.char_s,
            )
            for k in range(n)
        ]


class Mode(Enum):
    COMMAND = "command"
    SNIFF = "sniff"


class SerialPort(Protocol):
    """The slice of ``serial.Serial`` the transport uses (timeout=0)."""

    break_condition: bool

    def write(self, data: bytes, /) -> int | None: ...

    def read(self, size: int = 1, /) -> bytes: ...

    def reset_input_buffer(self) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


class OutputLines(Protocol):
    def set(self, levels: Mapping[int, bool], /) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class HatPins:
    """Where the CANary-HAT rev 2.0 wires the transceiver."""

    tty: str = "/dev/ttyAMA0"
    gpio_chip: str = "/dev/gpiochip0"
    enable: int = 26
    pullup: int = 24


def _open_serial(tty: str, baud: int) -> SerialPort:
    import serial

    port: SerialPort = serial.Serial(tty, baud, timeout=0)
    return port


#: Input flags a sniffing port must have set, and must have clear, for a break
#: to reach the reader as ``FF 00 00`` (termios(3), "PARMRK").
MARK_SET_FLAGS = ("PARMRK", "INPCK")
MARK_CLEAR_FLAGS = ("IGNBRK", "BRKINT", "IGNPAR", "ISTRIP")


def mark_breaks(port: SerialPort) -> bool:
    """Put ``port`` in mark-parity mode and prove it took; returns ``True``.

    Raises :class:`KlineTransportError` if the port has no file descriptor or
    the flags read back wrong: a sniffer whose breaks arrive as bare zeros
    cannot tell a tester's wake-up from data, which is how the SRS wake-up
    went unmeasured until 2026-10-08. pyserial clears ``PARMRK`` whenever it
    reconfigures a port, so this runs after the port is opened and nothing
    reconfigures it afterwards.
    """
    import termios

    fileno = getattr(port, "fileno", None)
    if fileno is None:
        raise KlineTransportError("the sniff port has no file descriptor to put in PARMRK mode")
    fd = int(fileno())
    attrs = termios.tcgetattr(fd)
    for name in MARK_SET_FLAGS:
        attrs[0] |= getattr(termios, name)
    for name in MARK_CLEAR_FLAGS:
        attrs[0] &= ~getattr(termios, name)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    missing = break_marking_faults(termios.tcgetattr(fd)[0])
    if missing:
        raise KlineTransportError(
            "the sniff port refused mark-parity mode (" + ", ".join(missing) + "): breaks "
            "would read as data zeros and wake-ups would go unrecorded"
        )
    return True


def break_marking_faults(iflag: int) -> list[str]:
    """What is wrong with ``iflag`` for break marking (empty: nothing)."""
    import termios

    faults = [f"{n} clear" for n in MARK_SET_FLAGS if not iflag & getattr(termios, n)]
    faults += [f"{n} set" for n in MARK_CLEAR_FLAGS if iflag & getattr(termios, n)]
    return faults


@dataclass
class WakePulse:
    """One wake-up CANary drove, timed on the host clock.

    ``fall`` is read just before the break is asserted and ``release`` just
    after it is lifted, so ``low_ms`` includes the two ioctls (tens of µs);
    ``end`` is when the high wait ended — the first byte after it went out no
    earlier. ``readback`` is what the port returned for the pulse before the
    input was discarded (an unmarked command port reads a break as ``00``).
    """

    name: str
    wanted_low_ms: float
    wanted_high_ms: float
    fall: float
    release: float
    end: float
    readback: bytes
    char_s: float
    #: Set by :meth:`KlineTransport.start_communication`: the module it was
    #: for, and whether that module answered.
    target: int | None = None
    answered: bool | None = None
    #: Why the pulse was not used (a host timing refusal), when it was not.
    refused: str | None = None
    #: The inter-byte gap the StartCommunication after it was sent with
    #: (``0``: back to back), and the gaps the host measured between its byte
    #: writes (start to start less one character), ms.
    p4_ms: float | None = None
    byte_gaps_ms: tuple[float, ...] = ()

    @property
    def low_ms(self) -> float:
        return (self.release - self.fall) * 1000.0

    @property
    def high_ms(self) -> float:
        return (self.end - self.release) * 1000.0


def _open_lines(pins: HatPins) -> OutputLines:
    from src.collection.gpio_lines import GpioOutputs

    return GpioOutputs(
        pins.gpio_chip, {pins.enable: False, pins.pullup: False}, consumer="canary-kline"
    )


class KlineTransport:
    def __init__(
        self,
        profile: KlineProfile,
        *,
        mode: Mode,
        pins: HatPins | None = None,
        open_port: Callable[[], SerialPort] | None = None,
        open_lines: Callable[[], OutputLines] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        mark_port: Callable[[SerialPort], bool] = mark_breaks,
    ) -> None:
        self.profile = profile
        self.mode = mode
        self.pins = pins or HatPins()
        pins_ = self.pins
        self._open_port = open_port or (lambda: _open_serial(pins_.tty, profile.baud))
        self._open_lines = open_lines or (lambda: _open_lines(pins_))
        self._clock = clock
        self._sleep = sleep
        self._port: SerialPort | None = None
        self._lines: OutputLines | None = None
        self._mark_port = mark_port
        self._marked = False
        self._last_rx = float("-inf")
        self.char_s = 10.0 / profile.baud  # 8N1: ten bit times per byte
        self._pulses: list[WakePulse] = []
        #: The inter-byte gap of the module being talked to; set by
        #: :meth:`start_communication`, the ECM's until then.
        self._p4_s = profile.p4_ms(profile.ecm_address) / 1000.0
        #: Measured gaps of the last frame :meth:`send_frame` spaced (ms).
        self.last_byte_gaps_ms: tuple[float, ...] = ()

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> KlineTransport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def open(self) -> None:
        if self._port is not None:
            raise KlineTransportError("transport already open")
        port = self._open_port()  # 1. the tty first
        try:
            # A sniffer that cannot see breaks cannot see wake-ups: marking is
            # part of opening the port, not a step a caller may skip.
            self._marked = self._mark_port(port) if self.mode is Mode.SNIFF else False
            lines = self._open_lines()  # claimed low
            try:
                lines.set({self.pins.enable: True, self.pins.pullup: self.mode is Mode.COMMAND})
                self._sleep(MODE_CHANGE_S)
                port.reset_input_buffer()  # 2. discard the mode change
            except BaseException:
                self._release_lines(lines)
                raise
        except BaseException:
            port.close()
            raise
        self._port, self._lines = port, lines

    def close(self) -> None:
        port, lines = self._port, self._lines
        self._port = self._lines = None
        try:
            if lines is not None:
                self._release_lines(lines)  # 6. EN and pull-up low first
        finally:
            if port is not None:
                port.close()

    def _release_lines(self, lines: OutputLines) -> None:
        try:
            lines.set({self.pins.enable: False, self.pins.pullup: False})
        finally:
            lines.close()

    @property
    def port(self) -> SerialPort:
        if self._port is None:
            raise KlineTransportError("transport is not open")
        return self._port

    def _require_command(self) -> None:
        if self.mode is not Mode.COMMAND:
            raise TransmitRefusedError("sniff mode never transmits")
        if tx_gate.passive_armed():
            raise PassiveArmedError(
                "passive mode is armed: the K-line wire belongs to another tool, so "
                "nothing is sent (not even StopCommunication)"
            )

    # ------------------------------------------------------------------ bytes

    def read_exact(self, count: int, timeout_s: float) -> bytes:
        buf = bytearray()
        deadline = self._clock() + timeout_s
        while len(buf) < count:
            chunk = self.port.read(count - len(buf))
            if chunk:
                buf += chunk
                continue
            if self._clock() > deadline:
                raise KlineTimeoutError(f"{len(buf)} of {count} bytes within {timeout_s:.3f} s")
            self._sleep(0.001)
        return bytes(buf)

    def listen(
        self,
        until: Callable[[], bool],
        sink: Callable[[RxChunk], None],
        *,
        poll_s: float = 0.001,
    ) -> None:
        """Hand every byte that arrives to ``sink`` until ``until()`` is true.
        For sniffing: never transmits. Run it on a thread of its own — a caller
        that stops reading (a busy event loop, a saturated executor) does not
        lose bytes, the tty buffers them, but it loses the knowledge of *when*
        they came, and each :class:`RxChunk` says how much of that it kept."""
        last_data = self._clock()
        seen_idle = last_data  # the last read that returned nothing
        while not until():
            chunk = self.port.read(4096)
            now = self._clock()
            if not chunk:
                seen_idle = now
                self._sleep(poll_s)
                continue
            sink(
                RxChunk(
                    at=now,
                    data=bytes(chunk),
                    quiet_s=max(0.0, seen_idle - last_data),
                    after=seen_idle,
                    char_s=self.char_s,
                    marked=self._marked,
                )
            )
            last_data = seen_idle = now

    def _idle_for(self, seconds: float) -> None:
        deadline = self._clock() + seconds
        while self._clock() < deadline:
            if self.port.read(64):
                raise BusBusyError("K-line traffic while waiting for an idle bus")
            self._sleep(0.002)

    def send_frame(self, frame: bytes) -> None:
        """Put ``frame`` on the line and check its echo. Back to back in one
        write when the current module's gap is zero (the ECM); otherwise byte
        by byte, each written that gap after the last one's end and echoed
        before the next goes."""
        self._require_command()
        self.last_byte_gaps_ms = ()
        if self._p4_s <= 0.0:
            self.port.write(frame)
            self.port.flush()
            echo = self._read_echo(frame, len(frame))
            if echo != frame:  # 4.
                raise EchoMismatchError(f"sent {frame.hex(' ')}, line carried {echo.hex(' ')}")
            return
        step = self.char_s + self._p4_s  # one byte start to the next
        gaps: list[float] = []
        previous: float | None = None
        for k, byte in enumerate(frame):
            if previous is not None:
                wait = previous + step - self._clock()
                if wait > 0:
                    self._sleep(wait)
            written = self._clock()
            if previous is not None:
                gaps.append((written - previous - self.char_s) * 1000.0)
                self.last_byte_gaps_ms = tuple(gaps)
            previous = written
            self.port.write(bytes([byte]))
            self.port.flush()
            echo = self._read_echo(frame, 1)
            if echo[0] != byte:  # 4., per byte
                raise EchoMismatchError(
                    f"sent {frame.hex(' ')}, byte {k} {byte:02x} came back as {echo.hex(' ')}"
                )
        if gaps and max(gaps) > P4_MAX_S * 1000.0:
            raise ByteTimingError(
                f"sent {frame.hex(' ')} {self._p4_s * 1000.0:g} ms apart, but the host "
                f"stretched a gap to {max(gaps):.1f} ms (ISO P4max {P4_MAX_S * 1000.0:g})"
            )

    def _read_echo(self, frame: bytes, count: int) -> bytes:
        try:
            return self.read_exact(count, count * self.char_s + 0.05)
        except KlineTimeoutError as exc:
            if not self.port.read(1):
                # Nothing at all came back: the transceiver is not driving the
                # line. Measured 2026-09-23 with the OBD plug out: no +12 V on
                # J2 means no VSUP, and the TLIN1027 is silent.
                raise NoEchoError(
                    f"sent {frame.hex(' ')} and nothing came back: the transceiver is not "
                    "driving K-line (no +12 V at J2, i.e. the OBD plug is out?)"
                ) from exc
            raise

    def read_frame(self, kind: Literal["addressed", "length"], timeout_s: float) -> bytes:
        first = self.read_exact(1, timeout_s)
        total = (first[0] & 0x3F) + 4 if kind == "addressed" else first[0] + 2
        rest = self.read_exact(total - 1, total * self.char_s + 0.05)
        self._last_rx = self._clock()
        return first + rest

    # ------------------------------------------------------------------ protocol

    def wake(self, wakeup: Wakeup) -> float:
        """Hold the line low then release it; returns the measured low (ms).
        Every pulse that reached the wire is kept (:meth:`take_wake_pulses`),
        a refused one included."""
        self._require_command()
        self._idle_for(IDLE_BEFORE_WAKEUP_S)
        start = self._clock()
        self.port.break_condition = True
        self._sleep(wakeup.low_ms / 1000.0)
        self.port.break_condition = False
        release = self._clock()
        measured_ms = (release - start) * 1000.0
        self._sleep(wakeup.high_ms / 1000.0)
        end = self._clock()
        readback = self.port.read(4096)  # kept: what the HAT heard of its own pulse
        self.port.reset_input_buffer()  # the low reads back as a break
        pulse = WakePulse(
            name=wakeup.name,
            wanted_low_ms=float(wakeup.low_ms),
            wanted_high_ms=float(wakeup.high_ms),
            fall=start,
            release=release,
            end=end,
            readback=bytes(readback),
            char_s=self.char_s,
        )
        self._pulses.append(pulse)
        tolerance = max(WAKEUP_TOLERANCE_MS, wakeup.low_ms * WAKEUP_TOLERANCE_FRACTION)
        if abs(measured_ms - wakeup.low_ms) > tolerance:  # 3.
            pulse.refused = "host timing"
            raise WakeupTimingError(
                f"{wakeup.name}: held low {measured_ms:.1f} ms, wanted {wakeup.low_ms} "
                f"± {tolerance:.1f}"
            )
        return measured_ms

    def take_wake_pulses(self) -> list[WakePulse]:
        """The wake-ups sent since the last call, oldest first."""
        pulses, self._pulses = self._pulses, []
        return pulses

    def start_communication(self, target: int | None = None) -> kp.StartCommunicationReply:
        """Try each wake-up in profile order until ``target`` (default: the
        ECM) answers. Its bytes, and every request of the session after it,
        are spaced by ``target``'s inter-byte gap (:meth:`KlineProfile.p4_ms`)."""
        failures: list[str] = []
        address = self.profile.ecm_address if target is None else target
        request = kp.start_communication(address, self.profile.tester_address)
        self._p4_s = self.profile.p4_ms(address) / 1000.0
        for wakeup in self.profile.wakeups:
            sent = len(self._pulses)
            try:
                self.wake(wakeup)
                self.send_frame(request)
                raw = self.read_frame("addressed", self.profile.timing.p2_max_ms / 1000.0)
            except (KlineTimeoutError, WakeupTimingError, ByteTimingError) as exc:
                failures.append(f"{wakeup.name}: {exc}")
                self._note_pulse(sent, address, answered=False)
                continue
            self._note_pulse(sent, address, answered=True)
            reply = kp.parse_start_communication_reply(raw)
            if reply.target != self.profile.tester_address:
                raise KlineTransportError(
                    f"StartCommunication answered to 0x{reply.target:02x}, "
                    f"not this tester 0x{self.profile.tester_address:02x}"
                )
            if reply.source != address:
                raise KlineTransportError(
                    f"StartCommunication to 0x{address:02x} answered by 0x{reply.source:02x}"
                )
            return reply
        raise KlineTimeoutError("no StartCommunication reply: " + "; ".join(failures))

    def _note_pulse(self, sent: int, target: int, *, answered: bool) -> None:
        if len(self._pulses) > sent:
            pulse = self._pulses[-1]
            pulse.target = target
            pulse.answered = answered
            if pulse.refused is None:  # the StartCommunication went out
                pulse.p4_ms = self._p4_s * 1000.0
                pulse.byte_gaps_ms = self.last_byte_gaps_ms

    def request(self, payload: bytes) -> bytes:
        """Send one request after StartCommunication; return the positive
        reply's parameters or raise :class:`kp.NegativeResponseError`."""
        kp.check_request(payload)  # 5.
        if self.profile.frame_format != "length_byte":
            raise KlineTransportError(f"frame format {self.profile.frame_format!r} not built yet")
        wait = self._last_rx + self.profile.timing.p3_min_ms / 1000.0 - self._clock()
        if wait > 0:
            self._sleep(wait)
        self.send_frame(kp.encode_length_frame(payload))
        deadline = self._clock() + RESPONSE_PENDING_MAX_S
        while True:
            raw = self.read_frame("length", self.profile.timing.p2_max_ms / 1000.0)
            reply = kp.decode_length_frame(raw)
            pending = reply[:1] == bytes([kp.SID_NEGATIVE_RESPONSE]) and reply[2:3] == b"\x78"
            if not pending:
                return kp.expect_positive(payload[0], reply)
            if self._clock() > deadline:
                raise KlineTimeoutError("ECU kept answering responsePending")

    def stop_communication(self) -> None:
        self.request(bytes([kp.SID_STOP_COMMUNICATION]))

    def tester_present(self) -> None:
        """Keep the session open across a pause longer than P3max allows."""
        self.request(bytes([kp.SID_TESTER_PRESENT]))


__all__ = [
    "BusBusyError",
    "ByteTimingError",
    "EchoMismatchError",
    "HatPins",
    "KlineTimeoutError",
    "KlineTransport",
    "KlineTransportError",
    "MARK_CLEAR_FLAGS",
    "MARK_SET_FLAGS",
    "Mode",
    "NoEchoError",
    "OutputLines",
    "P4_MAX_S",
    "RxChunk",
    "SerialPort",
    "PassiveArmedError",
    "TransmitRefusedError",
    "WakePulse",
    "WakeupTimingError",
    "break_marking_faults",
    "mark_breaks",
]
