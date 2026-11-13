"""Model-layer exports, resolved lazily.

This package imports nothing when it is imported. Every ``src.models.*``
import executes this file first, and it used to import every model module
eagerly, including ``lstm_autoencoder`` and ``tcn``, whose class bases import
``torch.nn``: so the daemon loaded torch (620 MB on disk) on the main thread
before recording, through an alert emitter that only wanted an Isolation Forest
column name (S01.2, 2026-09-22 cold-boot trace). The names below still resolve
as ``src.models.<name>``; the module behind one is imported on first access.
"""

from __future__ import annotations

import importlib
from typing import Any

#: Export name -> the module that defines it.
_EXPORTS: dict[str, str] = {
    "LABEL_COLUMN": "src.models.isolation_forest",
    "MISSING_FEATURES_COLUMN": "src.models.isolation_forest",
    "MODEL_MODE_COLUMN": "src.models.isolation_forest",
    "SCORE_COLUMN": "src.models.isolation_forest",
    "IsolationForestConfigLike": "src.models.isolation_forest",
    "IsolationForestManifest": "src.models.isolation_forest",
    "MissingModelDependencyError": "src.models.isolation_forest",
    "ModeTrainingSummary": "src.models.isolation_forest",
    "TrainedIsolationForest": "src.models.isolation_forest",
    "default_manifest_path": "src.models.isolation_forest",
    "fit_isolation_forest": "src.models.isolation_forest",
    "load_isolation_forest_model": "src.models.isolation_forest",
    "save_isolation_forest_model": "src.models.isolation_forest",
    "ClassificationMetrics": "src.models.isolation_forest_evaluation",
    "IsolationForestEvaluation": "src.models.isolation_forest_evaluation",
    "IsolationForestEvaluationReport": "src.models.isolation_forest_evaluation",
    "ModeEvaluation": "src.models.isolation_forest_evaluation",
    "ScoreDistribution": "src.models.isolation_forest_evaluation",
    "classification_metrics": "src.models.isolation_forest_evaluation",
    "evaluate_isolation_forest": "src.models.isolation_forest_evaluation",
    "score_distribution": "src.models.isolation_forest_evaluation",
    "LSTM_ANOMALY_COLUMN": "src.models.inference",
    "LSTM_SCORE_COLUMN": "src.models.inference",
    "LSTM_THRESHOLD_COLUMN": "src.models.inference",
    "SequenceInferenceEngine": "src.models.inference",
    "SequenceInferenceResult": "src.models.inference",
    "SequenceWindowBuffer": "src.models.inference",
    "SequenceAutoencoder": "src.models.sequence_protocol",
    "TrainedSequenceModel": "src.models.sequence_protocol",
    "SEQUENCE_BACKENDS": "src.models.sequence_registry",
    "SequenceBackend": "src.models.sequence_registry",
    "resolve_sequence_backend": "src.models.sequence_registry",
    "MissingTCNDependencyError": "src.models.tcn",
    "TCNAutoencoder": "src.models.tcn",
    "TCNAutoencoderArchitecture": "src.models.tcn",
    "TCNAutoencoderConfigLike": "src.models.tcn",
    "build_tcn_autoencoder": "src.models.tcn",
    "TCNAutoencoderTrainingConfigLike": "src.models.tcn_training",
    "calibrate_tcn_thresholds": "src.models.tcn_training",
    "default_tcn_manifest_path": "src.models.tcn_training",
    "load_tcn_autoencoder_model": "src.models.tcn_training",
    "save_tcn_autoencoder_model": "src.models.tcn_training",
    "train_tcn_autoencoder": "src.models.tcn_training",
    "LSTMAutoencoder": "src.models.lstm_autoencoder",
    "LSTMAutoencoderArchitecture": "src.models.lstm_autoencoder",
    "LSTMAutoencoderConfigLike": "src.models.lstm_autoencoder",
    "MissingLSTMAutoencoderDependencyError": "src.models.lstm_autoencoder",
    "ReconstructionReduction": "src.models.sequence_module",
    "build_lstm_autoencoder": "src.models.lstm_autoencoder",
    "EpochLoss": "src.models.sequence_calibration",
    "LSTMAutoencoderTrainingConfigLike": "src.models.lstm_training",
    "SequenceThresholdCalibration": "src.models.sequence_calibration",
    "calibrate_sequence_thresholds": "src.models.sequence_calibration",
    "ReconstructionErrorDistribution": "src.models.sequence_calibration",
    "calibrate_lstm_thresholds": "src.models.lstm_training",
    "default_lstm_manifest_path": "src.models.lstm_training",
    "load_lstm_autoencoder_model": "src.models.lstm_training",
    "save_lstm_autoencoder_model": "src.models.lstm_training",
    "train_lstm_autoencoder": "src.models.lstm_training",
    "DEFAULT_SEQUENCE_STRIDE": "src.models.sequence_dataset",
    "MissingSequenceDependencyError": "src.models.sequence_dataset",
    "SequenceWindow": "src.models.sequence_dataset",
    "SequenceWindowBuildResult": "src.models.sequence_dataset",
    "SequenceWindowDataset": "src.models.sequence_dataset",
    "build_sequence_windows": "src.models.sequence_dataset",
    "resolve_gap_columns": "src.models.sequence_dataset",
    "resolve_sequence_feature_columns": "src.models.sequence_dataset",
    "sequence_frame_from_sessions": "src.models.sequence_dataset",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORTS})


__all__ = list(_EXPORTS)
