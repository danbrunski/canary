"""Tests for the behavior-model refit driver (Task 4.E.8)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.analysis.refit import (
    Surface1DRecipe,
    Surface2DRecipe,
    choose_warmup_confounding_strategy,
    count_usable_cold_start_sessions,
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
from src.preprocessing.resampler import SESSION_COLUMN
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
    names = [r.name for r in default_recipes("nissan-xterra-2008")]
    assert len(names) == len(set(names)), f"duplicate recipe names: {names}"


def test_default_recipe_axes_use_one_synchronous_service() -> None:
    """A behavior surface must not mix mutually-exclusive Mode 01/22 samples.

    OUTPUTS may be obd:* (original surfaces), derived:* (computed by
    ``compute_anomaly_signals`` — air mass per cycle, bank balances, TPS
    ratio), or did:* (Mode 22 DID surfaces — injector PW, AFR voltage —
    which skip cleanly until the DIDs are polled). The output prefix is no
    longer constrained to obd:* now that the fuel/air surfaces (Phase
    2026-05) are in the catalog.
    """
    allowed_output_prefixes = ("obd:", "derived:", "did:")
    for recipe in default_recipes("nissan-xterra-2008"):
        axes = (
            [recipe.x_column]
            if isinstance(recipe, Surface1DRecipe)
            else [recipe.x_column, recipe.y_column]
        )
        output_service = recipe.output.split(":", 1)[0]
        axis_services = {axis.split(":", 1)[0] for axis in axes}
        if output_service == "did":
            assert axis_services == {"did"}, (
                f"{recipe.name} mixes Mode 22 output with non-DID axes: {axes}"
            )
        else:
            assert len(axis_services) == 1
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


def test_fit_recipe_persists_and_applies_rate_gate_and_tune_revision() -> None:
    frame = pd.DataFrame(
        {
            "x": [0.0, 1.0, 2.0, 3.0, 4.0],
            "x__roc": [0.0, 0.1, 5.0, -0.2, -6.0],
            "y": [1.0, 3.0, 5.0, 7.0, 9.0],
        }
    )
    recipe = Surface1DRecipe(
        name="steady",
        output="y",
        x_column="x",
        degree=1,
        min_samples=3,
        min_r_squared=None,
        max_abs_rate_of_change={"x": 0.25},
    )

    result = fit_recipe(recipe, frame, tune_revision="rom-1")

    assert result.model is not None
    assert result.model.fit.sample_count == 3
    assert result.model.fit.tune_revision == "rom-1"
    assert result.model.conditions[0].channel == "x__roc"
    assert result.model.conditions[0].max_abs_per_second == 0.25


def test_fit_recipe_refuses_rate_gate_without_derivative() -> None:
    recipe = Surface1DRecipe(
        name="steady",
        output="y",
        x_column="x",
        min_samples=2,
        max_abs_rate_of_change={"x": 1.0},
    )
    result = fit_recipe(recipe, pd.DataFrame({"x": [1.0, 2.0], "y": [2.0, 4.0]}))
    assert result.skipped is True
    assert "can't enforce steady-state rate gate" in result.reason


def test_fit_recipe_enforces_capture_allowlist_per_recipe() -> None:
    x = np.arange(100, dtype=float)
    frame = pd.concat(
        [
            pd.DataFrame({SESSION_COLUMN: "capture-a", "x": x, "y": 2.0 * x}),
            pd.DataFrame({SESSION_COLUMN: "capture-b", "x": x, "y": -100.0 - x}),
        ],
        ignore_index=True,
    )
    recipe = Surface1DRecipe(
        name="authorized_surface",
        output="y",
        x_column="x",
        degree=1,
        min_samples=50,
    )

    result = fit_recipe(
        recipe,
        frame,
        capture_recipe_allowlists={
            "capture-a": {"authorized_surface"},
            "capture-b": {"different_surface"},
        },
    )

    assert result.model is not None
    assert result.model.fit.sample_count == 100
    assert result.model.fit.sessions == ["capture-a"]
    assert result.model.fit.r_squared == 1.0


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
        # The synthetic fixture is a narrow idle-only cluster (not what this
        # test is about) — disable the 4.1 coverage gate so it doesn't
        # entangle with the edges-override assertion below.
        min_populated_cell_fraction=0.0,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted, result.reason
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


def test_refit_all_carries_over_priors_outside_refit_subset_when_catalog_given() -> None:
    """``--recipe foo`` should refit only foo while preserving the other
    canonical surfaces from disk — they're in the catalog but not the refit
    subset, so they carry over unchanged instead of being dropped."""
    frame = _warmup_frame()
    coolant = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        min_samples=50,
    )
    other = Surface1DRecipe(
        name="other_surface",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
    )
    catalog = [coolant, other]
    existing = BehaviorModels(
        schema_version=1,
        models=[
            _existing_model("coolant_warmup", version=2),
            _existing_model("other_surface", version=5),
        ],
    )

    new_models, results = refit_all([coolant], frame, existing=existing, catalog=catalog)

    by_name = {m.name: m for m in new_models.models}
    assert set(by_name) == {"coolant_warmup", "other_surface"}
    assert by_name["coolant_warmup"].version == 3  # refit → bumped
    assert by_name["other_surface"].version == 5  # not in refit subset → carried over
    assert [r.name for r in results] == ["coolant_warmup"]  # results only for what we ran


def test_refit_all_still_drops_retired_priors_when_catalog_given() -> None:
    """Priors not in the canonical catalog are still retired, even when
    ``--recipe`` doesn't list them."""
    frame = _warmup_frame()
    coolant = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        min_samples=50,
    )
    catalog = [coolant]
    existing = BehaviorModels(
        schema_version=1,
        models=[
            _existing_model("coolant_warmup", version=1),
            _existing_model("retired_surface", version=5),
        ],
    )

    new_models, _ = refit_all([coolant], frame, existing=existing, catalog=catalog)
    assert {m.name for m in new_models.models} == {"coolant_warmup"}


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


# ---------------------------------------------------------------------------
# engine-running gate (min_rpm)
# ---------------------------------------------------------------------------


def test_min_rpm_gate_excludes_koeo_rows_from_fit_and_auto_edges() -> None:
    """KOEO rows (rpm=0, wildly different output) must reach neither the
    fitted cells nor the auto-edge quantiles."""
    import numpy as np

    rng = np.random.default_rng(3)
    n_koeo, n_run = 600, 1400
    frame = pd.DataFrame(
        {
            "obd:RPM": np.concatenate([np.zeros(n_koeo), rng.uniform(700, 3000, n_run)]),
            "obd:ENGINE_LOAD": np.concatenate(
                [rng.uniform(0, 5, n_koeo), rng.uniform(15, 80, n_run)]
            ),
            # KOEO timing reads ~63°; running ~17°.
            "obd:TIMING_ADVANCE": np.concatenate(
                [np.full(n_koeo, 63.0), rng.normal(17.0, 2.0, n_run)]
            ),
        }
    )
    recipe = Surface2DRecipe(
        name="timing_gated",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        min_samples=100,
        min_rpm=400.0,
        min_cell_count=1,
        deviation=DeviationConfig(
            kind="relative", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
        ),
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted, result.reason
    model = result.model
    assert model is not None
    # No KOEO mass: the grid starts at running RPM and no cell mean is near 63°.
    assert model.axes[0].bin_edges[0] >= 400.0
    populated = [cell for cell in model.fit.cells if cell.count > 0]
    assert populated
    assert all(cell.mean < 40.0 for cell in populated)
    assert model.fit.sample_count == n_run


def test_min_rpm_gate_requires_rpm_column() -> None:
    """The gate is load-bearing — a frame without obd:RPM must skip, not
    silently fit unfiltered."""
    frame = pd.DataFrame(
        {
            "x": [1.0, 2.0, 3.0] * 50,
            "y": [1.0, 2.0, 3.0] * 50,
        }
    )
    recipe = Surface1DRecipe(
        name="gated",
        output="y",
        x_column="x",
        min_samples=10,
        min_rpm=400.0,
        deviation=DeviationConfig(kind="absolute", warning_band=1.0, critical_band=2.0),
    )
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert "min_rpm" in result.reason


# ---------------------------------------------------------------------------
# 4.1 — publish gate
# ---------------------------------------------------------------------------


def test_fit_recipe_1d_publish_gate_skips_poor_r_squared() -> None:
    rng = np.random.default_rng(1)
    n = 200
    frame = pd.DataFrame(
        {
            "obd:RUN_TIME": np.linspace(0, 600, n),
            "obd:COOLANT_TEMP": rng.normal(50, 30, n),  # pure noise vs run_time
        }
    )
    recipe = Surface1DRecipe(
        name="noisy",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        degree=1,
        min_samples=50,
        min_r_squared=0.6,
    )
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert result.model is None
    assert "r_squared" in result.reason


def test_fit_recipe_1d_publish_gate_allows_good_fit() -> None:
    frame = _warmup_frame()
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        degree=3,
        min_samples=50,
        min_r_squared=0.6,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted, result.reason


def test_fit_recipe_2d_publish_gate_skips_all_empty_grid() -> None:
    """The headline 4.1 bug — every row lands in one cell, so the grid is
    silently all-empty apart from that cell; coverage must refuse it."""
    frame = pd.DataFrame(
        {
            "obd:RPM": [700.0] * 600,
            "obd:ENGINE_LOAD": [20.0] * 600,
            "obd:TIMING_ADVANCE": [17.0] * 600,
        }
    )
    recipe = Surface2DRecipe(
        name="timing_sparse",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        x_edges=(600.0, 1000.0, 2000.0, 4000.0),
        y_edges=(0.0, 25.0, 50.0, 100.0),
        min_samples=100,
        min_populated_cell_fraction=0.3,
    )
    result = fit_recipe(recipe, frame)
    assert result.skipped
    assert result.model is None
    assert "coverage" in result.reason


def test_fit_recipe_2d_publish_gate_allows_well_covered_grid() -> None:
    frame = _warmup_frame(n=2000)
    recipe = Surface2DRecipe(
        name="timing_vs_rpm_load",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        min_samples=500,
        auto_bin_count=3,
        min_populated_cell_fraction=0.3,
    )
    result = fit_recipe(recipe, frame)
    assert result.fitted, result.reason


# ---------------------------------------------------------------------------
# 4.3 — frozen 2D bin edges across versions
# ---------------------------------------------------------------------------


def _timing_2d_recipe() -> Surface2DRecipe:
    return Surface2DRecipe(
        name="timing_vs_rpm_load",
        output="obd:TIMING_ADVANCE",
        x_column="obd:RPM",
        y_column="obd:ENGINE_LOAD",
        min_samples=500,
        auto_bin_count=4,
        min_populated_cell_fraction=0.0,  # not the concern of this test group
    )


def test_fit_recipe_2d_reuses_prior_edges_without_regrid() -> None:
    recipe = _timing_2d_recipe()
    first = fit_recipe(recipe, _warmup_frame(n=1000, seed=1))
    assert first.fitted and first.model is not None
    assert isinstance(first.model, BehaviorModel2D)
    assert first.model.grid_version == 1
    first_x_edges = list(first.model.axes[0].bin_edges)
    first_y_edges = list(first.model.axes[1].bin_edges)

    # Refit against genuinely different (shifted-seed) data, no regrid: the
    # stored edges must come back unchanged even though the raw data moved.
    second = fit_recipe(recipe, _warmup_frame(n=1000, seed=99), prior_model=first.model)
    assert second.fitted and second.model is not None
    assert isinstance(second.model, BehaviorModel2D)
    assert list(second.model.axes[0].bin_edges) == first_x_edges
    assert list(second.model.axes[1].bin_edges) == first_y_edges
    assert second.model.grid_version == 1


def test_fit_recipe_2d_explicit_regrid_recomputes_edges_and_bumps_grid_version() -> None:
    recipe = _timing_2d_recipe()
    first = fit_recipe(recipe, _warmup_frame(n=1000, seed=1))
    assert first.fitted and first.model is not None
    assert isinstance(first.model, BehaviorModel2D)

    second = fit_recipe(
        recipe, _warmup_frame(n=1000, seed=99), prior_model=first.model, regrid=True
    )
    assert second.fitted and second.model is not None
    assert isinstance(second.model, BehaviorModel2D)
    assert second.model.grid_version == 2
    assert list(second.model.axes[0].bin_edges) != list(first.model.axes[0].bin_edges)


def test_refit_all_regrid_recipes_only_regrids_named_recipes() -> None:
    recipe = _timing_2d_recipe()
    first_models, _ = refit_all([recipe], _warmup_frame(n=1000, seed=1), existing=None)
    first = first_models.models[0]
    assert isinstance(first, BehaviorModel2D)

    # No --regrid: edges frozen.
    unregridded, _ = refit_all([recipe], _warmup_frame(n=1000, seed=99), existing=first_models)
    assert isinstance(unregridded.models[0], BehaviorModel2D)
    assert list(unregridded.models[0].axes[0].bin_edges) == list(first.axes[0].bin_edges)
    assert unregridded.models[0].grid_version == 1

    # --regrid names this recipe: edges recompute, grid_version bumps.
    regridded, _ = refit_all(
        [recipe],
        _warmup_frame(n=1000, seed=99),
        existing=first_models,
        regrid_recipes={"timing_vs_rpm_load"},
    )
    assert isinstance(regridded.models[0], BehaviorModel2D)
    assert regridded.models[0].grid_version == 2


# ---------------------------------------------------------------------------
# 4.5 — version bump only on change
# ---------------------------------------------------------------------------


def test_refit_all_keeps_version_on_identical_refit() -> None:
    frame = _warmup_frame(seed=5)
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        degree=2,
        min_samples=50,
    )
    first_models, first_results = refit_all([recipe], frame, existing=None)
    assert first_results[0].fitted
    assert first_models.models[0].version == 1

    # Refit again against byte-identical training rows — a no-op.
    second_models, second_results = refit_all([recipe], frame, existing=first_models)
    assert second_results[0].fitted
    assert second_models.models[0].version == 1  # not bumped


def test_refit_all_bumps_version_on_genuinely_different_data() -> None:
    recipe = Surface1DRecipe(
        name="coolant_warmup",
        output="obd:COOLANT_TEMP",
        x_column="obd:RUN_TIME",
        fit_method="poly",
        degree=2,
        min_samples=50,
    )
    first_models, _ = refit_all([recipe], _warmup_frame(seed=5), existing=None)
    second_models, results = refit_all([recipe], _warmup_frame(seed=6), existing=first_models)
    assert results[0].fitted
    assert second_models.models[0].version == 2


# ---------------------------------------------------------------------------
# 4.6 — warmup confounding decision input
# ---------------------------------------------------------------------------


def test_count_usable_cold_start_sessions_requires_cold_start_event() -> None:
    frame = pd.DataFrame(
        {
            MODE_COLUMN: ["warmup"] * 20,
            SESSION_COLUMN: ["s1"] * 20,
            "obd:ENGINE_LOAD": np.linspace(10, 60, 20),  # plenty of spread
        }
    )
    assert count_usable_cold_start_sessions(frame, {"s1": set()}) == 0  # no cold_start


def test_count_usable_cold_start_sessions_requires_spread() -> None:
    frame = pd.DataFrame(
        {
            MODE_COLUMN: ["warmup"] * 20,
            SESSION_COLUMN: ["s1"] * 20,
            "obd:ENGINE_LOAD": [20.0] * 20,  # flat
            "obd:INTAKE_TEMP": [22.0] * 20,  # flat
        }
    )
    assert count_usable_cold_start_sessions(frame, {"s1": {"cold_start"}}) == 0


def test_count_usable_cold_start_sessions_counts_usable_sessions() -> None:
    load_by_session = {
        "s0": np.linspace(10, 60, 20),  # 50% spread — usable
        "s1": [20.0] * 20,  # flat — not usable
        "s2": np.linspace(5, 80, 20),  # usable
    }
    rows = [
        {MODE_COLUMN: "warmup", SESSION_COLUMN: sid, "obd:ENGINE_LOAD": v}
        for sid, values in load_by_session.items()
        for v in values
    ]
    frame = pd.DataFrame(rows)
    session_events = {sid: {"cold_start"} for sid in load_by_session}
    assert count_usable_cold_start_sessions(frame, session_events) == 2


def test_choose_warmup_confounding_strategy_branches_at_floor() -> None:
    assert choose_warmup_confounding_strategy(7) == "widen_bands"
    assert choose_warmup_confounding_strategy(8) == "context_axis"
    assert choose_warmup_confounding_strategy(20) == "context_axis"


def test_robust_recipe_calibrates_on_a_whole_held_out_session() -> None:
    rows = []
    for session, center in (
        ("2026-08-01T100000", 0.0),
        ("2026-08-02T100000", 0.1),
        ("2026-08-03T100000", 0.05),
    ):
        rows.extend(
            {
                SESSION_COLUMN: session,
                "balance": center + (index % 3 - 1) * 0.01,
            }
            for index in range(60)
        )
    recipe = Surface1DRecipe(
        name="balance",
        output="balance",
        x_column="balance",
        fit_method="robust_baseline",
        min_r_squared=None,
        min_samples=50,
        min_sessions=3,
        min_session_samples=30,
        deviation=DeviationConfig(
            kind="sigma", warning_band=3.0, critical_band=6.0, sigma_floor=0.1
        ),
    )
    result = fit_recipe(recipe, pd.DataFrame(rows))
    assert result.fitted, result.reason
    assert result.model is not None
    assert result.model.fit.sessions == ["2026-08-01T100000", "2026-08-02T100000"]
    assert result.model.fit.calibration_sessions == ["2026-08-03T100000"]
    assert result.model.fit.validation_coverage == 1.0
