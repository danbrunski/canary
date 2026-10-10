"""K-line transport: the TLIN1027 order, echo, wake-up timing, requests.

A fake port echoes what is written (K-line is one wire) and a scripted ECU
answers with the frames captured on the Xterra on 2026-09-23. Time is virtual,
so wake-up and P3 timing are exact and the tests are instant.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from src.collection.kline import protocol as kp
from src.collection.kline.profile import load_kline_profile
from src.collection.kline.transport import (
    P4_MAX_S,
    BusBusyError,
    EchoMismatchError,
    HatPins,
    KlineTimeoutError,
    KlineTransport,
    KlineTransportError,
    Mode,
    NoEchoError,
    RxChunk,
    TransmitRefusedError,
    WakeupTimingError,
    _open_serial,
    break_marking_faults,
    mark_breaks,
)

PROFILE = load_kline_profile(Path("data/nissan-xterra-2008/kline.yaml"))
FIXTURE = Path(__file__).parent / "fixtures" / "kline" / "uprev_session_frames.json"
F = {k: bytes.fromhex(v) for k, v in json.loads(FIXTURE.read_text())["frames"].items()}
PINS = HatPins()
EN, PU = PINS.enable, PINS.pullup


class World:
    def __init__(self) -> None:
        self.t = 0.0
        self.log: list[tuple[object, ...]] = []
        self.overshoot = 0.0  # extra seconds a sleep of >= 20 ms takes

    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds + (self.overshoot if seconds >= 0.02 else 0.0)


Ecu = Callable[[bytes], bytes | None]


def xterra(*, answer_wakeup: int = 0) -> Ecu:
    """Answers like the Xterra ECM; ignores StartCommunication until the
    ``answer_wakeup``-th attempt (0 = the first)."""
    state = {"starts": 0}
    replies = {
        F["read_did"]: F["native_read_reply"],
        F["native_stop"]: F["native_stop_reply"],
    }

    def ecu(frame: bytes) -> bytes | None:
        if frame == F["start_communication"]:
            state["starts"] += 1
            return F["start_communication_reply"] if state["starts"] > answer_wakeup else None
        return replies.get(frame)

    return ecu


class FakePort:
    def __init__(self, world: World, ecu: Ecu | None = None, *, corrupt_echo: bool = False):
        self.world = world
        self.ecu = ecu
        self.corrupt_echo = corrupt_echo
        self.rx = bytearray()
        self._break = False

    @property
    def break_condition(self) -> bool:
        return self._break

    @break_condition.setter
    def break_condition(self, value: bool) -> None:
        self._break = value
        self.world.log.append(("break", value))

    def write(self, data: bytes, /) -> int:
        self.world.log.append(("write", bytes(data), self.world.t))
        echo = bytearray(data)
        if self.corrupt_echo:
            echo[-1] ^= 0xFF
        self.rx += echo
        reply = self.ecu(bytes(data)) if self.ecu else None
        if reply:
            self.rx += reply
        return len(data)

    def read(self, size: int = 1, /) -> bytes:
        out = bytes(self.rx[:size])
        del self.rx[:size]
        return out

    def reset_input_buffer(self) -> None:
        self.world.log.append(("reset_input",))
        self.rx.clear()

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.world.log.append(("port_close",))


class FakeLines:
    def __init__(self, world: World) -> None:
        self.world = world
        world.log.append(("lines_claimed_low",))

    def set(self, levels: Mapping[int, bool], /) -> None:
        self.world.log.append(("lines", dict(levels)))

    def close(self) -> None:
        self.world.log.append(("lines_close",))


def make(
    world: World,
    *,
    mode: Mode = Mode.COMMAND,
    ecu: Ecu | None = None,
    port: FakePort | None = None,
) -> KlineTransport:
    the_port = port or FakePort(world, ecu)

    def mark_port(port: object) -> bool:
        world.log.append(("mark_breaks",))
        return True

    def open_port() -> FakePort:
        world.log.append(("port_open",))
        return the_port

    return KlineTransport(
        PROFILE,
        mode=mode,
        open_port=open_port,
        open_lines=lambda: FakeLines(world),
        clock=world.clock,
        sleep=world.sleep,
        mark_port=mark_port,
    )


def test_open_raises_en_after_the_tty_and_close_drops_it_before() -> None:
    world = World()
    with make(world):
        pass
    assert world.log == [
        ("port_open",),
        ("lines_claimed_low",),
        ("lines", {EN: True, PU: True}),
        ("reset_input",),
        ("lines", {EN: False, PU: False}),
        ("lines_close",),
        ("port_close",),
    ]


def test_sniff_mode_leaves_the_pullup_off_and_cannot_transmit() -> None:
    world = World()
    with make(world, mode=Mode.SNIFF) as transport:
        assert ("lines", {EN: True, PU: False}) in world.log
        with pytest.raises(TransmitRefusedError):
            transport.send_frame(b"\x01\x82\x83")
        with pytest.raises(TransmitRefusedError):
            transport.wake(PROFILE.wakeups[0])
    assert not [e for e in world.log if e[0] in ("write", "break")]


def test_lines_are_low_before_the_port_closes_when_the_body_raises() -> None:
    world = World()
    with pytest.raises(RuntimeError, match="boom"), make(world):
        raise RuntimeError("boom")
    assert world.log[-3:] == [("lines", {EN: False, PU: False}), ("lines_close",), ("port_close",)]


def test_a_failure_while_raising_en_releases_everything() -> None:
    world = World()

    class Broken(FakeLines):
        def set(self, levels: Mapping[int, bool], /) -> None:
            if levels.get(EN):
                raise OSError("EBUSY")
            super().set(levels)

    port = FakePort(world)
    transport = KlineTransport(
        PROFILE,
        mode=Mode.COMMAND,
        open_port=lambda: port,
        open_lines=lambda: Broken(world),
        clock=world.clock,
        sleep=world.sleep,
    )
    with pytest.raises(OSError, match="EBUSY"):
        transport.open()
    assert world.log[-3:] == [("lines", {EN: False, PU: False}), ("lines_close",), ("port_close",)]


def test_start_communication_answers_to_the_long_wakeup_byte_exact() -> None:
    world = World()
    with make(world, ecu=xterra()) as transport:
        reply = transport.start_communication()
    assert reply.key_bytes == bytes(PROFILE.key_bytes)
    writes = [e[1] for e in world.log if e[0] == "write"]
    assert writes == [F["start_communication"]]
    breaks = [e[1] for e in world.log if e[0] == "break"]
    assert breaks == [True, False]


def test_start_communication_falls_back_through_the_wakeups_in_order() -> None:
    world = World()
    with make(world, ecu=xterra(answer_wakeup=1)) as transport:
        transport.start_communication()
    assert [e[1] for e in world.log if e[0] == "break"] == [True, False, True, False]


def test_no_answer_to_any_wakeup_names_each_attempt() -> None:
    world = World()
    with (
        make(world, ecu=lambda _f: None) as transport,
        pytest.raises(KlineTimeoutError, match="long-low.*iso14230-fast"),
    ):
        transport.start_communication()


def test_a_wakeup_the_host_could_not_time_is_refused() -> None:
    world = World()
    world.overshoot = 0.05  # a 50 ms scheduling stall on every long sleep
    with make(world, ecu=xterra()) as transport, pytest.raises(WakeupTimingError, match="held low"):
        transport.wake(PROFILE.wakeups[0])


def test_traffic_before_a_wakeup_means_the_bus_is_busy() -> None:
    world = World()
    port = FakePort(world)
    with make(world, port=port) as transport:
        port.rx += b"\x04\x21\x83\x04\x01\xad"  # someone else polling
        with pytest.raises(BusBusyError):
            transport.wake(PROFILE.wakeups[0])


def test_the_known_answer_read_and_stop() -> None:
    world = World()
    with make(world, ecu=xterra()) as transport:
        transport.start_communication()
        (answer,) = PROFILE.known_answers
        params = transport.request(answer.request_bytes())
        transport.stop_communication()
    assert params[:2] == b"\x11\x03" and len(params) == 3
    writes = [e[1] for e in world.log if e[0] == "write"]
    assert writes == [F["start_communication"], F["read_did"], F["native_stop"]]


def test_requests_wait_p3_min_after_the_last_reply() -> None:
    world = World()
    with make(world, ecu=xterra()) as transport:
        transport.start_communication()
        replied_at = world.t  # the reply was read just now
        transport.request(PROFILE.known_answers[0].request_bytes())
    sent_at = [e[2] for e in world.log if e[0] == "write" and e[1] == F["read_did"]]
    assert sent_at == [pytest.approx(replied_at + PROFILE.timing.p3_min_ms / 1000.0, abs=1e-3)]


def test_an_echo_that_differs_is_another_tester() -> None:
    world = World()
    port = FakePort(world, xterra(), corrupt_echo=True)
    with (
        make(world, port=port) as transport,
        pytest.raises(EchoMismatchError, match="line carried"),
    ):
        transport.send_frame(F["native_stop"])


def test_a_write_service_is_refused_before_anything_is_sent() -> None:
    world = World()
    with make(world, ecu=xterra()) as transport:
        transport.start_communication()
        before = len([e for e in world.log if e[0] == "write"])
        with pytest.raises(kp.ServiceRefusedError):
            transport.request(bytes([0x3B, 0x01, 0x00]))
        assert len([e for e in world.log if e[0] == "write"]) == before


def test_response_pending_is_waited_through() -> None:
    world = World()
    pending = kp.encode_length_frame(bytes([0x7F, 0x22, 0x78]))

    def ecu(frame: bytes) -> bytes | None:
        if frame == F["start_communication"]:
            return F["start_communication_reply"]
        if frame == F["read_did"]:
            return pending + F["native_read_reply"]
        return None

    with make(world, ecu=ecu) as transport:
        transport.start_communication()
        assert transport.request(F["read_did"][1:-1]) == b"\x11\x03\x99"


def test_a_negative_response_names_the_code() -> None:
    world = World()

    def ecu(frame: bytes) -> bytes | None:
        if frame == F["start_communication"]:
            return F["start_communication_reply"]
        return kp.encode_length_frame(bytes([0x7F, 0x22, 0x31]))

    with make(world, ecu=ecu) as transport:
        transport.start_communication()
        with pytest.raises(kp.NegativeResponseError, match="requestOutOfRange"):
            transport.request(kp.read_did(0x1FFF, PROFILE.trailer()))


def test_no_echo_at_all_names_the_unpowered_transceiver() -> None:
    """What the HAT did on 2026-09-23 with the OBD plug out: silence."""
    world = World()

    class Silent(FakePort):
        def write(self, data: bytes, /) -> int:
            self.world.log.append(("write", bytes(data), self.world.t))
            return len(data)

    with make(world, port=Silent(world)) as transport, pytest.raises(NoEchoError, match="12 V"):
        transport.send_frame(F["native_stop"])


def _abs() -> Ecu:
    start = kp.start_communication(0x28, PROFILE.tester_address)
    replies = {
        start: bytes.fromhex("83 fc 28 c1 6d 8f 64"),
        kp.encode_length_frame(b"\x3e"): kp.encode_length_frame(b"\x7e"),
    }
    return replies.get


def test_start_communication_addresses_the_module_asked_for() -> None:
    """K01.5: the ABS at 0x28, bytes as the scan tool sent them on 2026-09-24."""
    world = World()
    with make(world, ecu=_abs()) as transport:
        reply = transport.start_communication(0x28)
        transport.tester_present()
    assert (reply.source, reply.key_bytes) == (0x28, b"\x6d\x8f")
    writes = [e[1] for e in world.log if e[0] == "write"]
    assert writes == [bytes.fromhex("81 28 fc 81 26"), kp.encode_length_frame(b"\x3e")]


def test_a_start_answered_by_another_module_is_refused() -> None:
    world = World()
    with (
        make(world, ecu=xterra()) as transport,
        pytest.raises(KlineTimeoutError),
    ):
        transport.start_communication(0x28)  # the ECM does not answer for the ABS
    start_28 = kp.start_communication(0x28, PROFILE.tester_address)
    with (
        make(World(), ecu=lambda f: F["start_communication_reply"] if f == start_28 else None) as t,
        pytest.raises(KlineTransportError, match="to 0x28 answered by 0x10"),
    ):
        t.start_communication(0x28)


def test_armed_passive_refuses_every_byte_by_name() -> None:
    """Passive armed means nothing of CANary's transmits: no wake-up, no 81,
    no request, no StopCommunication; the transport refuses before the port."""
    from src.collection import tx_gate
    from src.collection.kline.transport import PassiveArmedError

    world = World()
    with make(world, ecu=xterra()) as transport:
        transport.start_communication()
        tx_gate.set_passive(True, source="test")
        written = len([e for e in world.log if e[0] in ("write", "break")])
        (answer,) = PROFILE.known_answers
        for send in (
            lambda: transport.request(answer.request_bytes()),
            transport.stop_communication,
            transport.tester_present,
            lambda: transport.wake(PROFILE.wakeups[0]),
            transport.start_communication,
        ):
            with pytest.raises(PassiveArmedError, match="passive mode is armed"):
                send()
        assert len([e for e in world.log if e[0] in ("write", "break")]) == written


# --------------------------------------------------------------------------- breaks (H05.9)


def test_sniff_mode_marks_breaks_before_en_rises_and_command_mode_does_not() -> None:
    world = World()
    with make(world, mode=Mode.SNIFF):
        pass
    assert world.log[:3] == [("port_open",), ("mark_breaks",), ("lines_claimed_low",)]
    world = World()
    with make(world):
        pass
    assert ("mark_breaks",) not in world.log


def test_a_sniff_chunk_says_its_bytes_are_marked() -> None:
    world = World()
    port = FakePort(world)
    chunks: list[RxChunk] = []
    with make(world, mode=Mode.SNIFF, port=port) as transport:
        port.rx += b"\xff\x00\x00"  # after open, which discards the mode change
        transport.listen(lambda: bool(chunks) or world.t > 1.0, chunks.append)
    assert chunks[0].marked and chunks[0].data == b"\xff\x00\x00"


def _pty() -> tuple[int, int, str]:
    master, slave = os.openpty()
    return master, slave, os.ttyname(slave)


def test_the_real_sniff_port_reads_marked_bytes_and_says_so() -> None:
    """End to end on a pseudo-terminal: the default ``mark_breaks`` sets the
    flags through termios, they read back, and a literal 0xFF arrives doubled."""
    serial = pytest.importorskip("serial")
    termios = pytest.importorskip("termios")
    master, slave, name = _pty()
    try:
        port = serial.Serial(name, 10400, timeout=0)
        world = World()
        transport = KlineTransport(
            PROFILE,
            mode=Mode.SNIFF,
            open_port=lambda: port,
            open_lines=lambda: FakeLines(world),
        )
        with transport:
            iflag = termios.tcgetattr(port.fileno())[0]
            assert break_marking_faults(iflag) == []
            os.write(master, b"\xff\x81")
            chunks: list[RxChunk] = []
            deadline = time.monotonic() + 2.0
            transport.listen(
                lambda: sum(len(c.data) for c in chunks) >= 3 or time.monotonic() > deadline,
                chunks.append,
            )
        assert b"".join(c.data for c in chunks) == b"\xff\xff\x81"
        assert all(c.marked for c in chunks)
    finally:
        os.close(master)
        os.close(slave)


def test_pyserial_alone_does_not_mark_breaks_so_the_transport_must() -> None:
    """Why marking lives in ``KlineTransport.open``: a port pyserial opened has
    PARMRK clear, so a sniffer that skipped the step would see breaks as data."""
    termios = pytest.importorskip("termios")
    pytest.importorskip("serial")
    master, slave, name = _pty()
    try:
        port = _open_serial(name, 10400)
        try:
            iflag = termios.tcgetattr(port.fileno())[0]  # type: ignore[attr-defined]
            assert "PARMRK clear" in break_marking_faults(iflag)
        finally:
            port.close()
    finally:
        os.close(master)
        os.close(slave)


def test_mark_breaks_refuses_a_port_it_cannot_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    termios = pytest.importorskip("termios")
    serial = pytest.importorskip("serial")
    with pytest.raises(KlineTransportError, match="no file descriptor"):
        mark_breaks(FakePort(World()))
    master, slave, name = _pty()
    try:
        port = serial.Serial(name, 10400, timeout=0)
        monkeypatch.setattr(termios, "tcsetattr", lambda *_args: None)
        with pytest.raises(KlineTransportError, match="PARMRK clear"):
            mark_breaks(port)
        port.close()
    finally:
        os.close(master)
        os.close(slave)


class _BreakEchoPort(FakePort):
    """Reads its own wake-up back as an unmarked port does: one 0x00."""

    @property
    def break_condition(self) -> bool:
        return self._break

    @break_condition.setter
    def break_condition(self, value: bool) -> None:
        if self._break and not value:
            self.rx += b"\x00"
        self._break = value
        self.world.log.append(("break", value))


def test_every_wake_up_canary_sends_is_kept_with_its_measured_low_and_high() -> None:
    world = World()
    port = _BreakEchoPort(world, xterra(answer_wakeup=1))
    with make(world, port=port) as transport:
        transport.start_communication(0x10)
        first, second = transport.take_wake_pulses()
        assert transport.take_wake_pulses() == []
    long_low, fast = PROFILE.wakeups[0], PROFILE.wakeups[1]
    assert (first.name, first.target, first.answered) == (long_low.name, 0x10, False)
    assert (second.name, second.target, second.answered) == (fast.name, 0x10, True)
    assert second.low_ms == pytest.approx(fast.low_ms)
    assert second.high_ms == pytest.approx(fast.high_ms)
    assert first.readback == b"\x00" and first.refused is None


def test_a_refused_wake_up_is_still_kept() -> None:
    world = World()
    world.overshoot = 0.05
    with make(world, ecu=xterra()) as transport:
        with pytest.raises(WakeupTimingError):
            transport.wake(PROFILE.wakeups[0])
        (pulse,) = transport.take_wake_pulses()
    assert pulse.refused == "host timing" and pulse.low_ms > PROFILE.wakeups[0].low_ms


# ------------------------------------------------------- per-module P4 (2026-10-10)

SRS = 0x58
SRS_START = bytes.fromhex("81 58 fc 81 56")
SRS_START_REPLY = bytes.fromhex("83 fc 58 c1 6d 8f 94")
CHAR_S = 10.0 / PROFILE.baud


class SpacedModulePort(FakePort):
    """The airbag module as sniffed on 2026-10-10: it answers a frame only
    when its bytes came at least ``min_gap_s`` apart (the scan tool's were
    15 ms apart, CANary's back to back went unanswered), and drops one with a
    gap past ISO P4max. Bytes are assembled into frames across writes, so a
    frame written in one call is back to back."""

    def __init__(
        self,
        world: World,
        replies: Mapping[bytes, bytes],
        *,
        min_gap_s: float = 0.010,
        corrupt_byte: int | None = None,
    ) -> None:
        super().__init__(world)
        self.replies = dict(replies)
        self.min_gap_s = min_gap_s
        self.corrupt_byte = corrupt_byte
        self._frame = bytearray()
        self._at: list[float] = []
        self.heard: list[tuple[bytes, tuple[float, ...]]] = []

    def write(self, data: bytes, /) -> int:
        self.world.log.append(("write", bytes(data), self.world.t))
        for byte in data:
            k = len(self._frame)
            self.rx.append(byte ^ 0xFF if k == self.corrupt_byte else byte)
            self._frame.append(byte)
            self._at.append(self.world.t + k * CHAR_S if len(data) > 1 else self.world.t)
        first = self._frame[0]
        total = (first & 0x3F) + 4 if first & 0xC0 == 0x80 else first + 2
        if len(self._frame) >= total:
            frame = bytes(self._frame)
            gaps = tuple(b - a - CHAR_S for a, b in zip(self._at, self._at[1:], strict=False))
            self.heard.append((frame, gaps))
            self._frame.clear()
            self._at.clear()
            spaced = self.min_gap_s - 1e-9 <= min(gaps) and max(gaps) <= P4_MAX_S
            if frame in self.replies and spaced:
                self.rx += self.replies[frame]
        return len(data)


def _srs_port(world: World, **kwargs: object) -> SpacedModulePort:
    return SpacedModulePort(
        world,
        {
            SRS_START: SRS_START_REPLY,
            kp.encode_length_frame(b"\xa2\x01"): kp.encode_length_frame(b"\xe2\x01\x91\x34"),
            kp.encode_length_frame(b"\x82"): kp.encode_length_frame(b"\xc2"),
        },
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_airbag_module_gets_its_bytes_one_by_one_at_the_declared_gap() -> None:
    """The profile's 15 ms for 0x58: StartCommunication, the past-code read
    and StopCommunication each go out a byte per write, every byte's echo
    read before the next, 15 ms of idle between them."""
    world = World()
    port = _srs_port(world)
    with make(world, port=port) as transport:
        reply = transport.start_communication(SRS)
        params = transport.request(b"\xa2\x01")
        transport.stop_communication()
        (pulse,) = transport.take_wake_pulses()
    assert (reply.source, reply.key_bytes) == (SRS, b"\x6d\x8f")
    assert params == b"\x01\x91\x34"  # B1134, as the scan tool read it
    writes = [e for e in world.log if e[0] == "write"]
    assert all(len(e[1]) == 1 for e in writes)  # type: ignore[arg-type]
    assert b"".join(e[1] for e in writes[:5]) == SRS_START  # type: ignore[misc]
    for frame, gaps in port.heard:
        assert gaps == pytest.approx((0.015,) * (len(frame) - 1), abs=1e-9), frame.hex()
    # Recorded on the wake-up, so the next attempt documents itself.
    assert pulse.answered and pulse.p4_ms == pytest.approx(15.0)
    assert pulse.byte_gaps_ms == pytest.approx((15.0,) * 4)


def test_each_spaced_byte_is_echo_checked_before_the_next_goes_out() -> None:
    world = World()
    port = _srs_port(world, corrupt_byte=2)
    with make(world, port=port) as transport, pytest.raises(EchoMismatchError, match="byte 2 fc"):
        transport.start_communication(SRS)
    assert [e[1] for e in world.log if e[0] == "write"] == [b"\x81", b"\x58", b"\xfc"]


def test_back_to_back_bytes_are_what_the_airbag_module_ignored() -> None:
    """The control: the same port leaves CANary's old one-write frame
    unanswered, which is what both 2026-10-08 and 2026-10-10 saw."""
    world = World()
    port = _srs_port(world)
    with make(world, port=port) as transport:
        transport.wake(PROFILE.wakeups[1])
        transport._p4_s = 0.0  # the pre-2026-10-10 transport: one write
        transport.send_frame(SRS_START)
        with pytest.raises(KlineTimeoutError):
            transport.read_frame("addressed", PROFILE.timing.p2_max_ms / 1000.0)
    ((frame, gaps),) = port.heard
    assert frame == SRS_START and gaps == pytest.approx((0.0,) * 4, abs=1e-9)


def test_the_ecm_keeps_one_back_to_back_write() -> None:
    """The ECM (0x10) path is unchanged: ``timing.p4_ms`` is 0, so its frames
    go out in one write, and the wake-up says so."""
    world = World()
    with make(world, ecu=xterra()) as transport:
        transport.start_communication(0x10)
        transport.request(PROFILE.known_answers[0].request_bytes())
        (pulse,) = transport.take_wake_pulses()
    writes = [e[1] for e in world.log if e[0] == "write"]
    assert writes == [F["start_communication"], F["read_did"]]
    assert pulse.p4_ms == 0.0 and pulse.byte_gaps_ms == ()


def test_a_session_after_the_airbag_one_goes_back_to_the_ecms_timing() -> None:
    """The gap belongs to the module a StartCommunication addresses, not to
    the transport: an ECM start after an SRS one is back to back again."""
    world = World()
    port = SpacedModulePort(
        world,
        {SRS_START: SRS_START_REPLY, F["start_communication"]: F["start_communication_reply"]},
        min_gap_s=0.0,
    )
    with make(world, port=port) as transport:
        transport.start_communication(SRS)
        transport.start_communication(0x10)
    (srs_frame, srs_gaps), (ecm_frame, ecm_gaps) = port.heard
    assert (srs_frame, ecm_frame) == (SRS_START, F["start_communication"])
    assert srs_gaps == pytest.approx((0.015,) * 4)
    assert ecm_gaps == pytest.approx((0.0,) * 4, abs=1e-9)
    assert F["start_communication"] in [e[1] for e in world.log if e[0] == "write"]


class _StallingWorld(World):
    """A host whose every sleep of 10 ms or more runs ``overshoot`` long."""

    def sleep(self, seconds: float) -> None:
        self.t += seconds + (self.overshoot if seconds >= 0.01 else 0.0)


def test_a_gap_the_host_stretched_past_p4max_is_named_not_read_as_silence() -> None:
    world = _StallingWorld()
    world.overshoot = 0.008  # 15 ms becomes 23 ms: past ISO's 20 ms
    port = _srs_port(world)
    with (
        make(world, port=port) as transport,
        pytest.raises(KlineTimeoutError, match="stretched a gap to 23.0 ms"),
    ):
        transport.start_communication(SRS)
