"""Local-range steady-state context for behavior contracts (B07.6, B01 schema).

A persisted rate gate reads ``<channel>__roc``: the *net* change over the 30 s
feature horizon, so an up-and-back excursion inside the horizon passes it. A
:class:`~src.decode.behavior_models.LocalRangeCondition` bounds the channel's
``max - min`` over a short trailing window instead. The value is computed
here, once, for every path that grades a behavior model: the batch refit and
evaluation frames and the live ring buffer both reach it through
:func:`src.decode.behavior_decoder.append_behavior_prerequisites`, and the
refit appends it before selecting training rows.

Semantics, per session in time order: for a row at ``t`` the window is the
rows with ``t - window < t_row <= t``. The range is defined only when

* the session has a row at or before ``t - window`` (the whole window lies
  inside the recording — a daemon that just started, a replay that just
  began or a new ``#segment`` piece has no history yet), and
* every row in the window carries a fresh, finite value (a stale sample could
  hide the excursion the condition exists to see).

Otherwise it is NaN and the evaluator refuses the row by name. The column is
``context:*`` serving context: the rolling feature builder skips it and it is
never a trainable feature.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, gap_column
from src.decode.behavior_models import LocalRangeCondition
from src.preprocessing.features import CONTEXT_COLUMN_PREFIX
from src.signals.pairing import to_seconds


def local_range_column(condition: LocalRangeCondition) -> str:
    """The serving-context column carrying ``condition``'s local range."""
    return f"{CONTEXT_COLUMN_PREFIX}range_{condition.window_seconds:g}s:{condition.channel}"


def _window_ranges(seconds: np.ndarray, values: np.ndarray, window: float) -> np.ndarray:
    """Trailing-window ``max - min`` for one time-ordered session (NaN if undefined)."""
    out = np.full(len(values), np.nan)
    if not len(values):
        return out
    usable = np.isfinite(values)
    bad_before = np.concatenate(([0], np.cumsum(~usable)))
    starts = np.searchsorted(seconds, seconds - window, side="right")
    first = seconds[0]
    for i in range(len(values)):
        if seconds[i] - window < first:
            continue  # the window reaches back before the recording began
        lo = starts[i]
        if bad_before[i + 1] - bad_before[lo]:
            continue  # a stale or missing sample inside the window
        span = values[lo : i + 1]
        out[i] = float(span.max() - span.min())
    return out


def append_local_ranges(
    frame: pd.DataFrame, conditions: Iterable[LocalRangeCondition]
) -> pd.DataFrame:
    """Append each condition's local-range column; rows keep order and index.

    A frame without a session column is one recording; without a timestamp
    column no window can be placed and every value is NaN.
    """
    wanted = {local_range_column(c): c for c in conditions}
    out = frame.copy()
    if not wanted:
        return out
    if frame.empty or TIMESTAMP_COLUMN not in frame.columns:
        for name in wanted:
            out[name] = np.full(len(frame), np.nan)
        return out
    positions = np.arange(len(frame))
    groups = (
        [np.asarray(idx) for idx in frame.groupby(SESSION_COLUMN, sort=False).indices.values()]
        if SESSION_COLUMN in frame.columns
        else [positions]
    )
    stamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True)
    seconds = np.asarray(to_seconds(stamps), dtype=float)
    for name, condition in wanted.items():
        result = np.full(len(frame), np.nan)
        if condition.channel in frame.columns:
            values = pd.to_numeric(frame[condition.channel], errors="coerce").to_numpy(dtype=float)
            gap = gap_column(condition.channel)
            if gap in frame.columns:
                stale = frame[gap].astype(object).fillna(True).astype(bool).to_numpy()
                values = np.where(stale, np.nan, values)
            for group in groups:
                ordered = group[np.argsort(seconds[group], kind="stable")]
                result[ordered] = _window_ranges(
                    seconds[ordered], values[ordered], condition.window_seconds
                )
        out[name] = result
    return out


__all__ = ["append_local_ranges", "local_range_column"]
