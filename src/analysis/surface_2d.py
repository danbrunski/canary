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
# Evaluator
# ---------------------------------------------------------------------------


def evaluate_2d(model: BehaviorModel2D, x: float, y: float) -> GridCell | None:
    """Evaluate the surface at ``(x, y)`` — bilinear-interpolated (4.4) over
    the up-to-4 populated cells whose *centers* bracket the point, falling
    back to the containing cell alone when a full quad isn't available
    (grid edge, or a neighbor cell that's off-map/sparse). Returns ``None``
    when:

    * ``(x, y)`` is outside the grid extent, OR
    * neither interpolation nor the containing cell has a trustworthy
      expectation (``count < model.min_cell_count``, i.e. the operating
      point was never observed during fit, or too rarely to trust).

    Interpolating kills severity flips from operating-point drift across a
    bin edge — nearest-cell-only serving made two readings a fraction of a
    unit apart (one on either side of an edge) grade against two
    independently-fit cell means. Returning a whole (possibly blended)
    ``GridCell`` — not just ``mean`` — lets the decoder layer compute
    relative deviations off ``std``, which is why we persist it.
    """
    x_axis, y_axis = model.axes
    if not x_axis.bin_edges[0] <= x <= x_axis.bin_edges[-1]:
        return None
    if not y_axis.bin_edges[0] <= y <= y_axis.bin_edges[-1]:
        return None
    x_edges = np.asarray(x_axis.bin_edges, dtype=float)
    y_edges = np.asarray(y_axis.bin_edges, dtype=float)
    i = _bin_index(x, x_edges)
    j = _bin_index(y, y_edges)
    min_count = max(1, model.min_cell_count)

    interpolated = _bilinear_cell(model, x, y, x_edges, y_edges, i, j, min_count)
    if interpolated is not None:
        return interpolated

    cell = model.fit.cells[i * y_axis.bin_count + j]
    if not _cell_eligible(model, cell):
        return None
    return cell


def _bilinear_cell(
    model: BehaviorModel2D,
    x: float,
    y: float,
    x_edges: np.ndarray,
    y_edges: np.ndarray,
    i: int,
    j: int,
    min_count: int,
) -> GridCell | None:
    """Blend the 2×2 populated-cell quad around ``(x, y)`` by bin-center
    distance, or ``None`` if no full populated quad exists (grid too
    narrow near this edge, or any corner is off-map/sparse) — the caller
    falls back to the single containing cell in that case."""
    n_x, n_y = model.axes[0].bin_count, model.axes[1].bin_count
    x_centers = (x_edges[:-1] + x_edges[1:]) / 2.0
    y_centers = (y_edges[:-1] + y_edges[1:]) / 2.0

    i0 = i if x >= x_centers[i] else i - 1
    j0 = j if y >= y_centers[j] else j - 1
    if i0 < 0 or i0 + 1 >= n_x or j0 < 0 or j0 + 1 >= n_y:
        return None  # no full quad this close to the grid edge

    cells = model.fit.cells

    def cell_at(ii: int, jj: int) -> GridCell:
        return cells[ii * n_y + jj]

    c00, c10 = cell_at(i0, j0), cell_at(i0 + 1, j0)
    c01, c11 = cell_at(i0, j0 + 1), cell_at(i0 + 1, j0 + 1)
    if any(not _cell_eligible(model, c) for c in (c00, c10, c01, c11)):
        return None

    wx = min(max((x - x_centers[i0]) / (x_centers[i0 + 1] - x_centers[i0]), 0.0), 1.0)
    wy = min(max((y - y_centers[j0]) / (y_centers[j0 + 1] - y_centers[j0]), 0.0), 1.0)

    def blend(a00: float, a10: float, a01: float, a11: float) -> float:
        return float(
            a00 * (1 - wx) * (1 - wy) + a10 * wx * (1 - wy) + a01 * (1 - wx) * wy + a11 * wx * wy
        )

    return GridCell(
        mean=blend(c00.mean, c10.mean, c01.mean, c11.mean),
        # Variances combine linearly under a weighted mixture; interpolating
        # standard deviations directly systematically understates uncertainty.
        std=float(
            np.sqrt(
                max(
                    0.0,
                    blend(c00.std**2, c10.std**2, c01.std**2, c11.std**2),
                )
            )
        ),
        count=min(c00.count, c10.count, c01.count, c11.count),
    )


def _cell_eligible(model: BehaviorModel2D, cell: GridCell) -> bool:
    return (
        cell.count >= model.min_cell_count
        and cell.session_count >= model.min_cell_sessions
        and cell.dwell_seconds >= model.min_cell_dwell_seconds
    )


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
