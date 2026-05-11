"""Rolling-window features for telemetry channels.

Builds on the resampler's uniform time grid: for each numeric channel,
emit windowed mean, std, and rate-of-change over `window_seconds`. Windows
respect session boundaries — a 30-second window at the start of session B
does NOT see any of session A's tail.

Designed for the Phase 2 anomaly-detection pipeline:
  loader.read_sessions → loader.to_wide → resampler.resample_wide
      → segmenter.segment_operating_modes → features.compute_features
      → normalization → IsolationForest

The output preserves all input columns (including the gap flags and the
operating-mode label) and appends per-channel `__mean`, `__std`, `__roc`
columns. Downstream code can pick which features to feed the model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.preprocessing.resampler import (
    GAP_SUFFIX,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
)
from src.preprocessing.segmenter import MODE_COLUMN

MEAN_SUFFIX = "__mean"
STD_SUFFIX = "__std"
ROC_SUFFIX = "__roc"

_FEATURE_SUFFIXES: tuple[str, ...] = (MEAN_SUFFIX, STD_SUFFIX, ROC_SUFFIX)


@dataclass(frozen=True)
class FeatureConfig:
    """Parameters for the rolling-feature computation.

    `window_seconds` is the time window for the mean/std rolling stats and
    also the denominator for the rate-of-change calculation, so all three
    features summarize the same time horizon.

    `min_periods` controls how many in-window samples are needed before
    mean/std emit a non-NaN value. Default 1 lets mean start emitting
    immediately (with shorter effective windows during warmup) while std
    still falls back to NaN on a single sample because pandas can't
    compute σ from one point.
    """

    window_seconds: int = 30
    include_mean: bool = True
    include_std: bool = True
    include_roc: bool = True
    min_periods: int = 1

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.min_periods < 1:
            raise ValueError("min_periods must be at least 1")


def mean_column(channel: str) -> str:
    return f"{channel}{MEAN_SUFFIX}"


def std_column(channel: str) -> str:
    return f"{channel}{STD_SUFFIX}"


def roc_column(channel: str) -> str:
    return f"{channel}{ROC_SUFFIX}"


def compute_features(
    resampled: pd.DataFrame,
    *,
    config: FeatureConfig | None = None,
    value_columns: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Append rolling features to a resampled telemetry DataFrame.

    Window respects session boundaries — features for session B never see
    rows from session A's tail, even if the windows are adjacent in
    wall-clock time. The output is a new DataFrame, same length and order
    as the input, with `<channel>__mean`, `__std`, `__roc` columns added
    for every numeric value column.

    When `value_columns` is provided, only those channels get featurized;
    the rest pass through unchanged. Otherwise we featurize every numeric
    column that isn't a session/timestamp/mode/gap/feature column.
    """
    cfg = FeatureConfig() if config is None else config
    columns = _value_columns(resampled, value_columns)
    _require_columns(resampled, [SESSION_COLUMN, TIMESTAMP_COLUMN])

    if resampled.empty:
        return _empty_with_features(resampled, columns, cfg)

    parts: list[pd.DataFrame] = []
    for session, group in resampled.groupby(
        SESSION_COLUMN, sort=True, group_keys=False, dropna=False
    ):
        parts.append(_features_one_session(str(session), group, columns, cfg))
    return pd.concat(parts, ignore_index=True)


def _features_one_session(
    session: str,
    group: pd.DataFrame,
    columns: Sequence[str],
    cfg: FeatureConfig,
) -> pd.DataFrame:
    ordered = group.sort_values(TIMESTAMP_COLUMN, kind="stable").reset_index(drop=True)
    timestamps = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)

    new_cols: dict[str, np.ndarray] = {}

    if cfg.include_mean or cfg.include_std:
        windowed = ordered.set_index(timestamps)[list(columns)].rolling(
            f"{cfg.window_seconds}s", min_periods=cfg.min_periods
        )
        if cfg.include_mean:
            for col in columns:
                new_cols[mean_column(col)] = windowed[col].mean().to_numpy()
        if cfg.include_std:
            for col in columns:
                new_cols[std_column(col)] = windowed[col].std().to_numpy()

    if cfg.include_roc:
        # Rate-of-change uses a row-count shift derived from the median
        # inter-row interval. For a uniform-grid input (which resample_wide
        # produces) this equals window_seconds × hz. Falling back to a
        # one-row shift when the session has only one sample keeps the
        # operation defined; the resulting ROC will simply be NaN.
        n_rows = _rows_in_window(timestamps, cfg.window_seconds)
        for col in columns:
            shifted = ordered[col].shift(n_rows)
            roc = (ordered[col] - shifted) / cfg.window_seconds
            new_cols[roc_column(col)] = roc.to_numpy()

    if not new_cols:
        return ordered
    # Build all feature columns in one shot — `ordered.assign(**new_cols)`
    # adds them one at a time, which fragments the frame and triggers a
    # PerformanceWarning when there are dozens of channels. Drop any columns
    # we're about to recreate first so a second call on already-featurized
    # input replaces rather than duplicates them (matching the old assign()).
    to_replace = [c for c in new_cols if c in ordered.columns]
    base = ordered.drop(columns=to_replace) if to_replace else ordered
    feature_block = pd.DataFrame(new_cols, index=base.index)
    return pd.concat([base, feature_block], axis=1)


def _rows_in_window(timestamps: pd.Series, window_seconds: int) -> int:
    """Number of rows that span `window_seconds`, given the input grid."""
    if len(timestamps) < 2:
        return 1
    deltas = timestamps.diff().dt.total_seconds().dropna()
    if deltas.empty:
        return 1
    median_delta = float(deltas.median())
    if median_delta <= 0:
        return 1
    return max(1, int(round(window_seconds / median_delta)))


def _value_columns(
    df: pd.DataFrame,
    override: Iterable[str] | None,
) -> list[str]:
    """Choose which columns to featurize.

    Explicit override wins. Otherwise pick every numeric column that isn't
    one of the structural columns (session/timestamp/mode) and isn't itself
    a gap flag or a previously-emitted feature column.
    """
    if override is not None:
        cols = list(override)
        _require_columns(df, cols)
        for col in cols:
            if not pd.api.types.is_numeric_dtype(df[col]):
                raise TypeError(
                    f"column {col!r} is not numeric; rolling features require numeric input"
                )
        return cols

    skip_exact = {SESSION_COLUMN, TIMESTAMP_COLUMN, MODE_COLUMN}
    columns: list[str] = []
    for raw in df.columns:
        col = str(raw)
        if col in skip_exact:
            continue
        if any(col.endswith(suffix) for suffix in (GAP_SUFFIX, *_FEATURE_SUFFIXES)):
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        columns.append(col)
    return columns


def _require_columns(frame: pd.DataFrame, columns: Sequence[str]) -> None:
    missing = set(columns) - set(frame.columns)
    if missing:
        raise KeyError(f"missing required columns: {', '.join(sorted(missing))}")


def _empty_with_features(
    resampled: pd.DataFrame,
    columns: Sequence[str],
    cfg: FeatureConfig,
) -> pd.DataFrame:
    """Empty-input fast path: return an empty frame with the right schema."""
    feature_columns: list[str] = []
    for col in columns:
        if cfg.include_mean:
            feature_columns.append(mean_column(col))
        if cfg.include_std:
            feature_columns.append(std_column(col))
        if cfg.include_roc:
            feature_columns.append(roc_column(col))
    return pd.DataFrame(columns=[*resampled.columns, *feature_columns])
