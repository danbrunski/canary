"""Tests for the pure session time-series read module (Task 5.3)."""

from __future__ import annotations

import datetime as dt

from src.analysis.session_timeseries import compute_time_series


def _rows() -> list[dict[str, object]]:
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    rows: list[dict[str, object]] = []
    for i in range(10):
        rows.append(
            {
                "timestamp": t0 + dt.timedelta(seconds=i),
                "session": "S",
                "channel_key": "obd:RPM",
                "value_number": 800.0 + i * 10,
            }
        )
    for i in range(5):
        rows.append(
            {
                "timestamp": t0 + dt.timedelta(seconds=i * 2 + 0.5),
                "session": "S",
                "channel_key": "obd:LOAD",
                "value_number": 20.0 + i,
            }
        )
    return rows


def test_raw_mode_keeps_each_channels_own_timestamps() -> None:
    result = compute_time_series(_rows(), channels=["obd:RPM", "obd:LOAD"])
    assert result["resampled"] is False
    assert result["hz"] is None
    assert len(result["channels"]["obd:RPM"]["values"]) == 10
    assert len(result["channels"]["obd:LOAD"]["values"]) == 5
    assert result["channels"]["obd:RPM"]["values"][0] == 800.0


def test_missing_channel_returns_empty_arrays_not_an_error() -> None:
    result = compute_time_series(_rows(), channels=["obd:RPM", "obd:MISSING"])
    assert result["channels"]["obd:MISSING"] == {"timestamps": [], "values": []}


def test_resample_mode_aligns_channels_onto_one_shared_grid() -> None:
    result = compute_time_series(_rows(), channels=["obd:RPM", "obd:LOAD"], resample_hz=1.0)
    assert result["resampled"] is True
    assert result["hz"] == 1.0
    rpm_ts = result["channels"]["obd:RPM"]["timestamps"]
    load_ts = result["channels"]["obd:LOAD"]["timestamps"]
    assert rpm_ts == load_ts  # shared grid


def test_empty_rows_returns_empty_shaped_result() -> None:
    result = compute_time_series([], channels=["a", "b"])
    assert result == {
        "resampled": False,
        "hz": None,
        "channels": {"a": {"timestamps": [], "values": []}, "b": {"timestamps": [], "values": []}},
    }
