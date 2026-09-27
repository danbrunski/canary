"""LSM6DSOX settings: the chip's allowed rates and ranges, and their check.

The driver (:mod:`src.collection.lsm6dsox`) programs the chip from these
tables, and ``src.config.ImuConfig`` validates ``hardware.imu`` against them,
so the two cannot drift. They live here, with no ``collection`` import,
because ``config`` sits at layer 0 (``tests/test_layering.py``, R09.2) and
must not import the driver to validate a setting.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Nominal output data rates the chip supports, Hz -> ``ODR_XL``/``ODR_G``/``BDR`` code.
ODR_CODES: dict[float, int] = {
    12.5: 0x1,
    26.0: 0x2,
    52.0: 0x3,
    104.0: 0x4,
    208.0: 0x5,
    416.0: 0x6,
    833.0: 0x7,
}
#: Full scale (g) -> (``FS_XL`` code, g per LSB).
ACCEL_RANGES: dict[int, tuple[int, float]] = {
    2: (0b00, 0.061e-3),
    4: (0b10, 0.122e-3),
    8: (0b11, 0.244e-3),
    16: (0b01, 0.488e-3),
}
#: Full scale (deg/s) -> (``FS_G`` code, deg/s per LSB).
GYRO_RANGES: dict[int, tuple[int, float]] = {
    250: (0b00, 8.75e-3),
    500: (0b01, 17.5e-3),
    1000: (0b10, 35.0e-3),
    2000: (0b11, 70.0e-3),
}


@dataclass(frozen=True)
class ImuSettings:
    odr_hz: float
    accel_range_g: int
    gyro_range_dps: int

    def __post_init__(self) -> None:
        if self.odr_hz not in ODR_CODES:
            raise ValueError(f"LSM6DSOX ODR {self.odr_hz} Hz; supported: {sorted(ODR_CODES)}")
        if self.accel_range_g not in ACCEL_RANGES:
            raise ValueError(
                f"accel range {self.accel_range_g} g; supported: {sorted(ACCEL_RANGES)}"
            )
        if self.gyro_range_dps not in GYRO_RANGES:
            raise ValueError(
                f"gyro range {self.gyro_range_dps} dps; supported: {sorted(GYRO_RANGES)}"
            )


__all__ = ["ACCEL_RANGES", "GYRO_RANGES", "ODR_CODES", "ImuSettings"]
