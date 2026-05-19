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
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.models.lstm_autoencoder import MissingLSTMAutoencoderDependencyError
from src.models.lstm_training import LSTMThresholdCalibration, TrainedLSTMAutoencoder
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN
from src.timefmt import format_timestamp

LSTM_SCORE_COLUMN = "lstm_reconstruction_error"
LSTM_THRESHOLD_COLUMN = "lstm_reconstruction_threshold"
LSTM_ANOMALY_COLUMN = "lstm_anomaly"

#: Phase 7.2a — how many top channels (by ratio) the LSTM result carries
#: detail for. Keeps the ``AnomalyEvent.detail`` payload bounded; ~90 trained
#: channels × sequence_length 30 would flood the WebSocket otherwise.
LSTM_TOP_CHANNELS = 5


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
    #: Phase 7.2a evidence (compact, bounded by :data:`LSTM_TOP_CHANNELS`).
    #: ``top_channels`` are the channels with the largest ratio of recon
    #: error to threshold; ``reconstruction_series`` is the per-timestep
    #: MAE for each of those channels; ``reconstruction_overlays`` carries
    #: ``actual`` / ``reconstructed`` arrays so the dashboard can render
    #: the divergence directly. Empty when the model hasn't produced
    #: a window yet (back-compat default for stubs in tests).
    top_channels: tuple[str, ...] = ()
    reconstruction_series: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    reconstruction_overlays: Mapping[str, Mapping[str, tuple[float, ...]]] = field(
        default_factory=dict
    )

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
            "top_channels": list(self.top_channels),
            "reconstruction_series": {k: list(v) for k, v in self.reconstruction_series.items()},
            "reconstruction_overlays": {
                k: {sub: list(seq) for sub, seq in overlay.items()}
                for k, overlay in self.reconstruction_overlays.items()
            },
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
        mode_column: str = "operating_mode",
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
        self.mode_column = mode_column
        self.dtype = dtype
        self._values: deque[np.ndarray[Any, np.dtype[np.float32]]] = deque(maxlen=sequence_length)
        self._metadata: deque[tuple[str, str]] = deque(maxlen=sequence_length)
        self._session: str | None = None
        #: Phase 7.5 — the most recent appended row's operating mode, used by
        #: ``LSTMInferenceEngine.observe`` to pick the per-mode threshold
        #: (when the calibration carries one). Tracked separately from the
        #: timestamp/session metadata deque to avoid touching ``metadata()``'s
        #: 3-tuple contract.
        self._last_mode: str | None = None

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
        raw_mode = row.get(self.mode_column)
        self._last_mode = raw_mode if isinstance(raw_mode, str) else None
        return self.ready

    @property
    def ready(self) -> bool:
        return len(self._values) == self.sequence_length

    def clear(self) -> None:
        self._values.clear()
        self._metadata.clear()
        self._session = None
        self._last_mode = None

    @property
    def last_mode(self) -> str | None:
        """Operating mode of the most recently appended row (7.5)."""
        return self._last_mode

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
            operating_mode=self.buffer.last_mode,
        )

    def score_window(
        self,
        window: np.ndarray[Any, np.dtype[np.float32]],
        *,
        timestamp: str,
        session: str,
        window_start_timestamp: str,
        window_end_timestamp: str,
        operating_mode: str | None = None,
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
            # Phase 7.2a: one forward → all reductions + the actual/recon
            # arrays the dashboard overlay needs. Cheaper than calling
            # ``reconstruction_error`` twice for separate reductions and
            # never running ``forward`` again for the overlays.
            reconstructed = self.trained.model.forward(tensor)
            error_tensor = (reconstructed - tensor).abs()
            global_score = float(error_tensor.mean().item())
            channel_values = error_tensor.mean(dim=1).detach().cpu().numpy()[0].astype(float)
            timestep_error_np = error_tensor.detach().cpu().numpy()[0].astype(float)  # (T, F)
            actual_np = tensor.detach().cpu().numpy()[0].astype(float)  # (T, F)
            reconstructed_np = reconstructed.detach().cpu().numpy()[0].astype(float)  # (T, F)
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

        # Phase 7.2a evidence — rank channels by ratio (score / threshold)
        # when the threshold is positive (compares apples to apples across
        # channels with very different magnitudes); fall back to the raw
        # score otherwise. Keep only ``LSTM_TOP_CHANNELS`` so the per-event
        # detail payload stays bounded.
        def _ratio(column: str) -> float:
            threshold = channel_thresholds[column]
            score = channel_scores[column]
            return score / threshold if threshold > 0 else score

        top_channels = tuple(
            sorted(self.feature_columns, key=_ratio, reverse=True)[:LSTM_TOP_CHANNELS]
        )
        column_index = {column: index for index, column in enumerate(self.feature_columns)}
        reconstruction_series = {
            column: tuple(float(v) for v in timestep_error_np[:, column_index[column]])
            for column in top_channels
        }
        reconstruction_overlays = {
            column: {
                "actual": tuple(float(v) for v in actual_np[:, column_index[column]]),
                "reconstructed": tuple(float(v) for v in reconstructed_np[:, column_index[column]]),
            }
            for column in top_channels
        }
        # Phase 7.5 per-mode threshold: prefer the mode's held-out
        # reconstruction-error band when calibration carries one; fall back
        # to the single global threshold otherwise (back-compat with
        # pre-7.5 manifests where ``per_mode_errors`` is absent).
        per_mode = self.calibration.per_mode_errors or {}
        mode_distribution = per_mode.get(operating_mode) if operating_mode is not None else None
        global_threshold = (
            mode_distribution.threshold
            if mode_distribution is not None
            else self.calibration.global_error.threshold
        )
        # Gate the emit on the *global* reconstruction error only. The
        # per-channel thresholds are calibrated from training data and run far
        # too tight live: with ~90 channels at idle/warmup at least one is
        # essentially always over its band, so an `or bool(anomalous_channels)`
        # clause fired every tick and flooded /ws/anomalies with globally-
        # normal "anomalies". `anomalous_channels` / `channel_scores` /
        # `channel_thresholds` are still computed and carried in the event
        # detail so a genuine (global) anomaly stays fully drillable.
        is_anomaly = global_score > global_threshold
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
            top_channels=top_channels,
            reconstruction_series=reconstruction_series,
            reconstruction_overlays=reconstruction_overlays,
        )


def trained_version(trained: TrainedLSTMAutoencoder) -> str:
    """Return the manifest model version for a trained LSTM bundle."""
    return trained.manifest.model_version


def _format_timestamp(value: Any) -> str:
    if value is None or value == "":
        return ""
    return format_timestamp(value)


def _torch_module() -> Any:
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise MissingLSTMAutoencoderDependencyError(
            "torch is required for LSTM inference; run scripts/install_deps.py "
            "or install the 'ml' optional dependency"
        ) from exc
