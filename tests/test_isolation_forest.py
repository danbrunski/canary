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
    LABEL_COLUMN,
    MISSING_FEATURES_COLUMN,
    MODEL_MODE_COLUMN,
    SCORE_COLUMN,
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


def test_score_rejects_untrained_operating_mode() -> None:
    frame = _two_mode_frame()
    trained = fit_isolation_forest(
        frame,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED"],
        fitted_at="2026-05-12T01:02:03Z",
    )
    inference = frame.iloc[[0]].copy()
    inference.loc[inference.index[0], MODE_COLUMN] = "wot"

    with pytest.raises(ValueError, match="no IsolationForest model"):
        trained.score(inference)


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
