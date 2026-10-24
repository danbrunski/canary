"""P02.12: behavior deviations reach training, as they reach serving.

``build_feature_frame`` defaulted to the repo-level
``data/behavior_models.yaml``, which no vehicle has, and no trainer passed a
path. So trainer frames carried no ``deviation:*`` columns while the daemon's
live frames, built from the scoped ``config.daemon.behavior_models``, did.
The default is now the config's own file, the cache key and the feature spec
hash that file, ``featurize_wide_frame`` has no default at all, and a detector
trainer refuses a non-default-profile recording (R03.12), so the deviation
columns train on default-profile rows only.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.behavior.behavior_models import load_behavior_models
from src.config import CanaryConfig, UnscopedConfigError
from src.features.feature_cache import build_feature_frame_cache_key
from src.features.feature_pipeline import (
    build_feature_frame,
    build_feature_spec,
    configured_behavior_models,
    featurize_perturbed_frame,
    featurize_wide_frame,
)
from src.models.evaluation import (
    NonDefaultProfileTrainingError,
    refuse_non_default_profile_sessions,
)
from src.models.training_entry import (
    SEQUENCE_BACKBONES,
    train_isolation_forest,
    train_sequence_detector,
)
from src.storage.parquet_writer import write_records_to_parquet
from src.telemetry import TelemetryRecord
from src.vehicle.profiles import resolve_profile_for_tooling, scope_config
from tests.configs import EXAMPLE_YAML, example_config

DEVIATION = "deviation:timing_vs_rpm_load"


def _behavior_models_yaml() -> dict[str, Any]:
    """One 2-D surface over rpm x load that expects 10 deg at idle."""
    return {
        "schema_version": 1,
        "models": [
            {
                "name": "timing_vs_rpm_load",
                "kind": "surface_2d",
                "output": "obd:TIMING_ADVANCE",
                "axes": [
                    {"name": "rpm", "channel": "obd:RPM", "bin_edges": [0.0, 1000.0, 2000.0]},
                    {"name": "load", "channel": "obd:ENGINE_LOAD", "bin_edges": [0.0, 50.0, 100.0]},
                ],
                "fit": {
                    "kind": "grid_binned_mean",
                    "cells": [
                        {"mean": 10.0, "std": 1.0, "count": 20},
                        {"mean": 15.0, "std": 1.0, "count": 20},
                        {"mean": 20.0, "std": 1.0, "count": 20},
                        {"mean": 25.0, "std": 1.0, "count": 20},
                    ],
                    "sample_count": 80,
                    "fitted_at": "2026-01-01T00:00:00Z",
                },
                "deviation": {"kind": "absolute", "warning_band": 3.0, "critical_band": 8.0},
            }
        ],
    }


def _write_session(root: Path, name: str, *, rows: int = 40) -> Path:
    session = root / name
    records = []
    for i in range(rows):
        t = 1_780_000_000.0 + i
        records.extend(
            [
                TelemetryRecord.obd(t, "RPM", 500.0, "rpm"),
                TelemetryRecord.obd(t, "ENGINE_LOAD", 25.0, "%"),
                TelemetryRecord.obd(t, "TIMING_ADVANCE", 15.0, "degBTDC"),
            ]
        )
    write_records_to_parquet(records, session, max_rows=10_000)
    return session


def _config(behavior_models: Path) -> CanaryConfig:
    """A profile-less config (its paths are the vehicle's) naming ``behavior_models``."""
    base = example_config(EXAMPLE_YAML)
    return base.model_copy(
        update={"daemon": base.daemon.model_copy(update={"behavior_models": behavior_models})}
    )


@pytest.fixture
def models_path(tmp_path: Path) -> Path:
    path = tmp_path / "vehicle" / "behavior_models.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(_behavior_models_yaml()))
    return path


def _scoped_xterra() -> CanaryConfig:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML)
    return scope_config(config, resolve_profile_for_tooling(config, None))


# ---------------------------------------------------------------------------
# The default is the configured file
# ---------------------------------------------------------------------------


def test_build_feature_frame_reads_the_configured_behavior_models_by_default(
    tmp_path: Path, models_path: Path
) -> None:
    session = _write_session(tmp_path / "sessions", "2026-09-20T152758")
    features = build_feature_frame([session], _config(models_path), max_workers=1)
    assert DEVIATION in features.columns
    valued = features[DEVIATION].dropna()
    assert not valued.empty
    assert valued.eq(5.0).all(), "actual 15 deg against the surface's 10 deg"


def test_the_scoped_vehicle_frame_carries_every_served_deviation(tmp_path: Path) -> None:
    """Through the real scoped config, the trainer frame gets a column for
    every model the daemon serves, with no path passed (the Pi's shape)."""
    scoped = _scoped_xterra()
    assert scoped.daemon.behavior_models.is_file()
    served = load_behavior_models(scoped.daemon.behavior_models)
    session = _write_session(tmp_path / "sessions", "2026-09-20T152758")
    features = build_feature_frame([session], scoped, max_workers=1)
    expected = {f"deviation:{model.name}" for model in served.models}
    assert expected, "the vehicle serves behavior models"
    assert expected <= set(features.columns)


def test_configured_behavior_models_is_the_scoped_file_and_refuses_unscoped(
    models_path: Path,
) -> None:
    models = configured_behavior_models(_config(models_path))
    assert models is not None and [m.name for m in models.models] == ["timing_vs_rpm_load"]
    assert configured_behavior_models(_config(models_path.parent / "absent.yaml")) is None
    with pytest.raises(UnscopedConfigError):
        configured_behavior_models(CanaryConfig.from_yaml(EXAMPLE_YAML))


def test_featurization_states_its_behavior_models() -> None:
    """``behavior_models=None`` as a default let offline callers featurize
    without the served deviations; each must now say which set it means."""
    for function in (featurize_wide_frame, featurize_perturbed_frame):
        parameter = inspect.signature(function).parameters["behavior_models"]
        assert parameter.default is inspect.Parameter.empty, function.__name__
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# The cache key and the spec hash the file actually read
# ---------------------------------------------------------------------------


def test_the_cache_key_hashes_the_configured_behavior_models(
    tmp_path: Path, models_path: Path
) -> None:
    session = tmp_path / "2026-09-20T152758"
    session.mkdir()
    config = _config(models_path)
    default = build_feature_frame_cache_key([session], config)
    assert default == build_feature_frame_cache_key(
        [session], config, behavior_models_path=models_path
    )
    models_path.write_text(models_path.read_text() + "\n# refit\n")
    assert build_feature_frame_cache_key([session], config) != default


def test_the_feature_spec_records_the_behavior_models_by_content(
    tmp_path: Path, models_path: Path
) -> None:
    config = _config(models_path)
    session = _write_session(tmp_path / "sessions", "2026-09-20T152758")
    features = build_feature_frame([session], config, max_workers=1)
    spec = build_feature_spec(features, ["obd:RPM"], config=config)
    assert spec["behavior_models"] == {
        "path": str(models_path),
        "sha256": hashlib.sha256(models_path.read_bytes()).hexdigest(),
    }
    absent = _config(models_path.parent / "absent.yaml")
    assert build_feature_spec(features, ["obd:RPM"], config=absent)["behavior_models"] == {
        "path": str(models_path.parent / "absent.yaml"),
        "sha256": None,
    }


# ---------------------------------------------------------------------------
# Default profile only (R03.12/R04.6): the trainers refuse tuning recordings
# ---------------------------------------------------------------------------


def _capture(root: Path, name: str, *, corpus_eligible: bool) -> Path:
    session = _write_session(root, name)
    (session / "capture_profile.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "profile": "tuning",
                "started_at": "2026-09-24T12:00:00+00:00",
                "profile_snapshot": {"name": "tuning", "corpus_eligible": corpus_eligible},
            }
        )
    )
    return session


def test_a_non_default_profile_recording_is_refused_by_name(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    live = _write_session(root, "2026-09-20T152758")
    tuning = _write_session(root, "_tuning-2026-09-19T120000")
    capture = _capture(root, "_capture-tuning-2026-09-21T195844", corpus_eligible=False)
    eligible = _capture(root, "_capture-research-2026-09-22T100000", corpus_eligible=True)
    refuse_non_default_profile_sessions([live, eligible], purpose="train")
    with pytest.raises(NonDefaultProfileTrainingError, match="R03.12") as refused:
        refuse_non_default_profile_sessions([live, tuning, capture], purpose="train")
    message = str(refused.value)
    assert tuning.name in message and capture.name in message and live.name not in message


def test_the_isolation_forest_trainer_refuses_a_tuning_recording(
    tmp_path: Path, models_path: Path
) -> None:
    """Before any frame is built: an explicit path must not bring tuning rows,
    and the deviation columns computed on them, into a detector."""
    root = tmp_path / "sessions"
    live = _write_session(root, "2026-09-20T152758")
    tuning = _write_session(root, "_tuning-2026-09-19T120000")
    config = _config(models_path)
    with pytest.raises(NonDefaultProfileTrainingError, match="train the Isolation Forest"):
        train_isolation_forest([live, tuning], config, tmp_path / "out")
    with pytest.raises(NonDefaultProfileTrainingError, match="calibrate the Isolation Forest"):
        train_isolation_forest([live], config, tmp_path / "out", calibration_sessions=[tuning])
    assert not (tmp_path / "out").exists()


def test_the_sequence_trainer_refuses_a_tuning_recording(tmp_path: Path, models_path: Path) -> None:
    root = tmp_path / "sessions"
    live = _write_session(root, "2026-09-20T152758")
    capture = _capture(root, "_capture-tuning-2026-09-21T195844", corpus_eligible=False)
    config = _config(models_path)
    with pytest.raises(NonDefaultProfileTrainingError, match="train the sequence detector"):
        train_sequence_detector(
            SEQUENCE_BACKBONES["lstm"], [live, capture], config, tmp_path / "out"
        )
    with pytest.raises(NonDefaultProfileTrainingError, match="calibrate the sequence"):
        train_sequence_detector(
            SEQUENCE_BACKBONES["lstm"],
            [live],
            config,
            tmp_path / "out",
            calibration_sessions=[capture],
        )
