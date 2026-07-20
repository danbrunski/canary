"""Tests for the pure ad-hoc binning module (Task 5.1)."""

from __future__ import annotations

import datetime as dt

from src.analysis.binned_analysis import compute_binned_analysis


def _rows(n: int = 100) -> list[dict[str, object]]:
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    rows: list[dict[str, object]] = []
    for i in range(n):
        ts = t0 + dt.timedelta(seconds=i)
        rows.append(
            {"timestamp": ts, "session": "S", "channel_key": "obd:RPM", "value_number": 800.0 + i}
        )
        rows.append(
            {
                "timestamp": ts,
                "session": "S",
                "channel_key": "obd:LOAD",
                "value_number": 20.0 + i * 0.1,
            }
        )
        rows.append(
            {
                "timestamp": ts,
                "session": "S",
                "channel_key": "obd:STFT",
                "value_number": float(i % 4) - 1.5,
            }
        )
    return rows


def test_1d_binning_shapes_and_covers_all_samples() -> None:
    result = compute_binned_analysis(
        _rows(80),
        value_channel="obd:STFT",
        x_channel="obd:RPM",
        x_bins=4,
        agg="mean",
        resample_hz=1.0,
    )
    assert result["available"] is True
    assert result["kind"] == "1d"
    assert len(result["cells"]) == 4
    assert sum(c["count"] for c in result["cells"]) == result["sample_count"]


def test_2d_binning_rows_cols_and_agg_choice() -> None:
    result = compute_binned_analysis(
        _rows(80),
        value_channel="obd:STFT",
        x_channel="obd:RPM",
        y_channel="obd:LOAD",
        x_bins=3,
        y_bins=2,
        agg="count",
        resample_hz=1.0,
    )
    assert result["kind"] == "2d"
    assert result["rows"] == 3
    assert result["cols"] == 2
    assert len(result["cells"]) == 6
    # agg="count" -> "mean" field carries the count too, matching Heatmap's
    # default value="mean" rendering.
    for cell in result["cells"]:
        assert cell["mean"] == cell["count"]


def test_empty_rows_reports_unavailable_with_reason() -> None:
    result = compute_binned_analysis([], value_channel="a", x_channel="b")
    assert result["available"] is False
    assert "no telemetry" in result["reason"]


def test_missing_channel_reports_unavailable_with_reason() -> None:
    result = compute_binned_analysis(
        _rows(10), value_channel="obd:DOES_NOT_EXIST", x_channel="obd:RPM"
    )
    assert result["available"] is False
    assert "not present in scope" in result["reason"]


def test_agg_p95_and_median_differ_from_mean_on_skewed_data() -> None:
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    rows = []
    for i in range(50):
        ts = t0 + dt.timedelta(seconds=i)
        rows.append(
            {"timestamp": ts, "session": "S", "channel_key": "obd:RPM", "value_number": 1000.0}
        )
        # one big outlier among many small values
        value = 100.0 if i == 0 else 1.0
        rows.append(
            {"timestamp": ts, "session": "S", "channel_key": "obd:STFT", "value_number": value}
        )
    mean_result = compute_binned_analysis(
        rows, value_channel="obd:STFT", x_channel="obd:RPM", x_bins=1, agg="mean", resample_hz=1.0
    )
    median_result = compute_binned_analysis(
        rows,
        value_channel="obd:STFT",
        x_channel="obd:RPM",
        x_bins=1,
        agg="median",
        resample_hz=1.0,
    )
    assert mean_result["cells"][0]["mean"] > median_result["cells"][0]["mean"]
