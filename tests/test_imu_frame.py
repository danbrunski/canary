"""Tests for gravity/heading/sign frame resolution (Phase 2a.4)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.preprocessing.imu_frame import (
    estimate_gravity,
    quiescent_mask,
    read_yaw_offset,
    resolve_frame,
    resolve_longitudinal_sign,
    resolve_yaw_offset,
    straight_mask,
    write_yaw_offset,
)


def _ts(seconds: float) -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Individual building blocks
# ---------------------------------------------------------------------------


def test_quiescent_mask_true_only_when_speed_and_heading_are_steady() -> None:
    n = 40
    speed = pd.Series([60.0] * 20 + list(np.linspace(60, 90, 20)))
    heading = pd.Series([90.0] * n)
    mask = quiescent_mask(speed, heading, window=5)
    # Steady cruise should be quiescent once the rolling window has filled;
    # the accelerating tail should not be.
    assert bool(mask.iloc[10])
    assert not bool(mask.iloc[-1])


def test_quiescent_mask_without_heading_falls_back_to_speed_only() -> None:
    speed = pd.Series([60.0] * 20)
    mask = quiescent_mask(speed, None, window=5)
    assert bool(mask.iloc[-1])


def test_straight_mask_excludes_a_turn_but_not_an_accel_event() -> None:
    heading = pd.Series([90.0] * 15 + list(np.linspace(90, 180, 15)))
    mask = straight_mask(heading, window=5)
    assert bool(mask.iloc[10])
    assert not bool(mask.iloc[-1])


def test_estimate_gravity_converges_to_tilted_vector_during_quiescence() -> None:
    n = 200
    tilt = math.radians(8.0)
    gravity_true = np.array([math.sin(tilt), 0.0, math.cos(tilt)])
    accel = pd.DataFrame(
        {
            "x": gravity_true[0],
            "y": gravity_true[1],
            "z": gravity_true[2],
        },
        index=range(n),
    )
    mask = pd.Series(True, index=accel.index)
    gravity = estimate_gravity(accel, mask)
    assert gravity.iloc[-1]["x"] == pytest.approx(gravity_true[0], abs=1e-3)
    assert gravity.iloc[-1]["z"] == pytest.approx(gravity_true[2], abs=1e-3)


def test_estimate_gravity_holds_last_value_on_non_quiescent_rows() -> None:
    accel = pd.DataFrame({"x": [0.0] * 50 + [5.0] * 10, "y": 0.0, "z": [1.0] * 50 + [0.0] * 10})
    mask = pd.Series([True] * 50 + [False] * 10)
    gravity = estimate_gravity(accel, mask)
    # Held at the last quiescent estimate, not contaminated by the
    # non-quiescent spike.
    assert gravity.iloc[-1]["x"] == pytest.approx(0.0, abs=1e-6)
    assert gravity.iloc[-1]["z"] == pytest.approx(1.0, abs=1e-6)


def test_resolve_yaw_offset_recovers_known_axis_mod_pi() -> None:
    true_theta = math.radians(25.0)
    n = 100
    # Points lying along the true_theta line, alternating sign (accel vs.
    # brake both lie on the same axis) — exactly what the double-angle
    # formula is built to resolve mod pi.
    magnitudes = np.array([0.4 if i % 2 == 0 else -0.3 for i in range(n)])
    h1 = magnitudes * math.cos(true_theta)
    h2 = magnitudes * math.sin(true_theta)
    mask = pd.Series(True, index=range(n))
    offset = resolve_yaw_offset(pd.Series(h1), pd.Series(h2), mask)
    assert offset is not None
    # mod pi: recovered offset is true_theta or true_theta + pi.
    diff = (offset - true_theta) % math.pi
    assert min(diff, math.pi - diff) == pytest.approx(0.0, abs=1e-6)


def test_resolve_yaw_offset_none_when_nothing_usable() -> None:
    h1 = pd.Series([0.001] * 10)
    h2 = pd.Series([0.001] * 10)
    mask = pd.Series([False] * 10)
    assert resolve_yaw_offset(h1, h2, mask) is None


def test_resolve_longitudinal_sign_flips_when_anti_correlated_with_obd() -> None:
    timestamps = pd.Series([_ts(i) for i in range(10)])
    speed = pd.Series(np.linspace(0, 90, 10))  # accelerating: positive d(speed)/dt
    lon = pd.Series([-0.3] * 10)  # IMU reports negative during an accel event
    corrected, flipped = resolve_longitudinal_sign(lon, speed, timestamps)
    assert flipped is True
    assert (corrected > 0).all()


def test_resolve_longitudinal_sign_keeps_sign_when_correlated() -> None:
    timestamps = pd.Series([_ts(i) for i in range(10)])
    speed = pd.Series(np.linspace(0, 90, 10))
    lon = pd.Series([0.3] * 10)
    corrected, flipped = resolve_longitudinal_sign(lon, speed, timestamps)
    assert flipped is False
    assert (corrected == 0.3).all()


# ---------------------------------------------------------------------------
# Full resolve_frame: known tilt + known maneuvers
# ---------------------------------------------------------------------------


def _build_synthetic_session(
    *, pitch_deg: float, yaw_deg: float, hz: float = 10.0
) -> tuple[pd.DataFrame, float]:
    """A synthetic session with a known mount pitch + yaw and four labeled
    segments: quiescent cruise, straight accel, straight brake, and a
    constant-speed turn — enough to exercise gravity, heading, and sign
    resolution against ground truth we chose ourselves."""
    pitch = math.radians(pitch_deg)
    yaw = math.radians(yaw_deg)

    gravity_body = np.array([math.sin(pitch), 0.0, math.cos(pitch)])
    # Vehicle-forward/lateral axes as seen in body coordinates: the pitch
    # tilts the whole rigid mount (so "forward" picks up the same pitch
    # rotation gravity did); the yaw then additionally rotates the mount's
    # horizontal axes relative to the vehicle's actual forward/lateral.
    u = np.array([math.cos(pitch), 0.0, -math.sin(pitch)])  # pitch-tilted forward-ish
    v = np.array([0.0, 1.0, 0.0])  # untouched by a pure pitch
    e_lon_body = math.cos(yaw) * u + math.sin(yaw) * v
    e_lat_body = -math.sin(yaw) * u + math.cos(yaw) * v

    dt = 1.0 / hz
    segments = [
        # (duration_s, speed_start, speed_end, heading_start, heading_end, lon_g, lat_g)
        (5.0, 60.0, 60.0, 90.0, 90.0, 0.0, 0.0),  # quiescent cruise
        (5.0, 60.0, 90.0, 90.0, 90.0, 0.30, 0.0),  # straight hard accel
        (5.0, 90.0, 90.0, 90.0, 90.0, 0.0, 0.0),  # quiescent cruise
        (5.0, 90.0, 60.0, 90.0, 90.0, -0.35, 0.0),  # straight hard brake
        (5.0, 60.0, 60.0, 90.0, 180.0, 0.0, 0.25),  # constant-speed turn
    ]

    rows: list[dict[str, object]] = []
    t = 0.0
    for duration, speed0, speed1, heading0, heading1, lon_g, lat_g in segments:
        n = int(duration * hz)
        speeds = np.linspace(speed0, speed1, n)
        headings = np.linspace(heading0, heading1, n)
        for i in range(n):
            linear_body = lon_g * e_lon_body + lat_g * e_lat_body
            accel_body = gravity_body + linear_body
            rows.append(
                {
                    "timestamp": _ts(t),
                    "imu:ACCEL_X": accel_body[0],
                    "imu:ACCEL_Y": accel_body[1],
                    "imu:ACCEL_Z": accel_body[2],
                    "obd:SPEED": speeds[i],
                    "gps:heading": headings[i],
                    "_true_lon": lon_g,
                    "_true_lat": lat_g,
                }
            )
            t += dt

    return pd.DataFrame(rows), yaw


def test_resolve_frame_recovers_known_tilt_and_maneuvers() -> None:
    frame, true_yaw = _build_synthetic_session(pitch_deg=8.0, yaw_deg=20.0)

    result = resolve_frame(frame, quiescent_window=10)

    assert result.yaw_offset_rad is not None
    diff = (result.yaw_offset_rad - true_yaw) % math.pi
    assert min(diff, math.pi - diff) < math.radians(2.0)

    lon = result.linear_accel_lon
    lat = result.linear_accel_lat
    true_lon = frame["_true_lon"]
    true_lat = frame["_true_lat"]

    # Sample well inside each labeled segment (away from transition edges)
    # and compare recovered vs. ground truth.
    accel_rows = frame.index[(true_lon > 0.2)]
    brake_rows = frame.index[(true_lon < -0.2)]
    turn_rows = frame.index[(true_lat > 0.2)]
    quiescent_rows = frame.index[(true_lon == 0.0) & (true_lat == 0.0)]

    assert lon.loc[accel_rows].mean() == pytest.approx(0.30, abs=0.05)
    assert lon.loc[brake_rows].mean() == pytest.approx(-0.35, abs=0.05)
    assert lat.loc[turn_rows].mean() == pytest.approx(0.25, abs=0.05)
    assert lon.loc[quiescent_rows].abs().mean() < 0.05
    assert lat.loc[quiescent_rows].abs().mean() < 0.05


def test_resolve_frame_bno055_bypass_shares_yaw_and_sign_code() -> None:
    """gravity_removed=True (BNO055 path) skips the low-pass and reads
    imu:ACCEL_X/Y as already-fused horizontal components directly, but
    still runs the same heading + sign resolution as the raw-chip path."""
    true_yaw = math.radians(15.0)
    n = 100
    heading = pd.Series([90.0] * n)
    speed = pd.Series(np.linspace(0, 90, n))  # accelerating throughout
    # Fused horizontal accel already gravity-removed, expressed along an
    # unknown (to the code) body-horizontal axis rotated by true_yaw from
    # vehicle-forward.
    lon_true = 0.4
    h1 = lon_true * math.cos(true_yaw)
    h2 = lon_true * math.sin(true_yaw)
    frame = pd.DataFrame(
        {
            "timestamp": [_ts(i * 0.1) for i in range(n)],
            "imu:ACCEL_X": h1,
            "imu:ACCEL_Y": h2,
            "obd:SPEED": speed,
            "gps:heading": heading,
        }
    )

    result = resolve_frame(frame, gravity_removed=True, quiescent_window=10)

    assert result.yaw_offset_rad is not None
    diff = (result.yaw_offset_rad - true_yaw) % math.pi
    assert min(diff, math.pi - diff) < math.radians(2.0)
    assert result.linear_accel_lon.iloc[-1] == pytest.approx(lon_true, abs=0.05)


def test_resolve_frame_reports_unresolved_heading_without_gps() -> None:
    frame, _ = _build_synthetic_session(pitch_deg=0.0, yaw_deg=0.0)
    frame = frame.drop(columns=["gps:heading"])
    result = resolve_frame(frame, quiescent_window=10)
    assert result.yaw_offset_rad is None


def test_resolve_frame_accepts_a_persisted_yaw_offset_override() -> None:
    frame, true_yaw = _build_synthetic_session(pitch_deg=5.0, yaw_deg=10.0)
    result = resolve_frame(frame, quiescent_window=10, yaw_offset_rad=true_yaw)
    assert result.yaw_offset_rad == true_yaw


# ---------------------------------------------------------------------------
# Yaw offset persistence
# ---------------------------------------------------------------------------


def test_write_and_read_yaw_offset_roundtrip(tmp_path: Path) -> None:
    write_yaw_offset(tmp_path, 0.4321)
    assert read_yaw_offset(tmp_path) == pytest.approx(0.4321)


def test_read_yaw_offset_missing_returns_none(tmp_path: Path) -> None:
    assert read_yaw_offset(tmp_path) is None
