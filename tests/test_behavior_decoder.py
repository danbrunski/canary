"""Tests for the behavior-model runtime decoder (Tasks 4.E.6, 4.E.7)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.decode.accepted_baselines import (
    AcceptedBaselineOverride,
    DirectionalBand,
    Provenance,
)
from src.decode.behavior_decoder import (
    DEVIATION_COLUMN_PREFIX,
    append_deviation_columns,
    deviation,
    deviation_for_override,
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


def test_append_deviation_columns_propagates_axis_and_output_freshness() -> None:
    models = BehaviorModels(schema_version=1, models=[_make_coolant_model()])
    frame = pd.DataFrame(
        {
            "obd:RUN_TIME": [100.0, 100.0],
            "obd:RUN_TIME__gap": [False, True],
            "decoded:ECM_COOLANT_TEMP": [42.0, 42.0],
            "decoded:ECM_COOLANT_TEMP__gap": [False, False],
        }
    )
    out = append_deviation_columns(frame, models)
    assert out["deviation:coolant_warmup__gap"].tolist() == [False, True]


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


# ---------------------------------------------------------------------------
# 2.1 — directional deviation kind
# ---------------------------------------------------------------------------


def test_directional_grades_worse_side_and_ignores_better_side() -> None:
    # expected(t=100) = 40.0 (coolant_warmup fixture: 25 + 0.15*t)
    model = _make_coolant_model(
        DeviationConfig(
            kind="directional",
            tolerance=2.0,
            worse_direction="positive",
            warning_band=5.0,
            critical_band=10.0,
        )
    )
    point = {"obd:RUN_TIME": 100.0}
    # within tolerance (delta=1) → ok
    assert deviation(model, 41.0, point).severity == "ok"
    # worse side, past warning (delta=8) → warning
    assert deviation(model, 48.0, point).severity == "warning"
    # worse side, past critical (delta=15) → critical
    assert deviation(model, 55.0, point).severity == "critical"
    # better side (delta=-20) — better_direction: ignore never alerts,
    # no matter how far
    assert deviation(model, 20.0, point).severity == "ok"


def test_directional_worse_direction_negative() -> None:
    model = _make_coolant_model(
        DeviationConfig(
            kind="directional",
            tolerance=2.0,
            worse_direction="negative",
            warning_band=5.0,
            critical_band=10.0,
        )
    )
    point = {"obd:RUN_TIME": 100.0}
    # worsening is now actual < expected: delta=-8 → warning
    assert deviation(model, 32.0, point).severity == "warning"
    # improving is now actual > expected — ignored regardless of magnitude
    assert deviation(model, 60.0, point).severity == "ok"


def test_directional_std_scaled_uses_local_std_and_floor() -> None:
    model = _make_coolant_model(
        DeviationConfig(
            kind="directional",
            tolerance=1.0,
            worse_direction="positive",
            warning_band=3.0,
            critical_band=6.0,
            std_scaled=True,
            sigma_floor=0.5,
            reference_std=0.5,
        )
    )
    point = {"obd:RUN_TIME": 100.0}
    # delta=8 / std(0.5) = 16σ → critical
    assert deviation(model, 48.0, point).severity == "critical"


# ---------------------------------------------------------------------------
# 2.2 — override → alert path
# ---------------------------------------------------------------------------


def _override(**overrides: object) -> AcceptedBaselineOverride:
    band = DirectionalBand(
        tolerance=3.0, worse_direction="positive", worse_warning=3.0, worse_critical=6.0
    )
    provenance = Provenance(
        declared_by="dan",
        declared_at=date(2026, 5, 17),
        review_by=date(2026, 11, 17),
        reason="~190k mi, known weak #3 injector; owner not replacing",
    )
    defaults: dict[str, object] = {
        "channel": None,
        "behavior_model": "coolant_warmup",
        "accepted_center": None,
        "band": band,
        "provenance": provenance,
    }
    defaults.update(overrides)
    return AcceptedBaselineOverride.model_validate(defaults)


def test_behavior_model_override_recenters_band_not_center() -> None:
    # No static deviation config at all — the override supplies the entire
    # (one-sided) band; the center still comes from the model's own fitted
    # expected_value (40.0 at t=100), never from the override.
    model = _make_coolant_model(None)
    point = {"obd:RUN_TIME": 100.0}
    override = _override()

    assert deviation(model, 41.0, point, override=override).severity == "ok"  # within tolerance
    res = deviation(model, 43.5, point, override=override)  # delta=3.5 → warning
    assert res.severity == "warning"
    assert res.expected == pytest.approx(40.0)  # center unchanged by the override
    assert deviation(model, 47.0, point, override=override).severity == "critical"  # delta=7
    # improving direction (delta=-10) — never alerts
    assert deviation(model, 30.0, point, override=override).severity == "ok"


def test_behavior_model_override_ignores_accepted_center_if_set() -> None:
    # accepted_center only applies to raw-channel overrides (doc, 2.1); a
    # behavior_model-targeted override's to_deviation_config() must drop it
    # even if a caller somehow set one, so the center always stays the
    # model's own surface fit.
    model = _make_coolant_model(None)
    point = {"obd:RUN_TIME": 100.0}
    override = _override(accepted_center=999.0)
    res = deviation(model, 43.5, point, override=override)
    assert res.expected == pytest.approx(40.0)
    assert res.severity == "warning"


def test_no_override_falls_back_to_symmetric_config_unchanged() -> None:
    # Symmetric configs stay unchanged when no override is passed.
    model = _make_coolant_model(
        DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0)
    )
    point = {"obd:RUN_TIME": 100.0}
    assert deviation(model, 47.0, point).severity == "warning"
    assert deviation(model, 47.0, point, override=None).severity == "warning"


def test_channel_override_grades_directly_against_accepted_center() -> None:
    # The doc's own worked example: STFT B1 accepted at +12%, ±3 wander,
    # positive worsening, warning at +3/critical at +6 past center.
    override = _override(channel="obd:SHORT_FUEL_TRIM_1", behavior_model=None, accepted_center=12.0)
    assert deviation_for_override(override, 13.0).severity == "ok"  # within tolerance
    res = deviation_for_override(override, 16.0)  # delta=4 → warning
    assert res.severity == "warning"
    assert res.expected == pytest.approx(12.0)
    assert res.deviation == pytest.approx(4.0)
    assert deviation_for_override(override, 19.0).severity == "critical"  # delta=7
    # improving toward 0% — never alerts, however far
    assert deviation_for_override(override, 5.0).severity == "ok"


def test_channel_override_missing_actual_is_ok_with_no_deviation() -> None:
    override = _override(channel="obd:SHORT_FUEL_TRIM_1", behavior_model=None, accepted_center=12.0)
    res = deviation_for_override(override, None)
    assert res.deviation is None
    assert res.severity == "ok"


def test_expired_override_is_inert_via_active_override_for() -> None:
    """An override past review_by is inert (2.2) — active_override_for
    already returns None for it, so the caller falls back to the model's
    own (here: no) deviation config, never applying a stale recentering."""
    from src.decode.accepted_baselines import AcceptedBaselines

    model = _make_coolant_model(None)
    point = {"obd:RUN_TIME": 100.0}
    baselines = AcceptedBaselines(schema_version=1, overrides=[_override()])
    live_override = baselines.active_override_for(
        "coolant_warmup",
        None,
        today=date(2026, 12, 1),  # past review_by
    )
    assert live_override is None
    # delta=15 would be critical under the override's band; with no active
    # override and no static deviation config, it's "ok" (model is silent).
    assert deviation(model, 55.0, point, override=live_override).severity == "ok"
