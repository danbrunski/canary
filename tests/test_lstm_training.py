"""Tests for LSTM autoencoder training and checkpoint persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from src.config import CanaryConfig
from src.models.lstm_training import (
    LSTMAutoencoderManifest,
    calibrate_lstm_thresholds,
    default_lstm_manifest_path,
    load_lstm_autoencoder_model,
    train_lstm_autoencoder,
)
from src.models.manifest import validate_manifest_common
from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_training import SequenceThresholdCalibration
from src.preprocessing.normalization import ZScoreScaler
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_YAML = REPO_ROOT / "canary.example.yaml"


def _torch():
    return pytest.importorskip("torch")


def _config(**updates):
    base = CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder
    defaults = {
        "sequence_length": 4,
        "hidden_size": 6,
        "num_layers": 1,
        "latent_dim": 2,
        "batch_size": 4,
        "learning_rate": 0.01,
        "max_epochs": 3,
        "validation_fraction": 0.25,
        "early_stopping_patience": 2,
        "random_state": 7,
        "threshold_k": 2.0,
    }
    defaults.update(updates)
    return base.model_copy(update=defaults)


def _frame(rows: int = 14) -> pd.DataFrame:
    return pd.DataFrame(
        {
            SESSION_COLUMN: ["clean"] * rows,
            TIMESTAMP_COLUMN: pd.date_range("2026-05-12T00:00:00Z", periods=rows, freq="s"),
            "rpm": [700.0 + (idx % 4) for idx in range(rows)],
            "speed": [float(idx % 5) for idx in range(rows)],
        }
    )


def _scaler(frame: pd.DataFrame | None = None) -> ZScoreScaler:
    """Per-feature z-score scaler for the synthetic test data."""
    return ZScoreScaler.fit(frame if frame is not None else _frame(), columns=["rpm", "speed"])


def _dataset(rows: int = 14, sequence_length: int = 4) -> SequenceWindowDataset:
    frame = _frame(rows)
    scaler = _scaler(frame)
    # Tests run the model on the same scaled frame inference will see; the
    # ``SequenceInferenceEngine`` z-scores in-line so train/serve stay aligned.
    return SequenceWindowDataset(
        scaler.transform(frame),
        sequence_length=sequence_length,
        feature_columns=["rpm", "speed"],
    )


def test_config_exposes_training_knobs() -> None:
    config = CanaryConfig.from_yaml(EXAMPLE_YAML).models.lstm_autoencoder

    assert config.batch_size == 64
    assert config.learning_rate == 0.001
    assert config.max_epochs == 100
    assert config.validation_fraction == 0.2
    assert config.early_stopping_patience == 10
    assert config.min_delta == 0.0
    assert config.random_state == 42
    assert config.threshold_k == 3.0


def test_train_lstm_autoencoder_produces_manifest() -> None:
    _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(
        dataset,
        _config(max_epochs=2),
        trained_at="2026-05-12T12:00:00Z",
        model_version="unit-lstm",
    )

    manifest = trained.manifest
    assert manifest.model_type == "lstm_autoencoder"
    assert manifest.model_version == "unit-lstm"
    assert manifest.created_at == "2026-05-12T12:00:00Z"
    assert manifest.input_windows == len(dataset)
    assert manifest.train_windows + manifest.validation_windows == len(dataset)
    assert manifest.validation_windows >= 1
    assert manifest.best_epoch in {loss.epoch for loss in manifest.history}
    assert manifest.best_train_loss >= 0
    assert manifest.best_validation_loss is None or manifest.best_validation_loss >= 0
    assert manifest.schema["feature_columns"] == ["rpm", "speed"]
    assert manifest.architecture["input_size"] == 2
    assert manifest.architecture["sequence_length"] == 4
    assert manifest.hyperparams["loss"] == "masked_mse"
    assert manifest.hyperparams["optimizer"] == "adam"
    assert manifest.training_data_hash.startswith("sha256:")
    assert manifest.input_rows == len(dataset.frame)
    assert manifest.schema["feature_spec_version"] == 1
    assert manifest.schema["feature_health"]["selected_feature_count"] == 2
    assert manifest.schema["feature_health"]["constant_columns"] == []
    validate_manifest_common(manifest.to_dict())
    assert manifest.thresholds is not None
    calibration = SequenceThresholdCalibration.from_dict(manifest.thresholds)
    assert calibration.threshold_k == 2.0
    assert calibration.window_count == len(dataset)
    assert calibration.training_data_hash == manifest.training_data_hash
    assert set(calibration.channel_errors) == {"rpm", "speed"}
    assert calibration.global_error.threshold == pytest.approx(
        calibration.global_error.mean + 2.0 * calibration.global_error.std
    )
    assert calibration.channel_errors["rpm"].threshold == pytest.approx(
        calibration.channel_errors["rpm"].mean + 2.0 * calibration.channel_errors["rpm"].std
    )


def test_train_lstm_autoencoder_supports_no_validation_split() -> None:
    _torch()
    trained = train_lstm_autoencoder(
        _dataset(),
        _config(validation_fraction=0.0, max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
    )

    assert trained.manifest.validation_windows == 0
    assert trained.manifest.best_validation_loss is None
    assert trained.manifest.history[0].validation_loss is None


def test_save_and_load_lstm_autoencoder_checkpoint(tmp_path: Path) -> None:
    torch = _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(
        dataset,
        _config(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
        model_version="save-load-test",
    )
    artifact = tmp_path / "lstm.pt"

    trained.save(artifact)
    loaded = load_lstm_autoencoder_model(artifact)

    assert artifact.exists()
    assert default_lstm_manifest_path(artifact).exists()
    manifest_payload = json.loads(default_lstm_manifest_path(artifact).read_text())
    assert manifest_payload["model_version"] == "save-load-test"
    assert manifest_payload["thresholds"] is not None
    assert loaded.manifest.model_version == "save-load-test"
    assert loaded.manifest.thresholds == trained.manifest.thresholds

    value, mask, delta = (tensor.unsqueeze(0) for tensor in dataset[0])
    with torch.no_grad():
        reconstructed = loaded.model(value, mask, delta)
    assert tuple(reconstructed.shape) == tuple(value.shape)


def test_load_rejects_lstm_architecture_feature_mismatch(tmp_path: Path) -> None:
    torch = _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(
        dataset,
        _config(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
    )
    artifact = tmp_path / "lstm.pt"
    trained.save(artifact)

    payload = torch.load(artifact, map_location="cpu", weights_only=False)
    payload["manifest"]["architecture"]["input_size"] = 3
    torch.save(payload, artifact)

    with pytest.raises(ValueError, match="input_size"):
        load_lstm_autoencoder_model(artifact)


def test_save_persists_scaler_in_manifest(tmp_path: Path) -> None:
    """A trained artifact must carry the scaler it was fit against.

    Pre-scaler LSTM artifacts (May 2026) produced reconstruction errors of
    ~80k against raw OBD values; the threshold was calibrated on those raw
    errors. At inference, raw input → ~80k error vs ~80k threshold, the
    model never fired. Pin that the new training pipeline writes a scaler.
    """
    _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(
        dataset,
        _config(max_epochs=1),
        trained_at="2026-05-20T12:00:00Z",
        scaler=_scaler(),
    )
    artifact = tmp_path / "lstm.pt"
    trained.save(artifact)

    payload = json.loads(default_lstm_manifest_path(artifact).read_text())
    assert payload["scaler"] is not None
    assert payload["scaler"]["columns"] == ["rpm", "speed"]
    assert "rpm" in payload["scaler"]["means"]
    assert "speed" in payload["scaler"]["stds"]


def test_load_rejects_lstm_threshold_without_scaler(tmp_path: Path) -> None:
    """Calibrated thresholds + no scaler is the May 2026 degenerate shape — refuse it."""
    torch = _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(
        dataset,
        _config(max_epochs=1),
        trained_at="2026-05-20T12:00:00Z",
    )
    artifact = tmp_path / "lstm.pt"
    trained.save(artifact)

    payload = torch.load(artifact, map_location="cpu", weights_only=False)
    payload["manifest"]["scaler"] = None
    torch.save(payload, artifact)

    with pytest.raises(ValueError, match="no scaler"):
        load_lstm_autoencoder_model(artifact)


def test_load_rejects_lstm_threshold_feature_mismatch(tmp_path: Path) -> None:
    torch = _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(
        dataset,
        _config(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
    )
    artifact = tmp_path / "lstm.pt"
    trained.save(artifact)

    payload = torch.load(artifact, map_location="cpu", weights_only=False)
    del payload["manifest"]["thresholds"]["channel_errors"]["speed"]
    torch.save(payload, artifact)

    with pytest.raises(ValueError, match="thresholds missing"):
        load_lstm_autoencoder_model(artifact)


def test_manifest_round_trip() -> None:
    _torch()
    trained = train_lstm_autoencoder(
        _dataset(),
        _config(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
    )

    restored = LSTMAutoencoderManifest.from_dict(trained.manifest.to_dict())

    assert restored == trained.manifest


def test_calibrate_lstm_thresholds_can_be_run_explicitly() -> None:
    _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(
        dataset,
        _config(max_epochs=1),
        trained_at="2026-05-12T12:00:00Z",
        calibrate_thresholds=False,
    )

    calibrated = calibrate_lstm_thresholds(
        trained,
        dataset,
        threshold_k=1.5,
        calibrated_at="2026-05-12T13:00:00Z",
    )

    assert trained.manifest.thresholds is None
    assert calibrated.manifest.thresholds is not None
    calibration = SequenceThresholdCalibration.from_dict(calibrated.manifest.thresholds)
    assert calibration.threshold_k == 1.5
    assert calibration.calibrated_at == "2026-05-12T13:00:00Z"
    assert calibration.global_error.count == len(dataset)


def test_calibrate_lstm_thresholds_rejects_mismatched_schema() -> None:
    _torch()
    dataset = _dataset()
    trained = train_lstm_autoencoder(dataset, _config(max_epochs=1))
    mismatched = _dataset(sequence_length=3)

    with pytest.raises(ValueError, match="sequence_length"):
        calibrate_lstm_thresholds(trained, mismatched, threshold_k=3.0)


def test_train_lstm_autoencoder_rejects_invalid_inputs() -> None:
    _torch()
    with pytest.raises(ValueError, match="disabled"):
        train_lstm_autoencoder(_dataset(), _config(enabled=False))
    with pytest.raises(ValueError, match="batch_size"):
        train_lstm_autoencoder(_dataset(), _config(batch_size=0))
    with pytest.raises(ValueError, match="validation_fraction"):
        train_lstm_autoencoder(_dataset(), _config(validation_fraction=1.0))
    with pytest.raises(ValueError, match="threshold_k"):
        train_lstm_autoencoder(_dataset(), _config(threshold_k=-1.0))


def test_train_lstm_autoencoder_rejects_empty_dataset() -> None:
    _torch()
    empty = SequenceWindowDataset(
        pd.DataFrame(
            {
                SESSION_COLUMN: ["s1"],
                TIMESTAMP_COLUMN: pd.to_datetime(["2026-05-12T00:00:00Z"]),
                "rpm": [700.0],
            }
        ),
        sequence_length=2,
        feature_columns=["rpm"],
    )

    with pytest.raises(ValueError, match="no sequence windows"):
        train_lstm_autoencoder(empty, _config())


def test_train_lstm_autoencoder_calibrates_on_held_out_dataset_when_given() -> None:
    """LSTM mirror of the TCN out-of-sample calibration contract: a held-out
    ``calibration_dataset`` drives the manifest thresholds instead of the
    training windows."""
    _torch()
    train_ds = _dataset(rows=14)
    held_out_frame = _frame(rows=8)
    held_out_frame.loc[:, [SESSION_COLUMN]] = "calib"
    scaler = _scaler()
    calib_ds = SequenceWindowDataset(
        scaler.transform(held_out_frame),
        sequence_length=4,
        feature_columns=["rpm", "speed"],
    )
    assert len(train_ds) != len(calib_ds)

    trained = train_lstm_autoencoder(
        train_ds,
        _config(max_epochs=2),
        trained_at="2026-05-12T12:00:00Z",
        model_version="oos-calib",
        calibration_dataset=calib_ds,
    )
    assert trained.manifest.thresholds is not None
    calibration = SequenceThresholdCalibration.from_dict(trained.manifest.thresholds)
    # Calibration ran on the held-out dataset, not the training one.
    assert calibration.window_count == len(calib_ds)
    assert calibration.window_count != len(train_ds)
