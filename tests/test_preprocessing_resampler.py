"""Tests for uniform telemetry resampling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from src.preprocessing.resampler import ResampleConfig, gap_column, resample_wide


def _ts(seconds: int) -> datetime:
    return datetime(2026, 5, 11, 12, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def test_resample_wide_forward_fills_and_flags_fast_channel_gaps() -> None:
    wide = pd.DataFrame(
        [
            {"session": "s1", "timestamp": _ts(0), "obd:RPM": 700.0, "obd:COOLANT_TEMP": 80.0},
            {"session": "s1", "timestamp": _ts(1), "obd:RPM": 710.0, "obd:COOLANT_TEMP": None},
            {"session": "s1", "timestamp": _ts(5), "obd:RPM": None, "obd:COOLANT_TEMP": 85.0},
        ]
    )

    result = resample_wide(wide, config=ResampleConfig(hz=1.0, max_gap_factor=2.0))

    assert len(result) == 6
    assert list(result["obd:COOLANT_TEMP"]) == [80.0, 80.0, 80.0, 80.0, 80.0, 85.0]
    assert list(result["obd:RPM"]) == [700.0, 710.0, 710.0, 710.0, 710.0, 710.0]
    assert list(result[gap_column("obd:RPM")]) == [False, False, False, False, True, True]
    assert gap_column("obd:COOLANT_TEMP") not in result.columns


def test_resample_wide_preserves_session_boundaries() -> None:
    wide = pd.DataFrame(
        [
            {"session": "s1", "timestamp": _ts(0), "obd:RPM": 700.0},
            {"session": "s1", "timestamp": _ts(2), "obd:RPM": None},
            {"session": "s2", "timestamp": _ts(0), "obd:RPM": 900.0},
        ]
    )

    result = resample_wide(wide)

    s1 = result[result["session"] == "s1"]
    s2 = result[result["session"] == "s2"]
    assert list(s1["obd:RPM"]) == [700.0, 700.0, 700.0]
    assert list(s2["obd:RPM"]) == [900.0]


def test_resample_wide_marks_leading_fast_nulls_as_gaps() -> None:
    wide = pd.DataFrame(
        [
            {"session": "s1", "timestamp": _ts(0), "obd:RPM": None, "obd:SPEED": 0.0},
            {"session": "s1", "timestamp": _ts(2), "obd:RPM": 720.0, "obd:SPEED": 5.0},
        ]
    )

    result = resample_wide(wide)

    assert pd.isna(result.loc[0, "obd:RPM"])
    assert pd.isna(result.loc[1, "obd:RPM"])
    assert bool(result.loc[0, gap_column("obd:RPM")]) is True
    assert bool(result.loc[1, gap_column("obd:RPM")]) is True
    assert bool(result.loc[2, gap_column("obd:RPM")]) is False


def test_resample_wide_keeps_leading_slow_nulls_until_first_observation() -> None:
    wide = pd.DataFrame(
        [
            {"session": "s1", "timestamp": _ts(0), "obd:COOLANT_TEMP": None},
            {"session": "s1", "timestamp": _ts(2), "obd:COOLANT_TEMP": 82.0},
            {"session": "s1", "timestamp": _ts(4), "obd:COOLANT_TEMP": None},
        ]
    )

    result = resample_wide(wide)

    assert result["obd:COOLANT_TEMP"].isna().iloc[:2].all()
    assert result["obd:COOLANT_TEMP"].iloc[2:].tolist() == [82.0, 82.0, 82.0]
    assert gap_column("obd:COOLANT_TEMP") not in result.columns


def test_resample_wide_gap_threshold_is_strictly_greater_than_max_gap() -> None:
    wide = pd.DataFrame(
        [
            {"session": "s1", "timestamp": _ts(0), "obd:RPM": 700.0},
            {"session": "s1", "timestamp": _ts(3), "obd:RPM": None},
        ]
    )

    result = resample_wide(wide, config=ResampleConfig(hz=1.0, max_gap_factor=2.0))

    # Age == max_gap at t=2 is still acceptable; t=3 is the first gap.
    assert result[gap_column("obd:RPM")].tolist() == [False, False, False, True]


def test_resample_wide_supports_explicit_fast_channels() -> None:
    wide = pd.DataFrame([{"session": "s1", "timestamp": _ts(0), "obd:COOLANT_TEMP": 80.0}])

    result = resample_wide(
        wide,
        config=ResampleConfig(hz=1.0, fast_channels=frozenset({"obd:COOLANT_TEMP"})),
    )

    assert gap_column("obd:COOLANT_TEMP") in result.columns


def test_resample_config_rejects_invalid_hz() -> None:
    with pytest.raises(ValueError, match="hz"):
        ResampleConfig(hz=0)
