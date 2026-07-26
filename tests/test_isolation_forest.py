"""Tests for the Phase 2 Isolation Forest baseline model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from src.models.isolation_forest import (  # noqa: E402
    CALIBRATED_ANOMALY_COLUMN,
    CALIBRATED_THRESHOLD_COLUMN,
    LABEL_COLUMN,
    MISSING_FEATURE_FRACTION_COLUMN,
    MISSING_FEATURES_COLUMN,
    MODEL_MODE_COLUMN,
    SCORE_COLUMN,
    ModeTrainingSummary,
    default_manifest_path,
    fit_isolation_forest,
    load_isolation_forest_model,
)
from src.preprocessing.resampler import GAP_SUFFIX, SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402
from src.preprocessing.segmenter import MODE_COLUMN  # noqa: E402


@dataclass(frozen=True)
class _IFConfig:
    enabled: bool = True
    n_estimators: int = 50
    contamination: float = 0.05
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 42
    # Calibration on by default so the load-time guard (which refuses an
    # artifact with no calibrated mode) doesn't trip the standard tests.
    # Tests that specifically want pre-7.2 / uncalibrated artifacts set
    # ``calibration_holdout_fraction=0`` to opt out.
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.02


def _two_mode_frame() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n_per_mode = 60
    timestamps = pd.date_range("2026-05-11T00:00:00Z", periods=n_per_mode * 2, freq="1s")
    modes = ["idle"] * n_per_mode + ["city"] * n_per_mode
    rpm = np.concatenate(
        [
            rng.normal(loc=800.0, scale=12.0, size=n_per_mode),
            rng.normal(loc=2200.0, scale=80.0, size=n_per_mode),
        ]
    )
    speed = np.concatenate(
        [
            rng.normal(loc=0.0, scale=0.2, size=n_per_mode),
            rng.normal(loc=35.0, scale=3.0, size=n_per_mode),
        ]
    )
    load = np.concatenate(
        [
            rng.normal(loc=18.0, scale=1.0, size=n_per_mode),
            rng.normal(loc=43.0, scale=4.0, size=n_per_mode),
        ]
    )
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["session-a"] * n_per_mode + ["session-b"] * n_per_mode,
            TIMESTAMP_COLUMN: timestamps,
            MODE_COLUMN: modes,
            "obd:RPM": rpm,
            "obd:SPEED": speed,
            "obd:LOAD": load,
            "obd:RPM" + GAP_SUFFIX: [False] * n_per_mode * 2,
            "notes": ["clean"] * n_per_mode * 2,
        }
    )


def test_fit_trains_one_model_per_operating_mode_and_emits_manifest() -> None:
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=25, contamination=0.1),
        fitted_at="2026-05-12T01:02:03Z",
        model_version="iforest-test",
    )

    assert set(trained.models) == {"city", "idle"}
    assert trained.feature_columns == ("obd:RPM", "obd:SPEED", "obd:LOAD")

    manifest = trained.manifest.to_dict()
    assert manifest["model_type"] == "isolation_forest"
    assert manifest["model_version"] == "iforest-test"
    assert manifest["created_at"] == "2026-05-12T01:02:03Z"
    assert manifest["training_data_hash"].startswith("sha256:")
    assert manifest["training_start_timestamp"] == "2026-05-11T00:00:00Z"
    assert manifest["training_end_timestamp"] == "2026-05-11T00:01:59Z"
    assert manifest["training_sessions"] == ["session-a", "session-b"]
    assert manifest["hyperparams"]["n_estimators"] == 25
    assert manifest["hyperparams"]["contamination"] == 0.1
    assert manifest["hyperparams"]["per_mode_contamination"] == {}
    assert manifest["hyperparams"]["random_state"] == 42
    assert manifest["hyperparams"]["n_jobs"] == 1
    assert manifest["scaler"]["columns"] == ["obd:RPM", "obd:SPEED", "obd:LOAD"]
    assert manifest["schema"]["feature_columns"] == ["obd:RPM", "obd:SPEED", "obd:LOAD"]
    assert manifest["schema"]["feature_spec_version"] == 1
    assert manifest["schema"]["source_prefixes"] == ["obd"]
    assert manifest["schema"]["feature_health"]["selected_feature_count"] == 3
    assert manifest["schema"]["feature_health"]["constant_columns"] == []
    assert {mode["mode"] for mode in manifest["modes"]} == {"city", "idle"}
    assert {mode["contamination"] for mode in manifest["modes"]} == {0.1}


def test_score_orients_anomaly_score_so_outlier_is_larger() -> None:
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=75, contamination=0.05),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )

    inference = pd.DataFrame(
        {
            SESSION_COLUMN: ["session-c", "session-c"],
            TIMESTAMP_COLUMN: pd.to_datetime(
                ["2026-05-12T00:00:00Z", "2026-05-12T00:00:01Z"], utc=True
            ),
            MODE_COLUMN: ["idle", "idle"],
            "obd:RPM": [805.0, 5000.0],
            "obd:SPEED": [0.0, 120.0],
        }
    )
    scored = trained.score(inference)

    assert scored.loc[1, SCORE_COLUMN] > scored.loc[0, SCORE_COLUMN]
    assert scored.loc[1, LABEL_COLUMN] == -1
    assert scored.loc[0, MODEL_MODE_COLUMN] == "idle"
    assert scored.loc[1, MODEL_MODE_COLUMN] == "idle"


def _mode_gated_deviation_frame() -> pd.DataFrame:
    """Two modes where each carries a deviation surface gated to the *other*
    mode (all-NaN outside its own mode) — the shape that blanked the detector:
    every row had at least one NaN in the global feature set."""
    rng = np.random.default_rng(7)
    n = 60
    timestamps = pd.date_range("2026-05-11T00:00:00Z", periods=n * 2, freq="1s")
    idle_only = np.concatenate([rng.normal(0.0, 1.0, n), np.full(n, np.nan)])
    warmup_only = np.concatenate([np.full(n, np.nan), rng.normal(0.0, 1.0, n)])
    rpm = np.concatenate([rng.normal(800.0, 12.0, n), rng.normal(1200.0, 40.0, n)])
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * (n * 2),
            TIMESTAMP_COLUMN: timestamps,
            MODE_COLUMN: ["idle"] * n + ["warmup"] * n,
            "obd:RPM": rpm,
            "deviation:idle_only": idle_only,
            "deviation:warmup_only": warmup_only,
        }
    )


def test_per_mode_feature_set_excludes_other_modes_deviation_surfaces() -> None:
    trained = fit_isolation_forest(
        _mode_gated_deviation_frame(),
        _IFConfig(n_estimators=25),
        feature_columns=["obd:RPM", "deviation:idle_only", "deviation:warmup_only"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    # Each mode's model is trained only on the columns populated in that mode.
    assert trained.mode_feature_columns["idle"] == ("obd:RPM", "deviation:idle_only")
    assert trained.mode_feature_columns["warmup"] == ("obd:RPM", "deviation:warmup_only")
    assert trained.manifest.to_dict()["schema"]["mode_feature_columns"] == {
        "idle": ["obd:RPM", "deviation:idle_only"],
        "warmup": ["obd:RPM", "deviation:warmup_only"],
    }


def test_other_modes_deviation_nan_does_not_suppress_the_verdict() -> None:
    trained = fit_isolation_forest(
        _mode_gated_deviation_frame(),
        _IFConfig(n_estimators=25),
        feature_columns=["obd:RPM", "deviation:idle_only", "deviation:warmup_only"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    # A live idle row: warmup_only is NaN by design (wrong mode) but the idle
    # model never required it, so the verdict must score, not flag missing.
    idle_row = pd.DataFrame(
        {
            SESSION_COLUMN: ["live"],
            TIMESTAMP_COLUMN: pd.to_datetime(["2026-05-12T00:00:00Z"], utc=True),
            MODE_COLUMN: ["idle"],
            "obd:RPM": [805.0],
            "deviation:idle_only": [0.1],
            "deviation:warmup_only": [np.nan],
        }
    )
    scored = trained.score(idle_row)
    assert bool(scored.loc[0, MISSING_FEATURES_COLUMN]) is False
    assert np.isfinite(scored.loc[0, SCORE_COLUMN])
    assert scored.loc[0, MODEL_MODE_COLUMN] == "idle"

    # But a genuinely-missing in-mode feature still suppresses (idle_only NaN).
    broken = idle_row.copy()
    broken["deviation:idle_only"] = [np.nan]
    scored_broken = trained.score(broken)
    assert bool(scored_broken.loc[0, MISSING_FEATURES_COLUMN]) is True


def test_rows_with_missing_features_are_labeled_anomalous() -> None:
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )

    inference = frame.iloc[[0]].copy()
    inference.loc[inference.index[0], "obd:RPM"] = np.nan
    scored = trained.score(inference)

    assert scored.loc[inference.index[0], LABEL_COLUMN] == -1
    assert np.isinf(scored.loc[inference.index[0], SCORE_COLUMN])
    assert bool(scored.loc[inference.index[0], MISSING_FEATURES_COLUMN]) is True


def test_training_drops_rows_with_missing_features_and_records_count() -> None:
    frame = _two_mode_frame()
    frame.loc[0, "obd:RPM"] = np.nan
    frame.loc[10, "obd:SPEED"] = np.nan

    trained = fit_isolation_forest(
        frame,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )

    assert trained.manifest.input_rows == len(frame)
    assert trained.manifest.complete_rows == len(frame) - 2
    assert trained.manifest.dropped_missing_feature_rows == 2


def test_per_mode_contamination_override_is_recorded_in_manifest() -> None:
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(contamination=0.05, per_mode_contamination={"idle": 0.02}),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )

    manifest = trained.manifest.to_dict()
    assert manifest["hyperparams"]["per_mode_contamination"] == {"idle": 0.02}
    by_mode = {mode["mode"]: mode for mode in manifest["modes"]}
    assert by_mode["idle"]["contamination"] == 0.02
    assert by_mode["city"]["contamination"] == 0.05


def test_save_and_load_round_trip_joblib_artifact_and_json_manifest(tmp_path: Path) -> None:
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=20),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
        model_version="roundtrip",
    )

    artifact = tmp_path / "isolation_forest.joblib"
    trained.save(artifact)

    manifest_path = default_manifest_path(artifact)
    assert artifact.exists()
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text()) == trained.manifest.to_dict()

    loaded = load_isolation_forest_model(artifact)
    assert loaded.manifest.to_dict() == trained.manifest.to_dict()

    inference = frame.iloc[:6].copy()
    before = trained.score(inference)
    after = loaded.score(inference)
    np.testing.assert_allclose(before[SCORE_COLUMN], after[SCORE_COLUMN])
    assert before[LABEL_COLUMN].tolist() == after[LABEL_COLUMN].tolist()


def test_load_rejects_manifest_with_all_nan_feature_health(tmp_path: Path) -> None:
    joblib = pytest.importorskip("joblib")
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=20),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    artifact = tmp_path / "isolation_forest.joblib"
    trained.save(artifact)

    payload = joblib.load(artifact)
    payload["manifest"]["schema"]["feature_health"]["all_nan_columns"] = ["obd:RPM"]
    joblib.dump(payload, artifact)

    with pytest.raises(ValueError, match="all-NaN"):
        load_isolation_forest_model(artifact)


def test_load_rejects_artifact_with_no_calibrated_threshold(tmp_path: Path) -> None:
    """Pre-Phase-7.2 artifacts (no ``score_threshold``) refuse to load.

    The May 18 IF artifact in production had this exact shape and quietly
    locked the detector at severity=0 forever. The guard turns the silent
    no-op into a loud refusal at deploy time.
    """
    joblib = pytest.importorskip("joblib")
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=20),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    artifact = tmp_path / "isolation_forest.joblib"
    trained.save(artifact)

    # Strip the calibrated threshold from every mode, mimicking a manifest
    # written before Phase 7.2 added the field.
    payload = joblib.load(artifact)
    for mode in payload["manifest"]["modes"]:
        mode["score_threshold"] = None
    joblib.dump(payload, artifact)

    with pytest.raises(ValueError, match="no calibrated score_threshold"):
        load_isolation_forest_model(artifact)


def test_load_rejects_scaler_schema_mismatch(tmp_path: Path) -> None:
    joblib = pytest.importorskip("joblib")
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=20),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    artifact = tmp_path / "isolation_forest.joblib"
    trained.save(artifact)

    payload = joblib.load(artifact)
    payload["scaler"]["columns"] = ["obd:SPEED", "obd:RPM"]
    joblib.dump(payload, artifact)

    with pytest.raises(ValueError, match="scaler columns"):
        load_isolation_forest_model(artifact)


def test_score_withholds_untrained_mode_with_multiple_models() -> None:
    """An unmodeled mode no longer crashes the call; with >1 model and no
    single-model fallback those rows are withheld (flagged), and the other
    rows still score normally."""
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    inference = frame.iloc[[0, 1]].copy()
    inference.loc[inference.index[0], MODE_COLUMN] = "wot"  # untrained
    # row 1 stays "idle" (a trained mode)

    scored = trained.score(inference)  # no raise

    assert bool(scored.loc[scored.index[0], MISSING_FEATURES_COLUMN]) is True
    assert scored.loc[scored.index[0], LABEL_COLUMN] == -1
    assert bool(scored.loc[scored.index[1], MISSING_FEATURES_COLUMN]) is False
    assert pd.notna(scored.loc[scored.index[1], SCORE_COLUMN])
    assert scored.loc[scored.index[1], MODEL_MODE_COLUMN] == "idle"


def test_score_routes_untrained_mode_to_single_model_fallback() -> None:
    """A single-model bundle routes every mode (even unmodeled ones) through
    that one model — the 'collapse to a single model' contract."""
    idle_only = _two_mode_frame()
    idle_only = idle_only[idle_only[MODE_COLUMN] == "idle"].reset_index(drop=True)
    trained = fit_isolation_forest(
        idle_only,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    assert len(trained.models) == 1

    inference = idle_only.iloc[[0]].copy()
    inference.loc[inference.index[0], MODE_COLUMN] = "wot"  # not a trained mode

    scored = trained.score(inference)  # no raise

    assert bool(scored.loc[scored.index[0], MISSING_FEATURES_COLUMN]) is False
    assert pd.notna(scored.loc[scored.index[0], SCORE_COLUMN])
    # MODEL_MODE_COLUMN records the model actually used (the fallback), so a
    # routed score is observable downstream.
    assert scored.loc[scored.index[0], MODEL_MODE_COLUMN] == "idle"


def test_fit_rejects_disabled_config() -> None:
    with pytest.raises(ValueError, match="disabled"):
        fit_isolation_forest(_two_mode_frame(), _IFConfig(enabled=False))


def test_fit_rejects_all_missing_features() -> None:
    frame = _two_mode_frame()
    frame["obd:RPM"] = np.nan

    with pytest.raises(ValueError, match="all-NaN"):
        fit_isolation_forest(
            frame,
            _IFConfig(),
            feature_columns=["obd:RPM"],
            fitted_at="2026-05-12T01:02:03Z",
        )


# ---------------------------------------------------------------------------
# Phase 7.2 — held-out per-mode calibrated thresholds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CalibratedIFConfig(_IFConfig):
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.1


def test_calibration_records_threshold_and_scale_in_manifest() -> None:
    trained = fit_isolation_forest(
        _two_mode_frame(),
        _CalibratedIFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    for mode in trained.manifest.modes:
        assert mode.calibrated
        assert mode.score_threshold is not None
        assert mode.score_scale is not None and mode.score_scale > 0.0
        assert mode.holdout_samples == 12  # floor(60 * 0.2)
        assert mode.target_flag_rate == 0.1
        assert mode.achieved_flag_rate is not None
        assert 0.0 <= mode.achieved_flag_rate <= 0.5
    hp = trained.manifest.hyperparams
    assert hp["calibration_holdout_fraction"] == 0.2
    assert hp["target_flag_rate"] == 0.1
    assert set(trained.score_thresholds) == {"idle", "city"}
    assert set(trained.score_scales) == {"idle", "city"}


def test_score_emits_calibrated_columns_and_flags_injected_outlier() -> None:
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _CalibratedIFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    probe = frame.copy()
    probe.loc[probe.index[0], ["obd:RPM", "obd:SPEED", "obd:LOAD"]] = [9e4, 9e3, 9e3]
    scored = trained.score(probe)

    assert CALIBRATED_THRESHOLD_COLUMN in scored
    assert CALIBRATED_ANOMALY_COLUMN in scored
    assert str(scored[CALIBRATED_ANOMALY_COLUMN].dtype) == "boolean"
    # Both modes are calibrated → every row has a finite threshold + verdict.
    assert scored[CALIBRATED_THRESHOLD_COLUMN].notna().all()
    assert scored[CALIBRATED_ANOMALY_COLUMN].notna().all()
    outlier = scored.loc[probe.index[0]]
    assert bool(outlier[CALIBRATED_ANOMALY_COLUMN]) is True
    assert outlier[SCORE_COLUMN] >= outlier[CALIBRATED_THRESHOLD_COLUMN]


def test_calibration_round_trips_through_save_load(tmp_path: Path) -> None:
    trained = fit_isolation_forest(
        _two_mode_frame(),
        _CalibratedIFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    artifact = tmp_path / "if.joblib"
    trained.save(artifact)
    reloaded = load_isolation_forest_model(artifact)
    before = {m.mode: (m.score_threshold, m.score_scale) for m in trained.manifest.modes}
    after = {m.mode: (m.score_threshold, m.score_scale) for m in reloaded.manifest.modes}
    assert before == after
    assert reloaded.score_thresholds == trained.score_thresholds


def test_mode_summary_from_dict_is_back_compat_without_calibration() -> None:
    # A manifest written before 7.2 has no calibration keys.
    legacy = {
        "mode": "idle",
        "n_samples": 100,
        "contamination": 0.01,
        "sessions": ["s"],
        "start_timestamp": None,
        "end_timestamp": None,
    }
    summary = ModeTrainingSummary.from_dict(legacy)
    assert summary.score_threshold is None
    assert summary.score_scale is None
    assert summary.holdout_samples == 0
    assert summary.calibrated is False


def test_legacy_config_without_calibration_attrs_disables_it() -> None:
    @dataclass(frozen=True)
    class _LegacyConfig:
        # No calibration_holdout_fraction / target_flag_rate — getattr falls
        # back to 0.0 in ``_calibration_params``, disabling Phase 7.2 entirely.
        enabled: bool = True
        n_estimators: int = 50
        contamination: float = 0.05
        per_mode_contamination: dict[str, float] = field(default_factory=dict)
        random_state: int = 42

    trained = fit_isolation_forest(
        _two_mode_frame(),
        _LegacyConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    for mode in trained.manifest.modes:
        assert not mode.calibrated
        assert mode.score_threshold is None
        assert mode.holdout_samples == 0
    assert trained.score_thresholds == {}


# ---------------------------------------------------------------------------
# Task 2.2 — min_samples_per_mode floor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MinSamplesIFConfig(_IFConfig):
    min_samples_per_mode: int = 30


def _three_mode_frame_with_sparse_mode() -> pd.DataFrame:
    """`_two_mode_frame` plus a third mode with only a handful of rows —
    too few to trust a trained forest (Task 2.2)."""
    frame = _two_mode_frame()
    rng = np.random.default_rng(7)
    n_sparse = 5
    sparse = pd.DataFrame(
        {
            SESSION_COLUMN: ["session-c"] * n_sparse,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-11T00:05:00Z", periods=n_sparse, freq="1s"),
            MODE_COLUMN: ["wot"] * n_sparse,
            "obd:RPM": rng.normal(loc=4500.0, scale=100.0, size=n_sparse),
            "obd:SPEED": rng.normal(loc=60.0, scale=2.0, size=n_sparse),
            "obd:LOAD": rng.normal(loc=80.0, scale=3.0, size=n_sparse),
            "obd:RPM" + GAP_SUFFIX: [False] * n_sparse,
            "notes": ["clean"] * n_sparse,
        }
    )
    return pd.concat([frame, sparse], ignore_index=True)


def test_sparse_mode_is_skipped_and_listed_in_manifest() -> None:
    frame = _three_mode_frame_with_sparse_mode()
    trained = fit_isolation_forest(
        frame,
        _MinSamplesIFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )

    # The sparse mode never got a model persisted...
    assert "wot" not in trained.models
    assert set(trained.models) == {"idle", "city"}

    # ...and is listed in the manifest instead, with its actual fit-row count.
    skipped = {mode.mode: mode for mode in trained.manifest.skipped_modes}
    assert set(skipped) == {"wot"}
    assert skipped["wot"].fit_rows == 5
    assert skipped["wot"].min_samples_required == 30

    manifest = trained.manifest.to_dict()
    assert manifest["skipped_modes"] == [{"mode": "wot", "fit_rows": 5, "min_samples_required": 30}]


def test_min_samples_per_mode_floor_defaults_to_disabled_for_legacy_configs() -> None:
    """Configs/test doubles predating 2.2 (no ``min_samples_per_mode``
    attribute) train every mode regardless of size — the exact legacy
    behavior — via the same defensive-``getattr`` fallback ``_calibration_params``
    already uses for its own knobs."""

    @dataclass(frozen=True)
    class _NoFloorConfig:
        enabled: bool = True
        n_estimators: int = 50
        contamination: float = 0.05
        per_mode_contamination: dict[str, float] = field(default_factory=dict)
        random_state: int = 42

    frame = _three_mode_frame_with_sparse_mode()
    trained = fit_isolation_forest(
        frame,
        _NoFloorConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    assert set(trained.models) == {"idle", "city", "wot"}
    assert trained.manifest.skipped_modes == ()


# ---------------------------------------------------------------------------
# Task 2.4 — scaler leakage
# ---------------------------------------------------------------------------


def test_scaler_fits_only_on_fit_index_rows_not_calibration_holdout() -> None:
    """The z-score scaler is fit on each mode's *fit* rows only — not its
    calibration-holdout tail. Injecting a wild shift into idle's held-out
    rows (the timestamp-ordered tail) must not move the scaler's obd:RPM
    mean, which it would if the scaler still fit on the full pre-split
    frame (the leak this task retires)."""
    frame = _two_mode_frame()

    def _holdout_index(mode: str) -> pd.Index:
        rows = frame[frame[MODE_COLUMN] == mode].sort_values(TIMESTAMP_COLUMN)
        return rows.index[-12:]  # floor(60 * 0.2) == 12, matches the calibration split

    idle_holdout = _holdout_index("idle")
    city_holdout = _holdout_index("city")
    # Expected scaler mean: every mode's *fit* rows (excluding each mode's own
    # holdout tail), from the original (unshifted) values — the union the
    # scaler should be fit on regardless of the shift injected below.
    fit_index = frame.index.difference(idle_holdout).difference(city_holdout)
    fit_only_mean = frame.loc[fit_index, "obd:RPM"].mean()

    shifted = frame.copy()
    shifted.loc[idle_holdout, "obd:RPM"] = 500_000.0  # would blow up a leaked mean

    trained = fit_isolation_forest(
        shifted,
        _CalibratedIFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )

    assert trained.scaler.means["obd:RPM"] == pytest.approx(fit_only_mean, abs=1e-6)


def _sparse_mode_with_exclusive_deviation_frame() -> pd.DataFrame:
    """``idle`` is densely populated; ``wot`` is sparse (5 rows) and the only
    mode carrying ``deviation:wot_only`` (NaN everywhere else) — the shape
    that crashed 2.4's scaler fit once 2.2's floor skips ``wot``: the column
    is then NaN across the *entire* surviving-mode (idle) row union."""
    rng = np.random.default_rng(11)
    n_idle = 60
    n_wot = 5
    timestamps = pd.date_range("2026-05-11T00:00:00Z", periods=n_idle + n_wot, freq="1s")
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["s"] * (n_idle + n_wot),
            TIMESTAMP_COLUMN: timestamps,
            MODE_COLUMN: ["idle"] * n_idle + ["wot"] * n_wot,
            "obd:RPM": np.concatenate(
                [rng.normal(800.0, 12.0, n_idle), rng.normal(4500.0, 100.0, n_wot)]
            ),
            "deviation:wot_only": np.concatenate(
                [np.full(n_idle, np.nan), rng.normal(0.0, 1.0, n_wot)]
            ),
        }
    )


def test_scaler_skips_column_exclusive_to_a_floor_skipped_mode() -> None:
    """A mode-gated column that's NaN everywhere except a mode 2.2's
    ``min_samples_per_mode`` floor goes on to skip used to starve the 2.4
    scaler fit of every non-NaN row for that column — ``ZScoreScaler.fit``
    raised on the all-NaN column and crashed the whole run instead of just
    excluding the skipped mode (the bug the verifier reproduced)."""
    frame = _sparse_mode_with_exclusive_deviation_frame()

    trained = fit_isolation_forest(
        frame,
        _MinSamplesIFConfig(),
        feature_columns=["obd:RPM", "deviation:wot_only"],
        fitted_at="2026-05-12T01:02:03Z",
    )

    # wot was skipped (too few fit rows) exactly as in the plain 2.2 case.
    assert set(trained.models) == {"idle"}
    skipped = {mode.mode for mode in trained.manifest.skipped_modes}
    assert skipped == {"wot"}
    assert trained.mode_feature_columns["idle"] == ("obd:RPM",)

    # The scaler still declares the full feature schema (the manifest
    # contract other code relies on), but the column no surviving mode uses
    # gets an inert identity placeholder rather than an impossible all-NaN
    # fit — dead weight no model ever reads.
    assert trained.scaler.columns == ("obd:RPM", "deviation:wot_only")
    assert trained.scaler.means["deviation:wot_only"] == 0.0
    assert trained.scaler.stds["deviation:wot_only"] == 1.0
    # obd:RPM, which idle's model does use, still gets a real fit off idle's
    # own *fit* rows (its calibration-holdout tail excluded, per Task 2.4).
    idle_rows = frame[frame[MODE_COLUMN] == "idle"].sort_values(TIMESTAMP_COLUMN)
    idle_fit_mean = idle_rows.iloc[: -int(len(idle_rows) * 0.2)]["obd:RPM"].mean()
    assert trained.scaler.means["obd:RPM"] == pytest.approx(idle_fit_mean, abs=1e-6)


# ---------------------------------------------------------------------------
# Partial-missingness tolerance (2026-07-26). Requiring a fully populated row
# was unsatisfiable on real telemetry — OBD arrives round-robin at 0.1-0.5 Hz
# against a 1 Hz grid, so 0 of 26,843 corpus rows had every trained feature
# present and the IF self-suppressed on 100% of ticks (Pi: missing_feature_rate
# 1.0). Small gaps are now imputed to the training mean; large ones still
# withhold.
# ---------------------------------------------------------------------------


def _wide_feature_frame(n_features: int = 40, n_rows: int = 200) -> pd.DataFrame:
    """Enough features that one NaN is a small *fraction* of the vector."""
    rng = np.random.default_rng(7)
    data: dict[str, object] = {
        SESSION_COLUMN: ["s"] * n_rows,
        TIMESTAMP_COLUMN: pd.date_range("2026-07-22T14:00:00Z", periods=n_rows, freq="1s"),
        MODE_COLUMN: ["city"] * n_rows,
    }
    for i in range(n_features):
        data[f"obd:CH{i:02d}"] = rng.normal(loc=100.0 + i, scale=5.0, size=n_rows)
    return pd.DataFrame(data)


def test_score_imputes_small_gaps_instead_of_withholding() -> None:
    """One NaN in a 40-feature row is 2.5% missing — under the ceiling, so the
    row must produce a real finite score rather than self-suppressing."""
    frame = _wide_feature_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=25, contamination=0.05),
        fitted_at="2026-07-26T21:00:00Z",
    )
    row = frame.iloc[[0]].copy()
    row["obd:CH00"] = [np.nan]  # 1 of 40 == 2.5%
    scored = trained.score(row)
    assert bool(scored.loc[0, MISSING_FEATURES_COLUMN]) is False
    assert np.isfinite(scored.loc[0, SCORE_COLUMN])
    assert scored.loc[0, MISSING_FEATURE_FRACTION_COLUMN] == pytest.approx(1 / 40)


def test_score_still_withholds_when_too_much_is_missing() -> None:
    """Half the vector missing is not a small gap — imputing that far would
    score a row that is mostly training-mean, i.e. blind toward 'normal'."""
    frame = _wide_feature_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=25, contamination=0.05),
        fitted_at="2026-07-26T21:00:00Z",
    )
    row = frame.iloc[[0]].copy()
    for i in range(20):  # 20 of 40 == 50%
        row[f"obd:CH{i:02d}"] = [np.nan]
    scored = trained.score(row)
    assert bool(scored.loc[0, MISSING_FEATURES_COLUMN]) is True
    assert scored.loc[0, SCORE_COLUMN] == np.inf
    assert scored.loc[0, MISSING_FEATURE_FRACTION_COLUMN] == pytest.approx(0.5)


def test_missing_feature_fraction_recorded_for_complete_rows_too() -> None:
    """Recorded on every scored row, so partial degradation is observable
    rather than collapsing into one boolean."""
    frame = _wide_feature_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(n_estimators=25, contamination=0.05),
        fitted_at="2026-07-26T21:00:00Z",
    )
    scored = trained.score(frame.iloc[[5]])  # keeps index 5, so read positionally
    assert scored.iloc[0][MISSING_FEATURE_FRACTION_COLUMN] == pytest.approx(0.0)
