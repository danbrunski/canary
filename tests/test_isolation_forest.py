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
    trained = fit_isolation_forest(
        _two_mode_frame(),
        _IFConfig(),  # no calibration_holdout_fraction / target_flag_rate
        feature_columns=["obd:RPM", "obd:SPEED", "obd:LOAD"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    for mode in trained.manifest.modes:
        assert not mode.calibrated
        assert mode.score_threshold is None
        assert mode.holdout_samples == 0
    assert trained.score_thresholds == {}
