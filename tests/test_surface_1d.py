"""Tests for the 1D surface inferer (Task 4.E.2)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.surface_1d import (
    _coefficient_of_determination,
    evaluate_1d,
    fit_binned_lookup,
    fit_polynomial,
    fit_robust_baseline,
)
from src.behavior.behavior_models import LookupTableFit, PolynomialFit, RobustBaselineFit


def _warmup_frame(noise: float = 0.5, n: int = 200, seed: int = 0) -> pd.DataFrame:
    """Synthetic coolant warmup: y(t) = 25 + 60 * (1 - exp(-t/120))."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 600, n)
    y = 25 + 60 * (1 - np.exp(-t / 120)) + rng.normal(0, noise, n)
    return pd.DataFrame({"t": t, "coolant": y})


def test_robust_baseline_ignores_outlier_and_has_no_r_squared_claim() -> None:
    frame = pd.DataFrame({"balance": [0.0] * 99 + [100.0]})
    model = fit_robust_baseline(
        frame,
        y_column="balance",
        name="balance",
        output="balance",
    )
    assert isinstance(model.fit, RobustBaselineFit)
    assert model.fit.median == 0.0
    assert model.fit.r_squared is None
    assert evaluate_1d(model, 1000.0) == 0.0


# ---------------------------------------------------------------------------
# fit_polynomial
# ---------------------------------------------------------------------------


def test_fit_polynomial_recovers_known_coefficients() -> None:
    # y = 1 + 2x + 0.5x^2 exactly
    x = np.linspace(-5, 5, 50)
    y = 1 + 2 * x + 0.5 * x**2
    frame = pd.DataFrame({"x": x, "y": y})

    model = fit_polynomial(frame, x_column="x", y_column="y", name="test", output="y", degree=2)
    assert isinstance(model.fit, PolynomialFit)
    coeffs = model.fit.coefficients
    # numpy poly1d ordering: c0 + c1*x + c2*x^2  →  coeffs ≈ [1, 2, 0.5]
    assert coeffs == pytest.approx([1.0, 2.0, 0.5], abs=1e-9)
    assert model.fit.r_squared is not None
    assert model.fit.r_squared > 0.9999
    assert model.axis.domain == (-5.0, 5.0)


def test_fit_polynomial_warmup_curve_is_decent_fit() -> None:
    frame = _warmup_frame(noise=0.5)
    model = fit_polynomial(
        frame,
        x_column="t",
        y_column="coolant",
        name="coolant_warmup",
        output="ECM_COOLANT_TEMP",
        degree=3,
    )
    # cubic against an exponential ought to clear 0.95 R² over a single window
    assert model.fit.r_squared is not None
    assert model.fit.r_squared > 0.95
    # evaluator should return roughly the underlying curve at a midpoint
    mid = evaluate_1d(model, 200.0)
    assert mid is not None
    truth = 25 + 60 * (1 - np.exp(-200 / 120))
    assert abs(mid - truth) < 3.0  # noise + cubic-fit-of-exp slack


def test_fit_polynomial_rejects_too_few_rows() -> None:
    frame = pd.DataFrame({"x": [1.0, 2.0, 3.0], "y": [1.0, 2.0, 3.0]})
    with pytest.raises(ValueError, match="need ≥ 5 clean rows"):
        fit_polynomial(frame, x_column="x", y_column="y", name="t", output="y", degree=3)


def test_fit_polynomial_skips_nan_rows() -> None:
    frame = pd.DataFrame(
        {
            "x": [0.0, 1.0, 2.0, 3.0, np.nan, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            "y": [0.0, 1.0, 2.0, np.nan, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
        }
    )
    model = fit_polynomial(frame, x_column="x", y_column="y", name="t", output="y", degree=1)
    # 2 rows had NaN, so 9 clean rows
    assert model.fit.sample_count == 9
    assert isinstance(model.fit, PolynomialFit)
    assert model.fit.coefficients == pytest.approx([0.0, 1.0], abs=1e-9)


def test_fit_polynomial_evaluates_to_none_outside_domain() -> None:
    frame = _warmup_frame()
    model = fit_polynomial(
        frame,
        x_column="t",
        y_column="coolant",
        name="t",
        output="y",
        degree=3,
        domain=(0.0, 600.0),
    )
    assert evaluate_1d(model, -1.0) is None
    assert evaluate_1d(model, 600.1) is None
    assert evaluate_1d(model, 0.0) is not None
    assert evaluate_1d(model, 600.0) is not None


# ---------------------------------------------------------------------------
# fit_binned_lookup
# ---------------------------------------------------------------------------


def test_fit_binned_lookup_recovers_curve() -> None:
    frame = _warmup_frame(noise=0.5)
    model = fit_binned_lookup(
        frame,
        x_column="t",
        y_column="coolant",
        name="coolant_warmup",
        output="ECM_COOLANT_TEMP",
        n_knots=10,
    )
    assert isinstance(model.fit, LookupTableFit)
    assert len(model.fit.x_knots) >= 2
    assert model.fit.r_squared is not None
    assert model.fit.r_squared > 0.9
    # evaluator should be in the right ballpark
    mid = evaluate_1d(model, 300.0)
    assert mid is not None
    truth = 25 + 60 * (1 - np.exp(-300 / 120))
    assert abs(mid - truth) < 5.0


def test_fit_binned_lookup_x_knots_strictly_increasing() -> None:
    frame = _warmup_frame(n=200)
    model = fit_binned_lookup(
        frame, x_column="t", y_column="coolant", name="t", output="y", n_knots=8
    )
    assert isinstance(model.fit, LookupTableFit)
    knots = model.fit.x_knots
    assert knots == sorted(set(knots))


def test_fit_binned_lookup_handles_tied_x_distribution() -> None:
    # x stuck at one of two values — quantile bins should collapse but the
    # fit should still succeed (≥ 2 populated bins).
    frame = pd.DataFrame(
        {
            "x": [0.0] * 50 + [10.0] * 50,
            "y": [1.0] * 50 + [5.0] * 50,
        }
    )
    model = fit_binned_lookup(frame, x_column="x", y_column="y", name="t", output="y", n_knots=10)
    assert isinstance(model.fit, LookupTableFit)
    assert len(model.fit.x_knots) == 2


def test_fit_binned_lookup_rejects_constant_x() -> None:
    frame = pd.DataFrame({"x": [1.0] * 100, "y": list(range(100))})
    with pytest.raises(ValueError, match="too few distinct"):
        fit_binned_lookup(frame, x_column="x", y_column="y", name="t", output="y", n_knots=8)


def test_fit_binned_lookup_rejects_too_few_rows() -> None:
    frame = pd.DataFrame({"x": list(range(5)), "y": list(range(5))})
    with pytest.raises(ValueError, match="need ≥ 20 clean rows"):
        fit_binned_lookup(frame, x_column="x", y_column="y", name="t", output="y", n_knots=10)


# ---------------------------------------------------------------------------
# R² helper
# ---------------------------------------------------------------------------


def test_r_squared_perfect_fit() -> None:
    actual = np.array([1.0, 2.0, 3.0, 4.0])
    assert _coefficient_of_determination(actual, actual.copy()) == pytest.approx(1.0)


def test_r_squared_constant_target_returns_zero() -> None:
    actual = np.array([5.0, 5.0, 5.0])
    predicted = np.array([5.0, 5.0, 5.0])
    assert _coefficient_of_determination(actual, predicted) == 0.0


def test_fit_persists_training_data_hash_stable() -> None:
    frame = _warmup_frame(seed=42)
    m1 = fit_polynomial(frame, x_column="t", y_column="coolant", name="t", output="y", degree=3)
    m2 = fit_polynomial(frame, x_column="t", y_column="coolant", name="t", output="y", degree=3)
    assert m1.fit.training_data_hash == m2.fit.training_data_hash

    frame2 = _warmup_frame(seed=43)
    m3 = fit_polynomial(frame2, x_column="t", y_column="coolant", name="t", output="y", degree=3)
    assert m1.fit.training_data_hash != m3.fit.training_data_hash


# ---------------------------------------------------------------------------
# relative reference_std auto-fill
# ---------------------------------------------------------------------------


def test_relative_deviation_reference_std_auto_fills_from_residuals() -> None:
    """1D fits with relative bands and no declared reference_std get one from
    the fit residuals — recipes for unscaled outputs (raw injector counts)
    can't declare it upfront, and without it the model schema rejects the fit."""
    import numpy as np
    import pandas as pd

    from src.behavior.behavior_models import DeviationConfig

    rng = np.random.default_rng(7)
    x = rng.uniform(0, 100, 500)
    y = 2.0 * x + rng.normal(0, 3.0, 500)
    frame = pd.DataFrame({"x": x, "y": y})

    model = fit_polynomial(
        frame,
        x_column="x",
        y_column="y",
        name="tps_ratio",
        output="derived:TPS_VOLTAGE_PER_PCT",
        degree=1,
        deviation=DeviationConfig(
            kind="relative",  # type: ignore[arg-type]  # legacy alias, loads as sigma
            warning_band=5.0,
            critical_band=12.0,
            sigma_floor=0.01,
        ),
    )
    assert model.deviation is not None
    assert model.deviation.reference_std == pytest.approx(3.0, rel=0.2)

    # An explicitly declared reference_std always wins over the residual fill.
    explicit = fit_polynomial(
        frame,
        x_column="x",
        y_column="y",
        name="tps_ratio",
        output="derived:TPS_VOLTAGE_PER_PCT",
        degree=1,
        deviation=DeviationConfig(
            kind="relative",  # type: ignore[arg-type]  # legacy alias, loads as sigma
            warning_band=5.0,
            critical_band=12.0,
            reference_std=9.0,
            sigma_floor=0.01,
        ),
    )
    assert explicit.deviation is not None
    assert explicit.deviation.reference_std == 9.0
