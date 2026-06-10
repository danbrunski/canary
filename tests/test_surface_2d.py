"""Tests for the 2D surface inferer (Task 4.E.3)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.analysis.surface_2d import evaluate_2d, fit_grid_binned_mean


def _fuel_surface_frame(n: int = 1000, seed: int = 0) -> pd.DataFrame:
    """Synthetic 'fuel pulse-width' surface: pw = 0.001*rpm + 0.5*maf + noise."""
    rng = np.random.default_rng(seed)
    rpm = rng.uniform(600, 4000, n)
    maf = rng.uniform(1, 30, n)
    pw = 0.001 * rpm + 0.5 * maf + rng.normal(0, 0.05, n)
    return pd.DataFrame({"rpm": rpm, "maf": maf, "pw": pw})


# ---------------------------------------------------------------------------
# fit_grid_binned_mean
# ---------------------------------------------------------------------------


def test_grid_fit_correct_cell_count_and_layout() -> None:
    frame = _fuel_surface_frame()
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="fuel_surface",
        output="ECM_INJECTOR_PW",
        x_edges=[600, 1500, 2500, 4000],  # 3 RPM bins
        y_edges=[1, 10, 20, 30],  # 3 MAF bins
    )
    assert len(model.fit.cells) == 3 * 3
    # row-major: cells[i*3 + j] is (rpm_bin i, maf_bin j)
    for cell in model.fit.cells:
        assert cell.count > 0  # uniform fill — every cell hit


def test_grid_fit_per_cell_means_track_underlying_relationship() -> None:
    frame = _fuel_surface_frame(n=5000)
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="fuel_surface",
        output="ECM_INJECTOR_PW",
        x_edges=[600, 1500, 2500, 4000],
        y_edges=[1, 10, 20, 30],
    )
    # Low-rpm low-maf cell should be much smaller than high-rpm high-maf cell.
    low = model.fit.cells[0]  # rpm bin 0, maf bin 0
    high = model.fit.cells[-1]  # rpm bin 2, maf bin 2
    assert low.mean < high.mean
    assert low.std > 0  # noise produces non-zero spread


def test_grid_fit_marks_empty_cells_with_count_zero() -> None:
    # Only the cell (0,0) is populated.
    frame = pd.DataFrame(
        {
            "rpm": [700.0] * 100,
            "maf": [5.0] * 100,
            "pw": [1.2] * 100,
        }
    )
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="f",
        output="pw",
        x_edges=[600, 1500, 2500, 4000],
        y_edges=[1, 10, 20, 30],
    )
    populated = [c for c in model.fit.cells if c.count > 0]
    assert len(populated) == 1
    assert populated[0].count == 100
    assert populated[0].mean == pytest.approx(1.2)
    # rest are zero-count
    empty = [c for c in model.fit.cells if c.count == 0]
    assert len(empty) == 8
    for cell in empty:
        assert cell.mean == 0.0
        assert cell.std == 0.0


def test_grid_fit_drops_nan_rows() -> None:
    frame = pd.DataFrame(
        {
            "rpm": [700, 1000, np.nan, 2000, 2500, 3000],
            "maf": [5, 10, 15, np.nan, 20, 25],
            "pw": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        }
    )
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="f",
        output="pw",
        x_edges=[600, 1500, 4000],
        y_edges=[1, 15, 30],
    )
    # 4 of 6 rows clean
    total = sum(c.count for c in model.fit.cells)
    assert total == 4


def test_grid_fit_drops_out_of_range_rows() -> None:
    frame = pd.DataFrame(
        {
            "rpm": [700, 1000, 5000, 1200],  # 5000 outside top edge
            "maf": [5, 10, 15, 35],  # 35 outside top edge
            "pw": [1.0, 2.0, 3.0, 4.0],
        }
    )
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="f",
        output="pw",
        x_edges=[600, 1500, 4000],
        y_edges=[1, 15, 30],
    )
    total = sum(c.count for c in model.fit.cells)
    assert total == 2  # 2 in-range rows


def test_grid_fit_rejects_non_increasing_edges() -> None:
    frame = _fuel_surface_frame()
    with pytest.raises(ValueError, match="strictly increasing"):
        fit_grid_binned_mean(
            frame,
            x_column="rpm",
            y_column="maf",
            output_column="pw",
            name="f",
            output="pw",
            x_edges=[600, 600, 1500],
            y_edges=[1, 10, 30],
        )


# ---------------------------------------------------------------------------
# evaluate_2d
# ---------------------------------------------------------------------------


def test_evaluate_2d_returns_cell_for_in_range_point() -> None:
    frame = _fuel_surface_frame(n=5000)
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="f",
        output="pw",
        x_edges=[600, 1500, 2500, 4000],
        y_edges=[1, 10, 20, 30],
    )
    cell = evaluate_2d(model, 700.0, 5.0)
    assert cell is not None
    # cell (0,0): rpm in [600,1500), maf in [1,10)
    # underlying: 0.001*~1050 + 0.5*~5 = ~3.55
    assert 2.5 < cell.mean < 4.0
    assert cell.count > 50


def test_evaluate_2d_returns_none_outside_grid() -> None:
    frame = _fuel_surface_frame(n=1000)
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="f",
        output="pw",
        x_edges=[600, 1500, 2500, 4000],
        y_edges=[1, 10, 20, 30],
    )
    assert evaluate_2d(model, 100.0, 5.0) is None  # below RPM range
    assert evaluate_2d(model, 5000.0, 5.0) is None  # above RPM range
    assert evaluate_2d(model, 700.0, 0.5) is None  # below MAF range
    assert evaluate_2d(model, 700.0, 35.0) is None  # above MAF range


def test_evaluate_2d_returns_none_for_empty_cell() -> None:
    # Only (0,0) populated; querying any other cell should return None.
    frame = pd.DataFrame(
        {
            "rpm": [700.0] * 100,
            "maf": [5.0] * 100,
            "pw": [1.2] * 100,
        }
    )
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="f",
        output="pw",
        x_edges=[600, 1500, 2500, 4000],
        y_edges=[1, 10, 20, 30],
    )
    # In (0,0) it returns the populated cell
    assert evaluate_2d(model, 700.0, 5.0) is not None
    # An unpopulated cell returns None
    assert evaluate_2d(model, 2000.0, 15.0) is None


def test_evaluate_2d_handles_right_edge_inclusively() -> None:
    frame = _fuel_surface_frame(n=2000)
    model = fit_grid_binned_mean(
        frame,
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="f",
        output="pw",
        x_edges=[600, 1500, 4000],
        y_edges=[1, 30],
    )
    # x = top edge should still map to a real cell, not None
    assert evaluate_2d(model, 4000.0, 15.0) is not None


# ---------------------------------------------------------------------------
# min_cell_count gate
# ---------------------------------------------------------------------------


def test_evaluate_2d_treats_sparse_cells_as_off_map() -> None:
    """Cells below ``min_cell_count`` evaluate as None — a six-sample mean is
    not an expectation worth alerting against."""
    frame = _fuel_surface_frame(n=2000)
    # Push almost everything into the low-rpm half so the top cell is sparse.
    sparse = frame[frame["rpm"] > 3950]  # a handful of rows
    dense = frame[frame["rpm"] <= 3950]
    assert 0 < len(sparse) < 30
    model = fit_grid_binned_mean(
        pd.concat([dense, sparse]),
        x_column="rpm",
        y_column="maf",
        output_column="pw",
        name="fuel_surface",
        output="ECM_INJECTOR_PW",
        x_edges=[600, 3950, 4000],
        y_edges=[1, 30],
        min_cell_count=30,
    )
    assert model.min_cell_count == 30
    assert evaluate_2d(model, 700.0, 15.0) is not None  # dense cell still grades
    assert evaluate_2d(model, 3975.0, 15.0) is None  # sparse cell is off-map
