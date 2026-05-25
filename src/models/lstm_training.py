"""Training, threshold calibration, and persistence for the LSTM autoencoder.

Live inference is a separate task, but this module writes enough manifest
metadata for the inference path to load a checkpoint and apply calibrated
reconstruction-error thresholds.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import pandas as pd

from src.models.lstm_autoencoder import (
    LSTMAutoencoder,
    MissingLSTMAutoencoderDependencyError,
    build_lstm_autoencoder,
)
from src.models.manifest import validate_model_feature_spec
from src.models.sequence_dataset import SequenceWindowDataset
from src.models.sequence_training import (
    EpochLoss,
    SequenceThresholdCalibration,
    calibrate_sequence_thresholds,
    coerce_timestamp,
    cpu_state_dict,
    evaluate_loss,
    max_window_timestamp,
    min_window_timestamp,
    split_dataset,
    train_one_epoch,
    training_data_hash,
    training_sessions,
)
from src.preprocessing.feature_pipeline import build_feature_spec
from src.preprocessing.normalization import ZScoreScaler
from src.timefmt import format_timestamp as _format_timestamp

ARTIFACT_FORMAT_VERSION = 1
MODEL_TYPE = "lstm_autoencoder"


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


@dataclass(frozen=True)
class LSTMAutoencoderManifest:
    """JSON-safe metadata for a trained LSTM checkpoint."""

    artifact_format_version: int
    model_type: str
    model_version: str
    created_at: str
    training_data_hash: str
    training_start_timestamp: str | None
    training_end_timestamp: str | None
    training_sessions: tuple[str, ...]
    input_rows: int
    input_windows: int
    train_windows: int
    validation_windows: int
    best_epoch: int
    best_train_loss: float
    best_validation_loss: float | None
    stopped_epoch: int
    early_stopped: bool
    architecture: Mapping[str, Any]
    hyperparams: Mapping[str, Any]
    schema: Mapping[str, Any]
    history: tuple[EpochLoss, ...]
    thresholds: Mapping[str, Any] | None = None
    #: Per-channel z-score scaler ({means, stds, columns, std_floor}) fit on
    #: the training corpus and applied at inference. Pre-scaler artifacts
    #: (May 2026 era) have ``None`` here; the load-time guard refuses them
    #: when thresholds are also present, since a calibrated threshold against
    #: raw-magnitude reconstruction errors won't match scaled-input
    #: reconstruction errors at inference.
    scaler: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_format_version": self.artifact_format_version,
            "model_type": self.model_type,
            "model_version": self.model_version,
            "created_at": self.created_at,
            "training_data_hash": self.training_data_hash,
            "training_start_timestamp": self.training_start_timestamp,
            "training_end_timestamp": self.training_end_timestamp,
            "training_sessions": list(self.training_sessions),
            "input_rows": self.input_rows,
            "input_windows": self.input_windows,
            "train_windows": self.train_windows,
            "validation_windows": self.validation_windows,
            "best_epoch": self.best_epoch,
            "best_train_loss": self.best_train_loss,
            "best_validation_loss": self.best_validation_loss,
            "stopped_epoch": self.stopped_epoch,
            "early_stopped": self.early_stopped,
            "architecture": dict(self.architecture),
            "hyperparams": dict(self.hyperparams),
            "schema": dict(self.schema),
            "history": [loss.to_dict() for loss in self.history],
            "thresholds": None if self.thresholds is None else dict(self.thresholds),
            "scaler": None if self.scaler is None else dict(self.scaler),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> LSTMAutoencoderManifest:
        return cls(
            artifact_format_version=int(data["artifact_format_version"]),
            model_type=str(data["model_type"]),
            model_version=str(data["model_version"]),
            created_at=str(data["created_at"]),
            training_data_hash=str(data["training_data_hash"]),
            training_start_timestamp=_optional_str(data.get("training_start_timestamp")),
            training_end_timestamp=_optional_str(data.get("training_end_timestamp")),
            training_sessions=tuple(str(session) for session in data["training_sessions"]),
            input_rows=int(data.get("input_rows", data["input_windows"])),
            input_windows=int(data["input_windows"]),
            train_windows=int(data["train_windows"]),
            validation_windows=int(data["validation_windows"]),
            best_epoch=int(data["best_epoch"]),
            best_train_loss=float(data["best_train_loss"]),
            best_validation_loss=_optional_float(data.get("best_validation_loss")),
            stopped_epoch=int(data["stopped_epoch"]),
            early_stopped=bool(data["early_stopped"]),
            architecture=dict(data["architecture"]),
            hyperparams=dict(data["hyperparams"]),
            schema=dict(data["schema"]),
            history=tuple(EpochLoss.from_dict(item) for item in data["history"]),
            thresholds=None if data.get("thresholds") is None else dict(data["thresholds"]),
            scaler=None if data.get("scaler") is None else dict(data["scaler"]),
        )


@dataclass(frozen=True)
class TrainedLSTMAutoencoder:
    """A trained LSTM autoencoder plus its manifest."""

    model: LSTMAutoencoder
    manifest: LSTMAutoencoderManifest

    @property
    def feature_columns(self) -> tuple[str, ...]:
        return tuple(str(column) for column in self.manifest.schema["feature_columns"])

    def save(self, artifact_path: Path, manifest_path: Path | None = None) -> None:
        """Write the Torch checkpoint and JSON manifest."""
        save_lstm_autoencoder_model(self, artifact_path, manifest_path=manifest_path)


def train_lstm_autoencoder(
    dataset: SequenceWindowDataset,
    config: LSTMAutoencoderTrainingConfigLike,
    *,
    device: str = "cpu",
    dtype: str = "float32",
    model_version: str | None = None,
    trained_at: datetime | pd.Timestamp | str | None = None,
    calibrate_thresholds: bool = True,
    feature_spec: Mapping[str, Any] | None = None,
    scaler: ZScoreScaler | None = None,
) -> TrainedLSTMAutoencoder:
    """Train an autoencoder on clean sequence windows.

    The caller is responsible for passing only clean-baseline sessions into the
    ``SequenceWindowDataset``. The loop uses MAE loss, an optional validation
    split, and early stopping on validation loss when validation rows exist.
    """
    _validate_config(config)
    if len(dataset) == 0:
        raise ValueError("no sequence windows available to train LSTM autoencoder")

    torch, _nn, optim, data = _torch_training_modules()
    torch.manual_seed(config.random_state)

    device_obj = torch.device(device)
    model = build_lstm_autoencoder(
        config,
        input_size=len(dataset.feature_columns),
        device=device,
        dtype=dtype,
    )
    train_subset, validation_subset = split_dataset(
        dataset,
        validation_fraction=config.validation_fraction,
        random_state=config.random_state,
    )
    train_loader = data.DataLoader(
        train_subset,
        batch_size=config.batch_size,
        shuffle=True,
        generator=torch.Generator().manual_seed(config.random_state),
    )
    validation_loader = (
        None
        if validation_subset is None
        else data.DataLoader(validation_subset, batch_size=config.batch_size, shuffle=False)
    )

    optimizer = optim.Adam(model.parameters(), lr=config.learning_rate)

    best_epoch = 0
    best_train_loss = float("inf")
    best_validation_loss: float | None = None
    best_monitor_loss = float("inf")
    best_state: dict[str, Any] | None = None
    epochs_without_improvement = 0
    history: list[EpochLoss] = []
    early_stopped = False

    for epoch in range(1, config.max_epochs + 1):
        train_loss = train_one_epoch(
            model,
            train_loader,
            optimizer=optimizer,
            device=device_obj,
        )
        validation_loss = (
            None
            if validation_loader is None
            else evaluate_loss(model, validation_loader, device=device_obj)
        )
        history.append(
            EpochLoss(epoch=epoch, train_loss=train_loss, validation_loss=validation_loss)
        )

        monitor_loss = train_loss if validation_loss is None else validation_loss
        if monitor_loss < best_monitor_loss - config.min_delta:
            best_epoch = epoch
            best_train_loss = train_loss
            best_validation_loss = validation_loss
            best_monitor_loss = monitor_loss
            best_state = cpu_state_dict(model)
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= config.early_stopping_patience:
                early_stopped = True
                break

    if best_state is None:
        raise RuntimeError("LSTM training did not produce a checkpoint")
    model.load_state_dict(best_state)
    model.to(device_obj)
    model.eval()

    created_at = _format_timestamp(coerce_timestamp(trained_at))
    version = model_version or f"lstm_autoencoder_{created_at.replace(':', '')}"
    train_count = len(train_subset)
    validation_count = 0 if validation_subset is None else len(validation_subset)
    schema = (
        dict(feature_spec)
        if feature_spec is not None
        else build_feature_spec(
            dataset.frame,
            dataset.feature_columns,
            sequence_length=dataset.sequence_length,
            sequence_stride=dataset.stride,
        )
    )
    validate_model_feature_spec(schema, context="LSTM training feature spec")
    schema["sequence_length"] = dataset.sequence_length
    manifest = LSTMAutoencoderManifest(
        artifact_format_version=ARTIFACT_FORMAT_VERSION,
        model_type=MODEL_TYPE,
        model_version=version,
        created_at=created_at,
        training_data_hash=training_data_hash(dataset),
        training_start_timestamp=min_window_timestamp(dataset),
        training_end_timestamp=max_window_timestamp(dataset),
        training_sessions=training_sessions(dataset),
        input_rows=len(dataset.frame),
        input_windows=len(dataset),
        train_windows=train_count,
        validation_windows=validation_count,
        best_epoch=best_epoch,
        best_train_loss=best_train_loss,
        best_validation_loss=best_validation_loss,
        stopped_epoch=history[-1].epoch,
        early_stopped=early_stopped,
        architecture=model.architecture.to_dict(),
        hyperparams={
            "batch_size": config.batch_size,
            "learning_rate": config.learning_rate,
            "max_epochs": config.max_epochs,
            "validation_fraction": config.validation_fraction,
            "early_stopping_patience": config.early_stopping_patience,
            "min_delta": config.min_delta,
            "random_state": config.random_state,
            "loss": "masked_mse",
            "optimizer": "adam",
            "device": str(device_obj),
            "dtype": dtype,
        },
        schema=schema,
        history=tuple(history),
        scaler=_resolve_manifest_scaler(scaler, dataset).to_dict(),
    )
    trained = TrainedLSTMAutoencoder(model=model, manifest=manifest)
    if not calibrate_thresholds:
        return trained
    return calibrate_lstm_thresholds(
        trained,
        dataset,
        threshold_k=config.threshold_k,
        batch_size=config.batch_size,
        device=device,
        calibrated_at=created_at,
    )


def calibrate_lstm_thresholds(
    trained: TrainedLSTMAutoencoder,
    dataset: SequenceWindowDataset,
    *,
    threshold_k: float,
    batch_size: int = 64,
    device: str = "cpu",
    calibrated_at: datetime | pd.Timestamp | str | None = None,
) -> TrainedLSTMAutoencoder:
    """Attach masked reconstruction-error thresholds to a trained bundle.

    Thin wrapper over :func:`calibrate_sequence_thresholds` (shared with the
    TCN backbone) that writes the calibration into the LSTM manifest.
    """
    calibration = calibrate_sequence_thresholds(
        trained.model,
        dataset,
        feature_columns=trained.feature_columns,
        threshold_k=threshold_k,
        batch_size=batch_size,
        device=device,
        calibrated_at=calibrated_at,
    )
    manifest = replace(trained.manifest, thresholds=calibration.to_dict())
    return TrainedLSTMAutoencoder(model=trained.model, manifest=manifest)


def save_lstm_autoencoder_model(
    trained: TrainedLSTMAutoencoder,
    artifact_path: Path,
    *,
    manifest_path: Path | None = None,
) -> None:
    """Persist a trained LSTM checkpoint and manifest."""
    torch = _torch_module()
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = trained.manifest.to_dict()
    torch.save(
        {
            "artifact_format_version": ARTIFACT_FORMAT_VERSION,
            "model_type": MODEL_TYPE,
            "state_dict": cpu_state_dict(trained.model),
            "manifest": manifest,
        },
        artifact_path,
    )

    path = manifest_path or default_lstm_manifest_path(artifact_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def load_lstm_autoencoder_model(
    artifact_path: Path,
    *,
    device: str = "cpu",
    dtype: str = "float32",
) -> TrainedLSTMAutoencoder:
    """Load a checkpoint written by ``save_lstm_autoencoder_model``."""
    torch = _torch_module()
    payload = torch.load(artifact_path, map_location=device, weights_only=False)
    if int(payload["artifact_format_version"]) != ARTIFACT_FORMAT_VERSION:
        raise ValueError(f"unsupported LSTM artifact format {payload['artifact_format_version']!r}")
    if str(payload["model_type"]) != MODEL_TYPE:
        raise ValueError(f"unexpected model type {payload['model_type']!r}")

    manifest = LSTMAutoencoderManifest.from_dict(payload["manifest"])
    feature_columns = validate_model_feature_spec(
        manifest.schema,
        context=f"LSTM manifest {artifact_path}",
    )
    _validate_loaded_lstm_manifest(manifest, feature_columns=feature_columns)
    architecture = manifest.architecture
    model = LSTMAutoencoder(
        input_size=int(architecture["input_size"]),
        sequence_length=int(architecture["sequence_length"]),
        hidden_size=int(architecture["hidden_size"]),
        num_layers=int(architecture["num_layers"]),
        latent_dim=int(architecture["latent_dim"]),
        dropout=float(architecture["dropout"]),
        decoder_type=str(architecture.get("decoder_type", "gru")),
    )
    model.load_state_dict(payload["state_dict"])
    model.to(device=torch.device(device), dtype=getattr(torch, dtype))
    model.eval()
    return TrainedLSTMAutoencoder(model=model, manifest=manifest)


def default_lstm_manifest_path(artifact_path: Path) -> Path:
    """Return the conventional JSON manifest path for a checkpoint."""
    return artifact_path.with_suffix(".manifest.json")


def _validate_loaded_lstm_manifest(
    manifest: LSTMAutoencoderManifest,
    *,
    feature_columns: tuple[str, ...],
) -> None:
    architecture = manifest.architecture
    input_size = _required_int(architecture, "input_size", context="LSTM architecture")
    if input_size != len(feature_columns):
        raise ValueError(
            "LSTM architecture input_size does not match manifest feature_columns: "
            f"input_size={input_size}, feature_columns={len(feature_columns)}"
        )

    architecture_sequence_length = _required_int(
        architecture,
        "sequence_length",
        context="LSTM architecture",
    )
    schema_sequence_length = _required_int(
        manifest.schema,
        "sequence_length",
        context="LSTM feature schema",
    )
    if architecture_sequence_length != schema_sequence_length:
        raise ValueError(
            "LSTM architecture sequence_length does not match manifest schema: "
            f"architecture={architecture_sequence_length}, schema={schema_sequence_length}"
        )

    if manifest.thresholds is not None:
        _validate_lstm_thresholds(manifest.thresholds, feature_columns=feature_columns)
        # The threshold is computed on the model's reconstruction error
        # distribution during calibration. If training applied a scaler,
        # those errors are in *scaled* units; if it didn't, they're in raw
        # OBD/CAN units. Inference must match — and the only way it knows
        # which is which is via the saved scaler. A calibrated manifest
        # without a scaler is the May 2026 degenerate-fit artifact shape:
        # threshold of 171,895 against live scores of 87, completely
        # unworkable. Refuse it loudly at load.
        if manifest.scaler is None:
            raise ValueError(
                "LSTM manifest has calibrated thresholds but no scaler — "
                "the threshold scale won't match inference. Retrain on "
                "current code so the artifact carries a scaler dict."
            )
        _validate_lstm_scaler(manifest.scaler, feature_columns=feature_columns)


def _resolve_manifest_scaler(
    scaler: ZScoreScaler | None,
    dataset: SequenceWindowDataset,
) -> ZScoreScaler:
    """Pick the scaler to embed in the manifest.

    Production callers (``scripts/train_lstm.py``) fit a scaler on the raw
    feature frame, apply it, then pass the fitted scaler in — that one
    represents raw → scaled and is what inference applies to incoming
    windows. Test callers don't bother and pass the dataset's frame
    pre-scaled; for those, we fit a fresh scaler on whatever the dataset
    actually contains so the manifest stays self-consistent (means/stds
    match the model's expected input distribution). The post-load guard
    requires *some* scaler to exist, so this branch ensures we never write
    an artifact without one.
    """
    if scaler is not None:
        return scaler
    return ZScoreScaler.fit(dataset.frame, columns=dataset.feature_columns)


def _validate_lstm_scaler(
    scaler: Mapping[str, Any],
    *,
    feature_columns: tuple[str, ...],
) -> None:
    """Round-trip the scaler dict + ensure every feature column has stats."""
    try:
        loaded = ZScoreScaler.from_dict(dict(scaler))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid LSTM scaler in manifest: {exc}") from exc
    missing = [column for column in feature_columns if column not in loaded.means]
    if missing:
        raise ValueError("LSTM scaler missing means for: " + ", ".join(sorted(missing)))


def _validate_lstm_thresholds(
    thresholds: Mapping[str, Any],
    *,
    feature_columns: tuple[str, ...],
) -> None:
    try:
        calibration = SequenceThresholdCalibration.from_dict(thresholds)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid LSTM thresholds in manifest: {exc}") from exc

    if not math.isfinite(calibration.global_error.threshold):
        raise ValueError("LSTM thresholds global_error.threshold must be finite")
    missing = [column for column in feature_columns if column not in calibration.channel_errors]
    if missing:
        raise ValueError("LSTM thresholds missing channel_errors for: " + ", ".join(missing))
    for column in feature_columns:
        threshold = calibration.channel_errors[column].threshold
        if not math.isfinite(threshold):
            raise ValueError(f"LSTM threshold for {column!r} must be finite")


def _required_int(data: Mapping[str, Any], key: str, *, context: str) -> int:
    try:
        return int(data[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{context} missing integer {key!r}") from exc


def _validate_config(config: LSTMAutoencoderTrainingConfigLike) -> None:
    if not config.enabled:
        raise ValueError("LSTM autoencoder training requested but config is disabled")
    if config.batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if config.learning_rate <= 0:
        raise ValueError("learning_rate must be positive")
    if config.max_epochs <= 0:
        raise ValueError("max_epochs must be positive")
    if not 0.0 <= config.validation_fraction < 1.0:
        raise ValueError("validation_fraction must be >= 0 and < 1")
    if config.early_stopping_patience <= 0:
        raise ValueError("early_stopping_patience must be positive")
    if config.min_delta < 0:
        raise ValueError("min_delta must be non-negative")
    if config.threshold_k < 0:
        raise ValueError("threshold_k must be non-negative")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _torch_training_modules() -> tuple[Any, Any, Any, Any]:
    try:
        import torch
        from torch import nn, optim
        from torch.utils import data
    except ImportError as exc:
        raise MissingLSTMAutoencoderDependencyError(
            "torch is required to train LSTM autoencoders; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
    return torch, nn, optim, data


def _torch_module() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise MissingLSTMAutoencoderDependencyError(
            "torch is required for LSTM autoencoder checkpoints"
        ) from exc
    return torch
