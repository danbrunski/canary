"""Tests for the rolling-feature stage of the preprocessing pipeline."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.preprocessing.features import (
    FeatureConfig,
    compute_features,
    mean_column,
    roc_column,
    std_column,
)
from src.preprocessing.resampler import GAP_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.segmenter import MODE_COLUMN


def _uniform_session(
    session: str,
    n_rows: int,
    *,
    start: str = "2026-05-11T15:00:00Z",
    hz: float = 1.0,
    values: dict[str, list[float]] | None = None,
) -> pd.DataFrame:
    """Build a uniform-grid resampled-shape DataFrame."""
    interval = pd.Timedelta(seconds=1.0 / hz)
    timestamps = pd.date_range(start=start, periods=n_rows, freq=interval, tz="UTC")
    data: dict[str, object] = {
        SESSION_COLUMN: [session] * n_rows,
        TIMESTAMP_COLUMN: timestamps,
    }
    if values is not None:
        for name, vals in values.items():
            assert len(vals) == n_rows, f"{name} length mismatch"
            data[name] = vals
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_feature_config_rejects_non_positive_window():
    with pytest.raises(ValueError, match="window_seconds"):
        FeatureConfig(window_seconds=0)


def test_feature_config_rejects_min_periods_zero():
    with pytest.raises(ValueError, match="min_periods"):
        FeatureConfig(min_periods=0)


# ---------------------------------------------------------------------------
# Mean / std on simple signals
# ---------------------------------------------------------------------------


def test_mean_of_constant_signal_equals_constant():
    df = _uniform_session("s1", 10, values={"obd:RPM": [800.0] * 10})
    out = compute_features(df, config=FeatureConfig(window_seconds=5))
    means = out[mean_column("obd:RPM")]
    assert (means == 800.0).all()


def test_std_of_constant_signal_is_zero_after_first_row():
    df = _uniform_session("s1", 10, values={"obd:RPM": [800.0] * 10})
    out = compute_features(df, config=FeatureConfig(window_seconds=5))
    stds = out[std_column("obd:RPM")]
    # std of one sample is NaN by pandas convention; everything after is 0.
    assert math.isnan(stds.iloc[0])
    assert (stds.iloc[1:] == 0.0).all()


def test_mean_is_a_true_rolling_average_inside_window():
    """Within a 3-second window at 1 Hz, mean should be the 3-row trailing avg."""
    df = _uniform_session("s1", 6, values={"obd:RPM": [1, 2, 3, 4, 5, 6.0]})
    out = compute_features(df, config=FeatureConfig(window_seconds=3))
    means = out[mean_column("obd:RPM")].tolist()
    # min_periods=1 → first row is its own mean, then trailing averages
    assert means[0] == pytest.approx(1.0)
    assert means[1] == pytest.approx(1.5)
    assert means[2] == pytest.approx(2.0)  # (1+2+3)/3
    assert means[3] == pytest.approx(3.0)  # (2+3+4)/3
    assert means[4] == pytest.approx(4.0)  # (3+4+5)/3
    assert means[5] == pytest.approx(5.0)  # (4+5+6)/3


def test_std_matches_hand_calculated_trailing_window():
    df = _uniform_session("s1", 5, values={"obd:RPM": [1.0, 2.0, 3.0, 4.0, 5.0]})
    out = compute_features(df, config=FeatureConfig(window_seconds=3))
    stds = out[std_column("obd:RPM")]

    assert math.isnan(stds.iloc[0])
    assert stds.iloc[1] == pytest.approx(math.sqrt(0.5))  # sample std of [1, 2]
    assert stds.iloc[2] == pytest.approx(1.0)  # sample std of [1, 2, 3]
    assert stds.iloc[3] == pytest.approx(1.0)  # sample std of [2, 3, 4]
    assert stds.iloc[4] == pytest.approx(1.0)  # sample std of [3, 4, 5]


# ---------------------------------------------------------------------------
# Rate-of-change
# ---------------------------------------------------------------------------


def test_roc_of_linear_ramp_equals_slope():
    """A perfectly linear signal at 100 rpm/sec should yield ROC = 100 once the
    window is filled."""
    n = 20
    df = _uniform_session("s1", n, values={"obd:RPM": [800 + 100 * i for i in range(n)]})
    out = compute_features(df, config=FeatureConfig(window_seconds=5))
    roc = out[roc_column("obd:RPM")]
    # First 5 rows have no shifted partner yet → NaN
    assert roc.iloc[:5].isna().all()
    # Rows 5..19 should all be exactly 100 rpm/sec
    assert roc.iloc[5:].tolist() == pytest.approx([100.0] * 15)


def test_roc_of_constant_signal_is_zero():
    n = 20
    df = _uniform_session("s1", n, values={"obd:RPM": [800.0] * n})
    out = compute_features(df, config=FeatureConfig(window_seconds=5))
    roc = out[roc_column("obd:RPM")].iloc[5:]  # skip warmup NaNs
    assert roc.tolist() == pytest.approx([0.0] * len(roc))


def test_roc_negative_slope_decelerating_signal():
    n = 15
    df = _uniform_session("s1", n, values={"obd:RPM": [3000 - 200 * i for i in range(n)]})
    out = compute_features(df, config=FeatureConfig(window_seconds=4))
    roc = out[roc_column("obd:RPM")].iloc[4:]
    assert roc.tolist() == pytest.approx([-200.0] * len(roc))


def test_roc_uses_actual_elapsed_time_across_a_dropped_row_seam():
    """8.4: feature_pipeline._drop_fully_stale_rows can remove whole rows
    (a long power-off gap), leaving row-index-adjacent but time-distant
    neighbors on either side of the seam. A fixed row-count shift divided
    by the nominal window_seconds would understate the elapsed time and
    manufacture a slope 3x too steep right at the seam; dividing by the
    row pair's real elapsed time must recover the true underlying slope
    instead."""
    n = 20
    # A perfectly linear 100/sec ramp...
    df = _uniform_session("s1", n, values={"obd:RPM": [800 + 100 * i for i in range(n)]})
    # ...with the middle 10 seconds (rows 5..14) dropped, simulating
    # _drop_fully_stale_rows removing a long stale gap. The remaining rows
    # (0..4, 15..19) are re-indexed contiguously, so row 5 in the surviving
    # frame is 15 seconds — not 1 second — after row 4.
    kept = pd.concat([df.iloc[:5], df.iloc[15:]], ignore_index=True)

    out = compute_features(kept, config=FeatureConfig(window_seconds=5))
    roc = out[roc_column("obd:RPM")]

    # Rows 5..9 (surviving-frame index) are the ones whose 5-row-back shift
    # now reaches back across the 15-second seam. The true slope is 100/sec
    # regardless of the gap; the old fixed-window_seconds divisor would have
    # reported 300 (1500 rpm delta / 5s) here instead.
    assert roc.iloc[5:10].tolist() == pytest.approx([100.0] * 5)


# ---------------------------------------------------------------------------
# Session boundary handling
# ---------------------------------------------------------------------------


def test_window_does_not_cross_session_boundaries():
    """Two adjacent sessions: features at the start of B must not see A's tail."""
    a = _uniform_session("A", 10, values={"obd:RPM": [800.0] * 10})
    # B starts immediately after A in wall-clock time, but its values jump.
    b = _uniform_session(
        "B",
        10,
        start="2026-05-11T15:00:10Z",
        values={"obd:RPM": [3000.0] * 10},
    )
    df = pd.concat([a, b], ignore_index=True)
    out = compute_features(df, config=FeatureConfig(window_seconds=5))

    b_means = out.loc[out[SESSION_COLUMN] == "B", mean_column("obd:RPM")]
    # If the window leaked into A's tail of 800.0, the first B row would be
    # somewhere between 800 and 3000. With proper isolation, it's 3000.
    assert b_means.iloc[0] == 3000.0


def test_two_sessions_independent_warmup_in_roc():
    """ROC during B's warmup should be NaN, not the slope vs. A's last value."""
    a = _uniform_session("A", 10, values={"obd:RPM": [800.0] * 10})
    b = _uniform_session(
        "B",
        10,
        start="2026-05-11T15:00:10Z",
        values=({"obd:RPM": [3000.0 + 100 * i for i in range(10)]}),
    )
    df = pd.concat([a, b], ignore_index=True)
    out = compute_features(df, config=FeatureConfig(window_seconds=4))

    b_roc = out.loc[out[SESSION_COLUMN] == "B", roc_column("obd:RPM")]
    # First 4 rows of B have insufficient history within B → NaN.
    assert b_roc.iloc[:4].isna().all()
    # Once the window fills, ROC equals the slope (100 per second).
    assert b_roc.iloc[4:].tolist() == pytest.approx([100.0] * 6)


# ---------------------------------------------------------------------------
# Column selection
# ---------------------------------------------------------------------------


def test_skips_gap_flag_columns():
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0] * 5})
    df[f"obd:RPM{GAP_SUFFIX}"] = [False, False, True, False, False]
    out = compute_features(df)
    # Gap column stays as-is; no rolling feature columns generated for it.
    assert f"obd:RPM{GAP_SUFFIX}__mean" not in out.columns
    assert f"obd:RPM{GAP_SUFFIX}__roc" not in out.columns
    assert (out[f"obd:RPM{GAP_SUFFIX}"] == df[f"obd:RPM{GAP_SUFFIX}"]).all()


def test_skips_mode_column():
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0] * 5})
    df[MODE_COLUMN] = "idle"
    out = compute_features(df)
    assert f"{MODE_COLUMN}{'__mean'}" not in out.columns
    # The mode column itself is preserved
    assert (out[MODE_COLUMN] == "idle").all()


def test_skips_non_numeric_columns():
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0] * 5})
    df["notes"] = ["a", "b", "c", "d", "e"]
    out = compute_features(df)
    assert "notes__mean" not in out.columns
    assert (out["notes"] == ["a", "b", "c", "d", "e"]).all()


def test_explicit_value_columns_restricts_featurization():
    df = _uniform_session(
        "s1",
        5,
        values={"obd:RPM": [800.0] * 5, "obd:COOLANT": [85.0] * 5},
    )
    out = compute_features(df, value_columns=["obd:RPM"])
    assert mean_column("obd:RPM") in out.columns
    assert mean_column("obd:COOLANT") not in out.columns


def test_explicit_value_columns_rejects_non_numeric():
    df = _uniform_session("s1", 3, values={"obd:RPM": [800.0] * 3})
    df["notes"] = ["a", "b", "c"]
    with pytest.raises(TypeError, match="not numeric"):
        compute_features(df, value_columns=["notes"])


def test_existing_feature_columns_are_not_re_featurized():
    """If compute_features is called twice, it shouldn't recurse."""
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0] * 5})
    once = compute_features(df, config=FeatureConfig(window_seconds=3))
    twice = compute_features(once, config=FeatureConfig(window_seconds=3))
    feature_names = [c for c in twice.columns if c.endswith("__mean")]
    assert feature_names == [mean_column("obd:RPM")]
    # No double-featurization like obd:RPM__mean__mean
    assert "obd:RPM__mean__mean" not in twice.columns


# ---------------------------------------------------------------------------
# Feature toggles
# ---------------------------------------------------------------------------


def test_disable_mean_omits_mean_column():
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0] * 5})
    out = compute_features(df, config=FeatureConfig(include_mean=False))
    assert mean_column("obd:RPM") not in out.columns
    assert std_column("obd:RPM") in out.columns
    assert roc_column("obd:RPM") in out.columns


def test_disable_all_features_returns_input_shape():
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0] * 5})
    out = compute_features(
        df,
        config=FeatureConfig(include_mean=False, include_std=False, include_roc=False),
    )
    assert list(out.columns) == list(df.columns)
    assert len(out) == len(df)


# ---------------------------------------------------------------------------
# Empty / shape preservation
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_frame_with_feature_schema():
    """Empty frame with typed numeric columns: feature columns get appended."""
    df = pd.DataFrame(
        {
            SESSION_COLUMN: pd.Series([], dtype="object"),
            TIMESTAMP_COLUMN: pd.Series([], dtype="datetime64[us, UTC]"),
            "obd:RPM": pd.Series([], dtype="float64"),
        }
    )
    out = compute_features(df)
    assert out.empty
    assert mean_column("obd:RPM") in out.columns
    assert std_column("obd:RPM") in out.columns
    assert roc_column("obd:RPM") in out.columns


def test_output_length_matches_input_length():
    df = _uniform_session("s1", 17, values={"obd:RPM": list(range(17))})
    out = compute_features(df, config=FeatureConfig(window_seconds=4))
    assert len(out) == 17


def test_output_preserves_session_and_timestamp_columns():
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0] * 5})
    out = compute_features(df)
    assert (out[SESSION_COLUMN] == "s1").all()
    pd.testing.assert_series_equal(
        out[TIMESTAMP_COLUMN].reset_index(drop=True),
        pd.to_datetime(df[TIMESTAMP_COLUMN], utc=True).reset_index(drop=True),
        check_names=False,
    )


# ---------------------------------------------------------------------------
# Multi-channel interactions
# ---------------------------------------------------------------------------


def test_multiple_channels_each_get_independent_features():
    df = _uniform_session(
        "s1",
        10,
        values={
            "obd:RPM": [800 + 100 * i for i in range(10)],
            "obd:COOLANT": [85.0] * 10,
        },
    )
    out = compute_features(df, config=FeatureConfig(window_seconds=3))
    # RPM has a slope, COOLANT is flat
    rpm_roc = out[roc_column("obd:RPM")].iloc[3:].dropna()
    assert rpm_roc.tolist() == pytest.approx([100.0] * len(rpm_roc))
    coolant_roc = out[roc_column("obd:COOLANT")].iloc[3:]
    assert coolant_roc.tolist() == pytest.approx([0.0] * len(coolant_roc))


def test_higher_hz_resampling_keeps_window_in_seconds_consistent():
    """At 10 Hz with a 2-second window, ROC of a +50/s ramp should equal +50."""
    n = 50
    timestamps = pd.date_range(
        start="2026-05-11T15:00:00Z", periods=n, freq=pd.Timedelta(seconds=0.1), tz="UTC"
    )
    values = [800 + 5.0 * i for i in range(n)]  # +5 per row at 10 Hz = +50/sec
    df = pd.DataFrame({SESSION_COLUMN: ["s1"] * n, TIMESTAMP_COLUMN: timestamps, "obd:RPM": values})
    out = compute_features(df, config=FeatureConfig(window_seconds=2))
    roc = out[roc_column("obd:RPM")].iloc[20:]  # past warmup (2s × 10 Hz = 20 rows)
    assert roc.tolist() == pytest.approx([50.0] * len(roc))


def test_min_periods_two_yields_nan_for_first_mean():
    df = _uniform_session("s1", 5, values={"obd:RPM": [800.0, 810.0, 820.0, 830.0, 840.0]})
    out = compute_features(df, config=FeatureConfig(window_seconds=3, min_periods=2))
    means = out[mean_column("obd:RPM")]
    assert np.isnan(means.iloc[0])
    assert means.iloc[1] == pytest.approx(805.0)
