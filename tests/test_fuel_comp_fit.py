"""N02.5 / N02.6: the per-cell proposal, its validation, the knock-on, the report.

Synthetic throughout (no private ROM): hand-built design rows test the
solver -- a known injected cell pattern is recovered, sparse cells are
refused by name, a MAF-shaped error is called MAF-shaped and a pattern-free
one "no improvement" -- and invented calibrations test the required factor
and every sample refusal. The knock-on reads the committed Target AFR table
through the target-phi twin. The last tests pin that no code path writes or
exports a ROM: the tool's outputs are JSON, Markdown and Parquet only, and
no N02 module can write image bytes.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.analysis.ecu_application import load_application_model
from src.analysis.fuel_comp_droop import PressureSession
from src.analysis.fuel_comp_fit import (
    DIAGNOSTIC_CELLS,
    DIAGNOSTIC_MAF,
    DIAGNOSTIC_UNIFORM,
    MODELS,
    PREPARED_COLUMNS,
    SIDE,
    VERDICT_IMPROVEMENT,
    VERDICT_NONE,
    design_of,
    fit_both_ways,
    fit_table,
    prepare_fit_samples,
    validate,
)
from src.analysis.fuel_comp_inputs import (
    FitRules,
    FuelCompDeclaration,
    assign_drives,
    load_fuel_comp_declaration,
    load_interval,
)
from src.analysis.fuel_comp_knock_on import KnockOnTables, knock_on
from src.analysis.fuel_comp_report import (
    OUTPUT_SUFFIXES,
    PROPOSAL_HEADER,
    OutputRefusedError,
    build_result,
    render_markdown,
    write_outputs,
)
from src.analysis.fuel_comp_rom import FuelCompCalibration
from src.analysis.fuel_comp_samples import COLUMNS
from src.analysis.rom_trace import load_viewer_definition
from src.analysis.target_phi import load_twin_definition, predict, table_from_calibration
from src.decode.ecm_lookup import lookup_2d
from src.decode.fuel_comp_load import Interval
from src.signals.afr import load_afr_calibration
from tests.rom_fixtures import PROFILE
from tests.test_fuel_comp_samples import _calibration

REPO = Path(__file__).resolve().parents[1]
T0 = pd.Timestamp("2026-09-01T12:00:00Z")


@pytest.fixture(scope="module")
def declaration() -> FuelCompDeclaration:
    return load_fuel_comp_declaration(PROFILE)


@pytest.fixture(scope="module")
def rules(declaration: FuelCompDeclaration) -> FitRules:
    return declaration.fit


def _target(cells: np.ndarray | None = None) -> FuelCompCalibration:
    table = np.full((SIDE, SIDE), 128) if cells is None else cells.reshape(SIDE, SIDE)
    return dataclasses.replace(
        _calibration("target"),
        fuel_comp=tuple(tuple(int(v) for v in row) for row in table),
        k=28715,
        image_sha256="t" * 64,
    )


def _logged() -> FuelCompCalibration:
    """The k106 shape: load axis compressed to 16..144, a different K."""
    load_axis = (16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 144)
    return dataclasses.replace(
        _calibration("logged"), load_axis_raw=load_axis, k=28000, image_sha256="l" * 64
    )


# -- the design and the solver, on hand-built rows ----------------------------------------


def _prepared(rows: list[dict[str, Any]]) -> pd.DataFrame:
    base: dict[str, Any] = {name: np.nan for name in PREPARED_COLUMNS}
    base.update(route="closed_loop", unvalidated_route=False, fit_refusal=None, k_now=28715)
    rows = [{"drive": row.get("session"), **row} for row in rows]
    frame = pd.DataFrame([{**base, **row} for row in rows], columns=list(PREPARED_COLUMNS))
    frame["fit_refusal"] = frame["fit_refusal"].astype(object).where(frame["fit_refusal"].notna())
    return frame


def _bilinear(table: np.ndarray, r: int, fr: float, c: int, fc: float) -> float:
    t = table.reshape(SIDE, SIDE).astype(float)
    r1, c1 = min(r + 1, SIDE - 1), min(c + 1, SIDE - 1)
    top = t[r, c] * (1 - fc) + t[r, c1] * fc
    bottom = t[r1, c] * (1 - fc) + t[r1, c1] * fc
    return float(top * (1 - fr) + bottom * fr)


def test_the_design_row_is_the_bilinear_weights() -> None:
    frame = _prepared(
        [
            {"row_index": 3, "row_fraction": 0.25, "col_index": 5, "col_fraction": 0.5},
            {"row_index": 7, "row_fraction": 0.0, "col_index": 6, "col_fraction": 0.0},
        ]
    )
    design = design_of(frame)
    first = {
        int(c): float(w)
        for s, c, w in zip(design.sample, design.cell, design.weight, strict=True)
        if s == 0
    }
    assert first == pytest.approx(
        {3 * 16 + 5: 0.375, 3 * 16 + 6: 0.375, 4 * 16 + 5: 0.125, 4 * 16 + 6: 0.125}
    )
    on_breakpoint = [int(c) for s, c in zip(design.sample, design.cell, strict=True) if s == 1]
    assert on_breakpoint == [7 * 16 + 6]
    table = np.arange(256, dtype=float)
    assert design.predict(table)[0] == pytest.approx(_bilinear(table, 3, 0.25, 5, 0.5))


PATTERN = {(5, 7): 1.04, (5, 8): 1.035, (6, 7): 0.97, (6, 8): 1.0, (7, 8): 1.02}


def _pattern_rows(
    seed: int = 1,
    *,
    drives: int = 6,
    per_drive: int = 80,
    noise_pct: float = 0.4,
    shape: str = "cells",
) -> tuple[list[dict[str, Any]], np.ndarray, np.ndarray]:
    """Samples spread over rows 5-7 x columns 7-8 on six drives; the required
    factor is the true table there (``cells``) or the current one times a
    MAF step (``maf``), plus noise. MAF is drawn independently of position."""
    rng = np.random.default_rng(seed)
    current = 128 + (np.arange(256) % 5) - 2
    true = current.astype(float).copy()
    if shape == "cells":
        for (r, c), factor in PATTERN.items():
            true[r * SIDE + c] = current[r * SIDE + c] * factor
    rows = []
    for d in range(drives):
        for _ in range(per_drive):
            r, fr = int(rng.integers(5, 7)), float(rng.uniform(0, 1))
            c, fc = 7, float(rng.uniform(0, 1))
            maf = float(rng.uniform(5, 120))
            value = _bilinear(true, r, fr, c, fc)
            if shape == "maf":
                value *= 1.04 if maf > 60 else 0.99
            elif shape == "uniform":
                value *= 0.983
            rows.append(
                {
                    "session": f"drive-{d}",
                    "row_index": r,
                    "row_fraction": fr,
                    "col_index": c,
                    "col_fraction": fc,
                    "maf_gs": maf,
                    "required_raw": value * (1 + rng.normal(0, noise_pct / 100)),
                }
            )
    return rows, current, true


def test_a_known_injected_cell_pattern_is_recovered(rules: FitRules) -> None:
    rows, current, true = _pattern_rows()
    fit = fit_table(_prepared(rows), _target(current), rules, include_unvalidated=False)
    counts = fit.status_counts()
    assert sum(counts.values()) == 256
    block = [r * SIDE + c for r in (5, 6, 7) for c in (7, 8)]
    for k in block:
        cell = fit.cells[k]
        assert cell.status == "proposed", (k, cell.refusals)
        assert cell.fitted_raw == pytest.approx(true[k], abs=0.5), k
        assert cell.ci_raw is not None and len(cell.drives) == 6
    # The injected 4 % and -3 % steps are resolved; the unchanged cell is not.
    assert fit.cells[5 * SIDE + 7].change_resolved is True
    assert fit.cells[6 * SIDE + 7].change_resolved is True
    assert fit.cells[6 * SIDE + 8].change_pct == pytest.approx(0.0, abs=0.4)
    assert counts["not_covered"] == 256 - len(block)


def test_sparse_cells_are_refused_by_name(rules: FitRules) -> None:
    rows = [
        # Three samples from one drive: both refusals.
        *(
            {
                "session": "d0",
                "row_index": 2,
                "row_fraction": 0.0,
                "col_index": 2,
                "col_fraction": 0.0,
                "required_raw": 130.0,
            }
            for _ in range(3)
        ),
        # Plenty from one drive only: two drives are required.
        *(
            {
                "session": "d1",
                "row_index": 9,
                "row_fraction": 0.0,
                "col_index": 9,
                "col_fraction": 0.0,
                "required_raw": 131.0,
            }
            for _ in range(40)
        ),
        # 30 samples, but drive d3 contributes less than one weighted pair.
        *(
            {
                "session": "d2",
                "row_index": 11,
                "row_fraction": 0.0,
                "col_index": 4,
                "col_fraction": 0.0,
                "required_raw": 131.0,
            }
            for _ in range(30)
        ),
        {
            "session": "d3",
            "row_index": 11,
            "row_fraction": 0.0,
            "col_index": 4,
            "col_fraction": 0.5,
            "required_raw": 131.0,
        },
    ]
    fit = fit_table(_prepared(rows), _target(), rules, include_unvalidated=False)
    sparse = fit.cells[2 * SIDE + 2]
    assert sparse.status == "insufficient" and sparse.n == 3
    assert [r.split(":")[0] for r in sparse.refusals] == ["min_weighted_pairs", "min_drives"]
    lonely = fit.cells[9 * SIDE + 9]
    assert lonely.status == "insufficient" and lonely.drives == ["d1"]
    assert [r.split(":")[0] for r in lonely.refusals] == ["min_drives"]
    token = fit.cells[11 * SIDE + 4]
    assert token.status == "insufficient" and token.drives == ["d2"]
    assert fit.cells[0].status == "not_covered" and fit.cells[0].fitted_raw is None
    assert fit.status_counts()["proposed"] == 0


def test_the_unvalidated_route_is_excluded_by_default_and_flagged_when_included(
    rules: FitRules,
) -> None:
    rows, current, _ = _pattern_rows(drives=3, per_drive=30)
    for row in rows:
        row["unvalidated_route"] = True
    both = fit_both_ways(_prepared(rows), _target(current), rules)
    excluded, included = both["did_route_excluded"], both["did_route_included"]
    assert excluded.status_counts()["proposed"] == 0
    assert excluded.excluded_unvalidated == len(rows)
    proposed = [c for c in included.cells if c.status == "proposed"]
    assert proposed and all(c.as_dict()["unvalidated_route"] for c in proposed)
    assert all(c.unvalidated_weight_share == pytest.approx(1.0) for c in proposed)


def test_held_out_drives_improve_on_a_cell_shaped_error(rules: FitRules) -> None:
    rows, current, _ = _pattern_rows()
    result = validate(_prepared(rows), _target(current), rules, include_unvalidated=False)
    assert result.folds == 6 and result.folds_with_a_proposal == 6
    assert result.verdict == VERDICT_IMPROVEMENT
    cells = result.models["fitted_cells"]
    assert cells["drives_better_than_current"] > cells["drives_worse_than_current"]
    assert result.rms_cells_pct is not None and result.rms_current_pct is not None
    assert result.rms_cells_pct < result.rms_current_pct
    assert result.diagnostic == DIAGNOSTIC_CELLS
    assert result.comparisons["fitted_cells<global_factor"] >= 0.95
    # Every model reports its size per fold and its per-drive record.
    assert result.models["global_factor"]["parameters_per_fold"]["max"] == 1
    assert result.models["fitted_cells"]["parameters_per_fold"]["min"] >= 5
    assert set(result.models) == set(MODELS)


def test_a_maf_shaped_error_is_called_maf_shaped(rules: FitRules) -> None:
    rows, current, _ = _pattern_rows(shape="maf")
    result = validate(_prepared(rows), _target(current), rules, include_unvalidated=False)
    assert result.diagnostic == DIAGNOSTIC_MAF
    assert result.rms_maf_bins_pct is not None and result.rms_cells_pct is not None
    assert result.rms_maf_bins_pct < result.rms_cells_pct
    assert result.comparisons["maf_bins<global_factor"] >= 0.95


def test_a_uniform_offset_is_not_called_maf_shaped(rules: FitRules) -> None:
    """Every sample 1.7 % rich of the current table: one global factor
    explains it, so neither the bins nor the cells may claim it."""
    rows, current, _ = _pattern_rows(shape="uniform")
    result = validate(_prepared(rows), _target(current), rules, include_unvalidated=False)
    assert result.diagnostic == DIAGNOSTIC_UNIFORM
    assert result.comparisons["global_factor<current_table"] >= 0.95
    assert result.comparisons["maf_bins<global_factor"] < 0.95
    assert result.models["global_factor"]["mean_signed_residual_pct"] == pytest.approx(0, abs=0.3)
    assert result.models["current_table"]["mean_signed_residual_pct"] == pytest.approx(
        -1.7, abs=0.3
    )


def test_sessions_of_one_drive_count_once(rules: FitRules) -> None:
    """Three back-to-back sessions of one outing are one drive for
    min_drives and the folds: two outings are not three drives."""
    rows = [
        {
            "session": s,
            "drive": d,
            "row_index": 4,
            "row_fraction": 0.0,
            "col_index": 4,
            "col_fraction": 0.0,
            "required_raw": 130.0,
        }
        for s, d in (("a1", "A"), ("a2", "A"), ("a3", "A"))
        for _ in range(10)
    ]
    fit = fit_table(_prepared(rows), _target(), rules, include_unvalidated=False)
    cell = fit.cells[4 * SIDE + 4]
    assert cell.status == "insufficient" and cell.drives == ["A"]
    rows += [{**rows[0], "session": "b1", "drive": "B"} for _ in range(10)]
    fit = fit_table(_prepared(rows), _target(), rules, include_unvalidated=False)
    assert fit.cells[4 * SIDE + 4].drives == ["A", "B"]
    assert validate(_prepared(rows), _target(), rules, include_unvalidated=False).folds == 2


def test_drives_merge_sessions_closer_than_the_declared_gap(
    declaration: FuelCompDeclaration,
) -> None:
    unit = declaration.fit.drive_unit
    gap = unit.max_gap_minutes * 60
    spans = {
        "s1": (0.0, 600.0),
        "s2": (600.0 + 70, 1200.0),  # a reboot, 70 s later
        "s3": (1150.0, 1300.0),  # overlapping capture
        "s4": (1300.0 + gap + 1, 2000.0),
    }
    drives = assign_drives(spans, unit)
    assert drives == {"s1": "s1..s3", "s2": "s1..s3", "s3": "s1..s3", "s4": "s4"}


def test_no_pattern_is_reported_as_no_improvement(rules: FitRules) -> None:
    rows, current, _ = _pattern_rows(shape="none", noise_pct=1.0)
    result = validate(_prepared(rows), _target(current), rules, include_unvalidated=False)
    assert result.verdict.startswith(VERDICT_NONE)
    one_drive = [r for r in rows if r["session"] == "drive-0"]
    alone = validate(_prepared(one_drive), _target(current), rules, include_unvalidated=False)
    assert alone.verdict.startswith(VERDICT_NONE) and "not evaluable" in alone.verdict


# -- the required factor and the sample refusals, through prepare --------------------------


def _sample_frame(
    declaration: FuelCompDeclaration,
    calibration: FuelCompCalibration,
    specs: list[dict[str, Any]],
) -> pd.DataFrame:
    """N02.3-shaped rows (every column), with droop columns, from raw inputs."""
    idle = declaration.inputs.idle_air
    rows = []
    for i, spec in enumerate(specs):
        row: dict[str, Any] = {name: np.nan for name in COLUMNS}
        word = spec.get("word", 256)
        counts = Interval(*spec.get("counts", (300, 302)))
        purge = Interval(0, 0)
        interval = load_interval(
            throttle_counts=counts,
            rpm_words=[word],
            idle_air=idle.interval,
            purge=purge,
            calibration=calibration,
        )
        row.update(
            session=spec.get("session", "s0"),
            route="closed_loop",
            timestamp=T0 + pd.Timedelta(seconds=i),
            revision=spec.get("revision", calibration.name),
            image_sha256=calibration.image_sha256,
            state=spec.get("state", "eligible"),
            throttle_route="pid_0x11",
            throttle_validated=True,
            rpm=word * 12.5,
            rpm_word=word,
            rpm_word_lo=word,
            rpm_word_hi=word,
            throttle_counts_lo=counts.lo,
            throttle_counts_hi=counts.hi,
            purge_area_lo=0,
            purge_area_hi=0,
            byte_lo=interval.byte.lo,
            byte_hi=interval.byte.hi,
            cell_ambiguous=spec.get("ambiguous", False),
            idle_air_moves_cell=spec.get("ambiguous", False),
            purge_moves_cell=False,
            throttle_moves_cell=False,
            rpm_moves_cell=False,
            error_pct=spec.get("error", 3.0),
            maf_gs=spec.get("maf", 40.0),
            bfs_raw=spec.get("bfs", np.nan),
            droop_kind=spec.get("droop", "modelled"),
            droop_basis="modelled (test)" if spec.get("droop", "modelled") != "none" else "x",
            remainder_pct=spec.get("remainder", 2.0),
        )
        rows.append(row)
    return pd.DataFrame(rows)


def test_the_required_factor_uses_the_logged_table_and_both_ks(
    declaration: FuelCompDeclaration,
) -> None:
    logged = dataclasses.replace(
        _logged(), fuel_comp=tuple(tuple(120 + c for c in range(16)) for _ in range(16))
    )
    target = _target()
    frame = _sample_frame(declaration, logged, [{"remainder": 2.0}])
    prepared = prepare_fit_samples(frame, {"logged": logged}, target, declaration)
    row = prepared.iloc[0]
    assert row["fit_refusal"] is None
    expected_fc = lookup_2d(
        logged.fuel_comp, logged.rpm_breakpoints, logged.load_axis_raw, 256, int(row["byte_logged"])
    ).value
    assert row["fc_applied"] == expected_fc
    assert row["fc_applied_lo"] <= expected_fc <= row["fc_applied_hi"]
    assert row["required_raw"] == pytest.approx(expected_fc * 1.02 * 28000 / 28715)
    # Placed on the target's axes, not the logged ones.
    assert row["col_index"] != pd.Series(logged.load_axis_raw).searchsorted(row["byte_target"])


def test_every_refused_sample_names_why(declaration: FuelCompDeclaration) -> None:
    logged = _logged()
    frame = _sample_frame(
        declaration,
        logged,
        [
            {"droop": "none"},
            {"ambiguous": True},
            {"revision": "unknown"},
            {"state": "gated"},
            {"remainder": np.nan},
            {},
        ],
    )
    prepared = prepare_fit_samples(frame, {"logged": logged}, _target(), declaration)
    assert len(prepared) == 5  # the gated row is not a fit candidate at all
    reasons = list(prepared["fit_refusal"])
    assert reasons[0].startswith("droop_none")
    assert reasons[1] == "cell_ambiguous: idle_air"
    assert reasons[2].startswith("rom_image_unresolved")
    assert reasons[3].startswith("required_factor_missing")
    assert reasons[4] is None


# -- the knock-on -------------------------------------------------------------------------


def _knock_tables(target: FuelCompCalibration) -> KnockOnTables:
    twin, _ = load_twin_definition(PROFILE)
    table = table_from_calibration(
        load_afr_calibration(PROFILE / "afr_calibration.yaml"),
        twin,
        viewer=load_viewer_definition(PROFILE),
        application_model=load_application_model(PROFILE),
        revision="ZS44A-20260920",
        image_sha256="a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50",
        slot=1,
    )
    return KnockOnTables(target, table, twin, tuple(range(8, 136, 8)), 44019)


def test_the_knock_on_predicts_bfs_x_phi_and_the_timing_column(
    declaration: FuelCompDeclaration, rules: FitRules
) -> None:
    """Cell (7, 6) asks for +4 %; a sample at BFS 33,874 sits at X 98, so the
    shift to X 102 crosses Target AFR's 100 breakpoint but no timing column."""
    target = _target()
    rows = [
        {
            "session": f"d{d}",
            "row_index": 7,
            "row_fraction": 0.0,
            "col_index": 6,
            "col_fraction": 0.0,
            "required_raw": 128 * 1.04,
            "rpm_word": 256,
            "byte_target": 112,
            "fc_applied": 128,
            "k_logged": 28715,
            "bfs_raw": 33874.0,
            "maf_gs": 50.0,
        }
        for d in range(3)
        for _ in range(6)
    ]
    rows.append({**rows[0], "bfs_raw": np.nan, "maf_gs": np.nan})
    prepared = _prepared(rows)
    fit = fit_table(prepared, target, rules, include_unvalidated=False)
    assert fit.cells[7 * SIDE + 6].proposed_raw == 133
    tables = _knock_tables(target)
    block = knock_on(prepared, fit, tables, declaration)
    assert block["empty"] is None
    predictions = block["predictions"]
    measured = predictions[predictions["bfs_kind"] == "measured"].iloc[0]
    assert measured["bfs_ratio"] == pytest.approx(133 / 128)
    assert (measured["x_now"], measured["x_proposed"]) == (98, 102)
    assert bool(measured["crosses_target_afr_breakpoint"]) is True
    assert bool(measured["crosses_timing_breakpoint"]) is False
    twin = tables.twin
    expected = predict(tables.target_afr, twin, load_raw=102, rpm_raw=256, flag_raw=0).phi
    assert measured["phi_proposed"] == expected
    # Neither BFS nor MAF read: not predicted, and it says why.
    missing = predictions[predictions["bfs_kind"] == "none"].iloc[0]
    assert "not paired" in missing["bfs_reason"]
    (cell,) = block["cells"]
    assert cell["cell"] == [7, 6] and cell["crosses_target_afr_breakpoint"] == 18
    assert cell["bfs_not_predicted"] == 1
    assert block["bfs_model"]["refusal"].startswith("bfs_model_refused")


# -- the report, and that nothing writes a ROM ------------------------------------------------


def _pressure(name: str, kpa_per_psi: float) -> PressureSession:
    tp = np.arange(0, 120, 0.01)
    tm = np.arange(0.0, 120, 0.2)

    def maf(t: np.ndarray) -> np.ndarray:
        return np.where(t < 30, 3.0, 3.0 + (t - 30) * 1.3)

    flow = maf(tp)
    return PressureSession(
        session=name,
        pressure_times=1.79e9 + tp,
        pressure_kpa=(50.8 - 0.005 * flow - 1.2e-4 * flow * flow) * kpa_per_psi,
        fault_times=np.array([]),
        maf_times=1.79e9 + tm,
        maf_gs=maf(tm),
        rpm_times=1.79e9 + tm - 0.05,
        rpm=np.full(len(tm), 2000.0),
    )


def test_the_report_is_a_proposal_and_the_outputs_are_never_a_rom(
    tmp_path: Path, declaration: FuelCompDeclaration
) -> None:
    logged = _logged()
    specs: list[dict[str, Any]] = [
        {"session": f"drive-{d}", "counts": (c, c + 2), "error": 2.0 + 0.1 * d, "maf": 30.0}
        for d in range(4)
        for c in range(300, 420, 4)
    ]
    frame = _sample_frame(declaration, logged, specs)
    frame = frame.drop(columns=["droop_kind", "droop_basis", "remainder_pct"])
    pressure = {n: _pressure(n, declaration.droop.kpa_per_psi) for n in ("p1", "p2")}
    result = build_result(
        frame,
        declaration=declaration,
        pressure=pressure,
        calibrations={"logged": logged},
        target=_target(),
        tables=None,
        tables_refusal="no image in this test",
        load_byte_known_answer={"summary": "not run: test"},
        sessions=sorted({s["session"] for s in specs}),
    )
    report = render_markdown(result["fit_json"])
    assert report.splitlines()[2] == PROPOSAL_HEADER
    assert "NOT A MEASURED OUTCOME" in report and "Nothing here writes or exports a ROM" in report
    for sha in (logged.image_sha256, "t" * 64):
        assert sha in report
    assert "Rail-to-manifold pressure difference is not modelled" in report
    assert "knock-on not computed: no image in this test" in report
    assert set(result["fit_json"]["fits"]) == {"did_route_excluded", "did_route_included"}
    written = write_outputs(
        tmp_path / "out",
        {
            "fit.json": result["fit_json"],
            "report.md": report,
            "fit_samples.parquet": result["prepared"],
            "knock_on.parquet": result["predictions"],
        },
    )
    files = sorted(p.name for p in (tmp_path / "out").iterdir())
    assert files == sorted(p.name for p in written)
    assert {Path(f).suffix for f in files} <= set(OUTPUT_SUFFIXES)


@pytest.mark.parametrize(
    "name", ["proposal.bin", "ZS44A-Tuned.OsirisROM", "sub/fit.json", "image.hex", "fit"]
)
def test_any_other_output_is_refused_before_anything_is_written(tmp_path: Path, name: str) -> None:
    with pytest.raises(OutputRefusedError):
        write_outputs(tmp_path / "out", {"fit.json": {}, name: b"\x00" * 16})
    assert not (tmp_path / "out").exists()


N02_SOURCES = [
    *sorted((REPO / "src" / "analysis").glob("fuel_comp_*.py")),
    *sorted((REPO / "tools").glob("fuel_comp_*.py")),
]
#: What writing or exporting an image would need: the editor that patches
#: one, the container encoder, raw byte writes.
FORBIDDEN_MODULES = {"src.analysis.rom_targets", "src.analysis.osiris", "scripts.rom_targets"}
FORBIDDEN_NAMES = {"write_bytes", "serialize", "TargetEditor", "transform", "with_checksums"}


def test_no_n02_module_can_write_or_export_a_rom() -> None:
    assert len(N02_SOURCES) >= 8
    for path in N02_SOURCES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in FORBIDDEN_MODULES, (path.name, node.module)
            elif isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & FORBIDDEN_MODULES, path.name
            elif isinstance(node, ast.Attribute | ast.Name):
                name = node.attr if isinstance(node, ast.Attribute) else node.id
                assert name not in FORBIDDEN_NAMES, (path.name, name)
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
                modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                assert not any("b" in str(m) for m in modes), path.name


def test_the_fit_tool_writes_only_through_the_suffix_check() -> None:
    """``tools/fuel_comp_fit.py`` writes nothing itself: every output goes
    through :func:`write_outputs`, which refuses anything but JSON, Markdown
    and Parquet."""
    tree = ast.parse((REPO / "tools" / "fuel_comp_fit.py").read_text())
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "write_outputs" in calls
    assert not calls & {"write_text", "to_parquet", "open", "to_csv", "dump"}
