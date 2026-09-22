"""Tests for the behavior-model schema and loader (Task 4.E.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from src.decode.behavior_models import (
    Axis1D,
    Axis2D,
    BehaviorModel1D,
    BehaviorModel2D,
    BehaviorModels,
    DeviationConfig,
    GridBinnedMeanFit,
    GridCell,
    LookupTableFit,
    PolynomialFit,
    dump_behavior_models,
    flatten_grid,
    load_behavior_models,
)

# ---------------------------------------------------------------------------
# Minimal fixtures used across the suite
# ---------------------------------------------------------------------------


def _poly_fit() -> PolynomialFit:
    return PolynomialFit(
        kind="poly",
        coefficients=[20.0, 0.15, -0.0001],
        sample_count=12_450,
        r_squared=0.97,
        sessions=["2026-05-13T005522"],
        fitted_at="2026-05-13T15:00:00Z",
        training_data_hash="sha256:abc",
    )


def _grid_fit(rows: int = 2, cols: int = 3) -> GridBinnedMeanFit:
    cells = [GridCell(mean=1.0, std=0.1, count=100) for _ in range(rows * cols)]
    return GridBinnedMeanFit(
        kind="grid_binned_mean",
        cells=cells,
        sample_count=600,
        sessions=["2026-05-13T222447"],
        fitted_at="2026-05-13T16:00:00Z",
        training_data_hash="sha256:xyz",
    )


def _model_1d(fit: PolynomialFit | LookupTableFit | None = None) -> BehaviorModel1D:
    return BehaviorModel1D(
        name="coolant_warmup",
        kind="surface_1d",
        output="ECM_COOLANT_TEMP",
        output_unit="degC",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", unit="s", domain=(0.0, 600.0)),
        fit=fit or _poly_fit(),
        deviation=DeviationConfig(kind="absolute", warning_band=5.0, critical_band=15.0),
    )


def _model_2d(rows: int = 2, cols: int = 3) -> BehaviorModel2D:
    return BehaviorModel2D(
        name="fuel_surface_idle",
        kind="surface_2d",
        output="ECM_INJECTOR_PW",
        output_unit="ms",
        axes=(
            Axis2D(name="rpm", channel="obd:RPM", unit="rpm", bin_edges=[500, 700, 900]),
            Axis2D(name="maf", channel="obd:MAF", unit="g/s", bin_edges=[2, 4, 6, 8]),
        ),
        fit=_grid_fit(rows=rows, cols=cols),
        operating_mode="idle",
        deviation=DeviationConfig(
            kind="relative", warning_band=3.0, critical_band=6.0, sigma_floor=0.5
        ),
    )


# ---------------------------------------------------------------------------
# Axis validation
# ---------------------------------------------------------------------------


def test_axis_1d_rejects_inverted_domain() -> None:
    with pytest.raises(ValidationError, match="domain"):
        Axis1D(name="t", channel="obd:RUN_TIME", domain=(600.0, 0.0))


def test_axis_2d_rejects_non_increasing_bin_edges() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        Axis2D(name="rpm", channel="obd:RPM", bin_edges=[500, 500, 700])
    with pytest.raises(ValidationError, match="strictly increasing"):
        Axis2D(name="rpm", channel="obd:RPM", bin_edges=[700, 500])


def test_axis_2d_bin_count() -> None:
    axis = Axis2D(name="rpm", channel="obd:RPM", bin_edges=[500, 700, 900, 1100])
    assert axis.bin_count == 3


# ---------------------------------------------------------------------------
# Fit validation
# ---------------------------------------------------------------------------


def test_lookup_table_fit_rejects_length_mismatch() -> None:
    with pytest.raises(ValidationError, match="match length"):
        LookupTableFit(
            kind="lookup_table",
            x_knots=[0.0, 10.0, 20.0],
            y_knots=[0.0, 50.0],
            sample_count=100,
            fitted_at="now",
        )


def test_lookup_table_fit_rejects_non_increasing_x() -> None:
    with pytest.raises(ValidationError, match="strictly increasing"):
        LookupTableFit(
            kind="lookup_table",
            x_knots=[0.0, 10.0, 5.0],
            y_knots=[0.0, 50.0, 25.0],
            sample_count=100,
            fitted_at="now",
        )


# ---------------------------------------------------------------------------
# Deviation validation
# ---------------------------------------------------------------------------


def test_deviation_requires_at_least_one_band() -> None:
    with pytest.raises(ValidationError, match="warning_band / critical_band"):
        DeviationConfig(kind="absolute")


def test_deviation_warning_must_be_less_than_critical() -> None:
    with pytest.raises(ValidationError, match="warning_band must be"):
        DeviationConfig(kind="absolute", warning_band=10.0, critical_band=5.0)


def test_relative_deviation_on_1d_needs_reference_std() -> None:
    with pytest.raises(ValidationError, match="reference_std"):
        BehaviorModel1D(
            name="x",
            kind="surface_1d",
            output="y",
            axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
            fit=_poly_fit(),
            deviation=DeviationConfig(
                kind="relative", warning_band=1.0, critical_band=2.0, sigma_floor=0.1
            ),
        )


def test_relative_deviation_on_1d_with_reference_std_ok() -> None:
    model = BehaviorModel1D(
        name="x",
        kind="surface_1d",
        output="y",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
        fit=_poly_fit(),
        deviation=DeviationConfig(
            kind="relative",
            warning_band=1.0,
            critical_band=2.0,
            reference_std=4.0,
            sigma_floor=0.1,
        ),
    )
    assert model.deviation is not None
    assert model.deviation.reference_std == 4.0


def test_deviation_config_relative_requires_positive_sigma_floor() -> None:
    """4.2 — schema-enforced: kind='relative' without a sigma_floor (or a
    non-positive one) is a live hair-trigger, not a valid config."""
    with pytest.raises(ValidationError, match="sigma_floor"):
        DeviationConfig(kind="relative", warning_band=1.0, critical_band=2.0)
    with pytest.raises(ValidationError, match="sigma_floor"):
        DeviationConfig(kind="relative", warning_band=1.0, critical_band=2.0, sigma_floor=0.0)
    # absolute kind is unaffected — no floor required.
    DeviationConfig(kind="absolute", warning_band=1.0, critical_band=2.0)


def test_deviation_config_sigma_and_legacy_relative_alias_load_identically(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """9.4 — ``sigma`` is the canonical name (was ``relative``, which invited
    reading it as percent-of-expected instead of a z-score). The deprecated
    ``relative`` string must still load (existing behavior_models.yaml
    artifacts and Pi deploys use it) and normalize to the exact same
    validated config, with a deprecation warning logged."""
    canonical = DeviationConfig(kind="sigma", warning_band=1.0, critical_band=2.0, sigma_floor=0.1)
    assert canonical.kind == "sigma"

    with caplog.at_level("WARNING"):
        aliased = DeviationConfig(
            kind="relative", warning_band=1.0, critical_band=2.0, sigma_floor=0.1
        )
    assert aliased.kind == "sigma"  # normalized, not left as "relative"
    assert aliased == canonical  # identical validated config either way
    assert any("deprecated" in record.message for record in caplog.records)

    # An unrecognized kind is still rejected — the alias is a narrow,
    # explicit exception, not a loosened literal.
    with pytest.raises(ValidationError):
        DeviationConfig(kind="percent", warning_band=1.0, critical_band=2.0)


# ---------------------------------------------------------------------------
# 2.1 — directional deviation kind
# ---------------------------------------------------------------------------


def _directional(**overrides: object) -> DeviationConfig:
    defaults: dict[str, object] = {
        "kind": "directional",
        "tolerance": 3.0,
        "worse_direction": "positive",
        "warning_band": 3.0,
        "critical_band": 6.0,
    }
    defaults.update(overrides)
    return DeviationConfig(**defaults)  # type: ignore[arg-type]


def test_directional_requires_tolerance() -> None:
    with pytest.raises(ValidationError, match="tolerance"):
        DeviationConfig(
            kind="directional", worse_direction="positive", warning_band=3.0, critical_band=6.0
        )


def test_directional_requires_worse_direction() -> None:
    with pytest.raises(ValidationError, match="worse_direction"):
        DeviationConfig(kind="directional", tolerance=3.0, warning_band=3.0, critical_band=6.0)


def test_directional_tolerance_cannot_exceed_smallest_band() -> None:
    with pytest.raises(ValidationError, match="tolerance"):
        _directional(tolerance=10.0, warning_band=3.0, critical_band=6.0)


def test_directional_better_direction_defaults_to_ignore() -> None:
    cfg = _directional()
    assert cfg.better_direction == "ignore"


def test_directional_std_scaled_requires_positive_sigma_floor() -> None:
    with pytest.raises(ValidationError, match="sigma_floor"):
        _directional(std_scaled=True)
    _directional(std_scaled=True, sigma_floor=0.1)  # ok with a floor


def test_directional_std_scaled_on_1d_needs_reference_std() -> None:
    with pytest.raises(ValidationError, match="reference_std"):
        BehaviorModel1D(
            name="x",
            kind="surface_1d",
            output="y",
            axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
            fit=_poly_fit(),
            deviation=_directional(std_scaled=True, sigma_floor=0.1),
        )
    BehaviorModel1D(
        name="x",
        kind="surface_1d",
        output="y",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
        fit=_poly_fit(),
        deviation=_directional(std_scaled=True, sigma_floor=0.1, reference_std=1.0),
    )


# ---------------------------------------------------------------------------
# 2D cell-count enforcement
# ---------------------------------------------------------------------------


def test_behavior_model_2d_rejects_cell_count_mismatch() -> None:
    # 2 RPM bins × 3 MAF bins = 6 expected, only give 5.
    with pytest.raises(ValidationError, match="expected 6 cells"):
        _model_2d(rows=2, cols=2)  # 4 cells but 2×3 = 6


def test_behavior_model_2d_accepts_correct_cell_count() -> None:
    model = _model_2d(rows=2, cols=3)
    assert len(model.fit.cells) == 6


# ---------------------------------------------------------------------------
# Round-trip via YAML
# ---------------------------------------------------------------------------


def test_yaml_round_trip(tmp_path: Path) -> None:
    models = BehaviorModels(schema_version=1, models=[_model_1d(), _model_2d()])
    path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(models, path)

    loaded = load_behavior_models(path)
    assert loaded.schema_version == 1
    assert len(loaded.models) == 2
    assert loaded.by_name("coolant_warmup") is not None
    fuel = loaded.by_name("fuel_surface_idle")
    assert fuel is not None and fuel.kind == "surface_2d"


def test_yaml_round_trip_preserves_serving_contract(tmp_path: Path) -> None:
    """B01.6 — a schema-2 contract survives dump/load exactly."""
    from src.decode.behavior_models import (
        CURRENT_SCHEMA_VERSION,
        ColdStartRequirement,
        EngineStateRequirement,
        FuelControlRequirement,
        RequiredContext,
        ServingEligibility,
        TrainingSelection,
    )

    contract = ServingEligibility(
        engine_state=EngineStateRequirement(state="engine_running", min_running_rpm=400.0),
        fuel_control=FuelControlRequirement(allowed_states=[2]),
        cold_start=ColdStartRequirement(max_initial_coolant_c=40.0),
        required_context=[RequiredContext(any_of=["obd:INTAKE_TEMP", "decoded:IAT"])],
        training_selection=TrainingSelection(
            require_session_events=["cold_start"], require_contiguous_runtime=True, min_sessions=3
        ),
    )
    model = _model_1d().model_copy(update={"eligibility": contract})
    path = tmp_path / "behavior_models.yaml"
    dump_behavior_models(
        BehaviorModels(schema_version=CURRENT_SCHEMA_VERSION, models=[model, _model_2d()]), path
    )
    loaded = load_behavior_models(path)
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION
    assert loaded.models[0].eligibility == contract
    assert loaded.models[1].eligibility is None  # legacy model in a v2 document


def test_fuel_control_rejects_non_j1979_states() -> None:
    from src.decode.behavior_models import FuelControlRequirement

    with pytest.raises(ValidationError, match="J1979"):
        FuelControlRequirement(allowed_states=[3])


def test_shipped_artifact_loads_diagnostic_only() -> None:
    """B01.6 — the shipped schema-1 artifacts predate the serving contract;
    they load, but none is presented as a trusted alert source."""
    from src.decode.behavior_models import STATUS_LEGACY_NO_CONTRACT, diagnostic_only_models

    path = Path(__file__).resolve().parents[1] / "data/nissan-xterra-2008/behavior_models.yaml"
    models = load_behavior_models(path)
    diagnostic = diagnostic_only_models(models)
    assert set(diagnostic) == {model.name for model in models.models}
    assert all(STATUS_LEGACY_NO_CONTRACT in reasons for reasons in diagnostic.values())


def test_shipped_air_charge_surface_is_revision_scoped() -> None:
    path = Path(__file__).resolve().parents[1] / "data/nissan-xterra-2008/behavior_models.yaml"
    surface = load_behavior_models(path).by_name("air_charge_vs_rpm_throttle_did")

    assert surface is not None
    assert surface.fit.tune_revision == "ZS44A-k106"
    assert surface.fit.validation_sample_count >= 1_000
    assert surface.fit.validation_warning_rate is not None
    assert surface.fit.validation_warning_rate < 0.03


def test_load_rejects_unknown_field(tmp_path: Path) -> None:
    bad = {
        "schema_version": 1,
        "models": [
            {
                "name": "x",
                "kind": "surface_1d",
                "output": "y",
                "axis": {"name": "t", "channel": "obd:RUN_TIME", "domain": [0.0, 600.0]},
                "fit": {
                    "kind": "poly",
                    "coefficients": [1.0],
                    "sample_count": 1,
                    "fitted_at": "now",
                },
                "BOGUS": True,
            }
        ],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError, match="BOGUS"):
        load_behavior_models(path)


def test_load_picks_correct_discriminator(tmp_path: Path) -> None:
    # A surface_2d entry must use the 2D shape (`axes`, not `axis`) — the
    # Literal discriminator forces the right model class.
    bad = {
        "schema_version": 1,
        "models": [
            {
                "name": "wrong",
                "kind": "surface_2d",
                "output": "y",
                "axis": {  # ← should be `axes`, this is the 1D shape
                    "name": "t",
                    "channel": "obd:RUN_TIME",
                    "domain": [0.0, 600.0],
                },
                "fit": {
                    "kind": "grid_binned_mean",
                    "cells": [],
                    "sample_count": 0,
                    "fitted_at": "now",
                },
            }
        ],
    }
    path = tmp_path / "wrong.yaml"
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError):
        load_behavior_models(path)


# ---------------------------------------------------------------------------
# flatten_grid helper
# ---------------------------------------------------------------------------


def test_flatten_grid_row_major() -> None:
    cells = [
        [GridCell(mean=1, std=0, count=1), GridCell(mean=2, std=0, count=1)],
        [GridCell(mean=3, std=0, count=1), GridCell(mean=4, std=0, count=1)],
    ]
    flat = flatten_grid(cells)
    assert [c.mean for c in flat] == [1, 2, 3, 4]


def test_flatten_grid_rejects_ragged() -> None:
    cells = [
        [GridCell(mean=1, std=0, count=1), GridCell(mean=2, std=0, count=1)],
        [GridCell(mean=3, std=0, count=1)],
    ]
    with pytest.raises(ValueError, match="width"):
        flatten_grid(cells)


def test_flatten_grid_rejects_empty() -> None:
    with pytest.raises(ValueError, match="at least one row"):
        flatten_grid([])
    with pytest.raises(ValueError, match="at least one cell"):
        flatten_grid([[]])


def test_unknown_operating_modes_flags_unservable_models() -> None:
    """A catalog model gated on a mode the segmenter can't emit silently
    never evaluates — the loader-side check makes that loud."""
    from src.decode.behavior_models import unknown_operating_modes

    models = BehaviorModels(
        schema_version=1,
        models=[
            _model_1d().model_copy(update={"name": "coolant_warmup", "operating_mode": "warmup"}),
            _model_1d().model_copy(update={"name": "idle_rpm", "operating_mode": "idle"}),
            _model_1d().model_copy(update={"name": "ungated", "operating_mode": None}),
        ],
    )
    assert unknown_operating_modes(models, {"idle", "city", "highway"}) == {
        "coolant_warmup": "warmup"
    }
    assert unknown_operating_modes(models, {"idle", "warmup"}) == {}
