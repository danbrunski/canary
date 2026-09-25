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
   mismatch means another tester is talking;
5. every request passes :func:`src.collection.kline.protocol.check_request`,
   the read-only allowlist;
6. EN and the pull-up are driven low **before** the tty closes, on every exit
   path, because a released GPIO line keeps its level on the Pi 5.

``sniff`` mode listens with the pull-up off and cannot transmit at all; that
is the mode for watching UpRev or a scan tool (H06). ``command`` mode is the
tester: pull-up on, wake-ups, requests.
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

    def byte_bounds(self) -> list[tuple[float, float]]:
        """``(earliest, latest)`` arrival of each byte: bytes take ``char_s``
        each on the wire, the last arrived by ``at``, the first after ``after``."""
        n = len(self.data)
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
        self._last_rx = float("-inf")
        self.char_s = 10.0 / profile.baud  # 8N1: ten bit times per byte

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
        self._require_command()
        self.port.write(frame)
        self.port.flush()
        try:
            echo = self.read_exact(len(frame), len(frame) * self.char_s + 0.05)
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
        if echo != frame:  # 4.
            raise EchoMismatchError(f"sent {frame.hex(' ')}, line carried {echo.hex(' ')}")

    def read_frame(self, kind: Literal["addressed", "length"], timeout_s: float) -> bytes:
        first = self.read_exact(1, timeout_s)
        total = (first[0] & 0x3F) + 4 if kind == "addressed" else first[0] + 2
        rest = self.read_exact(total - 1, total * self.char_s + 0.05)
        self._last_rx = self._clock()
        return first + rest

    # ------------------------------------------------------------------ protocol

    def wake(self, wakeup: Wakeup) -> float:
        """Hold the line low then release it; returns the measured low (ms)."""
        self._require_command()
        self._idle_for(IDLE_BEFORE_WAKEUP_S)
        start = self._clock()
        self.port.break_condition = True
        self._sleep(wakeup.low_ms / 1000.0)
        self.port.break_condition = False
        measured_ms = (self._clock() - start) * 1000.0
        self._sleep(wakeup.high_ms / 1000.0)
        self.port.reset_input_buffer()  # the low reads back as a break
        tolerance = max(WAKEUP_TOLERANCE_MS, wakeup.low_ms * WAKEUP_TOLERANCE_FRACTION)
        if abs(measured_ms - wakeup.low_ms) > tolerance:  # 3.
            raise WakeupTimingError(
                f"{wakeup.name}: held low {measured_ms:.1f} ms, wanted {wakeup.low_ms} "
                f"± {tolerance:.1f}"
            )
        return measured_ms

    def start_communication(self, target: int | None = None) -> kp.StartCommunicationReply:
        """Try each wake-up in profile order until ``target`` (default: the
        ECM) answers."""
        failures: list[str] = []
        address = self.profile.ecm_address if target is None else target
        request = kp.start_communication(address, self.profile.tester_address)
        for wakeup in self.profile.wakeups:
            try:
                self.wake(wakeup)
                self.send_frame(request)
                raw = self.read_frame("addressed", self.profile.timing.p2_max_ms / 1000.0)
            except (KlineTimeoutError, WakeupTimingError) as exc:
                failures.append(f"{wakeup.name}: {exc}")
                continue
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
    "EchoMismatchError",
    "HatPins",
    "KlineTimeoutError",
    "KlineTransport",
    "KlineTransportError",
    "Mode",
    "NoEchoError",
    "OutputLines",
    "RxChunk",
    "SerialPort",
    "PassiveArmedError",
    "TransmitRefusedError",
    "WakeupTimingError",
]
