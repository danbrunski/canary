"""K-line transport: the TLIN1027 order, echo, wake-up timing, requests.

A fake port echoes what is written (K-line is one wire) and a scripted ECU
answers with the frames captured on the Xterra on 2026-09-23. Time is virtual,
so wake-up and P3 timing are exact and the tests are instant.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from src.collection.kline import protocol as kp
from src.collection.kline.profile import load_kline_profile
from src.collection.kline.transport import (
    BusBusyError,
    EchoMismatchError,
    HatPins,
    KlineTimeoutError,
    KlineTransport,
    KlineTransportError,
    Mode,
    NoEchoError,
    TransmitRefusedError,
    WakeupTimingError,
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
