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

import hashlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from src.decode.behavior_models import (
    Axis2D,
    BehaviorModel2D,
    DeviationConfig,
    GridBinnedMeanFit,
    GridCell,
)

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
) -> BehaviorModel2D:
    """Bin ``(x, y)`` into the configured grid and compute per-cell stats.

    Rows where any of ``x_column`` / ``y_column`` / ``output_column`` is NaN
    are dropped. Rows with ``x`` or ``y`` outside ``[edges[0], edges[-1]]`` are
    also dropped (they would land in a "before first / after last" bin we
    don't model). Each cell stores mean / std (ddof=0, the population std —
    matches what ``ZScoreScaler`` uses) / count.
    """
    x_edges_arr = np.asarray(x_edges, dtype=float)
    y_edges_arr = np.asarray(y_edges, dtype=float)
    _validate_edges(x_edges_arr, "x_edges")
    _validate_edges(y_edges_arr, "y_edges")

    x, y, z = _clean_xyz(frame, x_column, y_column, output_column)

    # in-range mask: closed-right on the final bin so the top edge isn't lost
    in_range = (
        (x >= x_edges_arr[0])
        & (x <= x_edges_arr[-1])
        & (y >= y_edges_arr[0])
        & (y <= y_edges_arr[-1])
    )
    x, y, z = x[in_range], y[in_range], z[in_range]
    if len(x) == 0:
        log.warning(
            "no in-range samples for 2D fit",
            extra={"name": name, "x_column": x_column, "y_column": y_column},
        )

    x_bins = _digitize_into_bins(x, x_edges_arr)
    y_bins = _digitize_into_bins(y, y_edges_arr)

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
            cells.append(
                GridCell(
                    mean=float(z_cell.mean()),
                    std=float(z_cell.std(ddof=0)),
                    count=int(z_cell.size),
                )
            )

    fit = GridBinnedMeanFit(
        kind="grid_binned_mean",
        cells=cells,
        sample_count=int(len(x)),
        r_squared=None,  # not meaningful for empirical per-cell means
        sessions=list(sessions),
        fitted_at=_now_iso() if fitted_at is None else fitted_at,
        training_data_hash=_hash_xyz(x, y, z),
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
    )


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


def evaluate_2d(model: BehaviorModel2D, x: float, y: float) -> GridCell | None:
    """Look up the cell ``(x, y)`` falls into. Returns ``None`` when:

    * ``(x, y)`` is outside the grid extent, OR
    * the cell has ``count == 0`` (operating point never observed during fit)

    Returning the whole ``GridCell`` (not just ``mean``) lets the decoder
    layer compute relative deviations using the per-cell ``std``, which is
    why we persist it.
    """
    x_axis, y_axis = model.axes
    if not x_axis.bin_edges[0] <= x <= x_axis.bin_edges[-1]:
        return None
    if not y_axis.bin_edges[0] <= y <= y_axis.bin_edges[-1]:
        return None
    i = _bin_index(x, np.asarray(x_axis.bin_edges, dtype=float))
    j = _bin_index(y, np.asarray(y_axis.bin_edges, dtype=float))
    cell = model.fit.cells[i * y_axis.bin_count + j]
    if cell.count == 0:
        return None
    return cell


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _digitize_into_bins(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Place each value into ``[0, n_bins-1]`` — closed on the right end."""
    bins = np.digitize(values, edges) - 1
    clipped: np.ndarray = np.clip(bins, 0, len(edges) - 2)
    return clipped


def _bin_index(value: float, edges: np.ndarray) -> int:
    return int(np.clip(np.digitize([value], edges)[0] - 1, 0, len(edges) - 2))


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


def _hash_xyz(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> str:
    h = hashlib.sha256()
    h.update(x.tobytes())
    h.update(y.tobytes())
    h.update(z.tobytes())
    return f"sha256:{h.hexdigest()[:16]}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
