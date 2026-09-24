"""TI ADS1015 12-bit ADC: single-shot, single-ended conversions.

The CANary-HAT's 12 V sense (U8, I²C1 0x48): the protected ``+12V`` rail
through a 100 k / 22 k divider (R11/R12) into AIN0. Full scale is ±4.096 V,
because ±2.048 V would clip the divider's ~2.9 V at 16 V; one LSB is 2 mV at
the pin, 11.1 mV at the rail (``docs/CANary-HAT-Revisions.md`` §13.4).

Talks through :class:`src.collection.i2c_dev.RegisterBus`. Both registers
used here are 16 bits, most significant byte first.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from src.collection.i2c_dev import RegisterBus

REG_CONVERSION = 0x00
REG_CONFIG = 0x01

CONFIG_OS_START = 0x8000  # write: start a conversion; read: 1 = idle (done)
MODE_SINGLE_SHOT = 0x0100
DATA_RATE_1600 = 0b100 << 5
COMP_DISABLE = 0b11
#: Full-scale range (V) -> PGA code.
PGA_CODES: dict[float, int] = {
    6.144: 0b000,
    4.096: 0b001,
    2.048: 0b010,
    1.024: 0b011,
    0.512: 0b100,
    0.256: 0b101,
}
#: One conversion at 1600 SPS takes 0.625 ms; allow margin before giving up.
CONVERSION_TIMEOUT_SECONDS = 0.02


class Ads1015Error(RuntimeError):
    """A conversion did not complete."""


def config_word(input_channel: int, full_scale_v: float) -> int:
    """Single-shot, single-ended ``AIN<input_channel>`` against GND."""
    if not 0 <= input_channel <= 3:
        raise ValueError(f"ADS1015 input {input_channel} outside 0..3")
    if full_scale_v not in PGA_CODES:
        raise ValueError(f"ADS1015 full scale {full_scale_v} V; supported: {sorted(PGA_CODES)}")
    mux = 0b100 | input_channel  # single-ended AINx vs GND
    return (
        CONFIG_OS_START
        | mux << 12
        | PGA_CODES[full_scale_v] << 9
        | MODE_SINGLE_SHOT
        | DATA_RATE_1600
        | COMP_DISABLE
    )


def code_to_volts(raw: bytes, full_scale_v: float) -> float:
    """12-bit result, left-justified in a signed 16-bit register."""
    code = int.from_bytes(raw[:2], "big", signed=True) >> 4
    return code * full_scale_v / 2048.0


class Ads1015:
    def __init__(
        self,
        bus: RegisterBus,
        *,
        full_scale_v: float = 4.096,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if full_scale_v not in PGA_CODES:
            raise ValueError(f"ADS1015 full scale {full_scale_v} V; supported: {sorted(PGA_CODES)}")
        self.bus = bus
        self.full_scale_v = full_scale_v
        self._clock = clock
        self._sleep = sleep

    def read_volts(self, input_channel: int) -> tuple[float, float]:
        """One conversion; returns (epoch seconds at the conversion, volts at the pin)."""
        word = config_word(input_channel, self.full_scale_v)
        started = self._clock()
        self.bus.write_block(REG_CONFIG, word.to_bytes(2, "big"))
        deadline = started + CONVERSION_TIMEOUT_SECONDS
        while not int.from_bytes(self.bus.read(REG_CONFIG, 2), "big") & CONFIG_OS_START:
            if self._clock() > deadline:
                raise Ads1015Error(f"conversion on AIN{input_channel} did not complete")
            self._sleep(0.0005)
        finished = self._clock()
        volts = code_to_volts(self.bus.read(REG_CONVERSION, 2), self.full_scale_v)
        return (started + finished) / 2.0, volts


__all__ = ["PGA_CODES", "Ads1015", "Ads1015Error", "code_to_volts", "config_word"]
