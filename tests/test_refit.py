"""Tests for the behavior-model refit driver (Task 4.E.8)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.analysis.refit import (
    Surface1DRecipe,
    Surface2DRecipe,
    default_recipes,
    fit_recipe,
    refit_all,
)
from src.decode.behavior_models import (
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    DeviationConfig,
)
from src.preprocessing.segmenter import MODE_COLUMN

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _warmup_frame(n: int = 400, seed: int = 0) -> pd.DataFrame:
    """Synthetic coolant warmup + OBD rpm/load — enough for the default recipes."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 600, n)
    rpm = np.clip(750 + rng.normal(0, 30, n), 600, 6000)
    coolant = 25 + 60 * (1 - np.exp(-t / 120)) + rng.normal(0, 0.5, n)
    load = np.clip(20 + rng.normal(0, 5, n), 0, 100)
    timing = 8 + 0.005 * rpm + rng.normal(0, 0.5, n)
    stft = rng.normal(0, 2, n)
    return pd.DataFrame(
        {
            "obd:RUN_TIME": t,
            "obd:RPM": rpm,
            "obd:COOLANT_TEMP": coolant,
            "obd:ENGINE_LOAD": load,
            "obd:TIMING_ADVANCE": timing,
            "obd:SHORT_FUEL_TRIM_1": stft,
            MODE_COLUMN: ["idle"] * n,
        }
    )


# ---------------------------------------------------------------------------
# default_recipes
# ---------------------------------------------------------------------------


def test_default_recipes_have_unique_names() -> None:
    names = [r.name for r in default_recipes()]
    assert len(names) == len(set(names)), f"duplicate recipe names: {names}"


def test_default_recipe_axes_are_obd_columns() -> None:
    """Recipe AXES must be obd:* — RPM / ENGINE_LOAD / RUN_TIME / THROTTLE_POS
    are the stable mode inputs always present when OBD is polling.

    OUTPUTS may be obd:* (original surfaces), derived:* (computed by
    ``compute_anomaly_signals`` — air mass per cycle, bank balances, TPS
    ratio), or did:* (Mode 22 DID surfaces — injector PW, AFR voltage —
    which skip cleanly until the DIDs are polled). The output prefix is no
    longer constrained to obd:* now that the fuel/air surfaces (Phase
    2026-05) are in the catalog.
    """
    allowed_output_prefixes = ("obd:", "derived:", "did:")
    for recipe in default_recipes():
        axes = (
            [recipe.x_column]
            if isinstance(recipe, Surface1DRecipe)
            else [recipe.x_column, recipe.y_column]
        )
        for axis in axes:
            assert axis.startswith("obd:"), f"{recipe.name} axis {axis} must be obd:*"
        assert recipe.output.startswith(allowed_output_prefixes), (
            f"{recipe.name} output {recipe.output} has an unexpected prefix"
        )


# ---------------------------------------------------------------------------
# fit_recipe: success paths
# ---------------------------------------------------------------------------


def test_fit_recipe_1d_polynomial() -> None:
    frame = _warmup_frame()
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        degree=3,
        min_samples=50,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted
    assert isinstance(result.model, BehaviorModel1D)
    assert result.model.fit.r_squared is not None
    assert result.model.fit.r_squared > 0.9


def test_fit_recipe_1d_binned_lookup() -> None:
    frame = _warmup_frame()
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="binned_lookup",
        n_knots=10,
        min_samples=50,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted


def test_fit_recipe_2d_grid_uses_auto_edges() -> None:
    frame = _warmup_frame(n=2000)
    recipe = Surface2DRecipe(
        name="timing_vs_rpm_load",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        min_samples=500,
        auto_bin_count=4,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted
    model = result.model
    assert isinstance(model, BehaviorModel2D)
    # 4×4 grid (or less if the data collapsed some quantile edges)
    assert len(model.fit.cells) >= 1
    # Edges actually came from the data quantiles
    assert len(model.axes[0].bin_edges) >= 2
    assert len(model.axes[1].bin_edges) >= 2


def test_fit_recipe_2d_with_explicit_edges_overrides_auto() -> None:
    frame = _warmup_frame(n=1000)
    recipe = Surface2DRecipe(
        name="timing",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        x_edges=(600.0, 1000.0, 2000.0, 4000.0),
        y_edges=(0.0, 25.0, 50.0, 100.0),
        min_samples=500,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted
    assert result.model is not None
    assert list(result.model.axes[0].bin_edges) == [600.0, 1000.0, 2000.0, 4000.0]


# ---------------------------------------------------------------------------
# fit_recipe: skip paths (never raise)
# ---------------------------------------------------------------------------


def test_fit_recipe_skips_when_required_column_missing() -> None:
    frame = pd.DataFrame({"obd:RPM": [700.0, 800.0]})
    recipe = Surface1DRecipe(name="coolant", output="obd:COOLANT_TEMP", x_column="obd:RUN_TIME")
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert "missing columns" in result.reason


def test_fit_recipe_skips_when_min_samples_not_met() -> None:
    frame = _warmup_frame(n=10)
    recipe = Surface1DRecipe(
        name="coolant",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        min_samples=1000,
    )
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert "min_samples=1000" in result.reason


def test_fit_recipe_skips_when_operating_mode_filter_drops_all_rows() -> None:
    frame = _warmup_frame()
    recipe = Surface1DRecipe(
        name="x",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        operating_mode="highway",  # frame is all "idle"
    )
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert "operating_mode='highway'" in result.reason


def test_fit_recipe_skips_when_no_operating_mode_column() -> None:
    frame = _warmup_frame().drop(columns=[MODE_COLUMN])
    recipe = Surface1DRecipe(
        name="x",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        operating_mode="idle",  # but frame has no mode column
    )
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert "operating_mode" in result.reason


def test_fit_recipe_swallows_internal_fitter_exceptions() -> None:
    # An x_column with all-constant values would make fit_binned_lookup raise,
    # but fit_recipe should swallow and report rather than propagate.
    frame = pd.DataFrame(
        {
            "obd:RUN_TIME": [100.0] * 100,
            "obd:COOLANT_TEMP": list(range(100)),
        }
    )
    recipe = Surface1DRecipe(
        name="x",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="binned_lookup",
        min_samples=10,
    )
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert result.model is None


# ---------------------------------------------------------------------------
# refit_all: versioning + carry-over
# ---------------------------------------------------------------------------


def _existing_model(name: str, version: int) -> BehaviorModel1D:
    """A minimal prior model with the right name + version for the carry-over tests."""
    from src.decode.behavior_models import Axis1D, PolynomialFit

    return BehaviorModel1D(
        name=name,
        kind="surface_1d",
        output="obd:COOLANT_TEMP",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
        fit=PolynomialFit(
            kind="poly",
            coefficients=[25.0, 0.1],
            sample_count=1,
            fitted_at="prior",
        ),
        version=version,
    )


def test_refit_all_bumps_version_on_successful_refit() -> None:
    frame = _warmup_frame()
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        degree=2,
        min_samples=50,
    )
    existing = BehaviorModels(
        schema_version=1, models=[_existing_model("coolant_warmup", version=3)]
    )
    new_models, results = refit_all([recipe], frame, existing=existing)
    assert len(new_models.models) == 1
    assert new_models.models[0].version == 4  # 3 + 1
    assert results[0].fitted


def test_refit_all_carries_over_skipped_recipes() -> None:
    frame = pd.DataFrame({"obd:RPM": [700.0, 800.0]})  # missing COOLANT
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
    )
    prior = _existing_model("coolant_warmup", version=7)
    existing = BehaviorModels(schema_version=1, models=[prior])
    new_models, results = refit_all([recipe], frame, existing=existing)
    # skipped → keep the prior model unchanged
    assert results[0].skipped
    assert len(new_models.models) == 1
    assert new_models.models[0].version == 7  # not bumped


def test_refit_all_drops_retired_recipes_from_existing() -> None:
    frame = _warmup_frame()
    # Existing has a "retired" model not in the new recipe set
    retired = _existing_model("retired_surface", version=5)
    coolant = _existing_model("coolant_warmup", version=1)
    existing = BehaviorModels(schema_version=1, models=[retired, coolant])

    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        min_samples=50,
    )
    new_models, _ = refit_all([recipe], frame, existing=existing)
    names = [m.name for m in new_models.models]
    assert names == ["coolant_warmup"]  # retired_surface dropped


def test_refit_all_first_run_starts_at_version_1() -> None:
    frame = _warmup_frame()
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        min_samples=50,
    )
    new_models, _ = refit_all([recipe], frame, existing=None)
    assert new_models.models[0].version == 1


def test_refit_all_preserves_recipe_order() -> None:
    frame = _warmup_frame()
    r1 = Surface1DRecipe(
        name="b",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        min_samples=50,
    )
    r2 = Surface1DRecipe(
        name="a",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        min_samples=50,
    )
    new_models, _ = refit_all([r1, r2], frame)
    assert [m.name for m in new_models.models] == ["b", "a"]


# ---------------------------------------------------------------------------
# RecipeResult ergonomics
# ---------------------------------------------------------------------------


def test_recipe_result_fitted_property_reflects_model() -> None:
    frame = _warmup_frame()
    recipe = Surface1DRecipe(
        name="coolant",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        min_samples=50,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted
    assert not result.skipped


def test_deviation_config_persists_on_fitted_model() -> None:
    frame = _warmup_frame()
    dev = DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0)
    recipe = Surface1DRecipe(
        name="coolant",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        deviation=dev,
        min_samples=50,
    )
    result = fit_recipe(recipe, frame)
    assert result.model is not None
    assert result.model.deviation == dev
