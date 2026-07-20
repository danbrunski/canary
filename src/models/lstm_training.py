"""Training, threshold calibration, and persistence for the LSTM autoencoder.

Task 2.1: a thin backbone-bound façade over
:mod:`src.models.sequence_autoencoder_training`, the shared engine both the
LSTM and TCN backbones route through. LSTM's only genuine divergences from
TCN — the ``strict=False`` decay-key load tolerance — are expressed as
:data:`~src.models.sequence_registry.LSTM_TRAINING_BACKEND` hooks; everything
else (manifest shape, training loop, calibration, checkpoint I/O, validation
guards) is shared, not duplicated here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.models.sequence_autoencoder_training import (
    ARTIFACT_FORMAT_VERSION,
    TrainedSequenceAutoencoder,
    calibrate_sequence_autoencoder_thresholds,
    default_sequence_manifest_path,
    load_sequence_autoencoder_model,
    save_sequence_autoencoder_model,
    train_sequence_autoencoder,
)
from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_registry import LSTM_TRAINING_BACKEND, SequenceAutoencoderManifest

MODEL_TYPE = LSTM_TRAINING_BACKEND.model_type

#: Back-compat aliases — both names existed as separate (byte-compatible)
#: dataclasses before Task 2.1; now they're the same class/type.
LSTMAutoencoderManifest = SequenceAutoencoderManifest
TrainedLSTMAutoencoder = TrainedSequenceAutoencoder


class LSTMAutoencoderTrainingConfigLike(Protocol):
    """Subset of ``src.config.LSTMAutoencoderConfig`` used for training."""

    enabled: bool
    sequence_length: int
    hidden_size: int
    num_layers: int
    latent_dim: int
    dropout: float
    batch_size: int
    learning_rate: float
    max_epochs: int
    validation_fraction: float
    early_stopping_patience: int
    min_delta: float
    random_state: int
    threshold_k: float


def train_lstm_autoencoder(
    dataset: SequenceWindowDataset,
    config: LSTMAutoencoderTrainingConfigLike,
    **kwargs: object,
) -> TrainedLSTMAutoencoder:
    """Train an LSTM autoencoder — see ``train_sequence_autoencoder`` for the contract."""
    return train_sequence_autoencoder(LSTM_TRAINING_BACKEND, dataset, config, **kwargs)  # type: ignore[arg-type]


def calibrate_lstm_thresholds(
    trained: TrainedLSTMAutoencoder,
    dataset: SequenceWindowDataset,
    **kwargs: object,
) -> TrainedLSTMAutoencoder:
    """Attach masked reconstruction-error thresholds to a trained LSTM bundle."""
    return calibrate_sequence_autoencoder_thresholds(trained, dataset, **kwargs)  # type: ignore[arg-type]


def save_lstm_autoencoder_model(
    trained: TrainedLSTMAutoencoder,
    artifact_path: Path,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Persist a trained LSTM checkpoint and manifest."""
    save_sequence_autoencoder_model(trained, artifact_path, manifest_path=manifest_path)


def load_lstm_autoencoder_model(
    artifact_path: Path,
    *,
    device: str = "cpu",
    dtype: str = "float32",
) -> TrainedLSTMAutoencoder:
    """Load a checkpoint written by ``save_lstm_autoencoder_model``."""
    return load_sequence_autoencoder_model(
        LSTM_TRAINING_BACKEND, artifact_path, device=device, dtype=dtype
    )


def default_lstm_manifest_path(artifact_path: Path) -> Path:
    """Return the conventional JSON manifest path for a checkpoint."""
    return default_sequence_manifest_path(artifact_path)


__all__ = [
    "ARTIFACT_FORMAT_VERSION",
    "MODEL_TYPE",
    "LSTMAutoencoderManifest",
    "LSTMAutoencoderTrainingConfigLike",
    "TrainedLSTMAutoencoder",
    "calibrate_lstm_thresholds",
    "default_lstm_manifest_path",
    "load_lstm_autoencoder_model",
    "save_lstm_autoencoder_model",
    "train_lstm_autoencoder",
]
