"""The one bin rule: which cell of a set of edges a value belongs to (R05.10, R24.6).

Cell ``i`` covers ``[edges[i], edges[i+1])`` and the last cell is closed,
``[edges[-2], edges[-1]]``, so the maximum of auto-computed edges lands in the
last cell rather than overflowing, and a value exactly on an interior edge
belongs to the cell that *starts* there.

* :func:`bin_indices` -- the serve-path form the behavior surfaces were fitted
  and are evaluated with: values below the first edge clip into cell 0 and
  values above the last into the last cell. Callers that must not bin an
  out-of-range value check the extent first, as
  :func:`src.behavior.surface_eval.containing_cell_indices` does. It does not
  look at NaN, which ``np.digitize`` sends to the last cell; mask it first.
* :func:`cell_codes` -- the grouping form for every page that groups samples
  into cells: ``-1`` for "no cell", and the caller names what happens to a
  value outside the edges (``"drop"`` or ``"clip"``). Non-finite values never
  get a cell.
* :func:`cell_names` -- the ``[lo, hi)`` / last ``[lo, hi]`` label of each cell.

Copies of this rule had grown apart: four in the surface code (R05.10), and
afterwards right-closed ``pd.cut`` band grouping in the delivery and WOT
digests, ``right=False`` cells in the workbench that dropped the top edge,
``include_lowest`` cells in the AFR error surface, and ``np.digitize`` /
``searchsorted`` loops in the dyno and fuel-comp fits that dropped it too. A
value exactly on an edge landed in different cells depending on which page
drew it. ``tests/test_bin_rule.py`` feeds every edge to each caller.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from numpy.typing import ArrayLike

#: :func:`cell_codes`' two answers for a value outside the edges.
OutOfRange = Literal["drop", "clip"]


def bin_indices(values: np.ndarray, edges: np.ndarray) -> np.ndarray:
    """Place each value into ``[0, len(edges) - 2]`` — closed on the right end."""
    bins = np.digitize(np.asarray(values, dtype=float), np.asarray(edges, dtype=float)) - 1
    clipped: np.ndarray = np.clip(bins, 0, len(edges) - 2)
    return clipped


def bin_index(value: float, edges: np.ndarray) -> int:
    """Scalar :func:`bin_indices`."""
    return int(bin_indices(np.asarray([value], dtype=float), edges)[0])


def cell_codes(values: ArrayLike, edges: ArrayLike, *, out_of_range: OutOfRange) -> np.ndarray:
    """Each value's cell under :func:`bin_indices`' rule, as ints; ``-1`` = no cell.

    ``out_of_range`` has no default, because both answers are in use:
    ``"drop"`` gives a value below the first edge or above the last no cell
    (the analysis pages' grouping); ``"clip"`` puts it in the first or last
    cell (:func:`bin_indices` itself). A non-finite value has no cell in
    either mode: NaN is no measurement, and ``bin_indices`` alone would clip
    it into the last cell.
    """
    if out_of_range not in ("drop", "clip"):
        raise ValueError(f"out_of_range must be 'drop' or 'clip', got {out_of_range!r}")
    edge_array = np.asarray(edges, dtype=float)
    numeric = np.asarray(values, dtype=float).reshape(-1)
    keep = np.isfinite(numeric)
    if out_of_range == "drop":
        keep &= (numeric >= edge_array[0]) & (numeric <= edge_array[-1])
    codes = np.full(len(numeric), -1, dtype=np.int64)
    if keep.any():
        codes[keep] = bin_indices(numeric[keep], edge_array)
    return codes


def cell_names(edges: ArrayLike) -> list[str]:
    """Labels for :func:`cell_codes`' cells: ``[lo, hi)``, the last ``[lo, hi]``."""
    edge_array = np.asarray(edges, dtype=float)
    last = len(edge_array) - 2
    return [
        str(pd.Interval(edge_array[i], edge_array[i + 1], closed="both" if i == last else "left"))
        for i in range(last + 1)
    ]


__all__ = ["OutOfRange", "bin_index", "bin_indices", "cell_codes", "cell_names"]
