"""The K-line transport's fakes: pins, lines, ports and the module world (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from src.collection.kline.profile import load_kline_profile
from src.collection.kline.transport import P4_MAX_S, HatPins

PROFILE = load_kline_profile(Path("data/nissan-xterra-2008/kline.yaml"))


#: Rev 2.0's lines; nothing here depends on which revision.
PINS = HatPins(enable=26, pullup=24)


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
