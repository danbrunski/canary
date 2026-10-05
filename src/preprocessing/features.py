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
from typing import Any

import numpy as np
import pandas as pd

from src.columns import (
    MEAN_SUFFIX,
    MODE_COLUMN,
    ROLLING_SUFFIXES,
    SESSION_COLUMN,
    STD_SUFFIX,
    TIMESTAMP_COLUMN,
    is_flag_column,
    mean_column,
    roc_column,
    std_column,
)
from src.dataframe_utils import require_columns as _require_columns

#: Mirrors ``src.behavior.session_context.CONTEXT_PREFIX`` (kept local to
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


#: P02.9 — the declared tolerance between :func:`compute_features_last_row`
#: and the last row of :func:`compute_features` over the same frame, for the
#: ``__mean`` and ``__std`` columns only (``__roc`` and every passthrough
#: column are bit-identical: they run the batch code itself).
#:
#: Mean/std are a **second kernel**, numpy over the one window
#: (:func:`_window_mean_std`), held to this tolerance rather than shared with
#: pandas' — kept for speed (operator-side decision, P02.9 verification,
#: 2026-10-05). pandas' rolling kernel slides a compensated sum (mean) and a
#: Welford update with removals (std) from the *frame's first row*, so its
#: value for one row carries rounding from every value that passed through
#: the window before, and moves with where the frame starts: the live ring
#: buffer's own output already did, every tick. The last-row kernel sums the
#: row's window afresh.
#:
#: The bound is split by column, each from the residue the *reference*
#: carries, with ``peak`` = max |x| of the channel over the session's frame
#: (the history the batch kernel slid over, not the window: that history is
#: where the residue comes from) and ``x`` the batch value:
#:
#: - ``__mean``: ``|d| <= 2**-23 |x| + 64 eps64 peak``. pandas' rolling mean
#:   is a compensated (Kahan) sum with removals, so its residue is a few
#:   eps64 of the largest value it ever held: measured <= 1.73 eps64 peak
#:   over 300 random histories with peaks 1e0..1e8 (P02.9 verification,
#:   2026-10-05). 64 is ~37x that margin. ``2**-23 |x|`` is one float32 ulp
#:   (:data:`FEATURE_DTYPE`) of the value, for the narrowing.
#: - ``__std``, bounded in *variance* terms: ``|x**2 - y**2| <= 128 eps64
#:   peak**2``, plus one float32 ulp of ``x`` (``y`` the last-row value). The
#:   Welford update with removals leaves a residue in the variance, not the
#:   std, of order eps64 peak**2, and ``|x - y| = |x**2 - y**2| / (x + y)``
#:   exactly, so the check is ``|x - y| <= 2**-23 |x| + 128 eps64 peak**2 /
#:   (x + y)``. That one form covers both regimes with no switch: for a
#:   window with real spread it is ~residue / (2 std); as the true std goes to
#:   0 it becomes ``x <= sqrt(128 eps64) peak`` (1.7e-7 peak). Measured
#:   residues: <= 11.1 eps64 peak**2 over 300 random histories with peaks
#:   1e0..1e8 (verifier), and **61 eps64 peak**2 on real data**:
#:   ``derived:STFT_BANK_BALANCE__std`` on 2026-10-04T194342, a window of two
#:   equal values (-0.78125) among 28 NaN, peak 1.72, batch 1.99e-7 against
#:   an exact 0. 128 is ~2x the real figure.
#:
#: A window-relative bound was tried and refused correct rows: a channel at
#: 1e5 for 60 s, then a 30 s window near 1 with std 0.0723, has a batch std of
#: 0.072299 against an exact 0.072290 (the last-row value), 1.2e-4 of the
#: window's own scale. There the std bound is 2.0e-3, so the residue (8.6e-6)
#: passes, and so would a ddof=0 std (off by 1.2e-3, a variance difference of
#: 78 eps64 peak**2): at that offset the batch reference is itself uncertain by
#: about that much, and no bound against it can resolve ddof. With a 1e3
#: history the same ddof=0 error is 7.8e5 eps64 peak**2 and is refused. The
#: mean bound there is 1.2e-7 and refuses a 1 % mean error (1.0e-2); the old
#: shared ``2**-23 peak`` (1.2e-2) accepted it. On real data the batch std of
#: a constant 30 s GPS heading window was 3.7e-6 (truth 0; 0 from the batch
#: too once the frame starts 100 rows later; 2026-10-04T215234), inside its
#: bound.
LAST_ROW_RTOL = 2.0**-23
#: The batch kernel's history residue, in eps64 x peak (mean) and in
#: eps64 x peak**2 of *variance* (std); derivations above.
LAST_ROW_MEAN_RESIDUE = 64.0
LAST_ROW_VARIANCE_RESIDUE = 128.0
_EPS64 = float(np.finfo(np.float64).eps)
_TINY64 = float(np.finfo(np.float64).tiny)


def last_row_mismatches(
    frame: pd.DataFrame,
    expected: pd.DataFrame,
    actual: pd.DataFrame,
) -> list[str]:
    """Columns where one-row ``actual`` breaks the P02.9 contract with one-row
    ``expected`` (the batch's last row over ``frame``): every
    column exact (NaN equal to NaN, the sign of zero kept) except
    ``__mean``/``__std``, each within its own bound (:func:`_last_row_bound`,
    derived at :data:`LAST_ROW_RTOL`). A column-name, order or
    dtype difference is reported as ``"<schema>"``.
    """
    if list(expected.columns) != list(actual.columns) or not expected.dtypes.equals(actual.dtypes):
        return ["<schema>"]
    session: pd.DataFrame | None = None
    out: list[str] = []
    for position, col in enumerate(expected.columns):
        # Positional, so a repeated passthrough label compares cell by cell.
        x: Any = expected.iat[0, position]
        y: Any = actual.iat[0, position]
        if pd.isna(x) and pd.isna(y):
            continue
        name = str(col)
        if name.endswith((MEAN_SUFFIX, STD_SUFFIX)) and not (pd.isna(x) or pd.isna(y)):
            if session is None:
                session = _last_session_group(frame)
            base = name.removesuffix(MEAN_SUFFIX).removesuffix(STD_SUFFIX)
            if abs(float(x) - float(y)) <= _last_row_bound(
                float(x), float(y), _peak(session, base), std=name.endswith(STD_SUFFIX)
            ):
                continue
        elif x == y and (not isinstance(x, float | np.floating) or np.signbit(x) == np.signbit(y)):
            continue
        out.append(name)
    return out


def _last_row_bound(expected: float, actual: float, peak: float, *, std: bool) -> float:
    """The P02.9 bound on ``|expected - actual|`` for one ``__mean``/``__std``
    cell (batch value ``expected``) over a channel peaking at ``peak`` — see
    :data:`LAST_ROW_RTOL`. The std form is the variance bound
    ``|x**2 - y**2| <= 128 eps64 peak**2`` divided through by ``x + y``."""
    ulp = LAST_ROW_RTOL * abs(expected)
    if not std:
        return ulp + LAST_ROW_MEAN_RESIDUE * _EPS64 * peak
    variance = LAST_ROW_VARIANCE_RESIDUE * _EPS64 * peak * peak
    return ulp + variance / max(abs(expected) + abs(actual), _TINY64)


def _peak(frame: pd.DataFrame, column: str) -> float:
    """Largest finite magnitude of ``column`` in ``frame`` (0 when none)."""
    if column not in frame.columns:
        return 0.0
    values = frame[column].to_numpy(dtype=np.float64, na_value=np.nan)
    finite = np.abs(values[np.isfinite(values)])
    return float(finite.max()) if finite.size else 0.0


def compute_features_last_row(
    resampled: pd.DataFrame,
    *,
    config: FeatureConfig | None = None,
    value_columns: Iterable[str] | None = None,
) -> pd.DataFrame:
    """The last row :func:`compute_features` would return for ``resampled``,
    computed from that row's own window rather than the whole frame (P02.9).

    The live tick featurizes its whole ring buffer every second and keeps one
    row; this computes only that row, with the same window semantics as the
    batch path: the row's session (the last group in ``groupby(sort=True)``
    order), time-ordered, a right-closed ``(t - window, t]`` window with
    ``min_periods``, infinities treated as missing for mean/std (as pandas'
    rolling does), a constant window's mean equal to its value and its std
    exactly 0, std with ``ddof=1``. ``__roc`` and the passthrough columns run
    the batch code itself (:func:`_rate_of_change_block` over the row pair,
    :func:`_with_feature_columns`) and are bit-identical. Mean/std are a
    second kernel (:func:`_window_mean_std`, numpy over the one window, kept
    for speed) held to :data:`LAST_ROW_RTOL`, not bit-for-bit — see there;
    :func:`last_row_mismatches` checks it.

    Stateless: nothing carries from one call to the next, so a reset, a
    replay, or a session change needs no invalidation. Returns a one-row
    frame (index ``[0]``) with the batch path's columns and dtypes, or the
    batch path's empty frame for empty input.
    """
    cfg = FeatureConfig() if config is None else config
    columns = _value_columns(resampled, value_columns)
    _require_columns(resampled, [SESSION_COLUMN, TIMESTAMP_COLUMN])
    if resampled.empty:
        return _empty_with_features(resampled, columns, cfg)

    ordered, stamps_ns = _last_row_session(resampled)
    last = len(ordered) - 1

    new_cols: dict[str, np.ndarray] = {}
    if columns and (cfg.include_mean or cfg.include_std):
        start = _window_start(stamps_ns, cfg.window_seconds)
        window = ordered[list(columns)].iloc[start:].to_numpy(dtype=np.float64, na_value=np.nan)
        mean, std = _window_mean_std(window, cfg.min_periods)
        if cfg.include_mean:
            for i, col in enumerate(columns):
                new_cols[mean_column(col)] = mean[i : i + 1].astype(FEATURE_DTYPE)
        if cfg.include_std:
            for i, col in enumerate(columns):
                new_cols[std_column(col)] = std[i : i + 1].astype(FEATURE_DTYPE)
    if columns and cfg.include_roc:
        # The batch's own ``__roc`` code over the row pair it would difference:
        # the shift is the whole session's (median spacing), so it is resolved
        # here and handed to :func:`_rate_of_change_block` as one row.
        timestamps = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
        previous = last - _rows_in_window(timestamps, cfg.window_seconds)
        rows = [previous, last] if previous >= 0 else [last]
        pair = ordered.iloc[rows].reset_index(drop=True)
        block = _rate_of_change_block(
            pair, timestamps.take(rows).reset_index(drop=True), columns, n_rows=1
        )
        new_cols.update({name: values[-1:] for name, values in block.items()})

    return _with_feature_columns(ordered.iloc[[last]].reset_index(drop=True), columns, new_cols)


def _last_row_session(frame: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """The session :func:`compute_features` emits last, time-ordered (index
    reset), with its stamps as epoch nanoseconds."""
    group = _last_session_group(frame)
    if not group[TIMESTAMP_COLUMN].is_monotonic_increasing:
        group = group.sort_values(TIMESTAMP_COLUMN, kind="stable")
    ordered = group.reset_index(drop=True)
    stamps = pd.to_datetime(ordered[TIMESTAMP_COLUMN], utc=True)
    return ordered, stamps.to_numpy(dtype="datetime64[ns]").view(np.int64)


def _window_start(stamps_ns: np.ndarray, window_seconds: int) -> int:
    """First row of the newest row's right-closed ``(t - window, t]`` window."""
    window_ns = int(window_seconds) * 1_000_000_000
    return int(np.searchsorted(stamps_ns, stamps_ns[-1] - window_ns, side="right"))


def _last_session_group(frame: pd.DataFrame) -> pd.DataFrame:
    """The rows of the session :func:`compute_features` emits last."""
    sessions = frame[SESSION_COLUMN]
    first = sessions.iloc[0]
    if not pd.isna(first) and bool((sessions == first).all()):
        return frame
    indices = frame.groupby(SESSION_COLUMN, sort=True, dropna=False).indices
    last_positions = list(indices.values())[-1]
    return frame.iloc[np.asarray(last_positions)]


def _window_mean_std(window: np.ndarray, min_periods: int) -> tuple[np.ndarray, np.ndarray]:
    """Per-column mean and std (ddof=1) of ``window`` (rows × columns),
    matching pandas' rolling kernel's NaN/inf, constant-window, sign and
    ``min_periods`` rules for one window."""
    values = np.where(np.isinf(window), np.nan, window)
    valid = ~np.isnan(values)
    nobs = valid.sum(axis=0)
    has = nobs > 0
    zeroed = np.where(valid, values, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw_mean = zeroed.sum(axis=0) / nobs
    # The newest valid value per column: pandas returns it as the mean of a
    # window whose valid values are all equal, and a std of exactly 0.
    newest = values.shape[0] - 1 - np.argmax(valid[::-1], axis=0)
    newest_value = values[newest, np.arange(values.shape[1])]
    constant = has & np.where(valid, values == newest_value, True).all(axis=0)
    negatives = (valid & np.signbit(values)).sum(axis=0)
    mean = np.where(constant, newest_value, raw_mean)
    mean = np.where(~constant & (negatives == 0) & (mean < 0), 0.0, mean)
    mean = np.where(~constant & (negatives == nobs) & (mean > 0), 0.0, mean)
    mean = np.where(has & (nobs >= min_periods), mean, np.nan)

    deviations = np.where(valid, values - raw_mean, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        variance = (deviations * deviations).sum(axis=0) / (nobs - 1)
    variance = np.where(constant, 0.0, variance)
    variance = np.where((nobs >= min_periods) & (nobs > 1), variance, np.nan)
    return mean, np.sqrt(variance)


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
        n_rows = _rows_in_window(timestamps, cfg.window_seconds)
        new_cols.update(_rate_of_change_block(ordered, timestamps, columns, n_rows=n_rows))

    return _with_feature_columns(ordered, columns, new_cols)


def _with_feature_columns(
    ordered: pd.DataFrame,
    columns: Sequence[str],
    new_cols: dict[str, np.ndarray],
) -> pd.DataFrame:
    """``ordered`` with its value columns narrowed and ``new_cols`` appended:
    the one output assembly for :func:`compute_features` and
    :func:`compute_features_last_row`."""
    # Narrow the passthrough value columns too, so the whole featurized
    # frame is float32 rather than just the appended feature columns.
    # Build all feature columns in one shot — `ordered.assign(**new_cols)`
    # adds them one at a time, which fragments the frame and triggers a
    # PerformanceWarning when there are dozens of channels. Drop any columns
    # we're about to recreate first so a second call on already-featurized
    # input replaces rather than duplicates them (matching the old assign()).
    to_replace = [c for c in new_cols if c in ordered.columns]
    base = ordered.drop(columns=to_replace) if to_replace else ordered
    base = _narrowed(base, [c for c in dict.fromkeys(columns) if c in base.columns])
    if not new_cols:
        return base
    # One float32 block, not one pandas column per feature: on the live
    # tick's one-row frame, the per-column forms (``astype`` with a dict and
    # a DataFrame from a dict of arrays) were most of the call (P02.9).
    feature_block = pd.DataFrame(
        np.column_stack(list(new_cols.values())).astype(FEATURE_DTYPE, copy=False),
        columns=list(new_cols),
        index=base.index,
    )
    return pd.concat([base, feature_block], axis=1)


def _narrowed(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """``frame`` with ``columns`` cast to :data:`FEATURE_DTYPE`, in place in
    the column order — the values ``frame.astype({c: FEATURE_DTYPE})`` gives,
    built as one block rather than column by column."""
    if not columns:
        return frame
    if frame.columns.has_duplicates:
        return frame.astype({c: FEATURE_DTYPE for c in columns})
    narrowed = pd.DataFrame(
        frame[list(columns)].to_numpy(dtype=FEATURE_DTYPE, na_value=np.nan),
        columns=list(columns),
        index=frame.index,
    )
    rest = frame.drop(columns=list(columns))
    return pd.concat([rest, narrowed], axis=1)[list(frame.columns)]


def _rate_of_change_block(
    ordered: pd.DataFrame,
    timestamps: pd.Series,
    columns: Sequence[str],
    *,
    n_rows: int,
) -> dict[str, np.ndarray]:
    """``{<channel>__roc: values}`` for time-ordered rows, differencing each
    row against the one ``n_rows`` before it.

    The only rate-of-change arithmetic. :func:`compute_features` and
    :func:`compute_rate_of_change` (the behavior models' prerequisite context,
    B01.2) call it over a whole session with
    ``n_rows = _rows_in_window(session stamps, window)``, so a persisted
    steady-state gate sees the exact derivative the refit filtered on;
    :func:`compute_features_last_row` calls it over the one row pair that
    shift selects, with ``n_rows=1`` (P02.9).
    """
    # Rate-of-change uses a row-count shift derived from the median
    # inter-row interval (``_rows_in_window``). For a uniform-grid input
    # (which resample_wide produces) this equals window_seconds × hz. A
    # session with one sample gets a one-row shift, so the ROC is NaN.
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
        n_rows = _rows_in_window(timestamps, cfg.window_seconds)
        block = _rate_of_change_block(ordered, timestamps, wanted, n_rows=n_rows)
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
    # A label that appears more than once is not one channel: ``df[col]`` is a
    # frame, never numeric, so it was always skipped here — and must stay
    # skipped now that dtypes are read without indexing by label.
    repeated = {str(c) for c in df.columns[df.columns.duplicated(keep=False)]}
    columns: list[str] = []
    # Dtypes read once: ``df[col]`` per column boxed a Series each time,
    # a third of the live tick's last-row featurization (P02.9).
    for raw, dtype in df.dtypes.items():
        col = str(raw)
        if col in skip_exact or col in repeated:
            continue
        # Serving context (B01.5, src.behavior.session_context) is
        # per-start bookkeeping for behavior eligibility, not telemetry.
        if col.startswith(CONTEXT_COLUMN_PREFIX):
            continue
        if is_flag_column(col) or col.endswith(ROLLING_SUFFIXES):
            continue
        if not pd.api.types.is_numeric_dtype(dtype):
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
