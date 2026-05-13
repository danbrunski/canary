"""Fit 1D expected-behavior surfaces from recorded data (Task 4.E.2).

Two fit families, both producing a ``BehaviorModel1D`` ready for serialization
via ``dump_behavior_models``:

* ``fit_polynomial`` — closed-form polynomial of configurable degree. Used
  for the coolant-warmup curve, idle-stability vs. coolant-temp curve, and
  any other monotonic warmup-style relationship. Stored as
  ``PolynomialFit`` (coefficients lowest-order first, numpy convention).
* ``fit_binned_lookup`` — a smoothed-lookup-table alternative for fits a
  polynomial can't represent well (sigmoid-ish, non-monotonic, has a knee).
  Bins x into ``n_knots`` equal-population quantile bins, takes the mean
  y per bin, and stores the bin-center → mean-y pairs as a
  ``LookupTableFit``. Equivalent in spirit to a piecewise-linear LOWESS
  without pulling scipy in.

Both compute R² against the input data and persist it on the fit, so the
runtime / refit CLI can refuse to publish a surface whose fit quality
collapsed.

Pure numpy/pandas — no scipy, no sklearn, so this layer doesn't lock the
project into an extra dependency.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from src.decode.behavior_models import (
    Axis1D,
    BehaviorModel1D,
    DeviationConfig,
    LookupTableFit,
    PolynomialFit,
)

log = logging.getLogger(__name__)


def fit_polynomial(
    frame: pd.DataFrame,
    *,
    x_column: str,
    y_column: str,
    name: str,
    output: str,
    output_unit: str = "unknown",
    axis_name: str = "x",
    axis_unit: str = "unknown",
    degree: int = 3,
    domain: tuple[float, float] | None = None,
    sessions: Sequence[str] = (),
    operating_mode: str | None = None,
    deviation: DeviationConfig | None = None,
    version: int = 1,
    notes: str | None = None,
    fitted_at: str | None = None,
) -> BehaviorModel1D:
    """Fit ``y_column = poly(x_column, degree)`` and wrap as a ``BehaviorModel1D``.

    Rows where either column is NaN are dropped before fitting (so a missing
    OBD reading doesn't poison the fit). Domain defaults to the observed
    ``[x.min(), x.max()]``. Raises ``ValueError`` if fewer than
    ``degree + 2`` clean rows remain (a degree-N poly through N+1 points is
    interpolation, not regression, and R² becomes meaningless).
    """
    x, y = _clean_xy(frame, x_column, y_column)
    if len(x) < degree + 2:
        raise ValueError(
            f"{name}: need ≥ {degree + 2} clean rows to fit a degree-{degree} polynomial, "
            f"got {len(x)} (x={x_column}, y={y_column})"
        )

    # numpy.polynomial.polynomial.polyfit returns coefficients lowest-order
    # first, matching our schema. (`np.polyfit` is the OLDER API and returns
    # highest-order first — easy way to introduce an off-by-one if confused.)
    coeffs = np.polynomial.polynomial.polyfit(x, y, deg=degree)
    predicted = np.polynomial.polynomial.polyval(x, coeffs)
    r_squared = _coefficient_of_determination(y, predicted)

    domain = (float(x.min()), float(x.max())) if domain is None else domain
    fit = PolynomialFit(
        kind="poly",
        coefficients=[float(c) for c in coeffs],
        sample_count=int(len(x)),
        r_squared=r_squared,
        sessions=list(sessions),
        fitted_at=_now_iso() if fitted_at is None else fitted_at,
        training_data_hash=_hash_xy(x, y),
    )
    return BehaviorModel1D(
        name=name,
        kind="surface_1d",
        output=output,
        output_unit=output_unit,
        axis=Axis1D(
            name=axis_name,
            channel=x_column,
            unit=axis_unit,
            domain=domain,
        ),
        operating_mode=operating_mode,
        version=version,
        fit=fit,
        deviation=deviation,
        notes=notes,
    )


def fit_binned_lookup(
    frame: pd.DataFrame,
    *,
    x_column: str,
    y_column: str,
    name: str,
    output: str,
    output_unit: str = "unknown",
    axis_name: str = "x",
    axis_unit: str = "unknown",
    n_knots: int = 10,
    domain: tuple[float, float] | None = None,
    sessions: Sequence[str] = (),
    operating_mode: str | None = None,
    deviation: DeviationConfig | None = None,
    version: int = 1,
    notes: str | None = None,
    fitted_at: str | None = None,
) -> BehaviorModel1D:
    """Bin x into ``n_knots`` quantile bins, take mean y per bin → lookup table.

    Equivalent to a piecewise-linear LOWESS without scipy: the curve is
    defined by ``(bin_center, mean_y_in_bin)`` pairs the runtime linearly
    interpolates between. Bins are equal-population (quantile-based), not
    equal-width, so a tail-heavy x distribution doesn't get one fat bin and
    a bunch of empty ones.

    R² is computed against the same piecewise-linear curve that ``apply``
    will use at runtime, so the persisted goodness-of-fit matches what
    consumers actually see.

    Empty bins (quantile bins collapsing because of ties — e.g. RPM stuck at
    750 for half the session) are dropped; we still need ``≥ 2`` populated
    bins to define a curve.
    """
    x, y = _clean_xy(frame, x_column, y_column)
    if len(x) < n_knots * 2:
        raise ValueError(
            f"{name}: need ≥ {n_knots * 2} clean rows to fit a {n_knots}-knot lookup, "
            f"got {len(x)} (x={x_column}, y={y_column})"
        )

    # quantile-based bin edges; duplicate-handling drops ties so np.digitize
    # doesn't put everything in one bin
    quantiles = np.linspace(0.0, 1.0, n_knots + 1)
    edges = np.unique(np.quantile(x, quantiles))
    if len(edges) < 3:
        raise ValueError(
            f"{name}: x={x_column} has too few distinct values for a {n_knots}-knot fit"
        )

    # bin assignment; subtract 1 so bins are 0..len(edges)-2, clip the right
    # endpoint so x.max() lands in the last bin instead of overflowing.
    bins = np.clip(np.digitize(x, edges) - 1, 0, len(edges) - 2)
    x_knots: list[float] = []
    y_knots: list[float] = []
    for b in range(len(edges) - 1):
        mask = bins == b
        if not mask.any():
            continue
        x_knots.append(float(x[mask].mean()))
        y_knots.append(float(y[mask].mean()))
    if len(x_knots) < 2:
        raise ValueError(f"{name}: fewer than 2 populated bins after collapse — bad x distribution")

    predicted = np.interp(x, x_knots, y_knots)
    r_squared = _coefficient_of_determination(y, predicted)

    domain = (float(x.min()), float(x.max())) if domain is None else domain
    fit = LookupTableFit(
        kind="lookup_table",
        x_knots=x_knots,
        y_knots=y_knots,
        sample_count=int(len(x)),
        r_squared=r_squared,
        sessions=list(sessions),
        fitted_at=_now_iso() if fitted_at is None else fitted_at,
        training_data_hash=_hash_xy(x, y),
    )
    return BehaviorModel1D(
        name=name,
        kind="surface_1d",
        output=output,
        output_unit=output_unit,
        axis=Axis1D(name=axis_name, channel=x_column, unit=axis_unit, domain=domain),
        operating_mode=operating_mode,
        version=version,
        fit=fit,
        deviation=deviation,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Evaluators — the inverse of the fitters; the decoder layer (4.E.6) wraps
# these into the public expected_value / deviation API.
# ---------------------------------------------------------------------------


def evaluate_1d(model: BehaviorModel1D, x: float) -> float | None:
    """Evaluate a 1D model at ``x``. Returns ``None`` outside the fit domain.

    The domain check is *strict* on purpose — extrapolating a polynomial
    out of its fit range is one of the easiest ways to create plausible-
    looking nonsense (especially for warmup curves that look monotonic
    until you fall off the fitted region and the cubic term explodes).
    """
    lo, hi = model.axis.domain
    if not lo <= x <= hi:
        return None
    fit = model.fit
    if isinstance(fit, PolynomialFit):
        return float(np.polynomial.polynomial.polyval(x, fit.coefficients))
    # LookupTableFit
    return float(np.interp(x, fit.x_knots, fit.y_knots))


def _clean_xy(frame: pd.DataFrame, x_column: str, y_column: str) -> tuple[np.ndarray, np.ndarray]:
    """Pull (x, y) as float arrays, dropping any row where either is NaN."""
    for col in (x_column, y_column):
        if col not in frame.columns:
            raise KeyError(f"column {col!r} not in frame; available={list(frame.columns)[:8]}…")
    x_series = pd.to_numeric(frame[x_column], errors="coerce")
    y_series = pd.to_numeric(frame[y_column], errors="coerce")
    mask = x_series.notna() & y_series.notna()
    return (
        x_series[mask].to_numpy(dtype=float),
        y_series[mask].to_numpy(dtype=float),
    )


def _coefficient_of_determination(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Standard R² = 1 − SS_res / SS_tot. Returns 0.0 when y is constant.

    R² being undefined for a constant target is a quirk — a perfect fit on
    a flat line is "0 / 0". 0.0 is the conservative answer (drift detection
    won't trigger on it anyway since the variance is nil).
    """
    ss_tot = float(((actual - actual.mean()) ** 2).sum())
    if ss_tot == 0:
        return 0.0
    ss_res = float(((actual - predicted) ** 2).sum())
    return 1.0 - ss_res / ss_tot


def _hash_xy(x: np.ndarray, y: np.ndarray) -> str:
    """Stable hash of the training rows so refits-with-same-data have the same hash."""
    h = hashlib.sha256()
    h.update(x.tobytes())
    h.update(y.tobytes())
    return f"sha256:{h.hexdigest()[:16]}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
