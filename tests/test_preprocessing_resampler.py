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
    # Coolant is slow (default 120s ceiling) — never exceeded in a 5s span, so
    # it forward-fills freely and now also carries a (all-False) gap column.
    assert list(result["obd:COOLANT_TEMP"]) == [80.0, 80.0, 80.0, 80.0, 80.0, 85.0]
    assert list(result[gap_column("obd:COOLANT_TEMP")]) == [False] * 6
    # RPM is fast (max_gap = 2s here) — bounded ffill (1.1) NaNs the cell once
    # its age exceeds max_gap, same rows the __gap flag (1.2) already marked.
    assert result["obd:RPM"].iloc[:4].tolist() == [700.0, 710.0, 710.0, 710.0]
    assert result["obd:RPM"].iloc[4:].isna().all()
    assert list(result[gap_column("obd:RPM")]) == [False, False, False, False, True, True]


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
    # Slow channels are now gap-tracked too (1.2): stale (never-yet-observed)
    # before the first reading (rows at t=0,1), fresh thereafter — well
    # inside the 120s default slow ceiling for this 4s span.
    assert list(result[gap_column("obd:COOLANT_TEMP")]) == [True, True, False, False, False]


def test_resample_wide_bounds_slow_channel_ffill_past_its_own_ceiling() -> None:
    """1.1 for slow channels: a stuck sensor doesn't forward-fill forever —
    it just gets a much longer leash than a fast channel."""
    wide = pd.DataFrame(
        [
            {"session": "s1", "timestamp": _ts(0), "obd:COOLANT_TEMP": 80.0},
            {"session": "s1", "timestamp": _ts(5), "obd:COOLANT_TEMP": None},
        ]
    )

    result = resample_wide(
        wide,
        config=ResampleConfig(hz=1.0, slow_channel_max_gap_seconds=2.0),
    )

    # Age <= 2s (rows 0-2) stays filled; beyond that (rows 3-5) goes NaN.
    assert result["obd:COOLANT_TEMP"].iloc[:3].tolist() == [80.0, 80.0, 80.0]
    assert result["obd:COOLANT_TEMP"].iloc[3:].isna().all()
    assert list(result[gap_column("obd:COOLANT_TEMP")]) == [
        False,
        False,
        False,
        True,
        True,
        True,
    ]


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
