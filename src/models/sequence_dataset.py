"""Sliding-window datasets for sequence models.

The LSTM autoencoder consumes fixed-length windows shaped ``(T, F)`` from
preprocessed telemetry. PyTorch's ``DataLoader`` then batches those samples
into ``(B, T, F)``. Window discovery is deliberately implemented without a
torch dependency so CI can validate the filtering rules before the optional
``ml`` extra is installed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.config import CanaryConfig
from src.preprocessing.features import FeatureConfig, compute_features
from src.preprocessing.loader import TelemetryValueKind, read_sessions, to_wide
from src.preprocessing.resampler import (
    GAP_SUFFIX,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    ResampleConfig,
    resample_wide,
)
from src.preprocessing.segmenter import MODE_COLUMN, segment_operating_modes

DEFAULT_SEQUENCE_STRIDE = 1


class MissingSequenceDependencyError(ImportError):
    """Raised when torch is required but the optional ml extra is not installed."""


@dataclass(frozen=True)
class SequenceWindow:
    """One valid sequence window in a sorted model-ready frame."""

    session: str
    start_row: int
    end_row: int
    start_timestamp: str
    end_timestamp: str

    @property
    def length(self) -> int:
        return self.end_row - self.start_row

    def to_dict(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "start_row": self.start_row,
            "end_row": self.end_row,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
        }


@dataclass(frozen=True)
class SequenceWindowBuildResult:
    """Resolved feature/gap columns and valid windows for a frame."""

    frame: pd.DataFrame
    feature_columns: tuple[str, ...]
    gap_columns: tuple[str, ...]
    windows: tuple[SequenceWindow, ...]


class SequenceWindowDataset:
    """Map-style PyTorch dataset for fixed-length telemetry windows.

    ``__getitem__`` returns one tensor shaped ``(sequence_length, n_features)``.
    A standard PyTorch ``DataLoader`` will collate those into ``(B, T, F)``.
    """

    def __init__(
        self,
        frame: pd.DataFrame,
        *,
        sequence_length: int,
        stride: int = DEFAULT_SEQUENCE_STRIDE,
        feature_columns: Iterable[str] | None = None,
        gap_columns: Iterable[str] | None = None,
        dtype: str = "float32",
    ) -> None:
        build = build_sequence_windows(
            frame,
            sequence_length=sequence_length,
            stride=stride,
            feature_columns=feature_columns,
            gap_columns=gap_columns,
        )
        self.frame = build.frame
        self.feature_columns = build.feature_columns
        self.gap_columns = build.gap_columns
        self.windows = build.windows
        self.sequence_length = sequence_length
        self.stride = stride
        self.dtype = dtype
        self._values = self.frame.loc[:, list(self.feature_columns)].to_numpy(dtype=dtype)

    @classmethod
    def from_sessions(
        cls,
        session_dirs: Iterable[Path],
        config: CanaryConfig,
        *,
        value: TelemetryValueKind = "value_number",
        sequence_length: int | None = None,
        stride: int = DEFAULT_SEQUENCE_STRIDE,
        feature_columns: Iterable[str] | None = None,
        gap_columns: Iterable[str] | None = None,
        dtype: str = "float32",
    ) -> SequenceWindowDataset:
        frame = sequence_frame_from_sessions(session_dirs, config, value=value)
        length = (
            config.models.lstm_autoencoder.sequence_length
            if sequence_length is None
            else sequence_length
        )
        return cls(
            frame,
            sequence_length=length,
            stride=stride,
            feature_columns=feature_columns,
            gap_columns=gap_columns,
            dtype=dtype,
        )

    def __len__(self) -> int:
        return len(self.windows)

    def __getitem__(self, index: int) -> Any:
        torch = _torch_module()
        window = self.windows[index]
        values = self._values[window.start_row : window.end_row].copy()
        dtype = getattr(torch, self.dtype)
        return torch.as_tensor(values, dtype=dtype)

    def window_metadata(self, index: int) -> SequenceWindow:
        """Return timestamps/session for a sample without materializing a tensor."""
        return self.windows[index]


def sequence_frame_from_sessions(
    session_dirs: Iterable[Path],
    config: CanaryConfig,
    *,
    value: TelemetryValueKind = "value_number",
) -> pd.DataFrame:
    """Load Parquet sessions and run the preprocessing path used by sequence models."""
    sessions = [Path(path) for path in session_dirs]
    if not sessions:
        raise ValueError("at least one session directory is required")

    long = read_sessions(sessions)
    wide = to_wide(long, value=value)
    value_columns = _numeric_value_columns(wide)
    if value_columns:
        resampled = resample_wide(
            wide,
            config=ResampleConfig(hz=config.preprocessing.resample_hz),
            value_columns=value_columns,
        )
    else:
        resampled = pd.DataFrame(
            {
                SESSION_COLUMN: wide[SESSION_COLUMN].copy(),
                TIMESTAMP_COLUMN: pd.to_datetime(wide[TIMESTAMP_COLUMN], utc=True),
            }
        )

    segmented = segment_operating_modes(resampled, config.preprocessing.operating_modes)
    return compute_features(
        segmented,
        config=FeatureConfig(window_seconds=config.preprocessing.rolling_window_seconds),
    )


def build_sequence_windows(
    frame: pd.DataFrame,
    *,
    sequence_length: int,
    stride: int = DEFAULT_SEQUENCE_STRIDE,
    feature_columns: Iterable[str] | None = None,
    gap_columns: Iterable[str] | None = None,
) -> SequenceWindowBuildResult:
    """Return valid windows that stay inside sessions and avoid gaps/NaNs."""
    _validate_window_params(sequence_length, stride)
    _require_columns(frame, [SESSION_COLUMN, TIMESTAMP_COLUMN])

    ordered = _ordered_frame(frame)
    gaps = resolve_gap_columns(ordered, gap_columns)
    features = resolve_sequence_feature_columns(ordered, feature_columns)
    if feature_columns is None and gaps:
        gap_set = set(gaps)
        features = tuple(column for column in features if column not in gap_set)
        if not features:
            raise ValueError("no numeric sequence feature columns available")
    windows: list[SequenceWindow] = []

    for session, group in ordered.groupby(SESSION_COLUMN, sort=True, dropna=False):
        positions = group.index.to_numpy(dtype=int)
        if len(positions) < sequence_length:
            continue
        for local_start in range(0, len(positions) - sequence_length + 1, stride):
            row_positions = positions[local_start : local_start + sequence_length]
            if _window_has_gap(ordered, row_positions, gaps):
                continue
            if _window_has_missing_features(ordered, row_positions, features):
                continue
            start_row = int(row_positions[0])
            end_row = int(row_positions[-1]) + 1
            timestamps = pd.to_datetime(ordered.loc[row_positions, TIMESTAMP_COLUMN], utc=True)
            windows.append(
                SequenceWindow(
                    session=str(session),
                    start_row=start_row,
                    end_row=end_row,
                    start_timestamp=_format_timestamp(timestamps.iloc[0]),
                    end_timestamp=_format_timestamp(timestamps.iloc[-1]),
                )
            )

    return SequenceWindowBuildResult(
        frame=ordered,
        feature_columns=features,
        gap_columns=gaps,
        windows=tuple(windows),
    )


def resolve_sequence_feature_columns(
    frame: pd.DataFrame,
    feature_columns: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """Select numeric model inputs, excluding structural and gap columns."""
    if feature_columns is not None:
        columns = tuple(str(column) for column in feature_columns)
        _require_columns(frame, columns)
        for column in columns:
            if not pd.api.types.is_numeric_dtype(frame[column]):
                raise TypeError(f"column {column!r} is not numeric")
        return columns

    skip = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN}
    discovered: list[str] = []
    for raw in frame.columns:
        column = str(raw)
        if column in skip or column.endswith(GAP_SUFFIX):
            continue
        if pd.api.types.is_numeric_dtype(frame[column]):
            discovered.append(column)
    if not discovered:
        raise ValueError("no numeric sequence feature columns available")
    return tuple(discovered)


def resolve_gap_columns(
    frame: pd.DataFrame,
    gap_columns: Iterable[str] | None = None,
) -> tuple[str, ...]:
    if gap_columns is not None:
        columns = tuple(str(column) for column in gap_columns)
        _require_columns(frame, columns)
        return columns
    return tuple(str(column) for column in frame.columns if str(column).endswith(GAP_SUFFIX))


def _numeric_value_columns(frame: pd.DataFrame) -> list[str]:
    skip = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN}
    return [
        str(column)
        for column in frame.columns
        if str(column) not in skip
        and not str(column).endswith(GAP_SUFFIX)
        and pd.api.types.is_numeric_dtype(frame[column])
    ]


def _ordered_frame(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.copy()
    ordered[TIMESTAMP_COLUMN] = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
    return ordered.sort_values([SESSION_COLUMN, TIMESTAMP_COLUMN], kind="stable").reset_index(
        drop=True
    )


def _window_has_gap(
    frame: pd.DataFrame,
    row_positions: np.ndarray[Any, np.dtype[np.int_]],
    gap_columns: Sequence[str],
) -> bool:
    if not gap_columns:
        return False
    return bool(frame.loc[row_positions, list(gap_columns)].fillna(False).astype(bool).any().any())


def _window_has_missing_features(
    frame: pd.DataFrame,
    row_positions: np.ndarray[Any, np.dtype[np.int_]],
    feature_columns: Sequence[str],
) -> bool:
    values = frame.loc[row_positions, list(feature_columns)]
    return bool(values.isna().any().any())


def _validate_window_params(sequence_length: int, stride: int) -> None:
    if sequence_length <= 0:
        raise ValueError("sequence_length must be positive")
    if stride <= 0:
        raise ValueError("stride must be positive")


def _require_columns(frame: pd.DataFrame, columns: Sequence[str]) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")


def _format_timestamp(value: pd.Timestamp) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")


def _torch_module() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise MissingSequenceDependencyError(
            "torch is required to materialize SequenceWindowDataset tensors; "
            "install the 'ml' optional dependency"
        ) from exc
    return torch
