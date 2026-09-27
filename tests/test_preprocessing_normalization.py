"""Tests for the z-score scaler used by the Phase 2 preprocessing pipeline."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from src.columns import GAP_SUFFIX, MODE_COLUMN, SESSION_COLUMN, TIMESTAMP_COLUMN
from src.preprocessing.normalization import (
    SchemaMismatchError,
    ZScoreScaler,
)

# ---------------------------------------------------------------------------
# fit
# ---------------------------------------------------------------------------


def test_fit_recovers_mean_and_std_exactly():
    df = pd.DataFrame({"obd:X": [1.0, 2.0, 3.0, 4.0, 5.0]})
    scaler = ZScoreScaler.fit(df)
    # Population std (ddof=0) of [1..5] = sqrt(2)
    assert scaler.means == {"obd:X": 3.0}
    assert scaler.stds["obd:X"] == pytest.approx(np.sqrt(2.0))


def test_fit_uses_ddof_zero_population_std():
    """Pinning the convention: we use population std, not sample std."""
    df = pd.DataFrame({"obd:X": [1.0, 2.0, 3.0]})
    scaler = ZScoreScaler.fit(df)
    # population variance of [1,2,3] = 2/3
    assert scaler.stds["obd:X"] == pytest.approx(np.sqrt(2.0 / 3.0))


def test_fit_default_picks_numeric_value_columns():
    df = pd.DataFrame(
        {
            SESSION_COLUMN: ["s1"] * 3,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-11", periods=3, tz="UTC"),
            MODE_COLUMN: ["idle"] * 3,
            "obd:RPM": [800.0, 900.0, 1000.0],
            "obd:RPM" + GAP_SUFFIX: [False, False, True],
            "obd:COOLANT": [85.0, 86.0, 87.0],
            "notes": ["a", "b", "c"],
        }
    )
    scaler = ZScoreScaler.fit(df)
    # session, timestamp, mode, gap-flag, and string columns all excluded.
    assert set(scaler.columns) == {"obd:RPM", "obd:COOLANT"}


def test_fit_explicit_columns_overrides_auto_discovery():
    df = pd.DataFrame({"obd:X": [1.0, 2.0], "obd:Y": [10.0, 20.0]})
    scaler = ZScoreScaler.fit(df, columns=["obd:X"])
    assert scaler.columns == ("obd:X",)
    assert "obd:Y" not in scaler.means


def test_fit_rejects_non_numeric_in_explicit_columns():
    df = pd.DataFrame({"notes": ["a", "b"]})
    with pytest.raises(TypeError, match="not numeric"):
        ZScoreScaler.fit(df, columns=["notes"])


def test_fit_rejects_missing_column_in_explicit_columns():
    df = pd.DataFrame({"obd:X": [1.0, 2.0]})
    with pytest.raises(KeyError, match="missing required columns"):
        ZScoreScaler.fit(df, columns=["obd:X", "obd:Y"])


def test_fit_errors_on_all_nan_column():
    """All-NaN columns produce NaN mean/std; the scaler is useless. Fail loudly."""
    df = pd.DataFrame({"obd:X": [np.nan, np.nan, np.nan]})
    with pytest.raises(ValueError, match="no non-NaN values"):
        ZScoreScaler.fit(df, columns=["obd:X"])


def test_fit_skips_nans_in_partial_column():
    df = pd.DataFrame({"obd:X": [1.0, np.nan, 3.0, np.nan, 5.0]})
    scaler = ZScoreScaler.fit(df)
    # mean of [1, 3, 5] = 3; population std = sqrt(8/3)
    assert scaler.means["obd:X"] == pytest.approx(3.0)
    assert scaler.stds["obd:X"] == pytest.approx(np.sqrt(8.0 / 3.0))


# ---------------------------------------------------------------------------
# transform
# ---------------------------------------------------------------------------


def test_transform_applies_z_score():
    df = pd.DataFrame({"obd:X": [1.0, 2.0, 3.0, 4.0, 5.0]})
    scaler = ZScoreScaler.fit(df)
    transformed = scaler.transform(df)
    # Mean = 3, std = sqrt(2). Z-scores: (-2,-1,0,1,2)/sqrt(2)
    expected = np.array([-2.0, -1.0, 0.0, 1.0, 2.0]) / np.sqrt(2.0)
    assert transformed["obd:X"].tolist() == pytest.approx(expected.tolist())


def test_fit_transform_produces_zero_mean_unit_std():
    """The classic property: scaled training data has μ≈0 and σ≈1."""
    rng = np.random.default_rng(42)
    df = pd.DataFrame({"obd:X": rng.normal(loc=10.0, scale=3.0, size=1000)})
    scaler = ZScoreScaler.fit(df)
    scaled = scaler.transform(df)
    assert scaled["obd:X"].mean() == pytest.approx(0.0, abs=1e-10)
    assert scaled["obd:X"].std(ddof=0) == pytest.approx(1.0, abs=1e-10)


def test_transform_passes_non_fitted_columns_through():
    """Session/timestamp/mode columns and other extras come out untouched."""
    train = pd.DataFrame({"obd:X": [1.0, 2.0, 3.0, 4.0, 5.0]})
    scaler = ZScoreScaler.fit(train)
    df = pd.DataFrame(
        {
            SESSION_COLUMN: ["s1", "s1"],
            TIMESTAMP_COLUMN: pd.date_range("2026-05-11", periods=2, tz="UTC"),
            MODE_COLUMN: ["idle", "idle"],
            "obd:X": [3.0, 5.0],
            "notes": ["a", "b"],
        }
    )
    out = scaler.transform(df)
    assert (out[SESSION_COLUMN] == ["s1", "s1"]).all()
    assert (out[MODE_COLUMN] == ["idle", "idle"]).all()
    assert (out["notes"] == ["a", "b"]).all()


def test_transform_rejects_missing_fitted_column():
    """The plan's explicit requirement: reject inference data with mismatched schema."""
    scaler = ZScoreScaler.fit(pd.DataFrame({"obd:X": [1.0, 2.0, 3.0]}))
    bad = pd.DataFrame({"obd:Y": [10.0, 20.0]})
    with pytest.raises(SchemaMismatchError, match="missing fitted columns"):
        scaler.transform(bad)


def test_transform_preserves_nan_values():
    df = pd.DataFrame({"obd:X": [1.0, 2.0, 3.0, 4.0, 5.0]})
    scaler = ZScoreScaler.fit(df)
    inference = pd.DataFrame({"obd:X": [3.0, np.nan, 5.0]})
    out = scaler.transform(inference)
    assert out["obd:X"].iloc[0] == pytest.approx(0.0)
    assert np.isnan(out["obd:X"].iloc[1])


def test_transform_constant_channel_centers_only():
    """When σ < std_floor, transform should reduce to (x - μ): no amplification."""
    train = pd.DataFrame({"obd:CONST": [5.0, 5.0, 5.0, 5.0]})
    scaler = ZScoreScaler.fit(train)
    assert scaler.stds["obd:CONST"] == 0.0
    inference = pd.DataFrame({"obd:CONST": [5.0, 6.0, 4.0]})
    out = scaler.transform(inference)
    # Centering only: scaled = x - 5
    assert out["obd:CONST"].tolist() == pytest.approx([0.0, 1.0, -1.0])


def test_transform_input_unchanged_no_side_effects():
    """Caller's DataFrame should not be mutated by transform."""
    df = pd.DataFrame({"obd:X": [1.0, 2.0, 3.0, 4.0, 5.0]})
    scaler = ZScoreScaler.fit(df)
    original_x = df["obd:X"].copy()
    _ = scaler.transform(df)
    assert (df["obd:X"] == original_x).all()


# ---------------------------------------------------------------------------
# Serialization (model manifest path)
# ---------------------------------------------------------------------------


def test_to_dict_round_trips_through_from_dict():
    df = pd.DataFrame({"obd:RPM": [800.0, 1000.0, 1200.0], "obd:SPEED": [0.0, 30.0, 60.0]})
    scaler = ZScoreScaler.fit(df)
    payload = scaler.to_dict()
    restored = ZScoreScaler.from_dict(payload)
    assert restored.means == scaler.means
    assert restored.stds == scaler.stds
    assert restored.columns == scaler.columns
    assert restored.std_floor == scaler.std_floor


def test_to_dict_is_json_safe():
    """Manifest format must round-trip through plain JSON without custom encoders."""
    df = pd.DataFrame({"obd:RPM": [800.0, 1000.0, 1200.0]})
    scaler = ZScoreScaler.fit(df)
    payload = scaler.to_dict()
    text = json.dumps(payload)
    restored = ZScoreScaler.from_dict(json.loads(text))
    assert restored.means == scaler.means


def test_from_dict_accepts_missing_std_floor():
    """Older manifests without std_floor should still load."""
    data = {
        "means": {"obd:X": 0.0},
        "stds": {"obd:X": 1.0},
        "columns": ["obd:X"],
    }
    restored = ZScoreScaler.from_dict(data)
    assert restored.std_floor == pytest.approx(1e-9)


def test_persist_then_apply_in_a_new_process():
    """Simulate the manifest workflow: fit → serialize → deserialize → transform."""
    train = pd.DataFrame({"obd:X": [1.0, 2.0, 3.0, 4.0, 5.0]})
    scaler = ZScoreScaler.fit(train)

    # Persist
    blob = json.dumps(scaler.to_dict())

    # ... time passes; process restarts ...

    loaded = ZScoreScaler.from_dict(json.loads(blob))
    inference = pd.DataFrame({"obd:X": [3.0]})
    out = loaded.transform(inference)
    # Mean was 3 → scaled value is 0
    assert out["obd:X"].iloc[0] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Validation invariants
# ---------------------------------------------------------------------------


def test_post_init_rejects_negative_std_floor():
    with pytest.raises(ValueError, match="std_floor"):
        ZScoreScaler(means={"obd:X": 0.0}, stds={"obd:X": 1.0}, columns=("obd:X",), std_floor=-1.0)


def test_post_init_rejects_missing_means():
    with pytest.raises(ValueError, match="means missing"):
        ZScoreScaler(
            means={"obd:X": 0.0}, stds={"obd:X": 1.0, "obd:Y": 1.0}, columns=("obd:X", "obd:Y")
        )


def test_post_init_rejects_missing_stds():
    with pytest.raises(ValueError, match="stds missing"):
        ZScoreScaler(
            means={"obd:X": 0.0, "obd:Y": 0.0}, stds={"obd:X": 1.0}, columns=("obd:X", "obd:Y")
        )


# ---------------------------------------------------------------------------
# Multi-channel / multi-session interactions
# ---------------------------------------------------------------------------


def test_multiple_channels_have_independent_parameters():
    df = pd.DataFrame(
        {
            "obd:RPM": [800.0, 900.0, 1000.0, 1100.0, 1200.0],
            "obd:SPEED": [0.0, 10.0, 20.0, 30.0, 40.0],
        }
    )
    scaler = ZScoreScaler.fit(df)
    assert scaler.means["obd:RPM"] != scaler.means["obd:SPEED"]
    assert scaler.stds["obd:RPM"] != scaler.stds["obd:SPEED"]

    scaled = scaler.transform(df)
    # Both channels should sit at zero mean
    assert scaled["obd:RPM"].mean() == pytest.approx(0.0)
    assert scaled["obd:SPEED"].mean() == pytest.approx(0.0)


def test_scaler_is_global_across_sessions():
    """Fitting on multi-session data computes ONE (μ, σ) across all rows.
    The scaler is intentionally not per-session — sessions are for boundary
    handling in features, not for model-input normalization."""
    df = pd.DataFrame(
        {
            SESSION_COLUMN: ["A"] * 3 + ["B"] * 3,
            "obd:X": [1.0, 2.0, 3.0, 100.0, 200.0, 300.0],
        }
    )
    scaler = ZScoreScaler.fit(df)
    # Mean over all 6 rows is 101.0, not 2.0 (session A) or 200.0 (session B).
    assert scaler.means["obd:X"] == pytest.approx(101.0)


def test_default_column_discovery_never_picks_an_external_sensor() -> None:
    """H01.1: with no column list, the scaler's discovery applies the source gate."""
    df = pd.DataFrame(
        {"obd:X": [1.0, 2.0, 3.0], "imu:ACCEL_X": [0.1, 0.2, 0.3], "gps:speed": [5.0, 6.0, 7.0]}
    )
    assert ZScoreScaler.fit(df).columns == ("obd:X",)
