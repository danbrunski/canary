"""E08.4: per-cell aggregation that keeps each kind of empty distinct.

Two kinds of input. *Hand-built* results are ``MonteCarloResult`` objects
whose arrays are written here, so every status, quantile and placement has a
known answer. *Run* results come from ``run_monte_carlo`` on E07.6's synthetic
image (``tests.test_sim_montecarlo``'s kit: flat Fuel Comp 128, rpm axis
400-6,400 rpm in 400s, fill axis 0-240 in 16s), so the membership rule is
checked against what the simulated ECU actually read.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import textwrap
import typing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from src.analysis import twin_runner
from src.analysis.paired_scenarios import OUTPUTS, SimKit, rpm_word, run_paired
from src.analysis.paired_steady import TRIM_AUTHORITY_UNDECODED, steady_bfs
from src.analysis.rom_effects import (
    CellEdit,
    EffectReport,
    OutputEffect,
    compare_roms,
    make_candidate,
)
from src.analysis.rom_image import sha256_hex
from src.analysis.sim_cells import (
    HEADLINE_BY_LOOP,
    MEMBERSHIP,
    SCHEMA_VERSION,
    STATUSES,
    X_NOT_COMPUTED,
    CellAxes,
    CellTable,
    aggregate_cells,
    cell_ranges,
    cell_table_schema,
    fuel_comp_axes,
    nearest_breakpoint,
    target_afr_axes,
)
from src.analysis.sim_contract import SimulationRefusalError
from src.analysis.sim_design import DesignPoint, SessionCheck, SimDesign
from src.analysis.sim_montecarlo import (
    UNCERTAINTY_FILE,
    LoadedUncertainty,
    MonteCarloDesign,
    MonteCarloResult,
    load_uncertainty,
    run_monte_carlo,
)
from src.decode.air_charge import rpm_load_lookup
from src.decode.ecm_lookup import lookup_2d
from tests.rom_fixtures import PROFILE
from tests.test_paired_scenarios import CRUISE, _scenario
from tests.test_sim_montecarlo import _template, build_kit_and_image

MODE = "conditional_rom_comparison"


@pytest.fixture(scope="module")
def kit_and_image() -> tuple[SimKit, bytes]:
    return build_kit_and_image()


@pytest.fixture(scope="module")
def uncertainty() -> LoadedUncertainty:
    return load_uncertainty(PROFILE / UNCERTAINTY_FILE)


@pytest.fixture(scope="module")
def axes(kit_and_image: tuple[SimKit, bytes]) -> CellAxes:
    kit, image = kit_and_image
    return fuel_comp_axes(kit, image, slot=1)


# -- designs ------------------------------------------------------------------------------

#: A warm part-load point (E07.6's cruise), every source measured.
_POINT: dict[str, Any] = dict(
    rpm=2000.0,
    maf_gps=18.0,
    maf_source="pid:0x10",
    baro_kpa=81.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    battery_v_source="measured",
    rail_kpa=350.0,
    rail_kpa_source="measured",
    map_kpa=40.0,
    map_kpa_source="measured",
    throttle_pct=10.0,
    throttle_pct_lo=10.0,
    throttle_pct_hi=10.0,
    throttle_pct_source="pid:0x11",
    pedal_flag=0,
    pedal_flag_source="stated_zero:not_logged",
    fill_byte_lo=60,
    fill_byte_hi=60,
    fill_byte_source="test",
    weight=1.0,
    regime="closed_loop",
)
WOT_POINT: dict[str, Any] = dict(rpm=4800.0, maf_gps=150.0, map_kpa=79.0, throttle_pct=90.0)


def _design(image: bytes, kit: SimKit, *points: dict[str, Any]) -> SimDesign:
    pts = []
    for i, over in enumerate(points):
        fields = {**_POINT, **over}
        fields.setdefault("throttle_pct_lo", fields["throttle_pct"])
        fields["throttle_pct_lo"] = fields["throttle_pct_hi"] = fields["throttle_pct"]
        pts.append(DesignPoint(index=i, session="_sim-test", t=float(i), **fields))
    n = len(pts)
    dwell = float(sum(p.weight for p in pts))
    return SimDesign(
        schema_version=1,
        kind="drive",
        vehicle_profile="test",
        base_revision="synthetic",
        base_image_sha256=sha256_hex(image),
        plant_version=kit.plant.plant_version,
        plant_file_sha256="0" * 64,
        plant_baseline_sha256="test",
        manifold_model="test",
        weight_basis="dwell_s",
        dwell_cap_s=2.0,
        sessions=[
            SessionCheck(
                session="_sim-test",
                dated_at=datetime(2026, 10, 15, tzinfo=UTC),
                revision_at_start="synthetic",
                revisions_in_reads=["synthetic"],
                reads=n,
                plant_states={"eligible": n},
                placement={"placed": n},
                eligible_dwell_s=dwell,
                placed_weight=dwell,
            )
        ],
        reads=n,
        plant_states={"eligible": n},
        placement={"placed": n},
        eligible_dwell_s=dwell,
        unplaced_dwell_s={},
        assumptions=[],
        points=pts,
    )


# -- hand-built results ---------------------------------------------------------------------


def _rom_outputs(**statuses: str) -> dict[str, dict[str, Any]]:
    out = {
        "target_equiv_ratio": {"status": "changed", "reasons": []},
        "base_fuel_schedule_word": {"status": "changed", "reasons": []},
    }
    for k, v in statuses.items():
        out[k] = {"status": v, "reasons": []}
    return out


def _hand(
    design: SimDesign,
    *,
    draws: int,
    fill: np.ndarray | None = None,
    base: dict[str, np.ndarray] | None = None,
    candidate: dict[str, np.ndarray] | None = None,
    support: dict[str, np.ndarray] | None = None,
    reason_sets: tuple[tuple[str, ...], ...] = ((),),
    loop: np.ndarray | None = None,
    rom_outputs: dict[str, dict[str, Any]] | None = None,
    outcome: str = "evaluable",
    learned: dict[str, Any] | None = None,
    affected: list[dict[str, Any]] | None = None,
    candidate_sha: str = "c" * 64,
) -> tuple[MonteCarloResult, EffectReport]:
    """A result whose every array is stated: 1.0 on both sides (delta 0) and
    supported unless given; the effect trace that matches it."""
    n = len(design.points)
    shape = (n, draws)
    if fill is None:
        fill = np.array([[p.fill_byte_lo] * draws for p in design.points], dtype=np.int64)
    b = {name: np.ones(shape) for name in OUTPUTS}
    c = {name: np.ones(shape) for name in OUTPUTS}
    b.update(base or {})
    c.update(candidate or {})
    s = {name: np.zeros(shape, dtype=np.int32) for name in OUTPUTS}
    s.update(support or {})
    rom = rom_outputs or _rom_outputs()
    report = {
        "outcome": outcome,
        "reason": None,
        "changed_nodes": ["fuel_comp_lookup"],
        "outputs": rom,
    }
    sides = {"stft_pct": 0.0, "ltft_pct": 0.0}
    result = MonteCarloResult(
        label="simulation",
        synthetic=True,
        outputs=OUTPUTS,
        weights=np.array([p.weight for p in design.points], dtype=float),
        fill_bytes=np.asarray(fill, dtype=np.int64),
        parameters={},
        base=b,
        candidate=c,
        support_index=s,
        reason_sets=reason_sets,
        loop=np.full(shape, "open_loop") if loop is None else loop,
        outcome=outcome,
        rom_report=report,
        affected_cells=list(affected or []),
        traced_outputs={},
        learned_state=learned or {"base": dict(sides), "candidate": dict(sides)},
        plant={},
        unresolved=["plant:not_qualified"],
        assumptions=[],
        caveats=["structural_error_not_sampled"],
        inert_parameters=[],
        coverage={},
        provenance={
            "design_sha256": design.sha256(),
            "base_image_sha256": design.base_image_sha256,
            "candidate_image_sha256": candidate_sha,
        },
    )
    effects = EffectReport(
        outcome=outcome,  # type: ignore[arg-type]
        reason=None,
        base_sha256=design.base_image_sha256,
        candidate_sha256=candidate_sha,
        slot=1,
        mode=MODE,
        changed_nodes=("fuel_comp_lookup",),
        outputs=tuple(
            OutputEffect(k, v["status"], (), tuple(v["reasons"]), "conditional")
            for k, v in rom.items()
        ),
    )
    return result, effects


def _refusal(fn: Any, *args: Any, **kw: Any) -> str:
    with pytest.raises(SimulationRefusalError) as err:
        fn(*args, **kw)
    return str(err.value.reason)


# -- membership ------------------------------------------------------------------------------


def test_the_grid_is_fuel_comps_own_axes_in_the_lookups_comparison_domain(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    """The breakpoints are what the firmware compares: on any table, a
    lookup on them gives the kernel's own Fuel Comp lookup on the raw axes."""
    assert axes.grid == "fuel_comp" and axes.table == "fuel_compensation" and axes.slot == 1
    assert axes.rows.table == "fuel_comp_engine_speed_axis"
    assert axes.cols.table == "fuel_comp_flow_axis"
    assert axes.rows.values == tuple(400.0 * (i + 1) for i in range(16))
    assert axes.rows.breakpoints == tuple(32 * (i + 1) for i in range(16))
    assert axes.cols.breakpoints == tuple(16 * i for i in range(16))
    rng = np.random.default_rng(4)
    cells = [[int(v) for v in row] for row in rng.integers(64, 192, size=(16, 16))]
    for word in range(0, 700, 3):
        for byte in range(0, 256, 5):
            mine = lookup_2d(cells, axes.rows.breakpoints, axes.cols.breakpoints, word, byte)
            kernel = rpm_load_lookup(cells, axes.rows.raw, axes.cols.raw, word, byte)
            assert mine.value == kernel


def test_a_cell_is_the_table_cell_the_lookup_weighs_most() -> None:
    """Nearest breakpoint: the cell is one the lookup reads, with weight >= 1/2;
    an exact half goes to the lower breakpoint; past either end, the end."""
    bps = (32, 64, 96, 160)
    cells = [[r * 10 + c for c in range(4)] for r in range(4)]
    for value in range(0, 220):
        k = nearest_breakpoint(bps, value)
        hit = lookup_2d(cells, bps, bps, value, value)
        assert (k, k) in hit.cells
        # The other cell it reads (if any) weighs at most as much.
        frac = hit.row_fraction
        assert (frac <= 0x8000) if k == hit.row.index else (frac > 0x8000)
    assert nearest_breakpoint(bps, 48) == 0  # exactly half way: the lower
    assert nearest_breakpoint(bps, 49) == 1
    assert nearest_breakpoint(bps, 128) == 2 and nearest_breakpoint(bps, 129) == 3
    assert nearest_breakpoint(bps, 0) == 0 and nearest_breakpoint(bps, 10_000) == 3
    ranges = cell_ranges(bps)
    assert ranges == [(None, 48), (49, 80), (81, 128), (129, None)]
    for value in range(0, 220):
        k = nearest_breakpoint(bps, value)
        lo, hi = ranges[k]
        assert (lo is None or lo <= value) and (hi is None or value <= hi)


def test_pairs_are_placed_by_the_byte_each_draw_used_never_the_midpoint(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    # Interval [20, 44]: bytes <= 24 belong to column 1 (bp 16), 25..40 to
    # column 2 (bp 32), 41..44 to column 3 (bp 48). The midpoint, 32, is column 2.
    design = _design(image, kit, dict(fill_byte_lo=20, fill_byte_hi=44))
    fill = np.array([[20, 24, 25, 32, 40, 41, 44, 44]])
    result, effects = _hand(design, draws=8, fill=fill)
    table = aggregate_cells(result, design, axes, effects=effects)
    row = nearest_breakpoint(axes.rows.breakpoints, 160)
    assert [table.cell(row, c).pairs for c in (1, 2, 3)] == [2, 3, 3]
    assert [table.cell(row, c).n_points for c in (1, 2, 3)] == [1, 1, 1]
    assert table.cell(row, 2).weight == pytest.approx(3 / 8)
    assert sum(c.pairs for c in table.cells) == 8


def _cells_except(table: CellTable, skip: set[tuple[int, int]]) -> list[dict[str, Any]]:
    return [c.model_dump() for c in table.cells if (c.row, c.col) not in skip]


def test_moving_one_point_across_a_breakpoint_moves_only_that_point(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    """rpm 2190 is word 175 (row 4, bp 160); 2210 is word 177 (row 5, bp 192):
    the boundary is 176. The same for a fill byte across 40/41."""
    kit, image = kit_and_image
    others = [dict(rpm=2000.0, weight=2.0), dict(rpm=2100.0, weight=3.0), dict(rpm=2400.0)]
    rng = np.random.default_rng(1)
    deltas = {"base_fuel_schedule": rng.normal(1.0, 0.1, size=(4, 6))}

    def table_for(rpm: float, fill: np.ndarray) -> CellTable:
        design = _design(
            image, kit, *others, dict(rpm=rpm, weight=1.5, fill_byte_lo=30, fill_byte_hi=50)
        )
        result, effects = _hand(design, draws=6, fill=fill, candidate=deltas)
        return aggregate_cells(result, design, axes, effects=effects)

    fill = np.array([[60] * 6] * 3 + [[40, 40, 40, 45, 45, 45]])
    before, after = table_for(2190.0, fill), table_for(2210.0, fill)
    moved = {(4, 2), (5, 2), (4, 3), (5, 3)}
    assert _cells_except(before, moved) == _cells_except(after, moved)
    # Point 3's pairs left row 4 for row 5, column for column.
    for col, n in ((2, 3), (3, 3)):
        assert 3 in before.cell(4, col).points and 3 not in before.cell(5, col).points
        assert 3 in after.cell(5, col).points and 3 not in after.cell(4, col).points
        assert after.cell(5, col).pairs == before.cell(4, col).pairs == n
    assert (
        after.cell(4, 2).pairs == 0 and after.cell(4, 2).outputs["target_phi"].status == "no_points"
    )
    # One draw's byte across 40/41 moves that pair alone.
    fill2 = fill.copy()
    fill2[3, 0] = 41
    shifted = table_for(2190.0, fill2)
    assert _cells_except(shifted, {(4, 2), (4, 3)}) == _cells_except(before, {(4, 2), (4, 3)})
    assert (shifted.cell(4, 2).pairs, shifted.cell(4, 3).pairs) == (2, 4)


# -- statuses ---------------------------------------------------------------------------------


def test_every_status_on_one_grid_and_no_kind_of_empty_collapses(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    """BFS is not reached (its ROM output unchanged); target phi is changed in
    one cell, unchanged in one, unsupported in one and partly supported in
    one; every other cell has no points."""
    kit, image = kit_and_image
    design = _design(
        image,
        kit,
        dict(rpm=800.0, fill_byte_lo=0, fill_byte_hi=0),  # (1, 0): changed
        dict(rpm=1600.0, fill_byte_lo=0, fill_byte_hi=0),  # (3, 0): unchanged
        dict(rpm=2400.0, fill_byte_lo=0, fill_byte_hi=0),  # (5, 0): unsupported
        dict(rpm=3200.0, fill_byte_lo=0, fill_byte_hi=0),  # (7, 0): partly
    )
    d = 4
    cand_phi = np.ones((4, d))
    cand_phi[0] = 1.1
    cand_phi[3] = 1.2
    support_phi = np.zeros((4, d), dtype=np.int32)
    support_phi[2] = 1
    support_phi[3, :2] = 1
    result, effects = _hand(
        design,
        draws=d,
        candidate={"target_phi": cand_phi},
        # BFS shares target phi's support: unreached comes before unsupported.
        support={"target_phi": support_phi, "base_fuel_schedule": support_phi},
        reason_sets=((), ("regime_unsupported:cold",)),
        rom_outputs=_rom_outputs(base_fuel_schedule_word="unchanged"),
    )
    table = aggregate_cells(result, design, axes, effects=effects)
    assert table.reach["base_fuel_schedule"].status == "not_reached"
    assert table.reach["target_phi"].status == "reached"
    phi = {k: table.cell(*k).outputs["target_phi"] for k in ((1, 0), (3, 0), (5, 0), (7, 0))}
    assert [o.status for o in phi.values()] == [
        "changed",
        "unchanged",
        "unsupported",
        "partly_supported",
    ]
    assert phi[(1, 0)].quantiles is not None and phi[(1, 0)].quantiles.over == "all_pairs"
    assert phi[(1, 0)].quantiles.delta.p50 == pytest.approx(0.1)
    assert phi[(1, 0)].quantiles.relative_delta.p50 == pytest.approx(0.1)  # type: ignore[union-attr]
    assert phi[(3, 0)].quantiles.delta.p05 == phi[(3, 0)].quantiles.delta.p95 == 0.0  # type: ignore[union-attr]
    unsupported = phi[(5, 0)]
    assert unsupported.quantiles is None and unsupported.supported_share == 0.0
    assert unsupported.support[0].reasons == ["regime_unsupported:cold"]
    partly = phi[(7, 0)]
    assert (partly.supported_share, partly.unsupported_share) == (0.5, 0.5)
    assert partly.quantiles is not None and partly.quantiles.over == "supported_pairs"
    assert partly.quantiles.pairs == 2 and partly.supported_effect == "changed"
    assert {e.status for e in partly.support} == {"supported", "unsupported"}
    # The same cells for BFS: not reached, never "unchanged", and not
    # "unsupported" either where no pair is supported: reach comes first.
    for k in phi:
        assert table.cell(*k).outputs["base_fuel_schedule"].status == "not_reached"
    bfs_unsupported = table.cell(5, 0).outputs["base_fuel_schedule"]
    assert bfs_unsupported.unsupported_share == 1.0
    assert bfs_unsupported.support[0].reasons == ["regime_unsupported:cold"]
    # Everywhere else: no points, for every output.
    empty = table.cell(10, 10)
    assert empty.pairs == 0 and {o.status for o in empty.outputs.values()} == {"no_points"}
    assert table.status_counts["target_phi"] == {
        "changed": 1,
        "unchanged": 1,
        "unsupported": 1,
        "partly_supported": 1,
        "no_points": 252,
    }
    assert table.status_counts["base_fuel_schedule"] == {"no_points": 252, "not_reached": 4}
    assert set(STATUSES) == set(table.status_counts["target_phi"]) | {"not_reached"}


def test_a_nonzero_delta_on_an_unreached_output_refuses_the_table(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    design = _design(image, kit, dict(rpm=800.0))
    bfs = np.ones((1, 3))
    bfs[0, 2] = 1.0001
    result, effects = _hand(
        design,
        draws=3,
        candidate={"base_fuel_schedule": bfs},
        rom_outputs=_rom_outputs(base_fuel_schedule_word="unchanged"),
    )
    assert _refusal(aggregate_cells, result, design, axes, effects=effects) == "reach_contradicted"


def test_a_learned_reset_reaches_the_fuel_side_and_identical_images_are_no_change(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    design = _design(image, kit, dict(rpm=800.0))
    unchanged = _rom_outputs(base_fuel_schedule_word="unchanged", target_equiv_ratio="unchanged")
    reset = {
        "base": {"stft_pct": 0.0, "ltft_pct": 2.34},
        "candidate": {"stft_pct": 0.0, "ltft_pct": 0.0},
    }
    pulse = np.full((1, 2), 1.02)
    result, effects = _hand(
        design,
        draws=2,
        rom_outputs=unchanged,
        learned=reset,
        candidate={"injector_pulse_ms": pulse},
    )
    table = aggregate_cells(result, design, axes, effects=effects)
    assert table.reach["base_fuel_schedule"].status == "not_reached"
    assert table.reach["injector_pulse_ms"].status == "reached"
    assert "learned:ltft_pct:2.34->0.0" in table.reach["injector_pulse_ms"].basis
    assert table.cell(1, 4).outputs["injector_pulse_ms"].status == "changed"
    assert table.cell(1, 4).outputs["target_phi"].status == "not_reached"
    # Identical images: no change to reach anything; the data decides.
    same, eff = _hand(design, draws=2, rom_outputs=unchanged, outcome="identical")
    table = aggregate_cells(same, design, axes, effects=eff)
    assert {r.status for r in table.reach.values()} == {"no_change"}
    assert {o.status for o in table.cell(1, 4).outputs.values()} == {"unchanged"}


# -- quantiles -----------------------------------------------------------------------------


def _brute_quantile(values: list[float], weights: list[int], q: float) -> float:
    """The smallest value whose cumulative weight reaches q of the total."""
    pairs = sorted(zip(values, weights, strict=True))
    total = sum(weights)
    run = 0
    for v, w in pairs:
        run += w
        if run >= q * total:
            return v
    return pairs[-1][0]


def test_quantiles_are_dwell_weighted_over_pairs_against_brute_force(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    weights = [1.0, 7.0, 2.0, 5.0, 3.0]
    design = _design(
        image, kit, *(dict(rpm=2000.0 + 10 * i, weight=w) for i, w in enumerate(weights))
    )
    d = 9
    rng = np.random.default_rng(20261015)
    base = rng.uniform(900.0, 1100.0, size=(5, d))
    cand = base + rng.normal(0.0, 5.0, size=(5, d))
    result, effects = _hand(
        design, draws=d, base={"base_fuel_schedule": base}, candidate={"base_fuel_schedule": cand}
    )
    table = aggregate_cells(result, design, axes, effects=effects)
    cell = table.cell(4, 4)  # 2000-2040 rpm, byte 60 (bp 64)
    assert cell.n_points == 5 and cell.pairs == 45
    assert cell.weight == pytest.approx(sum(weights))
    out = cell.outputs["base_fuel_schedule"]
    assert out.quantiles is not None
    delta = (cand - base).ravel().tolist()
    rel = (cand / base - 1.0).ravel().tolist()
    # Each pair weighs its point's dwell / draws; whole-number dwell x the
    # draw count makes each pair's replication count its point's dwell.
    reps = [int(w) for w in weights for _ in range(d)]
    for got, values in ((out.quantiles.delta, delta), (out.quantiles.relative_delta, rel)):
        assert got is not None
        replicated = np.repeat(values, reps)
        expected = np.quantile(replicated, [0.05, 0.5, 0.95], method="inverted_cdf")
        assert [got.p05, got.p50, got.p95] == expected.tolist()
        assert [got.p05, got.p50, got.p95] == [
            _brute_quantile(values, reps, q) for q in (0.05, 0.5, 0.95)
        ]
    # An unweighted quantile differs here: the weights matter.
    plain = np.quantile(delta, 0.5, method="inverted_cdf")
    assert out.quantiles.delta.p50 != plain


def test_regime_and_loop_shares_are_weight_shares(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    design = _design(
        image,
        kit,
        dict(weight=3.0, regime="closed_loop"),
        dict(weight=1.0, regime="enrichment"),
    )
    loop = np.array([["closed_loop"] * 4, ["open_loop"] * 4])
    result, effects = _hand(design, draws=4, loop=loop)
    cell = aggregate_cells(result, design, axes, effects=effects).cell(4, 4)
    assert cell.regime_shares == {"closed_loop": 0.75, "enrichment": 0.25}
    assert cell.loop_shares == {"closed_loop": 0.75, "open_loop": 0.25}
    assert [(b.loop, b.output, b.weight_share) for b in cell.by_loop] == [
        ("closed_loop", "fuel_trim_total_pct", 0.75),
        ("open_loop", "delivered_lambda", 0.25),
    ]


# -- run results ----------------------------------------------------------------------------


def _run(
    kit: SimKit,
    image: bytes,
    candidate: bytes,
    design: SimDesign,
    uncertainty: LoadedUncertainty,
    *,
    draws: int = 6,
) -> tuple[MonteCarloResult, EffectReport]:
    result = run_monte_carlo(
        kit,
        _template(kit, image, candidate),
        MonteCarloDesign.from_sim_design(design),
        image,
        candidate,
        uncertainty,
        draws=draws,
        seed=7,
    )
    effects = compare_roms(
        image,
        candidate,
        kit.rom_map,
        kit.model,
        kit.model_sha256,
        base_slot=1,
        candidate_slot=1,
        slot_source="default_slot",
        contract=kit.contract,
        mode=MODE,
    )
    return result, effects


def test_an_edited_cell_shows_in_its_own_grid_cell_on_a_real_run(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes, uncertainty: LoadedUncertainty
) -> None:
    """Fuel Comp cell (4, 4) (2,000 rpm, byte 64) +20 counts. Every pair in
    grid cell (4, 4) reads it with weight >= 1/2, so its BFS moves at every
    pair; a cell two breakpoints away never reads it and is unchanged."""
    kit, image = kit_and_image
    candidate = make_candidate(
        image, kit.rom_map, [CellEdit("fuel_compensation", 4, 4, 148, slot=1)], base_label="t"
    ).image
    rpms = (1700.0, 1850.0, 2000.0, 2150.0, 2300.0, 2800.0)
    design = _design(image, kit, *(dict(rpm=r, fill_byte_lo=40, fill_byte_hi=100) for r in rpms))
    result, effects = _run(kit, image, candidate, design, uncertainty, draws=24)
    table = aggregate_cells(result, design, axes, effects=effects)
    bfs = table.cell(4, 4).outputs["base_fuel_schedule"]
    assert bfs.status == "changed" and bfs.changed_share == 1.0
    assert bfs.quantiles is not None and bfs.quantiles.relative_delta is not None
    assert bfs.quantiles.relative_delta.p05 > 0
    # Two breakpoints away in either direction: the lookup never reads (4, 4).
    for r, c in ((4, 6), (4, 2), (6, 4)):
        cell = table.cell(r, c)
        if cell.pairs:
            assert cell.outputs["base_fuel_schedule"].status == "unchanged"
    assert table.cell(4, 6).pairs > 0 and table.cell(6, 4).pairs > 0
    # A changed pair lies within one breakpoint of the edit, never further.
    for cell in table.cells:
        if cell.outputs["base_fuel_schedule"].status == "changed":
            assert abs(cell.row - 4) <= 1 and abs(cell.col - 4) <= 1
    assert table.overlay.edited_cells[0].model_dump() == {
        "row": 4,
        "col": 4,
        "old": 128,
        "new": 148,
    }
    assert table.cell(4, 4).edited is not None and table.cell(4, 5).edited is None
    assert table.unplaced == {}


def test_closed_loop_cells_report_the_trim_with_its_reason_as_given(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes, uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    candidate = make_candidate(
        image,
        kit.rom_map,
        [CellEdit("fuel_compensation", r, c, 136, slot=1) for r in range(16) for c in range(16)],
        base_label="t",
    ).image
    design = _design(
        image,
        kit,
        dict(fill_byte_lo=60, fill_byte_hi=60),  # cruise: closed loop
        dict(**WOT_POINT, fill_byte_lo=200, fill_byte_hi=200, regime="enrichment"),
    )
    result, effects = _run(kit, image, candidate, design, uncertainty)
    table = aggregate_cells(result, design, axes, effects=effects)
    cruise = table.cell(4, 4)
    assert cruise.loop_shares == {"closed_loop": 1.0}
    lam = cruise.outputs["delivered_lambda"]
    assert lam.status == "unsupported" and lam.quantiles is None
    assert TRIM_AUTHORITY_UNDECODED in lam.support[0].reasons
    (block,) = cruise.by_loop
    assert (block.loop, block.output) == ("closed_loop", HEADLINE_BY_LOOP["closed_loop"])
    trim = block.cell
    assert trim.status == "unsupported"
    assert all(TRIM_AUTHORITY_UNDECODED in e.reasons for e in trim.support)
    assert trim.quantiles is not None and trim.quantiles.over == "computed_unsupported_pairs"
    # The run's own trim deltas at those pairs, carried: more Fuel Comp, BFS
    # up, so the candidate needs less trim to meter the base's fuel.
    assert result.relative_delta("base_fuel_schedule")[0].min() > 0.02
    computed = result.delta("fuel_trim_total_pct")[0]
    expected = np.quantile(computed, [0.05, 0.5, 0.95], method="inverted_cdf")
    got = trim.quantiles.delta
    assert [got.p05, got.p50, got.p95] == expected.tolist()
    assert got.p95 < 0
    assert trim.quantiles.relative_delta is None
    wot = table.cell(nearest_breakpoint(axes.rows.breakpoints, 384), 12)
    assert wot.loop_shares == {"open_loop": 1.0}
    (wot_block,) = wot.by_loop
    assert (wot_block.output, wot_block.cell.status) == ("delivered_lambda", "changed")


def test_the_overlay_marks_edits_axis_moves_and_moved_lookups(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes, uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    candidate = make_candidate(
        image,
        kit.rom_map,
        [
            CellEdit("fuel_compensation", 4, 4, 140, slot=1),
            CellEdit("fuel_comp_flow_axis", 0, 5, 84),
            CellEdit("fuel_targets", 2, 3, 120, slot=1),
        ],
        base_label="t",
    ).image
    design = _design(image, kit, dict(fill_byte_lo=60, fill_byte_hi=90))
    result, effects = _run(kit, image, candidate, design, uncertainty, draws=4)
    table = aggregate_cells(result, design, axes, effects=effects)
    overlay = table.overlay
    assert [(e.row, e.col) for e in overlay.edited_cells] == [(4, 4)]
    assert [(a.axis, a.index, a.old, a.new) for a in overlay.axis_edits] == [("cols", 5, 80, 84)]
    assert [(o.table, o.slot) for o in overlay.other_edits] == [("fuel_targets", 1)]
    assert all(table.cell(r, 5).col_axis_moved for r in range(16))
    assert not table.cell(4, 4).col_axis_moved and not table.cell(4, 4).row_axis_moved
    moved = {m.node: m for m in overlay.moved_lookups}
    assert set(moved) == {str(m["node"]) for m in effects.moved_lookups}
    assert "timing_trim_lookup" in moved and not moved["timing_trim_lookup"].on_this_grid
    # On the Target AFR grid the same edit is "edited here", and the moved
    # Target AFR lookup is on this grid.
    target = target_afr_axes(kit, image, slot=1)
    t_table = aggregate_cells(result, design, target, effects=effects)
    assert [(e.row, e.col) for e in t_table.overlay.edited_cells] == [(2, 3)]
    assert {o.table for o in t_table.overlay.other_edits} == {
        "fuel_compensation",
        "fuel_comp_flow_axis",
    }


def test_the_target_afr_grid_places_pairs_by_the_base_roms_x(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    from src.analysis.target_phi import x_from_bfs

    kit, image = kit_and_image
    target = target_afr_axes(kit, image, slot=1)
    assert target.grid == "target_afr" and target.cols.coordinate == "load_input_x"
    candidate = make_candidate(
        image, kit.rom_map, [CellEdit("fuel_compensation", 4, 4, 140, slot=1)], base_label="t"
    ).image
    design = _design(
        image,
        kit,
        dict(fill_byte_lo=40, fill_byte_hi=90),
        dict(**WOT_POINT, fill_byte_lo=200, fill_byte_hi=200, pedal_flag=1),
    )
    result, effects = _run(kit, image, candidate, design, uncertainty, draws=8)
    table = aggregate_cells(result, design, target, effects=effects)
    # A Fuel Comp edit moves where Target AFR is read: on this grid, not Fuel Comp's.
    on_grid = {m.node: m.on_this_grid for m in table.overlay.moved_lookups}
    assert on_grid["target_afr_lookup"] is True
    fc = aggregate_cells(result, design, fuel_comp_axes(kit, image, slot=1), effects=effects)
    assert {m.node: m.on_this_grid for m in fc.overlay.moved_lookups}["target_afr_lookup"] is False
    assert target.target is not None and target.target.load_point_scaler is not None
    row = nearest_breakpoint(target.rows.breakpoints, 160)
    for k in range(8):
        x = x_from_bfs(
            int(result.base["base_fuel_schedule"][0, k]), target.target.load_point_scaler, kit.twin
        )
        col = nearest_breakpoint(target.cols.breakpoints, x)
        assert 0 in table.cell(row, col).points
    # The forced-column flag reads the last column whatever X is.
    wot_row = nearest_breakpoint(target.rows.breakpoints, 384)
    last = len(target.cols.breakpoints) - 1
    assert table.cell(wot_row, last).pairs == 8 and 1 in table.cell(wot_row, last).points
    # A pair with no base BFS has no X: unplaced by name, its weight kept.
    bfs = result.base["base_fuel_schedule"].copy()
    bfs[0, :3] = np.nan
    gap = dataclasses.replace(result, base={**result.base, "base_fuel_schedule": bfs})
    gapped = aggregate_cells(gap, design, target, effects=effects)
    assert gapped.unplaced[X_NOT_COMPUTED].pairs == 3
    assert gapped.unplaced[X_NOT_COMPUTED].weight == pytest.approx(3 / 8)
    assert sum(c.pairs for c in gapped.cells) == 13


def test_an_identical_candidate_is_unchanged_in_every_visited_cell(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes, uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    design = _design(
        image,
        kit,
        dict(fill_byte_lo=40, fill_byte_hi=90),
        dict(**WOT_POINT, fill_byte_lo=190, fill_byte_hi=210, regime="enrichment"),
    )
    result, effects = _run(kit, image, bytes(image), design, uncertainty)
    table = aggregate_cells(result, design, axes, effects=effects)
    assert {r.status for r in table.reach.values()} == {"no_change"}
    for cell in table.cells:
        for name, o in cell.outputs.items():
            if cell.pairs == 0:
                assert o.status == "no_points"
            elif o.status == "partly_supported":
                assert o.supported_effect == "unchanged", (cell.row, cell.col, name)
            elif o.supported_share:
                assert o.status == "unchanged", (cell.row, cell.col, name)
    assert table.overlay.edited_cells == [] and table.overlay.axis_edits == []


# -- refusals ---------------------------------------------------------------------------------


def test_an_aggregate_first_path_cannot_be_written(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    assert typing.get_type_hints(aggregate_cells)["result"] is MonteCarloResult
    design = _design(image, kit, dict())
    result, effects = _hand(design, draws=2)
    for frame in (result.traces(), result.traces().groupby("output").mean(numeric_only=True)):
        assert (
            _refusal(aggregate_cells, frame, design, axes, effects=effects)
            == "not_a_monte_carlo_result"
        )


def test_inputs_that_are_not_the_runs_are_refused_by_name(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    design = _design(image, kit, dict(fill_byte_lo=50, fill_byte_hi=70))
    result, effects = _hand(design, draws=2)
    other = _design(image, kit, dict(rpm=2100.0, fill_byte_lo=50, fill_byte_hi=70))
    assert _refusal(aggregate_cells, result, other, axes, effects=effects) == "design_mismatch"
    reweighted = dataclasses.replace(result, weights=result.weights * 2)
    assert _refusal(aggregate_cells, reweighted, design, axes, effects=effects) == "design_mismatch"
    outside = dataclasses.replace(result, fill_bytes=np.array([[50, 71]]))
    assert _refusal(aggregate_cells, outside, design, axes, effects=effects) == "design_mismatch"
    foreign = dataclasses.replace(axes, base_image_sha256="e" * 64)
    assert (
        _refusal(aggregate_cells, result, design, foreign, effects=effects) == "axes_image_mismatch"
    )
    for change in (
        {"base_sha256": "f" * 64},
        {"candidate_sha256": "f" * 64},
        {"outcome": "partial"},
        {"outputs": ()},
        {"changed_nodes": ("k_constant_node",)},
    ):
        wrong = dataclasses.replace(effects, **change)
        assert _refusal(aggregate_cells, result, design, axes, effects=wrong) == "effects_mismatch"
    looped = dataclasses.replace(result, loop=np.full((1, 2), "warmup"))
    assert _refusal(aggregate_cells, looped, design, axes, effects=effects) == "loop_unknown"


def test_a_grid_axis_is_refused_when_it_cannot_be_a_cell_boundary(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    flat = make_candidate(
        image, kit.rom_map, [CellEdit("fuel_comp_flow_axis", 0, 5, 64)], base_label="t"
    ).image
    assert _refusal(fuel_comp_axes, kit, flat, slot=1) == "axis_not_increasing"
    tables = [
        t.model_copy(update={"decode": t.decode.model_copy(update={"scale": 51.3})})
        if t.id == "fuel_comp_engine_speed_axis" and t.decode is not None
        else t
        for t in kit.rom_map.tables
    ]
    odd = dataclasses.replace(kit, rom_map=kit.rom_map.model_copy(update={"tables": tables}))
    assert _refusal(fuel_comp_axes, odd, image, slot=1) == "axis_not_on_the_rpm_word"


def test_changed_means_any_supported_pair_moved_even_with_a_zero_median(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    """The accepted deviation: weighted p50 is 0, p95 is not. "|p50| above
    zero" would leave this cell no status; it is changed, with the share."""
    kit, image = kit_and_image
    design = _design(image, kit, dict())
    cand = np.array([[1.0, 1.0, 1.0, 1.5]])
    result, effects = _hand(design, draws=4, candidate={"base_fuel_schedule": cand})
    out = aggregate_cells(result, design, axes, effects=effects).cell(4, 4)
    bfs = out.outputs["base_fuel_schedule"]
    assert bfs.status == "changed"
    assert bfs.quantiles is not None
    assert (bfs.quantiles.delta.p50, bfs.quantiles.delta.p95) == (0.0, 0.5)
    assert bfs.changed_share == 0.25


def test_a_zero_base_is_counted_out_of_the_relative_quantiles(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    design = _design(image, kit, dict())
    base = np.array([[0.0, 2.0, 2.0, 2.0]])
    cand = np.array([[0.5, 2.2, 2.2, 2.2]])
    result, effects = _hand(
        design, draws=4, base={"target_phi": base}, candidate={"target_phi": cand}
    )
    q = aggregate_cells(result, design, axes, effects=effects).cell(4, 4)
    block = q.outputs["target_phi"].quantiles
    assert block is not None and block.pairs == 4 and block.relative_undefined_pairs == 1
    assert block.relative_delta is not None
    assert block.relative_delta.p05 == block.relative_delta.p95 == pytest.approx(0.1)
    assert block.delta.p05 == pytest.approx(0.2) and block.delta.p95 == 0.5


@pytest.mark.parametrize("rpm", [2190.0, 2193.75, 2206.25, 2210.0, 799.0, 4806.25])
def test_a_points_row_is_the_word_the_paired_run_read(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes, monkeypatch: pytest.MonkeyPatch, rpm: float
) -> None:
    """The row comes from paired_scenarios.rpm_word, the conversion ecu_chain
    itself uses: recorded at the steady-BFS call, never re-derived. 2193.75
    and 2206.25 are words 175.5 and 176.5, both rounding to 176 (half-even),
    which is row 4's tie edge."""
    kit, image = kit_and_image
    seen: list[int] = []
    # The steady-BFS call is the twin runner's ECU stage (E09.2c), which
    # run_paired calls on its stated points.
    real = steady_bfs

    def spy(*args: Any, **kw: Any) -> Any:
        seen.append(int(kw["rpm_word"]))
        return real(*args, **kw)

    monkeypatch.setattr(twin_runner, "steady_bfs", spy)
    point = dataclasses.replace(CRUISE, rpm=rpm)
    run_paired(kit, _scenario(kit, image, bytes(image), point), image, bytes(image))
    assert seen and set(seen) == {rpm_word(rpm, kit.contract)}
    design = _design(image, kit, dict(rpm=rpm))
    result, effects = _hand(design, draws=1)
    table = aggregate_cells(result, design, axes, effects=effects)
    (cell,) = [c for c in table.cells if c.pairs]
    assert cell.row == nearest_breakpoint(axes.rows.breakpoints, seen[0])


def test_the_validator_refuses_a_missing_repeated_or_misplaced_cell(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    design = _design(image, kit, dict())
    result, effects = _hand(design, draws=2)
    data = aggregate_cells(result, design, axes, effects=effects).to_dict()
    CellTable.model_validate(data)

    def refused(edit: Any, match: str) -> None:
        tampered = json.loads(json.dumps(data))
        edit(tampered["cells"])
        with pytest.raises(ValidationError, match=match):
            CellTable.model_validate(tampered)

    def duplicate(cells: list[dict[str, Any]]) -> None:
        cells[-1] = dict(cells[0])  # (15, 15) gone, (0, 0) twice; both hold no weight

    def drop(cells: list[dict[str, Any]]) -> None:
        cells.pop(17)

    def out_of_grid(cells: list[dict[str, Any]]) -> None:
        cells[3]["row"] = 99

    def wrong_breakpoint(cells: list[dict[str, Any]]) -> None:
        cells[3]["col_breakpoint"] += 1

    refused(duplicate, r"repeated \[\(0, 0\)\]")
    refused(drop, r"missing \[\(1, 1\)\]")
    refused(lambda cells: cells.reverse(), "row-major")
    refused(out_of_grid, r"out of grid \[\(99, 3\)\]")
    refused(wrong_breakpoint, "names breakpoints")


# -- the table's JSON form ----------------------------------------------------------------


def test_the_cell_table_is_explicit_versioned_json(
    kit_and_image: tuple[SimKit, bytes], axes: CellAxes
) -> None:
    kit, image = kit_and_image
    design = _design(image, kit, dict(fill_byte_lo=50, fill_byte_hi=70), dict(rpm=4800.0))
    cand = {"base_fuel_schedule": np.array([[1.02, 1.03, 1.0], [1.0, 1.0, 1.0]])}
    result, effects = _hand(
        design, draws=3, fill=np.array([[50, 60, 70], [60, 60, 60]]), candidate=cand
    )
    table = aggregate_cells(result, design, axes, effects=effects)
    data = json.loads(table.to_json())
    assert data["schema_version"] == SCHEMA_VERSION == 1
    assert data["tool"] == "sim_cells" and data["membership"] == MEMBERSHIP
    assert data["grid"] == "fuel_comp" and data["table"] == "fuel_compensation"
    assert len(data["cells"]) == 256 and data["rows"]["breakpoints"][0] == 32
    assert data["rows"]["cell_ranges"][0] == [None, 48]
    assert data["caveats"] == ["structural_error_not_sampled"]
    assert data["design_sha256"] == design.sha256()
    assert data["result_digest"] == result.digest()
    assert sorted(data["cells"][0]) == sorted(
        [
            "row",
            "col",
            "row_breakpoint",
            "col_breakpoint",
            "row_value",
            "col_value",
            "n_points",
            "points",
            "pairs",
            "weight",
            "regime_shares",
            "loop_shares",
            "outputs",
            "by_loop",
            "edited",
            "row_axis_moved",
            "col_axis_moved",
        ]
    )
    # Round trip, and nothing extra is accepted.
    assert CellTable.model_validate(data) == table
    with pytest.raises(ValidationError):
        CellTable.model_validate({**data, "note": "x"})
    tampered = json.loads(table.to_json())
    tampered["cells"][0]["weight"] = 9.0
    with pytest.raises(ValidationError, match="unplaced"):
        CellTable.model_validate(tampered)
    schema = cell_table_schema()
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["additionalProperties"] is False
    # Status counts cover every cell, per output.
    for name in OUTPUTS:
        assert sum(data["status_counts"][name].values()) == 256


# -- no write path ---------------------------------------------------------------------------

_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_aggregating_cells_imports_nothing_that_talks_to_the_ecu() -> None:
    script = textwrap.dedent(
        """
        import json, sys
        from src.analysis.sim_cells import aggregate_cells, fuel_comp_axes, target_afr_axes
        from tests.test_sim_cells import _design, _hand
        from tests.test_sim_montecarlo import build_kit_and_image
        kit, image = build_kit_and_image()
        design = _design(image, kit, dict())
        result, effects = _hand(design, draws=2)
        for axes in (fuel_comp_axes(kit, image, slot=1), target_afr_axes(kit, image, slot=1)):
            aggregate_cells(result, design, axes, effects=effects).to_json()
        print(json.dumps(sorted(sys.modules)))
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=300,
    )
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert "src.analysis.sim_cells" in loaded
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []
