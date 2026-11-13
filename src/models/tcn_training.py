"""Training, threshold calibration, and persistence for the TCN autoencoder.

Task 2.1: a thin backbone-bound façade over
:mod:`src.models.sequence_autoencoder_training`, the shared engine both the
LSTM and TCN backbones route through — see ``lstm_training.py`` for the one
genuine backbone-specific divergence (LSTM's decay-key load tolerance); TCN
has no such legacy checkpoint shape, so it loads strict via the same shared
engine with no extra hook.
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
from src.models.sequence_registry import TCN_TRAINING_BACKEND

MODEL_TYPE = TCN_TRAINING_BACKEND.model_type


class TCNAutoencoderTrainingConfigLike(Protocol):
    """Subset of a future ``src.config.TCNAutoencoderConfig`` used for training."""

    enabled: bool
    sequence_length: int
    channels: int
    kernel_size: int
    num_levels: int
    latent_dim: int
    dropout: float
    use_delta_decay: bool
    batch_size: int
    learning_rate: float
    max_epochs: int
    validation_fraction: float
    early_stopping_patience: int
    min_delta: float
    random_state: int
    threshold_k: float


def train_tcn_autoencoder(
    dataset: SequenceWindowDataset,
    config: TCNAutoencoderTrainingConfigLike,
    **kwargs: object,
) -> TrainedSequenceAutoencoder:
    """Train a TCN autoencoder — see ``train_sequence_autoencoder`` for the contract."""
    return train_sequence_autoencoder(TCN_TRAINING_BACKEND, dataset, config, **kwargs)  # type: ignore[arg-type]


def calibrate_tcn_thresholds(
    trained: TrainedSequenceAutoencoder,
    dataset: SequenceWindowDataset,
    **kwargs: object,
) -> TrainedSequenceAutoencoder:
    """Attach masked reconstruction-error thresholds to a trained TCN bundle."""
    return calibrate_sequence_autoencoder_thresholds(trained, dataset, **kwargs)  # type: ignore[arg-type]


def save_tcn_autoencoder_model(
    trained: TrainedSequenceAutoencoder,
    artifact_path: Path,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Persist a trained TCN checkpoint and manifest."""
    save_sequence_autoencoder_model(trained, artifact_path, manifest_path=manifest_path)


def load_tcn_autoencoder_model(
    artifact_path: Path,
    *,
    device: str = "cpu",
    dtype: str = "float32",
) -> TrainedSequenceAutoencoder:
    """Load a checkpoint written by ``save_tcn_autoencoder_model``."""
    return load_sequence_autoencoder_model(
        TCN_TRAINING_BACKEND, artifact_path, device=device, dtype=dtype
    )


def default_tcn_manifest_path(artifact_path: Path) -> Path:
    """Return the conventional JSON manifest path for a checkpoint."""
    return default_sequence_manifest_path(artifact_path)


__all__ = [
    "ARTIFACT_FORMAT_VERSION",
    "MODEL_TYPE",
    "TCNAutoencoderTrainingConfigLike",
    "calibrate_tcn_thresholds",
    "default_tcn_manifest_path",
    "load_tcn_autoencoder_model",
    "save_tcn_autoencoder_model",
    "train_tcn_autoencoder",
]
