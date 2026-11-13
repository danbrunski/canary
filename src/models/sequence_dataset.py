"""Sliding-window datasets for sequence models.

The GRU-D LSTM autoencoder consumes fixed-length windows shaped ``(T, F)`` from
preprocessed telemetry, together with a per-cell **mask** ``(T, F)`` (1 where a
channel was freshly observed, 0 where the value is carried forward / stale) and
**Δt** ``(T, F)`` (seconds since each channel's last real observation). PyTorch's
``DataLoader`` then collates those into ``(B, T, F)`` triples. Window discovery
is deliberately implemented without a torch dependency so CI can validate the
masking rules before the optional ``ml`` extra is installed.

Unlike the pre-GRU-D pipeline this module no longer *rejects* windows that touch
a gap: every window inside a session is valid, and staleness is handed to the
model through the mask/Δt channels rather than dropped. The only remaining guard
is leading rows before a channel's first observation, which are imputed to 0
(the empirical mean in z-scored feature space) and masked as unobserved.

"Freshly observed" is literal only under :data:`MASK_ARRIVED_SAMPLE` (A03.1,
a sample arrived this tick). Artifacts trained before it recorded
:data:`MASK_FRESH_WITHIN_MAX_GAP`, where forward-filled cells younger than
``max_gap`` (2 s fast, 120 s slow) also read as observed; they keep receiving
that mask (``OBSERVATION_MASK_SCHEMA_KEY``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.columns import (
    ARRIVED_SUFFIX,
    GAP_SUFFIX,
    MODE_COLUMN,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    base_channel,
    is_flag_column,
)
from src.config import CanaryConfig
from src.dataframe_utils import require_columns as _require_columns
from src.preprocessing.features import FeatureConfig, compute_features
from src.preprocessing.loader import TelemetryValueKind, resample_and_segment
from src.timefmt import format_timestamp as _format_timestamp
from src.trainability import source_exclusion_reason

DEFAULT_SEQUENCE_STRIDE = 1

#: A window never spans a hole in time longer than this (A01.3). Rows are on a
#: 1 s grid, so this tolerates a few missing ticks (a dropped live tick, a
#: dropped fully-stale row) and nothing longer: past it, the window no longer
#: describes one stretch of continuous operation. Offline window building and
#: the live ``SequenceWindowBuffer`` both read this one value, so a model is
#: calibrated on exactly the windows it will be served.
WINDOW_MAX_GAP_SECONDS = 5.0

#: A03.1 — manifest ``schema`` key recording what the sequence mask means for
#: an artifact: ``{"rule": <one of MASK_RULES>, "freshness_fallback_columns":
#: [...]}``. Training writes the rule the dataset actually used; serving reads
#: it, so an artifact is always fed the mask it was trained on. Absent on
#: artifacts trained before the record existed, which all used
#: :data:`MASK_FRESH_WITHIN_MAX_GAP`.
OBSERVATION_MASK_SCHEMA_KEY = "observation_mask"
#: Legacy: ``1`` where ``¬<channel>__gap`` and the value is finite — "the newest
#: sample is younger than ``max_gap``". Forward-filled cells read as observed
#: (baro: 52 real samples in 1,085 s, mask 1 on 90.8 % of rows). The deployed
#: 2026-09-20 artifact was trained on this; it keeps receiving it.
MASK_FRESH_WITHIN_MAX_GAP = "fresh_within_max_gap"
#: ``1`` where ``<channel>__arrived`` and the value is finite — a sample was
#: delivered this tick. A column whose channel carries no arrival flag (some
#: derived channels) falls back to the freshness rule and is named in the
#: record's ``freshness_fallback_columns``.
MASK_ARRIVED_SAMPLE = "arrived_sample"
MASK_RULES: tuple[str, ...] = (MASK_FRESH_WITHIN_MAX_GAP, MASK_ARRIVED_SAMPLE)
#: What new training uses.
DEFAULT_TRAINING_MASK_RULE = MASK_ARRIVED_SAMPLE


def observation_mask_record(
    rule: str, freshness_fallback_columns: Sequence[str] = ()
) -> dict[str, Any]:
    """The schema entry a trainer writes under :data:`OBSERVATION_MASK_SCHEMA_KEY`."""
    _check_mask_rule(rule)
    return {
        "rule": rule,
        "freshness_fallback_columns": [str(column) for column in freshness_fallback_columns],
    }


def observation_mask_from_schema(schema: Mapping[str, Any]) -> tuple[str, frozenset[str]]:
    """``(rule, freshness_fallback_columns)`` an artifact was trained with.

    A schema without the record predates it and was trained on
    :data:`MASK_FRESH_WITHIN_MAX_GAP`. An unknown rule is refused: guessing
    would feed the encoder a mask it never saw.
    """
    record = schema.get(OBSERVATION_MASK_SCHEMA_KEY)
    if record is None:
        return MASK_FRESH_WITHIN_MAX_GAP, frozenset()
    if not isinstance(record, Mapping):
        raise ValueError(f"schema {OBSERVATION_MASK_SCHEMA_KEY!r} must be a mapping")
    rule = str(record.get("rule"))
    _check_mask_rule(rule)
    fallback = record.get("freshness_fallback_columns") or ()
    return rule, frozenset(str(column) for column in fallback)


def _check_mask_rule(rule: str) -> None:
    if rule not in MASK_RULES:
        raise ValueError(f"unknown observation mask rule {rule!r}; expected one of {MASK_RULES}")


def arrival_column_for(feature_column: str) -> str:
    """The ``__arrived`` flag a feature column's mask reads (rolling siblings
    share their base channel's)."""
    return f"{_channel_base(feature_column)}{ARRIVED_SUFFIX}"


class MissingSequenceDependencyError(ImportError):
    """Raised when torch is required but the optional ml extra is not installed."""


@dataclass(frozen=True)
class SequenceWindow:
    """One sequence window in a sorted model-ready frame."""

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
    """Resolved feature/gap columns and windows for a frame.

    ``frame`` is the sorted, session-grouped frame the windows index into.
    ``mask`` / ``delta`` are full-frame ``(rows, F)`` float arrays in
    ``feature_columns`` order; a window slices the same row range out of all of
    ``values``/``mask``/``delta``. Δt is the cumulative seconds since the last
    observed (``mask == 1``) tick per channel, reset to 0 on every observation
    and accumulating the inter-row time delta otherwise.
    """

    frame: pd.DataFrame
    feature_columns: tuple[str, ...]
    gap_columns: tuple[str, ...]
    windows: tuple[SequenceWindow, ...]
    mask: np.ndarray[Any, np.dtype[np.float64]]
    delta: np.ndarray[Any, np.dtype[np.float64]]
    #: A03.1 — the mask rule applied, and the columns that fell back to the
    #: freshness rule under :data:`MASK_ARRIVED_SAMPLE` (no arrival flag).
    observation_mask: str = MASK_FRESH_WITHIN_MAX_GAP
    freshness_fallback_columns: tuple[str, ...] = ()


class SequenceWindowDataset:
    """Map-style PyTorch dataset for fixed-length telemetry windows.

    ``__getitem__`` returns a ``(value, mask, delta)`` tuple of tensors, each
    shaped ``(sequence_length, n_features)``. A standard PyTorch ``DataLoader``
    collates those into three ``(B, T, F)`` tensors.
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
        observation_mask: str | None = None,
        session_envelopes: Mapping[str, Any] | None = None,
    ) -> None:
        build = build_sequence_windows(
            frame,
            sequence_length=sequence_length,
            stride=stride,
            feature_columns=feature_columns,
            gap_columns=gap_columns,
            observation_mask=observation_mask,
        )
        #: A03.1 — the mask rule these windows carry (recorded in the manifest).
        self.observation_mask = build.observation_mask
        self.freshness_fallback_columns = build.freshness_fallback_columns
        #: A03.8 — per-session, per-mode envelope summaries of the *unscaled*
        #: frame (``sequence_training.session_envelopes``), attached by the
        #: caller that still has raw values. ``None`` when nobody did.
        self.session_envelopes = None if session_envelopes is None else dict(session_envelopes)
        self.frame = build.frame
        self.feature_columns = build.feature_columns
        self.gap_columns = build.gap_columns
        self.windows = build.windows
        self.sequence_length = sequence_length
        self.stride = stride
        self.dtype = dtype
        self._training_data_hash: str | None = None
        # Values are imputed to 0 wherever unobserved (leading NaN before a
        # channel's first observation); the mask records the imputation so the
        # loss never scores a fill. See ``_window_value_mask_delta``.
        raw = self.frame.loc[:, list(self.feature_columns)].to_numpy(dtype="float64")
        self._mask = build.mask.astype(dtype, copy=False)
        self._values = np.where(np.isfinite(raw), raw, 0.0).astype(dtype)
        self._delta = build.delta.astype(dtype)

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
        observation_mask: str | None = None,
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
            observation_mask=observation_mask,
        )

    def training_data_hash(self) -> str:
        """``sha256:`` of the window layout and the model-ready frame, cached."""
        if self._training_data_hash is not None:
            return self._training_data_hash
        digest = hashlib.sha256()
        digest.update(
            json.dumps(
                {
                    "feature_columns": list(self.feature_columns),
                    "sequence_length": self.sequence_length,
                    "windows": [window.to_dict() for window in self.windows],
                    "hash_layout": "contiguous-frame-v2",
                },
                sort_keys=True,
            ).encode("utf-8")
        )
        # Windows heavily overlap. Hash the model-ready frame once instead of
        # materializing and hashing the same row sequence_length times.
        values = self.frame.loc[:, list(self.feature_columns)].to_numpy(dtype=np.float64)
        digest.update(np.ascontiguousarray(values).tobytes())
        self._training_data_hash = f"sha256:{digest.hexdigest()}"
        return self._training_data_hash

    def __len__(self) -> int:
        return len(self.windows)

    def __getitem__(self, index: int) -> Any:
        torch = _torch_module()
        window = self.windows[index]
        rows = slice(window.start_row, window.end_row)
        dtype = getattr(torch, self.dtype)
        value = torch.as_tensor(self._values[rows], dtype=dtype)
        mask = torch.as_tensor(self._mask[rows], dtype=dtype)
        delta = torch.as_tensor(self._delta[rows], dtype=dtype)
        return value, mask, delta

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

    segmented = resample_and_segment(sessions, config, value=value)
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
    max_gap_seconds: float = WINDOW_MAX_GAP_SECONDS,
    observation_mask: str | None = None,
) -> SequenceWindowBuildResult:
    """Return every window that stays inside a session, with per-cell mask/Δt.

    A window also never spans a time gap longer than ``max_gap_seconds``: the
    run of rows restarts there, Δt included, exactly as the live buffer clears
    (see :func:`contiguous_segments`).

    GRU-D feeds missingness to the model, so this no longer rejects windows that
    touch a gap or contain forward-filled cells — every contiguous window inside
    a session is valid. The mask and Δt (cumulative seconds since the last
    observation per channel) are computed once over the full frame and sliced
    per window. What the mask means is ``observation_mask`` (A03.1, see
    :data:`MASK_RULES`): ``None`` picks :data:`MASK_ARRIVED_SAMPLE` when the
    frame carries ``__arrived`` flags for its features and the legacy
    :data:`MASK_FRESH_WITHIN_MAX_GAP` otherwise. Asking for the arrival rule on
    a frame without a single arrival flag (a feature frame built before the
    flags existed) is refused rather than silently degraded.
    """
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
    # When gaps are auto-resolved, only honor those for channels that are
    # actually features (an absent channel's perpetually-stale gap flag is
    # irrelevant to the mask of features that *are* present). An explicit
    # ``gap_columns`` is honored verbatim.
    if gap_columns is None:
        feature_bases = {_channel_base(column) for column in features}
        gaps = tuple(gap for gap in gaps if gap[: -len(GAP_SUFFIX)] in feature_bases)

    rule = _resolve_mask_rule(ordered, features, observation_mask)
    segments = contiguous_segments(ordered, max_gap_seconds=max_gap_seconds)
    mask, delta, fallback = _frame_mask_delta(
        ordered, features, gaps, gap_columns is not None, segments, observation_mask=rule
    )

    windows: list[SequenceWindow] = []
    for session, group in ordered.groupby(SESSION_COLUMN, sort=True, dropna=False):
        positions = group.index.to_numpy(dtype=int)
        runs = np.split(positions, np.flatnonzero(np.diff(segments[positions]) != 0) + 1)
        for run in runs:
            if len(run) < sequence_length:
                continue
            for local_start in range(0, len(run) - sequence_length + 1, stride):
                row_positions = run[local_start : local_start + sequence_length]
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
        mask=mask,
        delta=delta,
        observation_mask=rule,
        freshness_fallback_columns=fallback,
    )


def _resolve_mask_rule(
    ordered: pd.DataFrame, features: Sequence[str], requested: str | None
) -> str:
    has_arrivals = any(arrival_column_for(column) in ordered.columns for column in features)
    if requested is None:
        return MASK_ARRIVED_SAMPLE if has_arrivals else MASK_FRESH_WITHIN_MAX_GAP
    _check_mask_rule(requested)
    if requested == MASK_ARRIVED_SAMPLE and not has_arrivals and len(ordered):
        raise ValueError(
            "observation mask 'arrived_sample' requested but the frame carries no "
            "<channel>__arrived flags for any feature column — it was built before "
            "A03.1's arrival flags (rebuild the feature cache; FEATURE_FRAME_CACHE_VERSION "
            "5) or by a path that drops them"
        )
    return requested


def contiguous_segments(
    ordered: pd.DataFrame, *, max_gap_seconds: float = WINDOW_MAX_GAP_SECONDS
) -> np.ndarray[Any, np.dtype[np.int64]]:
    """Label each row of a session/time-ordered frame with its segment.

    A new segment starts at a session change, or where the time since the
    previous row is negative or exceeds ``max_gap_seconds``. Windows and Δt
    never cross a segment boundary — the offline twin of the live buffer
    clearing on the same gap (A01.3).
    """
    n_rows = len(ordered)
    if n_rows == 0:
        return np.zeros(0, dtype=np.int64)
    session_codes = pd.factorize(ordered[SESSION_COLUMN], sort=False)[0]
    times = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True).to_numpy()
    starts = np.zeros(n_rows, dtype=bool)
    starts[0] = True
    if n_rows > 1:
        diffs = (times[1:] - times[:-1]) / np.timedelta64(1, "s")
        starts[1:] = (
            (session_codes[1:] != session_codes[:-1]) | (diffs > max_gap_seconds) | (diffs < 0)
        )
    return (np.cumsum(starts) - 1).astype(np.int64)


def _frame_mask_delta(
    ordered: pd.DataFrame,
    features: Sequence[str],
    gaps: Sequence[str],
    explicit_gaps: bool,
    segments: np.ndarray[Any, np.dtype[np.int64]] | None = None,
    *,
    observation_mask: str = MASK_FRESH_WITHIN_MAX_GAP,
) -> tuple[
    np.ndarray[Any, np.dtype[np.float64]],
    np.ndarray[Any, np.dtype[np.float64]],
    tuple[str, ...],
]:
    """Compute per-cell observed-mask and Δt over the full frame.

    Under :data:`MASK_ARRIVED_SAMPLE` a column whose ``__arrived`` flag is in
    the frame is observed where the flag is set and the value is finite; the
    rest fall back to the freshness rule below and are returned as the third
    element. Under :data:`MASK_FRESH_WITHIN_MAX_GAP` (legacy): for an auto-resolved
    ``<channel>__gap`` column the resampler already encodes freshness, so
    ``mask = ¬gap`` AND the value is non-NaN. A channel with no gap column
    (slow / forward-filled, or an explicit gap-column caller) is observed
    wherever its value is non-NaN. Δt resets to 0 on every observed tick and
    accumulates the inter-row second delta within a segment otherwise
    (``segments``; one segment per session when omitted).
    """
    n_rows = len(ordered)
    n_features = len(features)
    mask = np.zeros((n_rows, n_features), dtype=np.float64)
    delta = np.zeros((n_rows, n_features), dtype=np.float64)
    fallback: list[str] = []
    if n_rows == 0 or n_features == 0:
        return mask, delta, ()

    gap_by_base = {gap[: -len(GAP_SUFFIX)]: gap for gap in gaps if gap.endswith(GAP_SUFFIX)}
    values = ordered.loc[:, list(features)].to_numpy(dtype="float64")
    observed_value = np.isfinite(values)

    for col_index, column in enumerate(features):
        fresh = observed_value[:, col_index]
        if observation_mask == MASK_ARRIVED_SAMPLE:
            arrived = arrival_column_for(column)
            if arrived in ordered.columns:
                arrived_flags = ordered[arrived].fillna(False).astype(bool).to_numpy()
                mask[:, col_index] = (fresh & arrived_flags).astype(np.float64)
                continue
            fallback.append(str(column))
        if not explicit_gaps:
            gap_col = gap_by_base.get(_channel_base(column))
            if gap_col is not None and gap_col in ordered.columns:
                not_gap = ~ordered[gap_col].fillna(True).astype(bool).to_numpy()
                fresh = fresh & not_gap
        mask[:, col_index] = fresh.astype(np.float64)

    # Per-segment cumulative Δt: seconds since the channel was last fresh,
    # restarting where a window may not continue (session change or long gap).
    session_codes = (
        pd.factorize(ordered[SESSION_COLUMN], sort=False)[0] if segments is None else segments
    )
    timestamps = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True).to_numpy()
    for _code in np.unique(session_codes):
        rows = np.flatnonzero(session_codes == _code)
        if rows.size == 0:
            continue
        row_times = timestamps[rows]
        # Inter-row seconds (0 at the session's first row).
        step_seconds = np.zeros(rows.size, dtype=np.float64)
        if rows.size > 1:
            diffs = (row_times[1:] - row_times[:-1]) / np.timedelta64(1, "s")
            step_seconds[1:] = np.maximum(diffs.astype(np.float64), 0.0)
        for col_index in range(n_features):
            col_fresh = mask[rows, col_index] > 0.5
            acc = 0.0
            col_delta = np.zeros(rows.size, dtype=np.float64)
            for local_index in range(rows.size):
                if col_fresh[local_index]:
                    acc = 0.0
                else:
                    acc += step_seconds[local_index]
                col_delta[local_index] = acc
            delta[rows, col_index] = col_delta
    return mask, delta, tuple(fallback)


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
        if column in skip or is_flag_column(column):
            continue
        # H01.1 source gate: discovery never picks an external sensor.
        if source_exclusion_reason(column) is not None:
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


def _channel_base(column: str) -> str:
    """Strip a rolling-feature suffix (``__mean``/``__std``/``__roc``) to the raw
    channel, so a feature and its derivatives map to one ``<channel>__gap``."""
    return base_channel(column)


def _ordered_frame(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = frame.copy()
    ordered[TIMESTAMP_COLUMN] = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
    return ordered.sort_values([SESSION_COLUMN, TIMESTAMP_COLUMN], kind="stable").reset_index(
        drop=True
    )


def _validate_window_params(sequence_length: int, stride: int) -> None:
    if sequence_length <= 0:
        raise ValueError("sequence_length must be positive")
    if stride <= 0:
        raise ValueError("stride must be positive")


def _torch_module() -> Any:
    try:
        import torch
    except ImportError as exc:
        raise MissingSequenceDependencyError(
            "torch is required to materialize SequenceWindowDataset tensors; "
            "install the 'ml' optional dependency"
        ) from exc
    return torch
