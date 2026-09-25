"""Fit 2D expected-behavior surfaces from recorded data (Task 4.E.3).

Single fit family for v1: grid-binned mean per ``(x_bin × y_bin)`` cell with
each cell carrying ``mean``, ``std``, ``count``. Used for fuel / AFR / VE /
ignition surfaces — the diagnostic value comes from seeing each cell's
deviation separately, not from compressing the surface into a few
coefficients. Empty cells (operating point never visited during the fit)
get ``count=0`` so the evaluator can return ``None`` rather than fabricate
an expected value from sparse coverage.

R² isn't really meaningful for a grid-binned-mean fit — the "model" is just
the empirical per-cell mean — so we leave ``fit.r_squared = None``. The
right quality signal at evaluation time is the per-cell ``count`` and ``std``,
both of which we persist.

Pure numpy/pandas; row-major flatten matches the schema (axis A is the outer
index).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np
import pandas as pd

from src.analysis.fit_provenance import fitted_now, training_data_hash
from src.decode.behavior_models import (
    Axis2D,
    BehaviorModel2D,
    DeviationConfig,
    GridBinnedMeanFit,
    GridCell,
)
from src.decode.surface_eval import (
    bin_indices,
    containing_cell_indices,
    evaluate_2d,
    trusted_cell_mask,
)
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

log = logging.getLogger(__name__)


def fit_grid_binned_mean(
    frame: pd.DataFrame,
    *,
    x_column: str,
    y_column: str,
    output_column: str,
    name: str,
    output: str,
    x_edges: Sequence[float],
    y_edges: Sequence[float],
    x_axis_name: str = "x",
    y_axis_name: str = "y",
    x_axis_unit: str = "unknown",
    y_axis_unit: str = "unknown",
    output_unit: str = "unknown",
    sessions: Sequence[str] = (),
    operating_mode: str | None = None,
    deviation: DeviationConfig | None = None,
    version: int = 1,
    notes: str | None = None,
    fitted_at: str | None = None,
    min_cell_count: int = 1,
    min_cell_sessions: int = 1,
    min_cell_dwell_seconds: float = 0.0,
    grid_version: int = 1,
    diagnostic_capabilities: Sequence[str] = (),
    diagnostic_limitations: Sequence[str] = (),
) -> BehaviorModel2D:
    """Bin ``(x, y)`` into the configured grid and compute per-cell stats.

    Rows where any of ``x_column`` / ``y_column`` / ``output_column`` is NaN
    are dropped. Rows with ``x`` or ``y`` outside ``[edges[0], edges[-1]]`` are
    also dropped (they would land in a "before first / after last" bin we
    don't model). Each cell stores mean / std (ddof=0, the population std —
    matches what ``ZScoreScaler`` uses) / count. ``min_cell_count`` is
    persisted on the model: cells below it still carry their stats in the
    YAML (useful for inspection) but evaluate as off-map.
    """
    x_edges_arr = np.asarray(x_edges, dtype=float)
    y_edges_arr = np.asarray(y_edges, dtype=float)
    _validate_edges(x_edges_arr, "x_edges")
    _validate_edges(y_edges_arr, "y_edges")

    required = [x_column, y_column, output_column]
    clean = frame.loc[
        :, [*required, *[c for c in (SESSION_COLUMN, TIMESTAMP_COLUMN) if c in frame]]
    ].copy()
    for column in required:
        clean[column] = pd.to_numeric(clean[column], errors="coerce")
    clean = clean.dropna(subset=required)
    x = clean[x_column].to_numpy(dtype=float)
    y = clean[y_column].to_numpy(dtype=float)
    z = clean[output_column].to_numpy(dtype=float)

    # in-range mask: closed-right on the final bin so the top edge isn't lost
    in_range = (
        (x >= x_edges_arr[0])
        & (x <= x_edges_arr[-1])
        & (y >= y_edges_arr[0])
        & (y <= y_edges_arr[-1])
    )
    x, y, z = x[in_range], y[in_range], z[in_range]
    clean = clean.loc[in_range].reset_index(drop=True)
    if len(x) == 0:
        log.warning(
            "no in-range samples for 2D fit",
            extra={"surface_name": name, "x_column": x_column, "y_column": y_column},
        )

    x_bins = bin_indices(x, x_edges_arr)
    y_bins = bin_indices(y, y_edges_arr)

    n_x = len(x_edges_arr) - 1
    n_y = len(y_edges_arr) - 1
    cells: list[GridCell] = []
    for i in range(n_x):
        for j in range(n_y):
            mask = (x_bins == i) & (y_bins == j)
            if not mask.any():
                cells.append(GridCell(mean=0.0, std=0.0, count=0))
                continue
            z_cell = z[mask]
            evidence = clean.loc[mask]
            session_count = (
                int(evidence[SESSION_COLUMN].astype(str).nunique())
                if SESSION_COLUMN in evidence
                else 1
            )
            dwell_seconds = _effective_dwell_seconds(evidence)
            cells.append(
                GridCell(
                    mean=float(z_cell.mean()),
                    std=float(z_cell.std(ddof=0)),
                    count=int(z_cell.size),
                    session_count=session_count,
                    dwell_seconds=dwell_seconds,
                )
            )

    fit = GridBinnedMeanFit(
        kind="grid_binned_mean",
        cells=cells,
        sample_count=int(len(x)),
        r_squared=None,  # not meaningful for empirical per-cell means
        sessions=list(sessions),
        fitted_at=fitted_now() if fitted_at is None else fitted_at,
        training_data_hash=training_data_hash(x, y, z),
    )
    return BehaviorModel2D(
        name=name,
        kind="surface_2d",
        output=output,
        output_unit=output_unit,
        axes=(
            Axis2D(
                name=x_axis_name,
                channel=x_column,
                unit=x_axis_unit,
                bin_edges=[float(e) for e in x_edges_arr],
            ),
            Axis2D(
                name=y_axis_name,
                channel=y_column,
                unit=y_axis_unit,
                bin_edges=[float(e) for e in y_edges_arr],
            ),
        ),
        operating_mode=operating_mode,
        version=version,
        fit=fit,
        deviation=deviation,
        notes=notes,
        min_cell_count=min_cell_count,
        min_cell_sessions=min_cell_sessions,
        min_cell_dwell_seconds=min_cell_dwell_seconds,
        grid_version=grid_version,
        diagnostic_capabilities=list(diagnostic_capabilities),
        diagnostic_limitations=list(diagnostic_limitations),
    )


def _effective_dwell_seconds(frame: pd.DataFrame) -> float:
    if TIMESTAMP_COLUMN not in frame.columns:
        return float(len(frame))
    timestamps = pd.to_datetime(frame[TIMESTAMP_COLUMN], utc=True, errors="coerce")
    if SESSION_COLUMN not in frame.columns:
        groups = [("", timestamps)]
    else:
        groups = [
            (str(session), timestamps.loc[index])
            for session, index in frame.groupby(SESSION_COLUMN, sort=False).groups.items()
        ]
    dwell = 0.0
    for _session, values in groups:
        ordered = values.dropna().sort_values()
        if len(ordered) < 2:
            continue
        deltas = ordered.diff().dt.total_seconds().dropna().clip(lower=0.0, upper=5.0)
        dwell += float(deltas.sum())
    return dwell


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _validate_edges(edges: np.ndarray, label: str) -> None:
    if edges.size < 2:
        raise ValueError(f"{label}: need at least 2 edges")
    if not np.all(np.diff(edges) > 0):
        raise ValueError(f"{label}: must be strictly increasing")


def _clean_xyz(
    frame: pd.DataFrame, x_column: str, y_column: str, output_column: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    for col in (x_column, y_column, output_column):
        if col not in frame.columns:
            raise KeyError(f"column {col!r} not in frame; available={list(frame.columns)[:8]}…")
    x = pd.to_numeric(frame[x_column], errors="coerce")
    y = pd.to_numeric(frame[y_column], errors="coerce")
    z = pd.to_numeric(frame[output_column], errors="coerce")
    mask = x.notna() & y.notna() & z.notna()
    return (
        x[mask].to_numpy(dtype=float),
        y[mask].to_numpy(dtype=float),
        z[mask].to_numpy(dtype=float),
    )


#: The serve-path evaluators live in :mod:`src.decode.surface_eval` (R05.10);
#: re-exported so analysis callers keep one import for a surface family.
__all__ = [
    "containing_cell_indices",
    "evaluate_2d",
    "fit_grid_binned_mean",
    "trusted_cell_mask",
]
