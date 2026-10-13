"""TI ADS8688A 16-bit, 8-channel SAR ADC: manual-mode bursts over SPI.

The CANary-HAT rev 2.1's analog front end (U8, SPI0 CE1, ``/dev/spidev0.1``,
``docs/CANary-HAT-Rev2.1.md`` §13.5.3). Every protocol fact below is from
TI's datasheet, SBAS680 (``hardware/canary-hat-rev2.1/datasheets/ads8684a.pdf``).

**Frames.** Each chip-select cycle is a frame. The device reads a 16-bit
command on SDI during the first 16 clocks and drives conversion data on SDO
during the next 16 (SPI mode 1, MSB first, so the data is the frame's third
and fourth bytes). The channel a frame converts was selected by the frame
*before* it: a command takes effect on the next falling edge of CS (§8.4.1.1.1).
So a burst over channels ``a, b, c`` is ``MAN_a, MAN_b, MAN_c, NO_OP``; frame
``k + 1`` carries channel ``k``'s sample, and frame 0's data (whatever was
selected last) is discarded. The whole burst is one ``SPI_IOC_MESSAGE``, so it
takes the bus once (:mod:`src.collection.spi_dev`).

**Program registers** take 24 clocks: address, a write bit and a data byte,
then the device echoes the byte written, or for a read returns the register
(§8.5.2.1). :meth:`Ads8688a.configure` writes each channel's range and reads it
back, so a missing or miswired chip fails loudly at start rather than
producing numbers.

**Codes are straight binary for every range** (§8.3.10): ``code 0`` is the
negative full scale, one LSB is ``FSR / 65536``. The range volts below assume
the internal 4.096 V reference (REFSEL low, as the board straps it).

**Only configured channels can be read.** The rev 2.1 board ties CH5-CH7 to
ground; reading one would return a plausible near-zero number that means
nothing, so :meth:`Ads8688a.read` refuses any channel the configuration did
not map (open item 33).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from src.collection.spi_dev import SpiBus

CHANNELS = 8
CODES = 65536

# Command register (Table 6).
CMD_NO_OP = 0x0000
CMD_RST = 0x8500


def cmd_manual(channel: int) -> int:
    """``MAN_Ch_n``: convert ``channel`` from the next frame on."""
    _check_channel(channel)
    return 0xC000 | channel << 10


# Program registers (Table 9).
REG_CHANNEL_POWER_DOWN = 0x02
REG_FEATURE_SELECT = 0x03


def reg_range(channel: int) -> int:
    """The channel's input-range register (05h-0Ch)."""
    _check_channel(channel)
    return 0x05 + channel


@dataclass(frozen=True)
class InputRange:
    """One of the device's input ranges (Table 15), in volts at the input pair."""

    name: str
    code: int
    low: float
    high: float

    @property
    def lsb_volts(self) -> float:
        return (self.high - self.low) / CODES

    def volts(self, code: float) -> float:
        """A code (or a mean of codes) to volts. Straight binary for every range."""
        return self.low + code * self.lsb_volts


#: Keyed by name as configuration spells it. VREF = 4.096 V (internal).
RANGES: dict[str, InputRange] = {
    r.name: r
    for r in (
        InputRange("+-10.24", 0b0000, -10.24, 10.24),
        InputRange("+-5.12", 0b0001, -5.12, 5.12),
        InputRange("+-2.56", 0b0010, -2.56, 2.56),
        InputRange("+-1.28", 0b0011, -1.28, 1.28),
        InputRange("+-0.64", 0b1011, -0.64, 0.64),
        InputRange("0-10.24", 0b0101, 0.0, 10.24),
        InputRange("0-5.12", 0b0110, 0.0, 5.12),
        InputRange("0-2.56", 0b0111, 0.0, 2.56),
        InputRange("0-1.28", 0b1111, 0.0, 1.28),
    )
}


class Ads8688aError(RuntimeError):
    """The device did not answer as configured."""


def _check_channel(channel: int) -> None:
    if not 0 <= channel < CHANNELS:
        raise ValueError(f"ADS8688A channel {channel} outside 0..{CHANNELS - 1}")


def command_frame(command: int) -> bytes:
    """A 32-clock frame: the command, then 16 clocks for the data out."""
    return command.to_bytes(2, "big") + b"\x00\x00"


def write_frame(register: int, value: int) -> bytes:
    return bytes([register << 1 | 1, value & 0xFF, 0x00])


def read_frame(register: int) -> bytes:
    return bytes([register << 1, 0x00, 0x00])


def frame_code(response: bytes) -> int:
    """The conversion result a 32-clock frame clocked in."""
    return int.from_bytes(response[2:4], "big")


def _register_values(ranges: Mapping[int, InputRange]) -> dict[int, int]:
    """Program register -> value for a configuration: each mapped channel's
    range, the rest powered down, and plain conversion data on SDO."""
    values = {reg_range(c): r.code for c, r in sorted(ranges.items())}
    values[REG_CHANNEL_POWER_DOWN] = sum(1 << c for c in range(CHANNELS) if c not in ranges)
    # Feature select: device ID 0, alarm off, conversion data only on SDO.
    values[REG_FEATURE_SELECT] = 0x00
    return values


#: Every program register the driver writes resets to 0x00 (Table 9).
RESET_VALUE = 0x00


@dataclass(frozen=True)
class Burst:
    """One burst: the channels' codes, and the moment they represent."""

    #: Epoch seconds, the middle of the burst (its samples span tens of µs).
    stamp: float
    codes: dict[int, int]
    #: The reader's slot number for this burst (0 when nobody numbers them).
    seq: int = 0


class Ads8688a:
    """One ADS8688A. :meth:`configure` before :meth:`read`."""

    def __init__(self, bus: SpiBus, *, clock: Callable[[], float] = time.time) -> None:
        self.bus = bus
        self._clock = clock
        self.ranges: dict[int, InputRange] = {}
        #: A register whose configured value differs from its reset value,
        #: read at the end of every burst: a reset clears every register, so
        #: this one catches it in the very burst it would have spoiled.
        self._sentinel: tuple[int, int] | None = None

    def configure(self, ranges: Mapping[int, InputRange]) -> None:
        """Reset the device, set each mapped channel's range, power down the
        rest, and verify every register by reading it back."""
        if not ranges:
            raise ValueError("configure at least one channel")
        for channel in ranges:
            _check_channel(channel)
        writes = _register_values(ranges)
        frames = [command_frame(CMD_RST)]
        frames += [write_frame(reg, value) for reg, value in writes.items()]
        frames += [read_frame(reg) for reg in writes]
        responses = self.bus.transfer(frames)
        echoes = responses[1 : 1 + len(writes)]
        readbacks = responses[1 + len(writes) :]
        for (reg, value), echo, readback in zip(writes.items(), echoes, readbacks, strict=True):
            if echo[2] != value or readback[2] != value:
                raise Ads8688aError(
                    f"register {reg:#04x}: wrote {value:#04x}, device echoed {echo[2]:#04x} "
                    f"and reads back {readback[2]:#04x} (is the ADS8688A on this SPI device, "
                    "in mode 1?)"
                )
        self.ranges = dict(ranges)
        self._sentinel = next(((r, v) for r, v in writes.items() if v != RESET_VALUE), None)

    def verify(self) -> None:
        """Read the configuration back; raise if the device has lost it.

        A reset (power-on, a brown-out on AVDD) returns every range to
        ±10.24 V, and the next ``MAN_Ch_n`` would quietly convert with it:
        codes that decode to plausible, wrong volts. Read between bursts, in
        its own message, so it costs no samples."""
        if not self.ranges:
            raise Ads8688aError("verify before configure")
        expected = _register_values(self.ranges)
        responses = self.bus.transfer([read_frame(reg) for reg in expected])
        for (reg, value), response in zip(expected.items(), responses, strict=True):
            if response[2] != value:
                raise Ads8688aError(
                    f"register {reg:#04x} reads {response[2]:#04x}, configured {value:#04x}: "
                    "the device lost its configuration (reset or brown-out)"
                )

    def read(self, channels: Sequence[int]) -> Burst:
        """Convert each channel once, in order, in one bus message.

        The message ends with a read of one configured register. If the chip
        reset since it was configured, its conversions just now used the
        reset ranges, so the burst is discarded and :class:`Ads8688aError`
        raised rather than misscaled codes returned."""
        if not channels:
            raise ValueError("read at least one channel")
        unmapped = [c for c in channels if c not in self.ranges]
        if unmapped:
            raise ValueError(
                f"ADS8688A channels {unmapped} are not configured "
                f"(configured: {sorted(self.ranges)}); an unmapped input is tied to ground "
                "or unconditioned and reads as a meaningless number"
            )
        frames = [command_frame(cmd_manual(c)) for c in channels]
        frames.append(command_frame(CMD_NO_OP))
        if self._sentinel is not None:
            frames.append(read_frame(self._sentinel[0]))
        started = self._clock()
        responses = self.bus.transfer(frames)
        finished = self._clock()
        if self._sentinel is not None:
            register, value = self._sentinel
            if responses[-1][2] != value:
                raise Ads8688aError(
                    f"register {register:#04x} reads {responses[-1][2]:#04x}, configured "
                    f"{value:#04x}: the device lost its configuration (reset or brown-out); "
                    "burst discarded"
                )
        data = responses[1 : 1 + len(channels)]
        codes = {c: frame_code(r) for c, r in zip(channels, data, strict=True)}
        return Burst(stamp=(started + finished) / 2.0, codes=codes)

    def volts(self, channel: int, code: float) -> float:
        return self.ranges[channel].volts(code)


__all__ = [
    "CHANNELS",
    "CMD_NO_OP",
    "CMD_RST",
    "CODES",
    "RANGES",
    "RESET_VALUE",
    "Ads8688a",
    "Ads8688aError",
    "Burst",
    "InputRange",
    "cmd_manual",
    "command_frame",
    "frame_code",
    "read_frame",
    "reg_range",
    "write_frame",
]
