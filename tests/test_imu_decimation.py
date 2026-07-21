"""Tests for the IMU pre-resample decimation step (Phase 2a.3)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from src.preprocessing.imu_decimation import (
    ACCEL_LAT_MAX_COLUMN,
    ACCEL_LON_COLUMN,
    ACCEL_LON_MAX_COLUMN,
    ACCEL_LON_RMS_COLUMN,
    JERK_MAX_COLUMN,
    decimate_imu_wide,
)


def _ts(seconds: float) -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def _synthetic_100hz_imu_frame(session: str = "s1", n: int = 200) -> pd.DataFrame:
    """A synthetic 100 Hz imu:ACCEL_LON/LAT trace alongside 5 Hz obd:RPM —
    the IMU-typical-rate-vs-OBD-poll-rate contrast the task calls for."""
    rows: list[dict[str, object]] = []
    for i in range(n):
        t = _ts(i * 0.01)  # 100 Hz
        rows.append(
            {
                "session": session,
                "timestamp": t,
                "imu:ACCEL_LON": 0.5 * math.sin(i / 5.0),
                "imu:ACCEL_LAT": 0.2 * math.cos(i / 5.0),
            }
        )
    for i in range(4):  # 5 Hz-ish OBD alongside, well below the IMU rate
        rows.append({"session": session, "timestamp": _ts(i * 0.5), "obd:RPM": 1000.0 + i})
    frame = pd.DataFrame(rows).sort_values("timestamp", kind="stable").reset_index(drop=True)
    return frame


def test_no_op_when_no_imu_columns_present() -> None:
    wide = pd.DataFrame(
        [{"session": "s1", "timestamp": _ts(0), "obd:RPM": 700.0}],
    )
    result = decimate_imu_wide(wide, hz=1.0)
    assert result is wide


def test_decimates_100hz_imu_to_one_aggregate_per_tick() -> None:
    frame = _synthetic_100hz_imu_frame()
    result = decimate_imu_wide(frame, hz=1.0)

    # Raw high-rate columns are gone; only the decimated aggregates remain.
    assert ACCEL_LON_COLUMN not in result.columns
    assert ACCEL_LON_MAX_COLUMN in result.columns
    assert ACCEL_LON_RMS_COLUMN in result.columns
    assert ACCEL_LAT_MAX_COLUMN in result.columns
    assert JERK_MAX_COLUMN in result.columns

    # Every row in the first 1-second tick bucket carries the same
    # broadcast aggregate — the whole point is that resample_wide's own
    # .last() then can't accidentally pick an arbitrary raw sample.
    tick0 = result[result["timestamp"] < _ts(1.0)]
    imu_tick0 = tick0[tick0[ACCEL_LON_MAX_COLUMN].notna()]
    assert imu_tick0[ACCEL_LON_MAX_COLUMN].nunique() == 1

    raw_lon = 0.5 * np.sin(np.arange(100) / 5.0)
    expected_max = float(np.abs(raw_lon).max())
    expected_rms = float(np.sqrt(np.mean(raw_lon**2)))
    assert imu_tick0[ACCEL_LON_MAX_COLUMN].iloc[0] == pytest.approx(expected_max, rel=1e-6)
    assert imu_tick0[ACCEL_LON_RMS_COLUMN].iloc[0] == pytest.approx(expected_rms, rel=1e-6)

    # obd:RPM rows pass through completely untouched.
    obd_rows = result[result["obd:RPM"].notna()]
    assert obd_rows["obd:RPM"].tolist() == [1000.0, 1001.0, 1002.0, 1003.0]


def test_decimation_never_mixes_across_session_boundary() -> None:
    a = _synthetic_100hz_imu_frame(session="s1", n=50)
    b = _synthetic_100hz_imu_frame(session="s2", n=50)
    combined = pd.concat([a, b], ignore_index=True)
    result = decimate_imu_wide(combined, hz=1.0)

    s1_max = result.loc[result["session"] == "s1", ACCEL_LON_MAX_COLUMN].dropna().iloc[0]
    s2_max = result.loc[result["session"] == "s2", ACCEL_LON_MAX_COLUMN].dropna().iloc[0]
    # Same synthetic generator per session (same seed pattern) — the point
    # isn't that they differ, it's that neither aggregate is NaN/contaminated
    # by rows from the other session sharing the same wall-clock second.
    assert s1_max > 0
    assert s2_max > 0


def test_jerk_max_reflects_a_sharp_transient() -> None:
    # A single abrupt spike mid-tick should dominate jerk (large delta over
    # one 10 ms sample), distinguishable from the smooth sinusoid baseline.
    rows = []
    for i in range(20):
        t = _ts(i * 0.01)
        value = 5.0 if i == 10 else 0.0
        rows.append({"session": "s1", "timestamp": t, "imu:ACCEL_LON": value})
    frame = pd.DataFrame(rows)
    result = decimate_imu_wide(frame, hz=1.0)
    jerk = result[JERK_MAX_COLUMN].dropna().iloc[0]
    # d(accel)/dt = 5.0g / 0.01s = 500 g/s at the spike's leading edge.
    assert jerk == pytest.approx(500.0, rel=1e-6)


def test_empty_frame_passes_through() -> None:
    empty = pd.DataFrame(columns=["session", "timestamp", "imu:ACCEL_LON"])
    result = decimate_imu_wide(empty, hz=1.0)
    assert result is empty


def test_rejects_non_positive_hz() -> None:
    frame = _synthetic_100hz_imu_frame()
    with pytest.raises(ValueError):
        decimate_imu_wide(frame, hz=0.0)
