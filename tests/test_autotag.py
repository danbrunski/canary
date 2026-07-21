"""Tests for the auto-tagger detection layer (Phase 2a.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src.analysis.autotag import (
    detect_cornering_events,
    detect_hard_accel_events,
    detect_hard_brake_events,
    detect_highway_cruise_events,
    detect_launch_events,
    detect_stopped_events,
    detect_tags,
)
from src.config import AutoTagConfig


def _ts(seconds: float) -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# GPS-only detectors — must work with no IMU columns at all.
# ---------------------------------------------------------------------------


def test_stopped_detected_from_gps_alone() -> None:
    rows = [{"timestamp": _ts(i), "gps:speed": 45.0 if i < 5 else 0.5} for i in range(10)]
    events = detect_stopped_events(_frame(rows), AutoTagConfig())
    assert len(events) == 1
    assert events[0].label == "stopped"
    assert events[0].fields["gps_speed_kph"] == pytest.approx(0.5)
    assert events[0].timestamp == pd.Timestamp(_ts(5))


def test_stopped_absent_without_gps_column() -> None:
    rows = [{"timestamp": _ts(i), "obd:SPEED": 0.0} for i in range(5)]
    assert detect_stopped_events(_frame(rows), AutoTagConfig()) == []


def test_highway_cruise_detected_from_sustained_gps_speed() -> None:
    config = AutoTagConfig(highway_sustain_seconds=5.0)
    rows = [{"timestamp": _ts(i), "gps:speed": 40.0 if i < 5 else 100.0} for i in range(15)]
    events = detect_highway_cruise_events(_frame(rows), config)
    assert len(events) == 1
    assert events[0].label == "highway_cruise"
    assert events[0].timestamp == pd.Timestamp(_ts(5))


def test_highway_cruise_not_tagged_when_not_sustained_long_enough() -> None:
    config = AutoTagConfig(highway_sustain_seconds=10.0)
    rows = [{"timestamp": _ts(i), "gps:speed": 40.0 if i < 5 else 100.0} for i in range(8)]
    assert detect_highway_cruise_events(_frame(rows), config) == []


# ---------------------------------------------------------------------------
# IMU-dependent detectors — synthetic only, dormant on real (IMU-less) data.
# ---------------------------------------------------------------------------


def test_hard_accel_requires_positive_speed_derivative() -> None:
    rows = [{"timestamp": _ts(i), "imu:ACCEL_LON_MAX": 0.5, "obd:SPEED": i * 5.0} for i in range(6)]
    events = detect_hard_accel_events(_frame(rows), AutoTagConfig())
    assert len(events) == 1
    assert events[0].label == "hard_accel"
    assert events[0].fields["peak_g"] == pytest.approx(0.5)


def test_hard_accel_absent_without_imu_column() -> None:
    rows = [{"timestamp": _ts(i), "obd:SPEED": i * 5.0} for i in range(6)]
    assert detect_hard_accel_events(_frame(rows), AutoTagConfig()) == []


def test_hard_brake_requires_negative_speed_derivative() -> None:
    rows = [
        {"timestamp": _ts(i), "imu:ACCEL_LON_MAX": 0.6, "obd:SPEED": 90.0 - i * 10.0}
        for i in range(6)
    ]
    events = detect_hard_brake_events(_frame(rows), AutoTagConfig())
    assert len(events) == 1
    assert events[0].label == "hard_brake"


def test_hard_accel_and_hard_brake_do_not_cross_fire() -> None:
    """A braking event (negative speed derivative) must never register as
    hard_accel, and vice versa — the OBD sign is what disambiguates them."""
    rows = [
        {"timestamp": _ts(i), "imu:ACCEL_LON_MAX": 0.6, "obd:SPEED": 90.0 - i * 10.0}
        for i in range(6)
    ]
    frame = _frame(rows)
    assert detect_hard_accel_events(frame, AutoTagConfig()) == []
    assert len(detect_hard_brake_events(frame, AutoTagConfig())) == 1


def test_cornering_detected_from_lateral_accel_peak() -> None:
    rows = [{"timestamp": _ts(i), "imu:ACCEL_LAT_MAX": 0.1 * (i + 1)} for i in range(6)]
    events = detect_cornering_events(_frame(rows), AutoTagConfig(cornering_g=0.3))
    assert len(events) == 1
    assert events[0].label == "cornering"
    # Peak of the run is the last (highest) sample over threshold.
    assert events[0].fields["peak_g"] == pytest.approx(0.6)


def test_launch_requires_near_stop_and_sustained_hard_accel() -> None:
    config = AutoTagConfig(launch_sustain_seconds=2.0)
    # Speed climbs slowly through the near-stop band for two rows (the first
    # row's own d(speed)/dt is undefined — no prior sample — so the
    # condition only starts holding at row 1) before pulling away.
    speeds = [0.0, 2.0, 4.0, 20.0, 30.0, 40.0]
    rows = [
        {"timestamp": _ts(i), "imu:ACCEL_LON_MAX": 0.5, "obd:SPEED": speeds[i]}
        for i in range(len(speeds))
    ]
    events = detect_launch_events(_frame(rows), config)
    assert len(events) == 1
    assert events[0].label == "launch"


def test_launch_absent_when_speed_derivative_not_sustained() -> None:
    config = AutoTagConfig(launch_sustain_seconds=10.0)
    rows = [
        {"timestamp": _ts(i), "imu:ACCEL_LON_MAX": 0.5, "obd:SPEED": min(i * 5.0, 40.0)}
        for i in range(6)
    ]
    rows[0]["obd:SPEED"] = 0.0
    assert detect_launch_events(_frame(rows), config) == []


# ---------------------------------------------------------------------------
# detect_tags aggregation + real-data smoke (no IMU columns => IMU tags dormant)
# ---------------------------------------------------------------------------


def test_detect_tags_returns_time_ordered_events_across_detectors() -> None:
    rows = [
        {
            "timestamp": _ts(i),
            "gps:speed": 0.5 if i < 3 else 120.0,
            "imu:ACCEL_LON_MAX": 0.6 if i == 4 else 0.0,
            "obd:SPEED": 10.0 * i,
        }
        for i in range(12)
    ]
    events = detect_tags(_frame(rows), AutoTagConfig(highway_sustain_seconds=2.0))
    labels = [e.label for e in events]
    assert "stopped" in labels
    assert "hard_accel" in labels
    timestamps = [e.timestamp for e in events]
    assert timestamps == sorted(timestamps)


def test_detect_tags_is_dormant_for_imu_labels_without_imu_columns() -> None:
    """The exact acceptance shape for real stored sessions today: only the
    GPS-only labels can ever appear; IMU labels never do, since there is no
    imu:* column to read."""
    rows = [{"timestamp": _ts(i), "gps:speed": 0.2, "obd:SPEED": 0.0} for i in range(5)]
    events = detect_tags(_frame(rows), AutoTagConfig())
    labels = {e.label for e in events}
    assert labels <= {"stopped", "highway_cruise"}
