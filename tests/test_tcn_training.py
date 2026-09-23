"""Tests for TCN autoencoder training, calibration, and persistence."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from src.models.inference import (  # noqa: E402 — after importorskip guard
    SequenceInferenceEngine,
    SequenceInferenceResult,
)
from src.models.manifest import validate_manifest_common  # noqa: E402
from src.models.sequence_dataset import SequenceWindowDataset  # noqa: E402
from src.models.sequence_training import SequenceThresholdCalibration  # noqa: E402
from src.models.tcn_training import (  # noqa: E402
    default_tcn_manifest_path,
    load_tcn_autoencoder_model,
    train_tcn_autoencoder,
)
from src.preprocessing.normalization import ZScoreScaler  # noqa: E402
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN  # noqa: E402

pytestmark = pytest.mark.slow  # heavy ML training/bake-off; see pyproject test tiers


@dataclass(frozen=True)
class _Cfg:
    enabled: bool = True
    sequence_length: int = 4
    channels: int = 8
    kernel_size: int = 2
    num_levels: int = 2
    latent_dim: int = 4
    dropout: float = 0.0
    use_delta_decay: bool = True
    batch_size: int = 4
    learning_rate: float = 0.01
    max_epochs: int = 3
    validation_fraction: float = 0.25
    early_stopping_patience: int = 2
    min_delta: float = 0.0
    random_state: int = 7
    threshold_k: float = 2.0


def _frame(rows: int = 14) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["clean"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=rows, freq="s"),
            "rpm": [700.0 + (idx % 4) for idx in range(rows)],
            "speed": [float(idx % 5) for idx in range(rows)],
        }
    )


def _dataset(rows: int = 14, sequence_length: int = 4) -> SequenceWindowDataset:
    frame = _frame(rows)
    scaler = ZScoreScaler.fit(frame, columns=["rpm", "speed"])
    return SequenceWindowDataset(
        scaler.transform(frame),
        sequence_length=sequence_length,
        feature_columns=["rpm", "speed"],
    )


def test_train_tcn_autoencoder_produces_manifest() -> None:
    dataset = _dataset()
    trained = train_tcn_autoencoder(
        dataset,
        _Cfg(max_epochs=2),
        trained_at="2026-05-12T12:00:00Z",
        model_version="unit-tcn",
        allow_in_sample_calibration=True,
    )
    manifest = trained.manifest

    assert manifest.model_type == "tcn_autoencoder"
    assert manifest.model_version == "unit-tcn"
    assert manifest.input_windows == len(dataset)
    # Leakage-aware split: single-session datasets drop a sequence_length
    # gap of windows between train and validation, so the two no longer
    # sum to the total — they must just both exist and not exceed it.
    assert manifest.train_windows >= 1
    assert manifest.train_windows + manifest.validation_windows <= len(dataset)
    assert manifest.schema["feature_columns"] == ["rpm", "speed"]
    assert manifest.architecture["input_size"] == 2
    assert manifest.architecture["kernel_size"] == 2
    assert manifest.architecture["num_levels"] == 2
    assert manifest.architecture["use_delta_decay"] is True
    assert manifest.architecture["receptive_field"] == 7
    assert manifest.hyperparams["loss"] == "masked_mse"
    validate_manifest_common(manifest.to_dict())
    assert manifest.calibration == "in_sample"

    assert manifest.thresholds is not None
    calibration = SequenceThresholdCalibration.from_dict(manifest.thresholds)
    assert calibration.threshold_k == 2.0
    assert calibration.window_count == len(dataset)
    assert set(calibration.channel_errors) == {"rpm", "speed"}
    assert calibration.global_error.threshold == pytest.approx(
        calibration.global_error.mean + 2.0 * calibration.global_error.std
    )


def test_train_tcn_autoencoder_supports_no_validation_split() -> None:
    trained = train_tcn_autoencoder(
        _dataset(),
        _Cfg(validation_fraction=0.0, max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    assert trained.manifest.validation_windows == 0
    assert trained.manifest.best_validation_loss is None


def test_save_and_load_tcn_checkpoint(tmp_path: Path) -> None:
    dataset = _dataset()
    trained = train_tcn_autoencoder(
        dataset,
        _Cfg(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
        model_version="save-load-tcn",
        allow_in_sample_calibration=True,
    )
    artifact = tmp_path / "tcn.pt"
    trained.save(artifact)
    loaded = load_tcn_autoencoder_model(artifact)

    assert artifact.exists()
    assert default_tcn_manifest_path(artifact).exists()
    payload = json.loads(default_tcn_manifest_path(artifact).read_text())
    assert payload["model_type"] == "tcn_autoencoder"
    assert payload["model_version"] == "save-load-tcn"
    assert loaded.manifest.thresholds == trained.manifest.thresholds

    value, mask, delta = (tensor.unsqueeze(0) for tensor in dataset[0])
    with torch.no_grad():
        reconstructed = loaded.model(value, mask, delta)
    assert tuple(reconstructed.shape) == tuple(value.shape)


def test_load_rejects_tcn_feature_mismatch(tmp_path: Path) -> None:
    trained = train_tcn_autoencoder(
        _dataset(),
        _Cfg(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    artifact = tmp_path / "tcn.pt"
    trained.save(artifact)

    payload = torch.load(artifact, map_location="cpu", weights_only=False)
    payload["manifest"]["architecture"]["input_size"] = 3
    torch.save(payload, artifact)

    with pytest.raises(ValueError, match="input_size"):
        load_tcn_autoencoder_model(artifact)


def test_sequence_inference_engine_serves_a_tcn_bundle() -> None:
    """The shared serve engine must drive a TCN model with no branching — the
    whole point of the interchangeable sequence-detector slot."""
    dataset = _dataset()
    trained = train_tcn_autoencoder(
        dataset,
        _Cfg(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    engine = SequenceInferenceEngine(trained)

    window = np.zeros((trained.manifest.architecture["sequence_length"], 2), dtype=np.float32)
    result = engine.score_window(
        window,
        timestamp="2026-05-12T12:00:03Z",
        session="clean",
        window_start_timestamp="2026-05-12T12:00:00Z",
        window_end_timestamp="2026-05-12T12:00:03Z",
    )
    assert isinstance(result, SequenceInferenceResult)
    assert math.isfinite(result.global_score)
    assert set(result.channel_scores) == {"rpm", "speed"}


def test_train_tcn_autoencoder_calibrates_on_held_out_dataset_when_given() -> None:
    """Out-of-sample calibration: when ``calibration_dataset`` is supplied,
    thresholds come from those held-out windows, not the training ones.
    In-sample calibration underestimates real cross-session error; this knob
    is the production-correct path."""
    train_ds = _dataset(rows=14)
    # Different-sized held-out dataset so we can tell from window_count which
    # one the calibration ran on. Same scaler (fit on training) for parity with
    # the real CLI flow.
    held_out_frame = _frame(rows=8)
    held_out_frame.loc[:, [SESSION_COLUMN]] = "calib"
    scaler = ZScoreScaler.fit(_frame(14), columns=["rpm", "speed"])
    calib_ds = SequenceWindowDataset(
        scaler.transform(held_out_frame),
        sequence_length=4,
        feature_columns=["rpm", "speed"],
    )
    assert len(train_ds) != len(calib_ds)  # the smoking-gun signal

    trained = train_tcn_autoencoder(
        train_ds,
        _Cfg(max_epochs=2),
        trained_at="2026-05-12T12:00:00Z",
        model_version="oos-calib",
        calibration_dataset=calib_ds,
        # One held-out session is refused (A02.2); override, and say so.
        calibration_override="test fixture: one held-out session",
    )
    assert trained.manifest.thresholds is not None
    assert trained.manifest.calibration == "held_out"
    calibration = SequenceThresholdCalibration.from_dict(trained.manifest.thresholds)
    # Calibration ran on the held-out dataset — not the training set.
    assert calibration.window_count == len(calib_ds)
    # Sanity-check it didn't silently fall through to in-sample.
    assert calibration.window_count != len(train_ds)


def test_train_tcn_autoencoder_requires_calibration_dataset_or_escape_hatch() -> None:
    """Task 3.1: omitting a held-out calibration set is a hard error by
    default — in-sample calibration was the exact mechanism behind an
    earlier FPR≈1.0 flood, so it must never be a silent fallback."""
    with pytest.raises(ValueError, match="calibration_dataset"):
        train_tcn_autoencoder(_dataset(), _Cfg(max_epochs=2), trained_at="2026-05-12T12:00:00Z")


def test_train_tcn_autoencoder_allow_in_sample_calibration_stamps_manifest() -> None:
    """The escape hatch trains fine and stamps ``calibration="in_sample"`` so
    a load-time consumer can flag the artifact."""
    dataset = _dataset()
    trained = train_tcn_autoencoder(
        dataset,
        _Cfg(max_epochs=2),
        trained_at="2026-05-12T12:00:00Z",
        allow_in_sample_calibration=True,
    )
    assert trained.manifest.calibration == "in_sample"
    assert trained.manifest.thresholds is not None
    calibration = SequenceThresholdCalibration.from_dict(trained.manifest.thresholds)
    assert calibration.window_count == len(dataset)


def test_train_tcn_autoencoder_calibrate_thresholds_false_skips_the_gate() -> None:
    """Deferring calibration to an explicit later call isn't the silent
    in-sample risk 3.1 targets — no escape hatch needed."""
    trained = train_tcn_autoencoder(
        _dataset(),
        _Cfg(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
        calibrate_thresholds=False,
    )
    assert trained.manifest.calibration is None
    assert trained.manifest.thresholds is None
