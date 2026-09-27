"""ST LSM6DSOX 6-axis IMU: configuration, FIFO draining, per-sample timing.

The CANary-HAT's IMU (I²C1, 0x6B). Register-level and transport-agnostic: it
talks through :class:`src.collection.i2c_dev.RegisterBus`, so tests use a fake
and the rev 3.0 MCU link can replace ``/dev/i2c-1`` later.

**Why the FIFO.** Polling the output registers costs one bus transaction per
sample and stamps each sample with the *read* instant, which jitters with
scheduling. Instead the chip batches accelerometer, gyroscope, temperature
and its own 25 µs timestamp counter into its FIFO, and one burst read drains
many 7-byte words (tag byte + 6 data bytes). The register address wraps from
``0x7E`` back to ``0x78`` during a burst — measured on the HAT 2026-09-23, not
assumed.

**How each sample gets its instant.** Words written for the same sample
instant share a 2-bit tag counter that advances by one per instant. A
timestamp word (batched every :data:`TS_DECIMATION` instants) carries the chip
counter for its instant; instants between two timestamp words are spaced by
the measured ticks per instant, which is updated from consecutive timestamp
words rather than assumed from the nominal output data rate. Chip ticks map to
host epoch time once per batch, by reading the live counter between two host
clock reads. The chip's timestamp resolution and its ODR share one oscillator
(both scale with ``INTERNAL_FREQ_FINE``), so ticks per instant does not depend
on that trim.

Units out: acceleration in g, angular rate in deg/s, temperature in °C, in
the chip's body frame. Vehicle-frame resolution is
:mod:`src.preprocessing.imu_frame`'s job.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from src.collection.i2c_dev import RegisterBus
from src.lsm6dsox_settings import ACCEL_RANGES, GYRO_RANGES, ODR_CODES, ImuSettings

WHO_AM_I = 0x0F
WHO_AM_I_VALUE = 0x6C

FIFO_CTRL3 = 0x09
FIFO_CTRL4 = 0x0A
CTRL1_XL = 0x10
CTRL2_G = 0x11
CTRL3_C = 0x12
CTRL10_C = 0x19
FIFO_STATUS1 = 0x3A
TIMESTAMP0 = 0x40
INTERNAL_FREQ_FINE = 0x63
FIFO_DATA_OUT_TAG = 0x78

CTRL3_SW_RESET = 0x01
#: Block data update + register auto-increment (needed for burst reads).
CTRL3_BDU_IF_INC = 0x44
CTRL10_TIMESTAMP_EN = 0x20
FIFO_MODE_BYPASS = 0b000
FIFO_MODE_CONTINUOUS = 0b110
#: ``DEC_TS_BATCH`` = 0b10: a timestamp word every 8 sample instants.
TS_DECIMATION_CODE = 0b10
TS_DECIMATION = 8
#: ``ODR_T_BATCH`` = 0b01: temperature into the FIFO at 1.6 Hz.
TEMP_BATCH_CODE = 0b01

TAG_GYRO = 0x01
TAG_ACCEL = 0x02
TAG_TEMP = 0x03
TAG_TIMESTAMP = 0x04

WORD_BYTES = 7
#: FIFO_STATUS2 bits.
STATUS2_OVR_LATCHED = 0x08
STATUS2_FULL = 0x20
STATUS2_OVR = 0x40

#: Instants discarded after power-up, in seconds of chip time. Measured on the
#: HAT 2026-09-23 at rest: the first accelerometer sample read 0.89 g against
#: 1.00 g after it, and the gyroscope read up to 33 deg/s for ~50 ms (its
#: datasheet turn-on time is 70 ms typical).
SETTLE_SECONDS = 0.1
#: Chip timestamp counter rate before the ``INTERNAL_FREQ_FINE`` trim.
TIMESTAMP_HZ = 40_000.0
TICKS_WRAP = 1 << 32


class Lsm6dsoxError(RuntimeError):
    """The device at the configured address is not an LSM6DSOX, or misbehaved."""


@dataclass(frozen=True)
class ImuSample:
    """One sample instant. Either vector may be missing if its word was lost."""

    timestamp: float  # host epoch seconds
    accel_g: tuple[float, float, float] | None
    gyro_dps: tuple[float, float, float] | None


@dataclass(frozen=True)
class TemperatureSample:
    timestamp: float
    celsius: float


@dataclass
class FifoBatch:
    samples: list[ImuSample] = field(default_factory=list)
    temperatures: list[TemperatureSample] = field(default_factory=list)
    words: int = 0
    #: The FIFO overflowed since the last read: samples were lost.
    overrun: bool = False
    #: Instants dropped because no timestamp word had dated them yet (only
    #: at start-up and after an overrun).
    undated: int = 0
    #: Instants dropped inside :data:`SETTLE_SECONDS` of power-up.
    settling: int = 0


def _vector(data: bytes, scale: float) -> tuple[float, float, float]:
    return (
        int.from_bytes(data[0:2], "little", signed=True) * scale,
        int.from_bytes(data[2:4], "little", signed=True) * scale,
        int.from_bytes(data[4:6], "little", signed=True) * scale,
    )


class FifoDecoder:
    """Stateful FIFO word decoder: groups words into sample instants and dates
    them in chip ticks. Pure — no bus — so the parsing rules are testable on
    captured bytes. State carries across batches because an instant's words
    can straddle two reads."""

    def __init__(self, settings: ImuSettings) -> None:
        self._accel_scale = ACCEL_RANGES[settings.accel_range_g][1]
        self._gyro_scale = GYRO_RANGES[settings.gyro_range_dps][1]
        self.ticks_per_instant = TIMESTAMP_HZ / settings.odr_hz
        self._counter: int | None = None
        self._ticks: float | None = None
        self._accel: tuple[float, float, float] | None = None
        self._gyro: tuple[float, float, float] | None = None
        self._last_stamp: int | None = None  # chip ticks of the last timestamp word
        self._instants_since_stamp = 0
        self._settle_until: float | None = None
        self._settle_ticks = SETTLE_SECONDS * TIMESTAMP_HZ
        self._settling_armed = False

    def reset(self) -> None:
        """Forget the timeline (after an overrun: counters no longer continue)."""
        self._counter = None
        self._ticks = None
        self._accel = None
        self._gyro = None
        self._last_stamp = None
        self._instants_since_stamp = 0

    def power_up(self) -> None:
        """The sensors just started: discard the settling window again."""
        self.reset()
        self._settle_until = None
        self._settling_armed = True

    def decode(self, raw: bytes, *, to_host: Callable[[float], float]) -> FifoBatch:
        """Decode whole 7-byte words into a batch (``words`` is the caller's)."""
        if len(raw) % WORD_BYTES:
            raise Lsm6dsoxError(f"FIFO read of {len(raw)} bytes is not whole words")
        batch = FifoBatch()
        temps = batch.temperatures
        for offset in range(0, len(raw), WORD_BYTES):
            header = raw[offset]
            data = raw[offset + 1 : offset + WORD_BYTES]
            tag = header >> 3
            counter = (header >> 1) & 0x3
            if counter != self._counter:
                self._flush(batch, to_host)
                self._advance(counter)
            if tag == TAG_TIMESTAMP:
                self._stamp(int.from_bytes(data[0:4], "little"))
            elif tag == TAG_ACCEL:
                self._accel = _vector(data, self._accel_scale)
            elif tag == TAG_GYRO:
                self._gyro = _vector(data, self._gyro_scale)
            elif tag == TAG_TEMP and self._ticks is not None:
                raw_t = int.from_bytes(data[0:2], "little", signed=True)
                temps.append(TemperatureSample(to_host(self._ticks), 25.0 + raw_t / 256.0))
            # Other tags (config change, step counter, sensor hub) carry nothing we use.
        return batch

    def _advance(self, counter: int) -> None:
        if self._counter is not None and self._ticks is not None:
            # The 2-bit counter advances once per instant; a caller only gets
            # here when it changed, so this is 1..3 (1 unless words were lost).
            steps = (counter - self._counter) % 4
            self._ticks += steps * self.ticks_per_instant
            self._instants_since_stamp += steps
        self._counter = counter

    def _stamp(self, ticks: int) -> None:
        if self._last_stamp is not None:
            span = (ticks - self._last_stamp) % TICKS_WRAP
            if self._instants_since_stamp > 0 and span > 0:
                measured = span / self._instants_since_stamp
                # Accept only a plausible update (within 5 % of the running
                # value): a skipped word would otherwise halve or double it.
                if abs(measured / self.ticks_per_instant - 1.0) < 0.05:
                    self.ticks_per_instant = measured
        self._last_stamp = ticks
        self._instants_since_stamp = 0
        self._ticks = float(ticks)
        if self._settling_armed and self._settle_until is None:
            self._settle_until = ticks + self._settle_ticks

    def _flush(self, batch: FifoBatch, to_host: Callable[[float], float]) -> None:
        if self._accel is None and self._gyro is None:
            return
        accel, gyro = self._accel, self._gyro
        self._accel = self._gyro = None
        if self._ticks is None:
            batch.undated += 1
            return
        if self._settle_until is not None:
            if (self._settle_until - self._ticks) % TICKS_WRAP < self._settle_ticks + 1:
                batch.settling += 1
                return
            self._settle_until = None
            self._settling_armed = False
        batch.samples.append(ImuSample(to_host(self._ticks), accel, gyro))

    def flush(self, to_host: Callable[[float], float]) -> list[ImuSample]:
        batch = FifoBatch()
        self._flush(batch, to_host)
        return batch.samples


class Lsm6dsox:
    """The device: configure, drain the FIFO, power down."""

    def __init__(
        self,
        bus: RegisterBus,
        settings: ImuSettings,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.bus = bus
        self.settings = settings
        self._clock = clock
        self.decoder = FifoDecoder(settings)
        self._ticks_per_second = TIMESTAMP_HZ

    def start(self) -> None:
        who = self.bus.read(WHO_AM_I, 1)[0]
        if who != WHO_AM_I_VALUE:
            raise Lsm6dsoxError(f"WHO_AM_I {who:#04x}, expected {WHO_AM_I_VALUE:#04x} (LSM6DSOX)")
        self.bus.write(CTRL3_C, CTRL3_SW_RESET)
        for _ in range(50):
            if not self.bus.read(CTRL3_C, 1)[0] & CTRL3_SW_RESET:
                break
            time.sleep(0.001)
        else:
            raise Lsm6dsoxError("software reset did not complete")
        self.bus.write(CTRL3_C, CTRL3_BDU_IF_INC)
        odr = ODR_CODES[self.settings.odr_hz]
        self.bus.write(CTRL1_XL, odr << 4 | ACCEL_RANGES[self.settings.accel_range_g][0] << 2)
        self.bus.write(CTRL2_G, odr << 4 | GYRO_RANGES[self.settings.gyro_range_dps][0] << 2)
        self.bus.write(CTRL10_C, CTRL10_TIMESTAMP_EN)
        fine = int.from_bytes(self.bus.read(INTERNAL_FREQ_FINE, 1), "little", signed=True)
        self._ticks_per_second = TIMESTAMP_HZ * (1.0 + 0.0015 * fine)
        self.bus.write(FIFO_CTRL3, odr << 4 | odr)
        self.bus.write(
            FIFO_CTRL4,
            TS_DECIMATION_CODE << 6 | TEMP_BATCH_CODE << 4 | FIFO_MODE_CONTINUOUS,
        )
        self.decoder.power_up()

    def stop(self) -> None:
        """FIFO to bypass and both sensors powered down (best effort)."""
        for register, value in (
            (FIFO_CTRL4, FIFO_MODE_BYPASS),
            (CTRL1_XL, 0),
            (CTRL2_G, 0),
        ):
            try:
                self.bus.write(register, value)
            except OSError:
                return

    def _host_mapping(self) -> Callable[[float], float]:
        before = self._clock()
        now_ticks = int.from_bytes(self.bus.read(TIMESTAMP0, 4), "little")
        after = self._clock()
        host_now = (before + after) / 2.0
        per_second = self._ticks_per_second

        def to_host(ticks: float) -> float:
            behind = (now_ticks - ticks) % TICKS_WRAP
            return host_now - behind / per_second

        return to_host

    def read_fifo(self) -> FifoBatch:
        """Drain whatever the FIFO holds now."""
        status = self.bus.read(FIFO_STATUS1, 2)
        words = status[0] | (status[1] & 0x03) << 8
        batch = FifoBatch(words=words)
        if status[1] & (STATUS2_OVR | STATUS2_OVR_LATCHED):
            batch.overrun = True
            self.decoder.reset()
        if words == 0:
            return batch
        to_host = self._host_mapping()
        chunk_words = 4096 // WORD_BYTES
        remaining = words
        while remaining:
            take = min(remaining, chunk_words)
            raw = self.bus.read(FIFO_DATA_OUT_TAG, take * WORD_BYTES)
            part = self.decoder.decode(raw, to_host=to_host)
            batch.samples.extend(part.samples)
            batch.temperatures.extend(part.temperatures)
            batch.undated += part.undated
            batch.settling += part.settling
            remaining -= take
        return batch


__all__ = [
    "FifoBatch",
    "FifoDecoder",
    "ImuSample",
    "Lsm6dsox",
    "Lsm6dsoxError",
    "TemperatureSample",
]
