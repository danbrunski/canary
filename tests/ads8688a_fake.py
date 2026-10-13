"""An ADS8688A on a fake SPI bus, faithful to the frame protocol in SBAS680.

Used by the driver's and the collector's tests. What it models, because the
driver depends on each:

- A 32-clock frame converts the channel selected by the *previous* frame and
  returns that code in its third and fourth bytes; the frame's own command
  takes effect from the next frame (§8.4.1.1.1).
- A 24-clock frame is a program-register access: a write is echoed in the
  third byte, a read returns the register there (§8.5.2.1).
- RST returns every register to its default and stops converting (§8.4.2.8).
- Codes are straight binary over the channel's configured range (§8.3.10).
- A powered-down or unselected channel returns no valid data.
"""

from __future__ import annotations

from collections.abc import Sequence

from src.collection.ads8688a import RANGES, InputRange

INVALID = 0xDEAD
DEFAULTS: dict[int, int] = {
    0x01: 0xFF,
    0x02: 0x00,
    0x03: 0x00,
    **{0x05 + c: 0x00 for c in range(8)},
}
_BY_CODE: dict[int, InputRange] = {r.code: r for r in RANGES.values()}


class FakeAds8688a:
    def __init__(self, inputs: dict[int, float] | None = None) -> None:
        #: Volts at each channel's input pair.
        self.inputs: dict[int, float] = dict(inputs or {})
        self.registers: dict[int, int] = dict(DEFAULTS)
        self._selected: int | None = None
        #: Every message, as the list of frames it carried.
        self.messages: list[list[bytes]] = []
        self.closed = False

    def code_for(self, channel: int) -> int:
        span = _BY_CODE[self.registers[0x05 + channel]]
        code = round((self.inputs.get(channel, 0.0) - span.low) / span.lsb_volts)
        return min(max(code, 0), 0xFFFF)

    def _command(self, frame: bytes) -> bytes:
        channel = self._selected
        powered = channel is not None and not self.registers[0x02] >> channel & 1
        code = self.code_for(channel) if channel is not None and powered else INVALID
        command = int.from_bytes(frame[:2], "big")
        if command == 0x8500:  # RST
            self.registers = dict(DEFAULTS)
            self._selected = None
        elif command & 0xE300 == 0xC000:  # MAN_Ch_n
            self._selected = command >> 10 & 0x7
        elif command != 0x0000:
            raise AssertionError(f"fake does not model command {command:#06x}")
        return b"\x00\x00" + code.to_bytes(2, "big")

    def _register(self, frame: bytes) -> bytes:
        address, write = frame[0] >> 1, frame[0] & 1
        if write:
            self.registers[address] = frame[1]
            return bytes([0, 0, frame[1]])
        return bytes([0, 0, self.registers.get(address, 0)])

    def transfer(self, frames: Sequence[bytes]) -> list[bytes]:
        if self.closed:
            raise OSError("closed")
        self.messages.append(list(frames))
        out: list[bytes] = []
        for frame in frames:
            if len(frame) == 4:
                out.append(self._command(frame))
            elif len(frame) == 3:
                out.append(self._register(frame))
            else:
                raise AssertionError(f"frame of {len(frame)} bytes")
        return out

    def close(self) -> None:
        self.closed = True


class AbsentBus:
    """An SPI device with nothing on it: MISO floats low."""

    def __init__(self) -> None:
        self.closed = False

    def transfer(self, frames: Sequence[bytes]) -> list[bytes]:
        return [bytes(len(f)) for f in frames]

    def close(self) -> None:
        self.closed = True
