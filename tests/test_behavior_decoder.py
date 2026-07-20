"""Tests for the behavior-model runtime decoder (Tasks 4.E.6, 4.E.7)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.decode.behavior_decoder import (
    DEVIATION_COLUMN_PREFIX,
    append_deviation_columns,
    deviation,
    expected_value,
    expected_value_column,
)
from src.decode.behavior_models import (
    Axis1D,
    Axis2D,
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    DeviationConfig,
    GridBinnedMeanFit,
    GridCell,
    PolynomialFit,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_coolant_model(
    deviation_cfg: DeviationConfig | None = None,
    operating_mode: str | None = None,
) -> BehaviorModel1D:
    # y(t) = 25 + 0.15 * t (a linear warmup, easy to reason about)
    return BehaviorModel1D(
        name="coolant_warmup",
        kind="surface_1d",
        output="ECM_COOLANT_TEMP",
        output_unit="degC",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", unit="s", domain=(0.0, 600.0)),
        fit=PolynomialFit(
            kind="poly",
            coefficients=[25.0, 0.15],
            sample_count=100,
            r_squared=0.99,
            sessions=["s1"],
            fitted_at="now",
        ),
        operating_mode=operating_mode,
        deviation=deviation_cfg,
    )


def _make_fuel_surface() -> BehaviorModel2D:
    # 2x2 grid, hand-built so we know the answers.
    # rpm bins: [600,1500) [1500,4000]; maf bins: [1,15) [15,30]
    cells = [
        GridCell(mean=1.0, std=0.1, count=100),  # (rpm0, maf0)
        GridCell(mean=4.0, std=0.2, count=50),  # (rpm0, maf1)
        GridCell(mean=2.0, std=0.15, count=80),  # (rpm1, maf0)
        GridCell(mean=0.0, std=0.0, count=0),  # (rpm1, maf1) — empty
    ]
    return BehaviorModel2D(
        name="fuel_idle",
        kind="surface_2d",
        output="ECM_INJECTOR_PW",
        output_unit="ms",
        axes=(
            Axis2D(name="rpm", channel="obd:RPM", unit="rpm", bin_edges=[600, 1500, 4000]),
            Axis2D(name="maf", channel="obd:MAF", unit="g/s", bin_edges=[1, 15, 30]),
        ),
        fit=GridBinnedMeanFit(
            kind="grid_binned_mean",
            cells=cells,
            sample_count=230,
            sessions=["s2"],
            fitted_at="now",
        ),
    )


# ---------------------------------------------------------------------------
# expected_value (scalar API)
# ---------------------------------------------------------------------------


def test_expected_value_1d_in_domain() -> None:
    model = _make_coolant_model()
    assert expected_value(model, {"obd:RUN_TIME": 100.0}) == pytest.approx(25 + 0.15 * 100)


def test_expected_value_1d_outside_domain_returns_none() -> None:
    model = _make_coolant_model()
    assert expected_value(model, {"obd:RUN_TIME": -1.0}) is None
    assert expected_value(model, {"obd:RUN_TIME": 700.0}) is None


def test_expected_value_1d_missing_input_returns_none() -> None:
    model = _make_coolant_model()
    assert expected_value(model, {}) is None
    assert expected_value(model, {"obd:RUN_TIME": float("nan")}) is None


def test_expected_value_2d_in_grid() -> None:
    model = _make_fuel_surface()
    assert expected_value(model, {"obd:RPM": 700.0, "obd:MAF": 5.0}) == 1.0
    assert expected_value(model, {"obd:RPM": 2000.0, "obd:MAF": 5.0}) == 2.0


def test_expected_value_2d_empty_cell_returns_none() -> None:
    model = _make_fuel_surface()
    assert expected_value(model, {"obd:RPM": 2000.0, "obd:MAF": 20.0}) is None


def test_expected_value_2d_outside_grid_returns_none() -> None:
    model = _make_fuel_surface()
    assert expected_value(model, {"obd:RPM": 100.0, "obd:MAF": 5.0}) is None
    assert expected_value(model, {"obd:RPM": 700.0, "obd:MAF": 0.5}) is None


def test_expected_value_respects_operating_mode_filter() -> None:
    model = _make_coolant_model(operating_mode="warmup")
    # right mode → expected available
    assert expected_value(
        model, {"obd:RUN_TIME": 100.0, "operating_mode": "warmup"}
    ) == pytest.approx(40.0)
    # wrong mode → no expectation
    assert expected_value(model, {"obd:RUN_TIME": 100.0, "operating_mode": "city"}) is None


# ---------------------------------------------------------------------------
# deviation grading
# ---------------------------------------------------------------------------


def test_deviation_grades_absolute_bands() -> None:
    model = _make_coolant_model(
        DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0)
    )
    # within warning band
    res = deviation(model, 42.0, {"obd:RUN_TIME": 100.0})
    assert res.expected == pytest.approx(40.0)
    assert res.deviation == pytest.approx(2.0)
    assert res.severity == "ok"
    # warning
    res = deviation(model, 47.0, {"obd:RUN_TIME": 100.0})
    assert res.severity == "warning"
    # critical
    res = deviation(model, 60.0, {"obd:RUN_TIME": 100.0})
    assert res.severity == "critical"


def test_deviation_returns_none_when_model_silent() -> None:
    model = _make_coolant_model(
        DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0)
    )
    res = deviation(model, 50.0, {"obd:RUN_TIME": 700.0})  # outside domain
    assert res.expected is None
    assert res.deviation is None
    assert res.severity == "ok"


def test_deviation_returns_none_when_actual_missing() -> None:
    model = _make_coolant_model(DeviationConfig(kind="absolute", warning_band=5.0))
    res = deviation(model, None, {"obd:RUN_TIME": 100.0})
    assert res.expected == pytest.approx(40.0)
    assert res.deviation is None
    res2 = deviation(model, math.nan, {"obd:RUN_TIME": 100.0})
    assert res2.deviation is None


def test_deviation_relative_2d_uses_cell_std() -> None:
    cells = [
        GridCell(mean=1.0, std=0.5, count=100),  # (rpm0, maf0): std = 0.5
    ]
    model = BehaviorModel2D(
        name="m",
        kind="surface_2d",
        output="out",
        axes=(
            Axis2D(name="rpm", channel="obd:RPM", bin_edges=[600, 1500]),
            Axis2D(name="maf", channel="obd:MAF", bin_edges=[1, 15]),
        ),
        fit=GridBinnedMeanFit(
            kind="grid_binned_mean", cells=cells, sample_count=100, fitted_at="now"
        ),
        deviation=DeviationConfig(
            kind="relative", warning_band=3.0, critical_band=6.0, sigma_floor=0.01
        ),
    )
    # 1.0 + 1.5 (delta) → 3.0 sigma → warning
    res = deviation(model, 2.5, {"obd:RPM": 700.0, "obd:MAF": 5.0})
    assert res.severity == "warning"
    # 1.0 + 3.5 → 7.0 sigma → critical
    res = deviation(model, 4.5, {"obd:RPM": 700.0, "obd:MAF": 5.0})
    assert res.severity == "critical"
    # 1.0 + 0.5 → 1.0 sigma → ok
    res = deviation(model, 1.5, {"obd:RPM": 700.0, "obd:MAF": 5.0})
    assert res.severity == "ok"


def test_deviation_relative_1d_uses_reference_std() -> None:
    model = _make_coolant_model(
        DeviationConfig(
            kind="relative",
            warning_band=2.0,
            critical_band=4.0,
            reference_std=1.0,
            sigma_floor=0.01,
        )
    )
    # delta = 2.5 → 2.5 sigma → warning
    res = deviation(model, 42.5, {"obd:RUN_TIME": 100.0})
    assert res.severity == "warning"
    res = deviation(model, 45.0, {"obd:RUN_TIME": 100.0})  # delta=5 → 5σ → critical
    assert res.severity == "critical"


# ---------------------------------------------------------------------------
# Vectorized API (Task 4.E.7)
# ---------------------------------------------------------------------------


def test_expected_value_column_1d_returns_nan_outside_domain() -> None:
    model = _make_coolant_model()
    frame = pd.DataFrame({"obd:RUN_TIME": [0.0, 100.0, 700.0, np.nan, 600.0]})
    series = expected_value_column(frame, model)
    assert series.iloc[0] == pytest.approx(25.0)
    assert series.iloc[1] == pytest.approx(40.0)
    assert np.isnan(series.iloc[2])  # outside domain
    assert np.isnan(series.iloc[3])  # NaN input
    assert series.iloc[4] == pytest.approx(115.0)


def test_expected_value_column_2d() -> None:
    model = _make_fuel_surface()
    frame = pd.DataFrame(
        {
            "obd:RPM": [700.0, 2000.0, 100.0, 2000.0, 700.0],
            "obd:MAF": [5.0, 5.0, 5.0, 20.0, np.nan],
        }
    )
    series = expected_value_column(frame, model)
    assert series.iloc[0] == 1.0
    assert series.iloc[1] == 2.0
    assert np.isnan(series.iloc[2])  # rpm below grid
    assert np.isnan(series.iloc[3])  # empty cell (rpm1,maf1)
    assert np.isnan(series.iloc[4])  # NaN MAF


def test_expected_value_column_returns_nan_when_input_column_missing() -> None:
    model = _make_coolant_model()
    frame = pd.DataFrame({"other": [1.0, 2.0]})
    series = expected_value_column(frame, model)
    assert series.isna().all()
    assert len(series) == 2


def test_expected_value_column_respects_operating_mode() -> None:
    model = _make_coolant_model(operating_mode="warmup")
    frame = pd.DataFrame(
        {
            "obd:RUN_TIME": [100.0, 100.0],
            "operating_mode": ["warmup", "city"],
        }
    )
    series = expected_value_column(frame, model)
    assert series.iloc[0] == pytest.approx(40.0)
    assert np.isnan(series.iloc[1])


def test_append_deviation_columns_writes_one_column_per_model() -> None:
    coolant = _make_coolant_model()
    fuel = _make_fuel_surface()
    models = BehaviorModels(schema_version=1, models=[coolant, fuel])
    frame = pd.DataFrame(
        {
            "obd:RUN_TIME": [100.0, 200.0],
            "obd:RPM": [700.0, 2000.0],
            "obd:MAF": [5.0, 5.0],
            "decoded:ECM_COOLANT_TEMP": [42.0, 60.0],  # actual
            "decoded:ECM_INJECTOR_PW": [1.1, 2.1],  # actual
        }
    )
    out = append_deviation_columns(frame, models)
    assert f"{DEVIATION_COLUMN_PREFIX}coolant_warmup" in out.columns
    assert f"{DEVIATION_COLUMN_PREFIX}fuel_idle" in out.columns
    # row 0: expected coolant 40, actual 42 → deviation 2.0
    assert out["deviation:coolant_warmup"].iloc[0] == pytest.approx(2.0)
    # row 0: expected fuel 1.0, actual 1.1 → deviation 0.1
    assert out["deviation:fuel_idle"].iloc[0] == pytest.approx(0.1)


def test_append_deviation_columns_writes_nan_when_actual_missing() -> None:
    coolant = _make_coolant_model()
    models = BehaviorModels(schema_version=1, models=[coolant])
    frame = pd.DataFrame({"obd:RUN_TIME": [100.0]})
    out = append_deviation_columns(frame, models)
    # actual column doesn't exist → deviation NaN; expected exists internally
    # but isn't surfaced as a separate column for this task.
    assert np.isnan(out["deviation:coolant_warmup"].iloc[0])


def test_append_deviation_columns_honors_output_columns_override() -> None:
    coolant = _make_coolant_model()
    models = BehaviorModels(schema_version=1, models=[coolant])
    frame = pd.DataFrame(
        {
            "obd:RUN_TIME": [100.0],
            "obd:COOLANT_TEMP": [50.0],  # the "actual" but at a non-default column
        }
    )
    out = append_deviation_columns(
        frame, models, output_columns={"coolant_warmup": "obd:COOLANT_TEMP"}
    )
    # expected 40, actual 50 → deviation 10
    assert out["deviation:coolant_warmup"].iloc[0] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# sigma_floor (relative deviation)
# ---------------------------------------------------------------------------


def _make_quantized_timing_surface(sigma_floor: float) -> BehaviorModel2D:
    """One cell fit on quantization-flat data (std below the 0.5°/LSB step)
    and one cell where the std collapsed to exactly 0."""
    cells = [
        GridCell(mean=17.0, std=0.13, count=292),  # flat idle stretch
        GridCell(mean=17.0, std=0.0, count=125),  # perfectly flat — std 0
        GridCell(mean=30.0, std=2.0, count=400),  # healthy spread
        GridCell(mean=35.0, std=2.5, count=350),
    ]
    return BehaviorModel2D(
        name="timing",
        kind="surface_2d",
        output="obd:TIMING_ADVANCE",
        output_unit="degBTDC",
        axes=(
            Axis2D(name="rpm", channel="obd:RPM", unit="rpm", bin_edges=[400, 1500, 4000]),
            Axis2D(name="iat", channel="obd:INTAKE_TEMP", unit="degC", bin_edges=[0, 30, 60]),
        ),
        fit=GridBinnedMeanFit(
            kind="grid_binned_mean",
            cells=cells,
            sample_count=1167,
            sessions=["s1"],
            fitted_at="now",
        ),
        deviation=DeviationConfig(
            kind="relative", warning_band=3.0, critical_band=6.0, sigma_floor=sigma_floor
        ),
    )


def test_sigma_floor_stops_sub_lsb_hair_trigger() -> None:
    """A cell fit on quantization-flat data (std 0.13, below the 0.5°/LSB
    step) grades a same-magnitude flicker as "ok" once floored to the LSB,
    not a false warning. (4.2 schema-enforces sigma_floor > 0 for every
    relative config now, so the pre-fix "unfloored" construction this test
    used to compare against can no longer be built — see
    test_behavior_models.py::test_deviation_config_relative_requires_positive_sigma_floor.)
    """
    point = {"obd:RPM": 800.0, "obd:INTAKE_TEMP": 20.0}  # cell 0: std 0.13
    floored = _make_quantized_timing_surface(sigma_floor=0.5)
    res = deviation(floored, 17.5, point)  # 0.5° flicker / 0.5 floor = 1.0σ
    assert res is not None and res.severity == "ok"
    # A genuine excursion still alerts through the floor.
    res = deviation(floored, 21.0, point)  # 4.0° / 0.5 = 8σ
    assert res is not None and res.severity == "critical"


def test_sigma_floor_revives_zero_std_cells() -> None:
    """A std=0.0 cell grades normally once floored, instead of the
    permanently-mute behavior a floor-less relative config used to have
    (now schema-unreachable — see the sibling test above)."""
    point = {"obd:RPM": 800.0, "obd:INTAKE_TEMP": 45.0}  # cell 1: std 0.0
    floored = _make_quantized_timing_surface(sigma_floor=0.5)
    res = deviation(floored, 25.0, point)  # 8° / 0.5 = 16σ
    assert res is not None and res.severity == "critical"


def test_sigma_floor_does_not_revive_off_map_points() -> None:
    """The floor bounds a *present* std; empty cells stay off-map."""
    model = _make_fuel_surface()
    assert model.deviation is None  # uses bands below
    model = model.model_copy(
        update={
            "deviation": DeviationConfig(
                kind="relative", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
            )
        }
    )
    res = deviation(model, 99.0, {"obd:RPM": 2000.0, "obd:MAF": 20.0})  # count=0 cell
    assert res is not None and res.severity == "ok" and res.expected is None
