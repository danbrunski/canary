"""Serve-path evaluation of behavior surfaces, and the one bin rule (R05.10).

The evaluators — :func:`evaluate_1d`, :func:`evaluate_2d` and the envelope
helpers :func:`trusted_cell_mask` / :func:`containing_cell_indices` — are what
the live decoder, the daemon and replay call on every tick. They lived in the
analysis *fitting* modules (``surface_1d`` / ``surface_2d``), which made
``decode.behavior_decoder`` import ``analysis``; they belong beside the model
schema they read. The fitters stay in analysis and re-export these names.

The one bin rule, :func:`~src.signals.bins.bin_indices`, lives in
:mod:`src.signals.bins` (layer 0, R24.6) so the signal and analysis layers
group samples with the same cells the surfaces serve. Bin ``i`` covers
``[edges[i], edges[i+1])`` and the last bin is closed; values outside the
edges clip, so :func:`containing_cell_indices` checks the extent first.
"""

from __future__ import annotations

import numpy as np

from src.behavior.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    GridCell,
    PolynomialFit,
    RobustBaselineFit,
)
from src.signals.bins import bin_index, bin_indices


def evaluate_1d(model: BehaviorModel1D, x: float) -> float | None:
    """Evaluate a 1D model at ``x``. Returns ``None`` outside the fit domain.

    The domain check is *strict* on purpose — extrapolating a polynomial
    out of its fit range is one of the easiest ways to create plausible-
    looking nonsense (especially for warmup curves that look monotonic
    until you fall off the fitted region and the cubic term explodes).
    """
    fit = model.fit
    if isinstance(fit, RobustBaselineFit):
        return fit.median
    lo, hi = model.axis.domain
    if not lo <= x <= hi:
        return None
    if isinstance(fit, PolynomialFit):
        return float(np.polynomial.polynomial.polyval(x, fit.coefficients))
    # LookupTableFit
    return float(np.interp(x, fit.x_knots, fit.y_knots))


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
    i = bin_index(x, x_edges)
    j = bin_index(y, y_edges)
    min_count = max(1, model.min_cell_count)

    interpolated = _bilinear_cell(model, x, y, x_edges, y_edges, i, j, min_count)
    if interpolated is not None:
        return interpolated

    cell = model.fit.cells[i * y_axis.bin_count + j]
    if not cell_eligible(model, cell):
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
    if any(not cell_eligible(model, c) for c in (c00, c10, c01, c11)):
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


def cell_eligible(model: BehaviorModel2D, cell: GridCell) -> bool:
    return (
        cell.count >= model.min_cell_count
        and cell.session_count >= model.min_cell_sessions
        and cell.dwell_seconds >= model.min_cell_dwell_seconds
    )


def trusted_cell_mask(model: BehaviorModel2D) -> np.ndarray:
    """Row-major mask of cells the evaluator will serve (count, sessions, dwell)."""
    return np.asarray([cell_eligible(model, cell) for cell in model.fit.cells], dtype=bool)


def containing_cell_indices(model: BehaviorModel2D, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Flat row-major index of the cell containing each ``(x, y)``; ``-1`` off-grid.

    Uses the same bin rule as :func:`evaluate_2d` (closed on the right end),
    so envelope-coverage accounting (B02.4) counts the cells serving uses.
    """
    x_axis, y_axis = model.axes
    x_edges = np.asarray(x_axis.bin_edges, dtype=float)
    y_edges = np.asarray(y_axis.bin_edges, dtype=float)
    xv = np.asarray(x, dtype=float)
    yv = np.asarray(y, dtype=float)
    inside = (
        np.isfinite(xv)
        & np.isfinite(yv)
        & (xv >= x_edges[0])
        & (xv <= x_edges[-1])
        & (yv >= y_edges[0])
        & (yv <= y_edges[-1])
    )
    out = np.full(len(xv), -1, dtype=int)
    if inside.any():
        i = bin_indices(xv[inside], x_edges)
        j = bin_indices(yv[inside], y_edges)
        out[inside] = i * y_axis.bin_count + j
    return out


__all__ = [
    "cell_eligible",
    "containing_cell_indices",
    "evaluate_1d",
    "evaluate_2d",
    "trusted_cell_mask",
]
