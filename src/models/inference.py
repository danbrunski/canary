"""Runtime inference helpers for trained anomaly models.

The LSTM path is intentionally small and synchronous: callers feed already
preprocessed feature rows in timestamp order, the buffer maintains the latest
fixed-length sequence, and the engine emits one result per full window.
"""

from __future__ import annotations

import importlib
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError
from src.models.lstm_training import LSTMThresholdCalibration, TrainedLSTMAutoencoder
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

LSTM_SCORE_COLUMN = "lstm_reconstruction_error"
LSTM_THRESHOLD_COLUMN = "lstm_reconstruction_threshold"
LSTM_ANOMALY_COLUMN = "lstm_anomaly"


@dataclass(frozen=True)
class LSTMInferenceResult:
    """One LSTM inference result for the current sliding window."""

    model_version: str
    timestamp: str
    session: str
    window_start_timestamp: str
    window_end_timestamp: str
    global_score: float
    global_threshold: float
    channel_scores: Mapping[str, float]
    channel_thresholds: Mapping[str, float]
    anomalous_channels: tuple[str, ...]
    is_anomaly: bool
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": self.model_version,
            "timestamp": self.timestamp,
            "session": self.session,
            "window_start_timestamp": self.window_start_timestamp,
            "window_end_timestamp": self.window_end_timestamp,
            "global_score": self.global_score,
            "global_threshold": self.global_threshold,
            "channel_scores": dict(self.channel_scores),
            "channel_thresholds": dict(self.channel_thresholds),
            "anomalous_channels": list(self.anomalous_channels),
            "is_anomaly": self.is_anomaly,
            "latency_ms": self.latency_ms,
        }


class LSTMSequenceBuffer:
    """Fixed-length buffer for model-ready feature rows."""

    def __init__(
        self,
        *,
        feature_columns: tuple[str, ...],
        sequence_length: int,
        timestamp_column: str = TIMESTAMP_COLUMN,
        session_column: str = SESSION_COLUMN,
        dtype: str = "float32",
    ) -> None:
        if sequence_length <= 0:
            raise ValueError("sequence_length must be positive")
        if not feature_columns:
            raise ValueError("feature_columns must not be empty")
        self.feature_columns = feature_columns
        self.sequence_length = sequence_length
        self.timestamp_column = timestamp_column
        self.session_column = session_column
        self.dtype = dtype
        self._values: deque[np.ndarray[Any, np.dtype[np.float32]]] = deque(
            maxlen=sequence_length
        )
        self._metadata: deque[tuple[str, str]] = deque(maxlen=sequence_length)
        self._session: str | None = None

    def append(self, row: Mapping[str, Any] | pd.Series) -> bool:
        """Append a feature row and return whether a full window is ready.

        Rows missing a feature or containing non-finite feature values reset the
        buffer. A session change also resets the buffer so windows never span
        sessions.
        """
        session = str(row.get(self.session_column, ""))
        timestamp = _format_timestamp(row.get(self.timestamp_column))
        if self._session is not None and session != self._session:
            self.clear()
        self._session = session

        try:
            values = np.asarray(
                [float(row[column]) for column in self.feature_columns],
                dtype=self.dtype,
            )
        except (KeyError, TypeError, ValueError):
            self.clear()
            return False

        if not bool(np.isfinite(values).all()):
            self.clear()
            return False

        self._values.append(values)
        self._metadata.append((timestamp, session))
        return self.ready

    @property
    def ready(self) -> bool:
        return len(self._values) == self.sequence_length

    def clear(self) -> None:
        self._values.clear()
        self._metadata.clear()
        self._session = None

    def window_array(self) -> np.ndarray[Any, np.dtype[np.float32]]:
        if not self.ready:
            raise ValueError("sequence buffer is not ready")
        return np.stack(tuple(self._values), axis=0)

    def metadata(self) -> tuple[str, str, str]:
        """Return start timestamp, end timestamp, and current session."""
        if not self.ready:
            raise ValueError("sequence buffer is not ready")
        start_timestamp, session = self._metadata[0]
        end_timestamp, _session = self._metadata[-1]
        return start_timestamp, end_timestamp, session


class LSTMInferenceEngine:
    """Streaming LSTM autoencoder inference over model-ready feature rows."""

    def __init__(
        self,
        trained: TrainedLSTMAutoencoder,
        *,
        device: str = "cpu",
        dtype: str = "float32",
        time_fn: Callable[[], float] = time.perf_counter,
    ) -> None:
        if trained.manifest.thresholds is None:
            raise ValueError("trained LSTM manifest has no calibrated thresholds")
        self.trained = trained
        self.device = device
        self.dtype = dtype
        self.time_fn = time_fn
        self.calibration = LSTMThresholdCalibration.from_dict(trained.manifest.thresholds)
        self.feature_columns = trained.feature_columns
        self.sequence_length = int(trained.manifest.schema["sequence_length"])
        self.buffer = LSTMSequenceBuffer(
            feature_columns=self.feature_columns,
            sequence_length=self.sequence_length,
            dtype=dtype,
        )
        self._torch = _torch_module()
        self._device = self._torch.device(device)
        self.trained.model.to(device=self._device, dtype=getattr(self._torch, dtype))
        self.trained.model.eval()

    def observe(self, row: Mapping[str, Any] | pd.Series) -> LSTMInferenceResult | None:
        """Append one row and score the current window once enough rows exist."""
        if not self.buffer.append(row):
            return None
        start_timestamp, end_timestamp, session = self.buffer.metadata()
        return self.score_window(
            self.buffer.window_array(),
            timestamp=end_timestamp,
            session=session,
            window_start_timestamp=start_timestamp,
            window_end_timestamp=end_timestamp,
        )

    def score_window(
        self,
        window: np.ndarray[Any, np.dtype[np.float32]],
        *,
        timestamp: str,
        session: str,
        window_start_timestamp: str,
        window_end_timestamp: str,
    ) -> LSTMInferenceResult:
        """Score one complete ``(T, F)`` sequence window."""
        if window.shape != (self.sequence_length, len(self.feature_columns)):
            raise ValueError(
                "window shape must be "
                f"({self.sequence_length}, {len(self.feature_columns)}); got {window.shape}"
            )
        started = self.time_fn()
        tensor = self._torch.as_tensor(window, dtype=getattr(self._torch, self.dtype))
        tensor = tensor.unsqueeze(0).to(self._device)
        with self._torch.no_grad():
            global_score = float(
                self.trained.model.reconstruction_error(tensor, reduction="sample")
                .detach()
                .cpu()
                .item()
            )
            channel_values = (
                self.trained.model.reconstruction_error(tensor, reduction="channel")
                .detach()
                .cpu()
                .numpy()[0]
                .astype(float)
            )
        latency_ms = (self.time_fn() - started) * 1000.0

        channel_scores = {
            column: float(channel_values[index])
            for index, column in enumerate(self.feature_columns)
        }
        channel_thresholds = {
            column: self.calibration.channel_errors[column].threshold
            for column in self.feature_columns
        }
        anomalous_channels = tuple(
            column for column, score in channel_scores.items() if score > channel_thresholds[column]
        )
        global_threshold = self.calibration.global_error.threshold
        is_anomaly = global_score > global_threshold or bool(anomalous_channels)
        return LSTMInferenceResult(
            model_version=trained_version(self.trained),
            timestamp=timestamp,
            session=session,
            window_start_timestamp=window_start_timestamp,
            window_end_timestamp=window_end_timestamp,
            global_score=global_score,
            global_threshold=global_threshold,
            channel_scores=channel_scores,
            channel_thresholds=channel_thresholds,
            anomalous_channels=anomalous_channels,
            is_anomaly=is_anomaly,
            latency_ms=latency_ms,
        )


def trained_version(trained: TrainedLSTMAutoencoder) -> str:
    """Return the manifest model version for a trained LSTM bundle."""
    return trained.manifest.model_version


def _format_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")


def _torch_module() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise MissingLSTMAutoencoderDependencyError(
            "torch is required for LSTM inference; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
