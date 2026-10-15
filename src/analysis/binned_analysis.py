"""Ad-hoc binned aggregation over telemetry channels (Task 5.1,
engine-analysis-and-mapping.md).

Answers "trims by rpm×load" (or any value/x[/y] combination) without a
hand-written SQL query or a refit: pick a value channel, an x channel, an
optional y channel, and an aggregation, and get back a 1D or 2D grid of
aggregated values — the same on-demand shape
:func:`src.analysis.surface_2d.fit_grid_binned_mean` persists for a *fitted*
behavior surface, but computed fresh over whatever scope the caller asks for
(one session, a date range, the whole corpus) and with a choice of
aggregation (``surface_2d`` only ever computes mean/std/count).

Callers (:mod:`src.control.binned_analysis`) hand in the raw long-format rows
from :meth:`src.storage.telemetry_query.TelemetryQuery.fetch_channel_rows`
(one row per ``(timestamp, channel)`` sample). Channels sampled
independently (OBD PIDs polled in sequence, CAN signals decoded off
different frames) essentially never share an exact timestamp, so this pivots
them onto a common grid via :func:`src.preprocessing.resampler.resample_wide`
— the identical forward-fill alignment the training/refit pipeline already
uses — rather than an exact-match join that would silently return nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.behavior.surface_eval import bin_indices
from src.preprocessing.loader import to_wide
from src.preprocessing.resampler import ResampleConfig, resample_wide

Aggregation = Literal["mean", "median", "p95", "count"]

#: Default grid resolution for an axis when the caller doesn't specify one —
#: fine enough to show cell-to-cell structure, coarse enough that a single
#: session (a few thousand resampled rows) still populates most cells.
DEFAULT_BIN_COUNT = 12

_AGGREGATIONS: tuple[Aggregation, ...] = ("mean", "median", "p95", "count")


def _empty_result(
    *,
    value_channel: str,
    x_channel: str,
    y_channel: str | None,
    agg: Aggregation,
    reason: str,
) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "value_channel": value_channel,
        "x_channel": x_channel,
        "y_channel": y_channel,
        "agg": agg,
    }


def _quantile_edges(values: np.ndarray, n_bins: int) -> list[float]:
    """Quantile-spaced bin edges, deduplicated — evenly *populated* bins
    rather than evenly *spaced* ones, so an idle-heavy drive doesn't dump
    every low-rpm sample into one giant bin while the highway bins sit
    empty. Falls back to a small manufactured range when every value is
    identical (a degenerate but real case — e.g. a session that never left
    idle)."""
    quantiles = np.linspace(0.0, 1.0, max(2, n_bins + 1))
    edges = np.unique(np.quantile(values, quantiles))
    if edges.size < 2:
        center = float(edges[0])
        return [center - 0.5, center + 0.5]
    return [float(e) for e in edges]


def _aggregate(values: np.ndarray, agg: Aggregation) -> float:
    if agg == "count":
        return float(values.size)
    if agg == "mean":
        return float(np.mean(values))
    if agg == "median":
        return float(np.median(values))
    if agg == "p95":
        return float(np.quantile(values, 0.95))
    raise ValueError(f"unknown aggregation {agg!r}")  # pragma: no cover — Literal-guarded


def _cell(values: np.ndarray, agg: Aggregation) -> dict[str, float | int]:
    """One grid cell in the same ``{mean, std, count}`` shape
    ``GridCell``/``Heatmap.tsx`` already expect — ``mean`` carries whatever
    aggregation was requested (so a caller asking for ``agg="p95"`` still
    gets a heatmap-renderable cell), ``std``/``count`` are always the
    population values regardless of ``agg``, since "how much did we actually
    see here" is useful context no matter which aggregation was picked."""
    return {
        "mean": _aggregate(values, agg),
        "std": float(np.std(values, ddof=0)),
        "count": int(values.size),
    }


def rows_to_resampled_frame(
    rows: Sequence[Mapping[str, Any]],
    channels: Sequence[str],
    *,
    resample_hz: float,
) -> pd.DataFrame:
    """Pivot ``fetch_channel_rows``-shaped long rows onto a common time grid.

    Reuses :func:`src.preprocessing.loader.to_wide` (pivot) +
    :func:`src.preprocessing.resampler.resample_wide` (forward-fill onto a
    fixed-hz grid, per session) — the exact pattern
    :func:`src.features.feature_transform.featurize_wide_frame` builds
    training frames with — rather than a fresh alignment implementation.
    Returns an empty frame (with the requested columns) when ``rows`` is
    empty.
    """
    if not rows:
        return pd.DataFrame(columns=["session", "timestamp", *channels])
    frame = pd.DataFrame(rows).rename(columns={"channel_key": "source_channel"})
    wide = to_wide(frame, value="value_number")
    # Deliberately NOT the training recipe (``resample_config_for``): this is
    # an ad-hoc display grid at a caller-chosen rate for the binned-analysis
    # and session-timeseries tools, with no CanaryConfig in scope. It answers
    # "what did these channels read together", not "what did the model see",
    # so the ResampleConfig defaults for the gap rules are the intent here.
    # Allowlisted in tests/test_resample_config_construction.py.
    return resample_wide(wide, config=ResampleConfig(hz=resample_hz))


def compute_binned_analysis(
    rows: Sequence[Mapping[str, Any]],
    *,
    value_channel: str,
    x_channel: str,
    y_channel: str | None = None,
    x_bins: int = DEFAULT_BIN_COUNT,
    y_bins: int | None = None,
    agg: Aggregation = "mean",
    resample_hz: float = 1.0,
) -> dict[str, Any]:
    """Bin ``value_channel`` by ``x_channel`` (and ``y_channel`` if given)
    over the scoped ``rows`` (from ``TelemetryQuery.fetch_channel_rows``).

    ``available: False`` with a ``reason`` when there's no scoped data, or
    none of it carries all the requested channels together (rather than a
    stack trace or a silently-empty-looking grid). Otherwise: 1D result
    carries ``x_edges``/``cells`` (one per bin); 2D carries ``x_edges``/
    ``y_edges``/``rows``/``cols``/``cells`` (row-major, ``x`` outer —
    matching ``GridBinnedMeanFit``/``Heatmap.tsx``'s existing convention).
    """
    channels = [value_channel, x_channel, *([y_channel] if y_channel else [])]
    if not rows:
        return _empty_result(
            value_channel=value_channel,
            x_channel=x_channel,
            y_channel=y_channel,
            agg=agg,
            reason="no telemetry in scope",
        )
    resampled = rows_to_resampled_frame(rows, channels, resample_hz=resample_hz)
    missing = [c for c in channels if c not in resampled.columns]
    if missing:
        return _empty_result(
            value_channel=value_channel,
            x_channel=x_channel,
            y_channel=y_channel,
            agg=agg,
            reason=f"channel(s) not present in scope: {sorted(missing)}",
        )
    subset = resampled[channels].apply(pd.to_numeric, errors="coerce").dropna()
    if subset.empty:
        return _empty_result(
            value_channel=value_channel,
            x_channel=x_channel,
            y_channel=y_channel,
            agg=agg,
            reason="no rows with every requested channel present at once",
        )

    x = subset[x_channel].to_numpy(dtype=float)
    value = subset[value_channel].to_numpy(dtype=float)
    x_edges = np.asarray(_quantile_edges(x, x_bins), dtype=float)

    if y_channel is None:
        x_idx = bin_indices(x, x_edges)
        cells = []
        for i in range(len(x_edges) - 1):
            mask = x_idx == i
            cells.append(
                _cell(value[mask], agg) if mask.any() else {"mean": 0.0, "std": 0.0, "count": 0}
            )
        return {
            "available": True,
            "kind": "1d",
            "value_channel": value_channel,
            "x_channel": x_channel,
            "y_channel": None,
            "agg": agg,
            "sample_count": int(len(x)),
            "x_edges": [float(e) for e in x_edges],
            "cells": cells,
        }

    y = subset[y_channel].to_numpy(dtype=float)
    y_edges = np.asarray(_quantile_edges(y, y_bins or x_bins), dtype=float)
    x_idx = bin_indices(x, x_edges)
    y_idx = bin_indices(y, y_edges)
    n_x, n_y = len(x_edges) - 1, len(y_edges) - 1
    cells = []
    for i in range(n_x):
        for j in range(n_y):
            mask = (x_idx == i) & (y_idx == j)
            if mask.any():
                cells.append(_cell(value[mask], agg))
            else:
                cells.append({"mean": 0.0, "std": 0.0, "count": 0})
    return {
        "available": True,
        "kind": "2d",
        "value_channel": value_channel,
        "x_channel": x_channel,
        "y_channel": y_channel,
        "agg": agg,
        "sample_count": int(len(x)),
        "x_edges": [float(e) for e in x_edges],
        "y_edges": [float(e) for e in y_edges],
        "rows": n_x,
        "cols": n_y,
        "cells": cells,
    }


#: ``bin_indices`` is the one bin rule (:mod:`src.behavior.surface_eval`, R05.10),
#: re-exported for analysis callers.
__all__ = [
    "DEFAULT_BIN_COUNT",
    "Aggregation",
    "bin_indices",
    "compute_binned_analysis",
    "rows_to_resampled_frame",
]
