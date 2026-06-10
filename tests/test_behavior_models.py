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
        deviation=DeviationConfig(kind="relative", warning_band=3.0, critical_band=6.0),
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
            deviation=DeviationConfig(kind="relative", warning_band=1.0, critical_band=2.0),
        )


def test_relative_deviation_on_1d_with_reference_std_ok() -> None:
    model = BehaviorModel1D(
        name="x",
        kind="surface_1d",
        output="y",
        axis=Axis1D(name="t", channel="obd:RUN_TIME", domain=(0.0, 600.0)),
        fit=_poly_fit(),
        deviation=DeviationConfig(
            kind="relative", warning_band=1.0, critical_band=2.0, reference_std=4.0
        ),
    )
    assert model.deviation is not None
    assert model.deviation.reference_std == 4.0


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
