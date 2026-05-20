"""CI regression guard for detector quality (Phase 7.5).

Builds a tiny seeded multi-mode clean frame, fits IF with held-out
calibration, runs the synthetic-fault harness, and asserts both that
each fault kind is detected at a useful rate AND that the nominal
false-positive rate stays below a sane ceiling. The same module also
asserts that LSTM training completes and writes per-mode thresholds when
the validation set spans multiple modes — the explicit "LSTM training
completion" check 7.5 requires.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from src.config import CanaryConfig  # noqa: E402
from src.models.isolation_forest import fit_isolation_forest  # noqa: E402
from src.models.synthetic_fault_eval import (  # noqa: E402
    DEFAULT_FAULT_KINDS,
    evaluate_detectors,
)
from src.preprocessing.feature_pipeline import featurize_wide_frame  # noqa: E402
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402
from src.preprocessing.segmenter import MODE_COLUMN  # noqa: E402

EXAMPLE_YAML = Path(__file__).resolve().parent.parent / "canary.example.yaml"


@dataclass(frozen=True)
class _IFConfig:
    enabled: bool = True
    n_estimators: int = 60
    contamination: float = 0.02
    per_mode_contamination: dict[str, float] = field(default_factory=dict)
    random_state: int = 7
    calibration_holdout_fraction: float = 0.2
    target_flag_rate: float = 0.05


def _two_mode_resampled(seed: int = 0) -> pd.DataFrame:
    """80 rows of clean idle + 80 rows of clean city, with the OBD signals the
    segmenter needs to actually produce two modes via the example config."""
    rng = np.random.default_rng(seed)
    n = 80
    timestamps = pd.date_range("2026-05-12T00:00:00Z", periods=n * 2, freq="1s")
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["clean-a"] * n + ["clean-b"] * n,
            TIMESTAMP_COLUMN: timestamps,
            "obd:RPM": np.concatenate(
                [
                    rng.normal(750.0, 8.0, size=n),  # idle (rpm_max=900)
                    rng.normal(2200.0, 50.0, size=n),  # city
                ]
            ),
            "obd:SPEED": np.concatenate(
                [
                    np.zeros(n),  # idle (speed_max=0)
                    rng.normal(35.0, 3.0, size=n),  # city (speed_max=50)
                ]
            ),
            "obd:THROTTLE_POS": np.concatenate(
                [
                    rng.normal(18.0, 1.0, size=n),
                    rng.normal(32.0, 2.5, size=n),
                ]
            ),
            # Warm coolant for both halves so the warmup/idle split in the
            # example config resolves to idle (warmup requires
            # coolant_temp <= 70). Synthetic test data isn't modeling cold
            # starts; this just keeps the test's mode count stable across
            # the warmup-mode refactor.
            "obd:COOLANT_TEMP": np.full(n * 2, 85.0),
        }
    )


def _config() -> CanaryConfig:
    return CanaryConfig.from_yaml(EXAMPLE_YAML)


# ---------------------------------------------------------------------------
# Detector-quality regression guard
# ---------------------------------------------------------------------------


def test_evaluate_detectors_grades_each_fault_kind_and_keeps_nominal_fp_low() -> None:
    config = _config()
    resampled = _two_mode_resampled()
    # featurize → fit IF on the clean frame; calibration enabled in _IFConfig.
    nominal_features = featurize_wide_frame(resampled, config).features
    trained = fit_isolation_forest(
        nominal_features,
        _IFConfig(),
        feature_columns=["obd:RPM", "obd:SPEED", "obd:THROTTLE_POS"],
        fitted_at="2026-05-12T00:00:00Z",
    )
    # Sanity: held-out calibration produced per-mode thresholds for both modes.
    assert set(trained.score_thresholds) >= {"idle", "city"}

    report = evaluate_detectors(
        resampled,
        config,
        trained,
        fault_channel="obd:RPM",
        n_injections=3,
    )

    # Report shape + JSON-safe (the training ledger / dashboard consume it).
    assert set(report) == {"fault_kinds", "n_injections", "seed", "nominal_fp", "per_fault"}
    json.loads(json.dumps(report))

    # The four real sensor-failure modes (spike/drift/dropout/stuck) all
    # produce clearly anomalous values — IF must catch them every run. The
    # multiplicative ``scale`` fault is a strict-shape change that can land
    # inside the model's plausibility envelope; require detection in at
    # least one run as the regression floor.
    metrics = {(m["detector"], m["fault_kind"]): m["detection_rate"] for m in report["per_fault"]}
    for kind in ("spike", "drift", "dropout", "stuck"):
        assert metrics[("isolation_forest", kind)] >= 1.0, kind
        assert metrics[("fused", kind)] >= 1.0, kind
    assert metrics[("isolation_forest", "scale")] > 0.0
    assert metrics[("fused", "scale")] > 0.0
    # Every fault kind from DEFAULT_FAULT_KINDS got graded.
    assert {m["fault_kind"] for m in report["per_fault"]} == set(DEFAULT_FAULT_KINDS)

    # Nominal-FP ceiling: target_flag_rate=0.05 at 1 Hz is 180/hr, with
    # quantile-noise headroom on the tiny holdout split (~16 rows/mode). The
    # ceiling is set loose enough to absorb that noise while still catching
    # regressions that blow FP past ~10%; it is *not* a substitute for the
    # tighter Done-Definition v1 #7 (≤1 FP/hr) gate, which needs a real
    # held-out drive.
    assert report["nominal_fp"]["isolation_forest"]["per_hour"] < 400.0
    assert report["nominal_fp"]["fused"]["per_hour"] < 400.0


# ---------------------------------------------------------------------------
# LSTM training completion + per-mode thresholds (the 7.5 "assert LSTM
# training completion" check; per_mode_errors populated when modes present).
# ---------------------------------------------------------------------------


def test_lstm_training_completes_and_populates_per_mode_thresholds() -> None:
    pytest.importorskip("torch")

    from src.models.lstm_training import train_lstm_autoencoder
    from src.models.sequence_dataset import SequenceWindowDataset

    rng = np.random.default_rng(1)
    n = 60
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: ["m"] * (n * 2),
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=n * 2, freq="1s"),
            "rpm": np.concatenate([rng.normal(700, 5, size=n), rng.normal(2000, 30, size=n)]),
            "speed": np.concatenate([np.zeros(n), rng.normal(35, 2, size=n)]),
            MODE_COLUMN: ["idle"] * n + ["city"] * n,
        }
    )
    dataset = SequenceWindowDataset(
        frame,
        sequence_length=4,
        feature_columns=["rpm", "speed"],
    )

    config = CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder
    tiny = config.model_copy(
        update={
            "sequence_length": 4,
            "hidden_size": 6,
            "num_layers": 1,
            "latent_dim": 2,
            "batch_size": 4,
            "learning_rate": 0.01,
            "max_epochs": 2,
            "validation_fraction": 0.25,
            "early_stopping_patience": 2,
            "random_state": 5,
            "threshold_k": 2.0,
        }
    )
    trained = train_lstm_autoencoder(
        dataset,
        tiny,
        trained_at="2026-05-12T00:00:00Z",
        model_version="ci-lstm",
    )
    assert trained.manifest.thresholds is not None
    thresholds = dict(trained.manifest.thresholds)
    per_mode = thresholds.get("per_mode_errors")
    assert per_mode is not None and per_mode, "per-mode thresholds missing in multi-mode dataset"
    # Each persisted mode has a finite threshold (the calibration completed).
    for mode, dist in per_mode.items():
        assert isinstance(mode, str)
        assert np.isfinite(dist["threshold"])
