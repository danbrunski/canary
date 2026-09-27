"""Live IMU frame resolution and decimation (H02.4).

Raw ``imu:ACCEL_X/Y/Z`` records in, per-tick ``imu:ACCEL_LON_MAX``-style
aggregates out, through :class:`LiveFeatureSource` to
:class:`LiveAutoTagHook`. Synthetic, with a mount we chose.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.analysis.committees import STANDARD_GRAVITY
from src.config import AutoTagConfig, CanaryConfig
from src.daemon.autotag_hook import LiveAutoTagHook
from src.decode.bindings import load_bindings
from src.preprocessing.imu_live import LiveImuFrame
from src.preprocessing.live import LiveFeatureSource
from src.telemetry import TelemetryRecord
from tests.configs import example_config

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
IMU_HZ = 50
LON_MAX = "imu:ACCEL_LON_MAX"
LAT_MAX = "imu:ACCEL_LAT_MAX"


def _rot(axis: str, deg: float) -> np.ndarray:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


MOUNT = _rot("z", 140.0) @ _rot("y", 6.0) @ _rot("x", -4.0)


def _profile(t: float) -> tuple[float, float, float, float]:
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


def _records(
    start: float, stop: float, *, speed_channel: str = "SPEED"
) -> Iterator[TelemetryRecord]:
    """Records in ``[start, stop)``: IMU at 50 Hz, speed and course at 1 Hz."""
    for i in range(round(start * IMU_HZ), round(stop * IMU_HZ)):
        t = i / IMU_HZ
        stamp = (T0 + timedelta(seconds=t)).timestamp()
        kph, heading, a_g, lat_g = _profile(t)
        if i % IMU_HZ == 0:
            if speed_channel == "SPEED":
                yield TelemetryRecord.obd(stamp, "SPEED", round(kph), "km/h")
            yield TelemetryRecord.gps(stamp, "speed", kph, "km/h")
            yield TelemetryRecord.gps(stamp, "heading", heading % 360.0, "deg")
        body = MOUNT @ np.array([a_g, lat_g, 1.0])
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), body, strict=True):
            yield TelemetryRecord.imu(stamp, axis, float(value), "g")


def _feed_frame(frame: LiveImuFrame, start: float, stop: float) -> None:
    for record in _records(start, stop):
        value = float(record.value)  # type: ignore[arg-type]
        if record.source == "imu":
            frame.observe_imu(record.channel, value, record.timestamp)
        else:
            frame.observe_input(f"{record.source}:{record.channel}", value, record.timestamp)


def test_live_frame_names_what_it_waits_for_then_splits_lon_and_lat() -> None:
    frame = LiveImuFrame()
    assert frame.status == "no_imu_samples"
    _feed_frame(frame, 0.0, 1.0)
    assert frame.status == "no_quiescent_period"  # decisions wait for the look-ahead
    _feed_frame(frame, 1.0, 10.0)
    assert frame.status == "yaw_unresolved"  # at rest: gravity known, forward not
    values, _ = frame.take_tick()
    assert values == {}  # nothing to split yet, so nothing emitted

    _feed_frame(frame, 10.0, 13.0)
    frame.take_tick()
    _feed_frame(frame, 13.0, 14.0)
    assert frame.status == "resolving"
    values, observed_at = frame.take_tick()
    pull_g = 55.0 / 3.6 / 4.0 / STANDARD_GRAVITY
    assert values[LON_MAX] == pytest.approx(pull_g, abs=0.005)
    assert values["imu:ACCEL_LON_RMS"] == pytest.approx(pull_g, abs=0.005)
    assert values[LAT_MAX] < 0.005
    assert observed_at is not None and observed_at < T0 + timedelta(seconds=14)

    _feed_frame(frame, 14.0, 23.0)
    frame.take_tick()
    _feed_frame(frame, 23.0, 24.0)
    values, _ = frame.take_tick()
    assert values[LAT_MAX] == pytest.approx(0.30, abs=0.005)
    assert values[LON_MAX] < 0.005


def test_live_gravity_ignores_the_first_second_of_a_pull_from_rest() -> None:
    """Speed is 1 Hz: for a second after the pull starts it still reads 0. The
    look-ahead keeps that second out of gravity, so the cruise after the pull
    reads ~0 g, not the leaked pull."""
    frame = LiveImuFrame()
    _feed_frame(frame, 0.0, 18.0)
    frame.take_tick()
    _feed_frame(frame, 18.0, 19.0)
    values, _ = frame.take_tick()
    assert values[LON_MAX] < 0.002

    # The estimate itself: the mount's "up", untouched by the pull.
    assert frame._gravity is not None
    np.testing.assert_allclose(frame._gravity, MOUNT[:, 2], atol=1e-6)


def test_live_frame_steers_by_0x2d1_counts_without_obd_speed() -> None:
    """``decoded:VEHICLE_SPEED_2D1`` arrives as raw counts (0.1 km/h each);
    it is scaled through the binding, so a steady 60 km/h (600 ± 1 count)
    is quiescent and a gravity reference forms with no obd:SPEED at all."""
    frame = LiveImuFrame(bindings=load_bindings(Path("data/bindings.yaml")))
    for i in range(5 * IMU_HZ):
        t = T0 + timedelta(seconds=i / IMU_HZ)
        if i % 5 == 0:
            frame.observe_input("decoded:VEHICLE_SPEED_2D1", 600.0 + (i // 5) % 2, t)
        for axis, value in zip(("ACCEL_X", "ACCEL_Y", "ACCEL_Z"), MOUNT[:, 2], strict=True):
            frame.observe_imu(axis, float(value), t)
    assert frame.status == "yaw_unresolved"  # gravity formed; no heading, no yaw


def test_reset_is_a_session_boundary() -> None:
    frame = LiveImuFrame()
    _feed_frame(frame, 0.0, 14.0)
    assert frame.status == "resolving"
    frame.reset()
    assert frame.status == "no_imu_samples"


@pytest.fixture
def config() -> CanaryConfig:
    return example_config(Path("canary.example.yaml"))


def test_live_auto_tag_hook_receives_the_decimated_channel(
    config: CanaryConfig, tmp_path: Path
) -> None:
    """End to end on the live path: raw IMU records into LiveFeatureSource,
    ``imu:ACCEL_LON_MAX`` on its operating point, and LiveAutoTagHook writes
    ``hard_accel`` (0.39 g against the default 0.35 g) and ``cornering``
    (a 0.30 g turn against a 0.28 g threshold)."""
    source = LiveFeatureSource(config=config)
    hook = LiveAutoTagHook(
        config=AutoTagConfig(live_enabled=True, cornering_g=0.28),
        session_dir=lambda: tmp_path,
    )
    seen: list[dict[str, float]] = []
    for second in range(30):
        for record in _records(float(second), float(second + 1)):
            source.observe(record)
        now = T0 + timedelta(seconds=second + 1)
        source.tick(now)
        point = source.latest_operating_point
        assert point is not None
        if isinstance(point.get(LON_MAX), float) and not math.isnan(point[LON_MAX]):
            seen.append({LON_MAX: point[LON_MAX], LAT_MAX: point[LAT_MAX]})
        hook.tick(point, now)

    assert seen, "the operating point never carried imu:ACCEL_LON_MAX"
    assert max(s[LON_MAX] for s in seen) == pytest.approx(0.389, abs=0.01)
    labels = [
        json.loads(line)["label"] for line in (tmp_path / "tags.jsonl").read_text().splitlines()
    ]
    assert "hard_accel" in labels
    assert "cornering" in labels
    assert source.imu_frame.status == "resolving"

    source.reset()
    assert source.imu_frame.status == "no_imu_samples"
