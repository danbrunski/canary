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

import logging
from collections.abc import Sequence

import numpy as np
import pandas as pd

from src.analysis.fit_provenance import fitted_now, training_data_hash
from src.behavior.behavior_models import (
    Axis1D,
    BehaviorModel1D,
    DeviationConfig,
    LookupTableFit,
    PolynomialFit,
    RobustBaselineFit,
)
from src.behavior.surface_eval import evaluate_1d
from src.signals.bins import bin_indices

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
    diagnostic_capabilities: Sequence[str] = (),
    diagnostic_limitations: Sequence[str] = (),
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
    deviation = _fill_relative_reference_std(name, deviation, y, predicted)

    domain = (float(x.min()), float(x.max())) if domain is None else domain
    fit = PolynomialFit(
        kind="poly",
        coefficients=[float(c) for c in coeffs],
        sample_count=int(len(x)),
        r_squared=r_squared,
        sessions=list(sessions),
        fitted_at=fitted_now() if fitted_at is None else fitted_at,
        training_data_hash=training_data_hash(x, y),
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
        diagnostic_capabilities=list(diagnostic_capabilities),
        diagnostic_limitations=list(diagnostic_limitations),
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
    diagnostic_capabilities: Sequence[str] = (),
    diagnostic_limitations: Sequence[str] = (),
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

    # bin assignment: the one bin rule, so x.max() lands in the last bin.
    bins = bin_indices(x, edges)
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
    deviation = _fill_relative_reference_std(name, deviation, y, predicted)

    domain = (float(x.min()), float(x.max())) if domain is None else domain
    fit = LookupTableFit(
        kind="lookup_table",
        x_knots=x_knots,
        y_knots=y_knots,
        sample_count=int(len(x)),
        r_squared=r_squared,
        sessions=list(sessions),
        fitted_at=fitted_now() if fitted_at is None else fitted_at,
        training_data_hash=training_data_hash(x, y),
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
        diagnostic_capabilities=list(diagnostic_capabilities),
        diagnostic_limitations=list(diagnostic_limitations),
    )


def fit_robust_baseline(
    frame: pd.DataFrame,
    *,
    y_column: str,
    name: str,
    output: str,
    output_unit: str = "unknown",
    sessions: Sequence[str] = (),
    operating_mode: str | None = None,
    deviation: DeviationConfig | None = None,
    version: int = 1,
    notes: str | None = None,
    fitted_at: str | None = None,
    diagnostic_capabilities: Sequence[str] = (),
    diagnostic_limitations: Sequence[str] = (),
    **_ignored: object,
) -> BehaviorModel1D:
    """Fit a median/MAD baseline; the output channel is a presence-only axis."""
    values = pd.to_numeric(frame[y_column], errors="coerce").dropna().to_numpy(dtype=float)
    if len(values) < 2:
        raise ValueError(f"{name}: need at least 2 clean rows, got {len(values)}")
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    sigma_floor = (
        deviation.sigma_floor
        if deviation is not None and deviation.sigma_floor is not None
        else 0.0
    )
    robust_sigma = max(1.4826 * mad, sigma_floor)
    if deviation is not None and deviation.kind == "sigma":
        deviation = deviation.model_copy(update={"reference_std": robust_sigma})
    fit = RobustBaselineFit(
        kind="robust_baseline",
        median=median,
        mad=mad,
        p01=float(np.quantile(values, 0.01)),
        p99=float(np.quantile(values, 0.99)),
        sample_count=len(values),
        r_squared=None,
        sessions=list(sessions),
        fitted_at=fitted_now() if fitted_at is None else fitted_at,
        training_data_hash=training_data_hash(np.arange(len(values), dtype=float), values),
    )
    return BehaviorModel1D(
        name=name,
        kind="surface_1d",
        output=output,
        output_unit=output_unit,
        axis=Axis1D(
            name="observation",
            channel=y_column,
            unit=output_unit,
            domain=(float(np.min(values)), float(np.max(values))),
        ),
        operating_mode=operating_mode,
        version=version,
        fit=fit,
        deviation=deviation,
        notes=notes,
        diagnostic_capabilities=list(diagnostic_capabilities),
        diagnostic_limitations=list(diagnostic_limitations),
    )


# ---------------------------------------------------------------------------
# Evaluators — the inverse of the fitters; the decoder layer (4.E.6) wraps
# these into the public expected_value / deviation API.
# ---------------------------------------------------------------------------


def _fill_relative_reference_std(
    name: str,
    deviation: DeviationConfig | None,
    actual: np.ndarray,
    predicted: np.ndarray,
) -> DeviationConfig | None:
    """Auto-fill ``reference_std`` for sigma bands from the fit residuals.

    1D fits carry one global residual band (unlike 2D grids' per-cell std),
    and recipes for outputs whose scaling isn't pinned yet (raw injector-PW
    counts) can't declare a meaningful ``reference_std`` upfront. Filling it
    from the residual std around the fitted curve makes ``sigma`` bands
    self-calibrating and mirrors what the 2D fitter does per cell. An
    explicit ``reference_std`` in the recipe always wins.
    """
    if deviation is None or deviation.kind != "sigma" or deviation.reference_std is not None:
        return deviation
    residual_std = float(np.std(actual - predicted))  # ddof=0, matches the 2D cells
    if deviation.sigma_floor is not None:
        residual_std = max(residual_std, deviation.sigma_floor)
    if residual_std <= 0:
        raise ValueError(
            f"{name}: relative deviation needs a positive residual std, but the fit "
            "residuals are exactly zero — declare reference_std (or sigma_floor) "
            "explicitly or use absolute bands"
        )
    return deviation.model_copy(update={"reference_std": residual_std})


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


#: :func:`evaluate_1d` lives in :mod:`src.behavior.surface_eval` (R05.10);
#: re-exported so analysis callers keep one import for a surface family.
__all__ = ["evaluate_1d", "fit_binned_lookup", "fit_polynomial", "fit_robust_baseline"]
