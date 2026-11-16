"""IMU stream fixtures: segments, records, sessions and the config fixture (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.config import CanaryConfig
from src.preprocessing.imu_session import ImuSessionFrame, resolve_session_imu_frame
from src.signals.committees import STANDARD_GRAVITY
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from tests.configs import example_config

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


IMU_HZ = 50


NOW = datetime(2026, 9, 26, 13, 0, tzinfo=UTC)


def _rot(axis: str, deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


MOUNT = _rot("z", 140.0) @ _rot("y", 6.0) @ _rot("x", -4.0)


@dataclass(frozen=True)
class Segment:
    seconds: float
    kph0: float
    kph1: float
    heading0: float = 90.0
    heading1: float = 90.0
    lat_g: float = 0.0
    imu: bool = True


# Stop, a 0.38 g pull from rest, on to highway, a 0.45 g brake to a stop, a
# pull, then a 0.35 g left turn at constant speed.
DRIVE = [
    Segment(15.0, 0.0, 0.0),
    Segment(4.5, 0.0, 60.0),
    Segment(4.0, 60.0, 100.0),
    Segment(10.0, 100.0, 100.0),
    Segment(6.3, 100.0, 0.0),
    Segment(10.0, 0.0, 0.0),
    Segment(5.0, 0.0, 40.0),
    Segment(8.0, 40.0, 40.0, heading0=90.0, heading1=0.0, lat_g=0.35),
    Segment(10.0, 40.0, 40.0, heading0=0.0, heading1=0.0),
]


def records(
    segments: list[Segment] = DRIVE,
    *,
    imu: bool = True,
    speeds: tuple[str, ...] = ("obd", "gps"),
) -> list[TelemetryRecord]:
    """IMU at 50 Hz (g); OBD speed/rpm/coolant/throttle and GPS speed/course
    at 1 Hz, so the live segmenter has every input it requires. ``speeds``
    picks the speed sources: ``obd`` (Mode 01), ``gps``, and ``2d1`` (raw
    ``0x2D1`` frames at 10 Hz, bytes 1-2 little-endian, 0.1 km/h a count)."""
    rng = np.random.default_rng(0)
    out: list[TelemetryRecord] = []
    t = 0.0
    for seg in segments:
        n = round(seg.seconds * IMU_HZ)
        a_g = (seg.kph1 - seg.kph0) / 3.6 / seg.seconds / STANDARD_GRAVITY
        for i in range(n):
            frac = i / n
            kph = seg.kph0 + (seg.kph1 - seg.kph0) * frac
            heading = (seg.heading0 + (seg.heading1 - seg.heading0) * frac) % 360.0
            stamp = (T0 + timedelta(seconds=t)).timestamp()
            if "2d1" in speeds and round(t * IMU_HZ) % (IMU_HZ // 10) == 0:
                counts = round(kph * 10.0).to_bytes(2, "little")
                out.append(TelemetryRecord.can(stamp, 0x2D1, b"\x00" + counts + bytes(5)))
            if round(t * IMU_HZ) % IMU_HZ == 0:
                if "obd" in speeds:
                    out.append(TelemetryRecord.obd(stamp, "SPEED", float(round(kph)), "km/h"))
                out.append(TelemetryRecord.obd(stamp, "RPM", 1500.0, "rpm"))
                out.append(TelemetryRecord.obd(stamp, "COOLANT_TEMP", 85.0, "C"))
                out.append(TelemetryRecord.obd(stamp, "THROTTLE_POS", 20.0, "%"))
                if "gps" in speeds:
                    out.append(TelemetryRecord.gps(stamp, "speed", kph, "km/h"))
                out.append(TelemetryRecord.gps(stamp, "heading", heading, "deg"))
            if imu and seg.imu:
                body = MOUNT @ np.array([a_g, seg.lat_g, 1.0]) + rng.normal(0.0, 0.004, 3)
                for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), body, strict=True):
                    out.append(TelemetryRecord.imu(stamp, axis, float(value), "g"))
            t += 1.0 / IMU_HZ
    return out


def write_session(
    root: Path,
    name: str = "2026-09-26T120000",
    *,
    segments: list[Segment] = DRIVE,
    imu: bool = True,
    resolve: bool = True,
    speeds: tuple[str, ...] = ("obd", "gps"),
) -> Path:
    session = root / name
    write_records_to_parquet(records(segments, imu=imu, speeds=speeds), session, max_rows=400)
    if resolve:
        assert isinstance(resolve_session_imu_frame(session, now=NOW), ImuSessionFrame)
    return session


@pytest.fixture
def config() -> CanaryConfig:
    """The example config with the accel-gated modes placed ahead of the speed
    bands (the example lists them after ``city``/``highway``, which claim every
    moving row first)."""
    data = example_config(Path("canary.example.yaml")).model_dump()
    data["preprocessing"]["operating_modes"] = {
        "launch": {"speed_max": 30, "accel_lon_min": 0.3},
        "hard_brake": {"speed_min": 1, "accel_lon_min": 0.4},
        "cornering": {"accel_lat_min": 0.3},
        "stopped": {"speed_max": 0},
        "city": {"speed_min": 1, "speed_max": 80},
        "highway": {"speed_min": 80},
    }
    return CanaryConfig.model_validate(data)


def imu_live_profile(t: float) -> tuple[float, float, float, float]:
    """(kph, heading, a_g, lat_g) at ``t``: rest, a 0.39 g straight pull,
    cruise, then a 0.3 g left turn at constant speed."""
    pull_g = 55.0 / 3.6 / 4.0 / STANDARD_GRAVITY
    if t < 10.0:
        return 0.0, 90.0, 0.0, 0.0
    if t < 14.0:
        return 55.0 * (t - 10.0) / 4.0, 90.0, pull_g, 0.0
    if t < 20.0:
        return 55.0, 90.0, 0.0, 0.0
    if t < 26.0:
        return 55.0, 90.0 - 15.0 * (t - 20.0), 0.0, 0.30
    return 55.0, 0.0, 0.0, 0.0
