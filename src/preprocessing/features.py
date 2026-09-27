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

from src.columns import (
    MODE_COLUMN,
    ROLLING_SUFFIXES,
    SESSION_COLUMN,
    TIMESTAMP_COLUMN,
    is_flag_column,
    mean_column,
    roc_column,
    std_column,
)
from src.dataframe_utils import require_columns as _require_columns

#: Mirrors ``src.preprocessing.session_context.CONTEXT_PREFIX`` (kept local to
#: avoid importing the behavior schema into this leaf module).
CONTEXT_COLUMN_PREFIX = "context:"

# Telemetry features feed a downstream model after z-score normalization;
# float32 (~7 significant digits) is ample and roughly halves the RAM held
# by the featurized frame, which is the dominant cost during training (each
# raw channel becomes raw + __mean + __std + __roc). The rolling reductions
# below are still computed by pandas in float64 — only the stored result is
# narrowed. Scaler μ/σ stats stay float64 (see normalization.py).
FEATURE_DTYPE = np.float32


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
        # Whole-frame, not per column. `windowed[col].mean()` in a loop asks
        # pandas to re-validate and re-dispatch the same window once per
        # channel: profiling the live tick showed ~135 such calls per tick
        # and 70.8 % of the stage's runtime inside pandas machinery (numpy
        # was 3.9 %) — dispatch overhead, not arithmetic. `windowed.mean()`
        # computes every column in one pass for bit-identical output.
        if cfg.include_mean:
            means = windowed.mean()
            for col in columns:
                new_cols[mean_column(col)] = means[col].to_numpy(dtype=FEATURE_DTYPE)
        if cfg.include_std:
            stds = windowed.std()
            for col in columns:
                new_cols[std_column(col)] = stds[col].to_numpy(dtype=FEATURE_DTYPE)

    if cfg.include_roc:
        new_cols.update(_rate_of_change_block(ordered, timestamps, columns, cfg.window_seconds))

    # Narrow the passthrough value columns too, so the whole featurized
    # frame is float32 rather than just the appended feature columns.
    value_downcast = {col: FEATURE_DTYPE for col in columns}
    if not new_cols:
        return ordered.astype(value_downcast)
    # Build all feature columns in one shot — `ordered.assign(**new_cols)`
    # adds them one at a time, which fragments the frame and triggers a
    # PerformanceWarning when there are dozens of channels. Drop any columns
    # we're about to recreate first so a second call on already-featurized
    # input replaces rather than duplicates them (matching the old assign()).
    to_replace = [c for c in new_cols if c in ordered.columns]
    base = ordered.drop(columns=to_replace) if to_replace else ordered
    base = base.astype({c: t for c, t in value_downcast.items() if c in base.columns})
    feature_block = pd.DataFrame(new_cols, index=base.index)
    return pd.concat([base, feature_block], axis=1)


def _rate_of_change_block(
    ordered: pd.DataFrame,
    timestamps: pd.Series,
    columns: Sequence[str],
    window_seconds: int,
) -> dict[str, np.ndarray]:
    """``{<channel>__roc: values}`` for one time-ordered session.

    The only rate-of-change implementation: :func:`compute_features` and
    :func:`compute_rate_of_change` (the behavior models' prerequisite
    context, B01.2) both call it, so a persisted steady-state gate sees the
    exact derivative the refit filtered on.
    """
    # Rate-of-change uses a row-count shift derived from the median
    # inter-row interval. For a uniform-grid input (which resample_wide
    # produces) this equals window_seconds × hz. Falling back to a
    # one-row shift when the session has only one sample keeps the
    # operation defined; the resulting ROC will simply be NaN.
    n_rows = _rows_in_window(timestamps, window_seconds)
    # Divide by the row pair's actual elapsed time, not the nominal
    # window_seconds (8.4). On a dense grid the two are identical
    # (n_rows was derived to make n_rows × median_delta ≈ window_seconds),
    # but feature_pipeline._drop_fully_stale_rows can remove whole rows
    # (a long power-off gap), leaving row-index-adjacent-but-time-distant
    # neighbors on either side of the seam. Dividing that pair's real
    # delta by the fixed window_seconds understates the elapsed time and
    # manufactures a spurious slope right at the seam; the true elapsed
    # time normalizes it away instead. A non-positive/undefined elapsed
    # time (warmup, or a duplicate timestamp) makes the ratio meaningless
    # — NaN it rather than divide by zero.
    shifted_timestamps = timestamps.shift(n_rows)
    elapsed = (timestamps - shifted_timestamps).dt.total_seconds()
    safe_elapsed = elapsed.where(elapsed > 0)
    # Whole-frame for the same reason as the rolling block above: one
    # `shift` and one divide over every channel rather than three pandas
    # round-trips per channel.
    values = ordered[list(columns)]
    deltas = values - values.shift(n_rows)
    rocs = deltas.div(safe_elapsed, axis=0)
    return {roc_column(col): rocs[col].to_numpy(dtype=FEATURE_DTYPE) for col in columns}


def compute_rate_of_change(
    frame: pd.DataFrame,
    columns: Iterable[str],
    *,
    config: FeatureConfig | None = None,
) -> pd.DataFrame:
    """Append ``<channel>__roc`` for ``columns`` without reordering ``frame``.

    Prerequisite context for behavior models with persisted rate gates
    (B01.2): the behavior decoder runs *before* :func:`compute_features`,
    so the derivatives it needs are computed here first — same session
    grouping, time ordering, horizon and units as :func:`compute_features`,
    through the same :func:`_rate_of_change_block`. Rows keep their input
    order and index (the pre-feature operating frame is not re-sorted).
    Columns absent from ``frame`` are skipped (the gate then refuses with a
    named reason); an already-derived ``__roc`` column is never itself
    differentiated.
    """
    cfg = FeatureConfig() if config is None else config
    wanted = [
        col
        for col in dict.fromkeys(columns)
        if col in frame.columns and not col.endswith(ROLLING_SUFFIXES)
    ]
    out = frame.copy()
    if not wanted or frame.empty:
        for col in wanted:
            out[roc_column(col)] = np.full(len(frame), np.nan, dtype=FEATURE_DTYPE)
        return out
    _require_columns(frame, [SESSION_COLUMN, TIMESTAMP_COLUMN])
    numeric = frame[wanted].apply(pd.to_numeric, errors="coerce")
    results = {roc_column(col): np.full(len(frame), np.nan, dtype=FEATURE_DTYPE) for col in wanted}
    positions = np.arange(len(frame))
    for _, idx in frame.groupby(SESSION_COLUMN, sort=True, dropna=False).indices.items():
        group_positions = positions[np.asarray(idx)]
        stamps = pd.to_datetime(frame[TIMESTAMP_COLUMN].iloc[group_positions], utc=True)
        order = np.argsort(stamps.to_numpy(), kind="stable")
        ordered_positions = group_positions[order]
        ordered = numeric.iloc[ordered_positions].reset_index(drop=True)
        timestamps = stamps.iloc[order].reset_index(drop=True)
        block = _rate_of_change_block(ordered, timestamps, wanted, cfg.window_seconds)
        for name, values in block.items():
            results[name][ordered_positions] = values
    for name, values in results.items():
        out[name] = values
    return out


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
        # Serving context (B01.5, src.preprocessing.session_context) is
        # per-start bookkeeping for behavior eligibility, not telemetry.
        if col.startswith(CONTEXT_COLUMN_PREFIX):
            continue
        if is_flag_column(col) or col.endswith(ROLLING_SUFFIXES):
            continue
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        columns.append(col)
    return columns


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
