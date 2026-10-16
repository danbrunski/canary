"""W09.6: the ROM Editor's simulate on the operator's real image (ZS44A-20261004).

Skipped by name when the image is absent (``tests.rom_fixtures.OPERATOR_ROMS``),
as ``tests/test_fuel_comp_restore_private_rom.py`` and
``tests/test_rom_sim_private_rom.py`` are, so nothing here passes because
something was missing.

**The GUI path, not the CLI.** E08.7 reproduces its known answers through
``tools/rom_sim.py`` (stated designs, ``traces.parquet``). This file runs the
page's own path: :func:`src.analysis.rom_editor.simulate_edits` (and the
private ``_simulate`` seam for the negative control) on the library's plain
image as a **matched revision**, ``run_job`` and pickling of a real result, and
one ``POST /control/rom/library/{sha}/simulate`` through a ``TestClient`` with
the ``.OsirisROM`` uploaded through the library's own route.

**The base is truly the recorded revision.** The decoded image's sha256 must be
``tune_revisions.yaml``'s ``image_sha256`` for ZS44A-20261004, and
``revision_match`` (the library's function, with the tracked registry, which is
what the library's ``active_registry`` is with no dashboard-recorded flash) must
say ``matched`` to that revision.

**The grid is the one the form suggests** (W09 "Decisions", ``lib/romSim.ts``
``suggestEdges`` / ``edgesFromBreakpoints`` / ``trimLoadEdges``, ported in
:func:`form_edges`): the midpoints between Fuel Comp's breakpoints on this image,
half a step past each end. On 20261004 the fill axis runs 16..255 raw (7.1 % to
113.8 %), so the suggested load edges pass the fill model's 100 % and the form
blocks Run until the operator presses "Trim load edges to 0-100 %": the grid
that can run is 16 rpm x 14 load = 224 points, not 16 x 16. Both are stated
below as numbers, and the port must reproduce them.

**Nothing is defaulted.** Every environmental and learned value is stated in
:data:`ENV` / :data:`LEARNED_ZERO` / :data:`E076_WOT_ENV` / :data:`E076_LEARNED`.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import pickle
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.analysis import rom_editor, sim_montecarlo
from src.analysis.paired_scenarios import PairedScenario, SimKit, load_kit, run_paired
from src.analysis.paired_scenarios import rpm_word as sim_rpm_word
from src.analysis.paired_steady import TRIM_AUTHORITY_UNDECODED
from src.analysis.rom_edit_spec import load_edit_spec
from src.analysis.rom_image import revision_match, sha256_hex, table_values
from src.analysis.sim_design import SimDesign, SimDesignRefusalError
from src.analysis.sim_montecarlo import MonteCarloResult
from src.control import workbench_jobs
from src.dashboard.routes import rom_editor as rom_editor_routes
from src.decode.air_charge import TP_SCALE, TP_SCALE_SHIFT
from src.vehicle.tune_revisions import TuneRevisionRegistry, load_tune_revisions
from tests.configs import isolated_config
from tests.rom_fixtures import OPERATOR_ROMS, PROFILE, operator_image
from tests.test_rom_editor_api import AUTH, OCTET, CountingExecutor, make_app

pytestmark = pytest.mark.slow

REVISION = "ZS44A-20261004"
IMAGE_KEY = "r20261004"
SEED = 20261015
#: Draws for the 224-point grid cases: per-pair checks need few, the suite needs speed.
DRAWS = 4
#: Draws for the 3-point WOT case, as E08.7 item 4 (per-point quantiles).
WOT_DRAWS = 200
FIXTURE_SPEC = Path(__file__).parent / "fixtures" / "rom_edit_spec" / "fuel_comp_light_load.yaml"

#: The form's suggestion on this image, as numbers (``edgesFromBreakpoints``
#: on the Fuel Comp rpm breakpoints 400..6,400 step 400, and on the fill
#: breakpoints 16..255 raw x 0.446429, then ``trimLoadEdges``).
FORM_RPM_EDGES = [200.0 + 400.0 * i for i in range(17)]
FORM_LOAD_EDGES_SUGGESTED = [
    3.571429, 10.71429, 17.85714, 25.0, 32.14286, 39.28571, 46.42857, 53.57143,
    60.71429, 67.85714, 75.0, 82.14286, 89.28571, 96.42857, 103.5714, 110.4911, 117.1875,
]  # fmt: skip
FORM_LOAD_EDGES = [*FORM_LOAD_EDGES_SUGGESTED[:14], 100.0]
FORM_POINTS = 16 * 14

#: A stated warm, home-altitude environment for the grid cases (E07.6's WOT
#: values for baro, IAT, coolant, battery and rail; a part throttle).
ENV: dict[str, Any] = dict(
    load_basis="fill_fraction_as_manifold_ratio",
    baro_kpa=81.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    rail_kpa=350.0,
    throttle_pct=20.0,
    pedal_flag=0,
)
LEARNED_ZERO: dict[str, Any] = dict(
    stft_pct=0.0,
    ltft_pct=0.0,
    throttle_closed_learned=0.0,
    idle_air_learned=0.0,
    evap_purge_area=0.0,
)
#: E07.6's WOT point's environment (``tests/test_paired_scenarios.py`` ``WOT``:
#: baro 81, IAT 25, coolant 88, battery 14, rail 350, throttle 90) with the
#: forced-column flag set: bit 0 of ``mem:ECM_PEDAL_ENRICH_FLAGS``
#: (``target_phi_twin.yaml``) makes the Target AFR lookup read its last column,
#: as the truck does at full pedal. A grid cannot reach that column by X on this
#: image (see ``test_no_grid_point_reaches_e076s_wot_bfs_or_the_wot_column_by_x``).
E076_WOT_ENV: dict[str, Any] = {**ENV, "throttle_pct": 90.0, "pedal_flag": 1}
#: E07.6's ``LEARNED`` (STFT 0, LTFT 2.34).
E076_LEARNED: dict[str, Any] = {**LEARNED_ZERO, "ltft_pct": 2.34}
#: One rpm row centred on E07.6's 4,800 rpm (the form's own 4,600-5,000 cell)
#: at the form's three highest fill cells.
WOT_RPM_EDGES = [4600.0, 5000.0]
WOT_LOAD_EDGES = FORM_LOAD_EDGES[-4:]

#: E07.6's WOT figures (E07 doc, E07.6 tests; re-checked by E08.7 item 4).
E076_PHI_DELTA = 0.03125
E076_PULSE_DELTA_MS = 0.41
E076_LAMBDA_REL = -0.025
#: E07.6's stated tolerance: lambda "-2.52...-2.49 % over the assumptions".
E076_LAMBDA_RANGE = (-0.0252, -0.0249)
#: E08.7: the written +0.41 ms holds only at steady BFS >= ~41,997 (one 0.01 ms
#: pulse quantum); E07.6's figures were measured with BFS stated at 42,000.
E076_BFS_FOR_PULSE = 41_997
#: E08.7 item 4's tolerance for a Monte Carlo against E07.6's own central run:
#: p50 within this of ``run_paired``'s relative delta, p05-p95 inside its range.
P50_VS_CENTRAL = 2e-4

#: The Target AFR table's last column (the top X breakpoint, the column the
#: forced-column flag reads): the WOT cells, every rpm row.
WOT_COLUMN = 7
TARGET_PLUS_4_WOT: dict[str, Any] = {
    "table": "fuel_targets",
    "slot": 1,
    "cells": [[row, WOT_COLUMN] for row in range(8)],
    "add_raw": 4,
}
#: Support reasons the editor's cells name (``paired_steady``, ``sim_montecarlo``,
#: the target lookup's enrichment-gate hysteresis band).
SHORT_PULSE = "short_pulse_undecoded"
GATE_UNKNOWN = "target_lookup:enrichment_gate_state_unknown"
K_PLUS_5: dict[str, Any] = {
    "table": "k_constant",
    "slot": 1,
    "all": True,
    "scale": 1.05,
    "rounding": "nearest",
}
#: E08.1's private check on this image: the region edit's cells, raw old -> new
#: (rpm rows 2-3 = 1,200 / 1,600 rpm, fill cols 2-3 = 21.43 / 28.57 %), and
#: the spec's second edit, idle cell (0, 0) add_raw -2.
FC_REGION_CELLS = {(2, 2): (130, 133), (2, 3): (130, 133), (3, 2): (130, 133), (3, 3): (128, 131)}
FC_EDITED_CELLS = {**FC_REGION_CELLS, (0, 0): (128, 126)}


# -- the real image ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Real:
    image: bytes
    sha256: str
    revision_match: dict[str, Any]
    registry: TuneRevisionRegistry
    kit: SimKit


@pytest.fixture(scope="module")
def real() -> Real:
    image = operator_image(IMAGE_KEY)
    if image is None:
        pytest.skip(f"private ROM not on this machine: {OPERATOR_ROMS[IMAGE_KEY]}")
    registry = load_tune_revisions(PROFILE / "tune_revisions.yaml")
    assert registry is not None
    (recorded,) = [r for r in registry.revisions if r.revision == REVISION]
    sha = sha256_hex(image)
    assert sha == recorded.image_sha256, "the decoded file is not the recorded flash"
    match = revision_match(sha, registry)
    assert match == {"revision": REVISION, "status": "matched", "reference": None}
    return Real(image, sha, match, registry, load_kit(PROFILE))


def request(edits: list[dict[str, Any]], **over: Any) -> rom_editor.SimRequest:
    """A complete simulate request: every field stated, the form's grid unless overridden."""
    body: dict[str, Any] = dict(
        label="W09.6",
        edits=edits,
        grid="fuel_comp",
        rpm_edges=FORM_RPM_EDGES,
        load_edges=FORM_LOAD_EDGES,
        **ENV,
        **LEARNED_ZERO,
        candidate_events="none",
        draws=DRAWS,
        seed=SEED,
    )
    body.update(over)
    return rom_editor.SimRequest.model_validate(body)


@dataclasses.dataclass(frozen=True)
class Run:
    out: dict[str, Any]
    result: MonteCarloResult
    template: PairedScenario
    design: SimDesign
    candidate: bytes


def simulate(real: Real, req: rom_editor.SimRequest, *, negative_control: bool = False) -> Run:
    """The editor's simulate, with the Monte Carlo result it aggregated kept
    (a spy on ``rom_editor.run_monte_carlo``) for per-pair checks."""
    runs: list[tuple[tuple[Any, ...], MonteCarloResult]] = []
    real_run = sim_montecarlo.run_monte_carlo

    def spy(*args: Any, **kw: Any) -> MonteCarloResult:
        result = real_run(*args, **kw)
        runs.append((args, result))
        return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(rom_editor, "run_monte_carlo", spy)
        if negative_control:
            base = rom_editor.library_base(real.image, real.revision_match, sha256=real.sha256)
            out = rom_editor._simulate(
                base,
                bytes(real.image),
                {"route": "negative_control"},
                req,
                kit=real.kit,
                profile_dir=PROFILE,
                registry=real.registry,
            )
        else:
            out = rom_editor.simulate_edits(
                real.image,
                real.revision_match,
                req,
                PROFILE,
                sha256=real.sha256,
                kit=real.kit,
                registry=real.registry,
            )
    ((args, result),) = runs
    # The same design the run used (built the same way; the hash says so).
    design = rom_editor._grid_design(req, REVISION, PROFILE, real.registry)
    assert design.sha256() == out["design"]["sha256"]
    template, candidate = args[1], args[4]
    assert isinstance(template, PairedScenario) and isinstance(candidate, bytes)
    return Run(out, result, template, design, candidate)


def _table(real: Real, table_id: str, slot: int | None) -> dict[str, Any]:
    """One table as ``GET .../tables`` sends it (``rom_image.table_values``)."""
    (table,) = [
        t
        for t in table_values(real.image, real.kit.rom_map)
        if (t["id"], t["slot"]) == (table_id, slot)
    ]
    return table


def _edges_from_breakpoints(breakpoints: Sequence[float]) -> list[float]:
    """``lib/romSim.ts`` ``edgesFromBreakpoints``: midpoints, half a step past
    each end, each to 7 significant figures (``Number(x.toPrecision(7))``)."""
    bp = list(breakpoints)
    edges = [bp[0] - (bp[1] - bp[0]) / 2]
    edges += [(a + b) / 2 for a, b in zip(bp, bp[1:], strict=False)]
    edges.append(bp[-1] + (bp[-1] - bp[-2]) / 2)
    return [float(f"{edge:.7g}") for edge in edges]


def _trim_load_edges(edges: Sequence[float]) -> list[float]:
    """``lib/romSim.ts`` ``trimLoadEdges`` (LOAD_MIN 0, LOAD_MAX 100)."""
    out = [e for e in edges if 0.0 <= e <= 100.0]
    if any(e < 0.0 for e in edges) and out[0] > 0.0:
        out.insert(0, 0.0)
    if any(e > 100.0 for e in edges) and out[-1] < 100.0:
        out.append(100.0)
    return out


def form_edges(real: Real) -> tuple[list[float], list[float]]:
    """The rpm edges and the untrimmed load edges the form suggests, from the
    Fuel Comp (Map 1) axes classified by decode quantity, never by rows/cols."""
    table = _table(real, "fuel_compensation", 1)
    by_kind: dict[str, list[float]] = {}
    for axis in (table["axes"]["rows"], table["axes"]["cols"]):
        quantity = axis["decode"]["quantity"].lower()
        kind = "rpm" if "rpm" in quantity or "engine speed" in quantity else "load"
        assert kind not in by_kind
        by_kind[kind] = [float(v) for v in axis["decoded"]]
    return _edges_from_breakpoints(by_kind["rpm"]), _edges_from_breakpoints(by_kind["load"])


def _fuel_comp_axes_raw(real: Real) -> tuple[list[int], list[int]]:
    """Fuel Comp's rpm axis as the ECM compares it (axis byte x 4 against the
    engine-speed word) and its fill axis (byte for byte), from the image."""
    axes = _table(real, "fuel_compensation", 1)["axes"]
    return [4 * b for b in axes["rows"]["raw"]], list(axes["cols"]["raw"])


def _nonzero_corners(breakpoints: Sequence[int], x: int) -> set[int]:
    """The breakpoint indices a 1-D linear interpolation gives a non-zero
    weight at ``x``, clamped at both ends. Written here from the axis, not
    through ``ecm_lookup``: the check below is that nothing outside these
    corners moves."""
    if x <= breakpoints[0]:
        return {0}
    if x >= breakpoints[-1]:
        return {len(breakpoints) - 1}
    for i in range(len(breakpoints) - 1):
        if breakpoints[i] <= x < breakpoints[i + 1]:
            return {i} if x == breakpoints[i] else {i, i + 1}
    raise AssertionError(f"{x} not bracketed by {list(breakpoints)}")


def _visited(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in out["cells"]["cells"] if c["pairs"]]


# -- the grid the form suggests ---------------------------------------------------------------


def test_the_form_suggests_these_edges_and_only_the_trimmed_grid_can_run(real: Real) -> None:
    rpm, load = form_edges(real)
    assert rpm == FORM_RPM_EDGES
    assert load == FORM_LOAD_EDGES_SUGGESTED
    assert _trim_load_edges(load) == FORM_LOAD_EDGES
    # Untrimmed, the design refuses past the fill model (the form blocks Run first).
    with pytest.raises(SimDesignRefusalError) as err:
        rom_editor._grid_design(
            request([K_PLUS_5], load_edges=load), REVISION, PROFILE, real.registry
        )
    assert str(err.value).startswith("load_outside_model")
    assert request([K_PLUS_5]).grid_points == FORM_POINTS


# -- 1. the negative control ------------------------------------------------------------------


@pytest.fixture(scope="module")
def negative(real: Real) -> Run:
    return simulate(real, request([K_PLUS_5]), negative_control=True)


def test_the_negative_control_is_unchanged_in_every_visited_cell(negative: Run) -> None:
    """The base's own bytes as the candidate, through the seam simulate_edits
    uses (E08.1 refuses a no-op spec). Every visited cell: nothing changed;
    an output with no supported pair is ``unsupported`` by name, its computed
    deltas zero."""
    out, result = negative.out, negative.result
    assert out["outcome"] == "identical"
    assert {r["status"] for r in out["cells"]["reach"].values()} == {"no_change"}
    visited = _visited(out)
    assert len(visited) == out["design"]["points"] == FORM_POINTS
    statuses: Counter[str] = Counter()
    reasons: set[str] = set()
    for cell in visited:
        for name, o in cell["outputs"].items():
            where = (cell["row"], cell["col"], name)
            statuses[o["status"]] += 1
            assert o["status"] in ("unchanged", "unsupported"), where
            assert o["changed_share"] in (0.0, None), where
            q = o["quantiles"]
            if q is not None:
                assert q["delta"] == {"p05": 0.0, "p50": 0.0, "p95": 0.0}, where
            if o["status"] == "unsupported":
                named = {r for entry in o["support"] for r in entry["reasons"]}
                assert named, where
                reasons |= named
        # The ECU side (BFS, phi) is unchanged wherever the target lookup
        # resolved; one cell's lookup sits in the enrichment gate's hysteresis
        # band (4,000 rpm, fill bytes 191-192 here) and names that instead.
        for name in ("base_fuel_schedule", "target_phi"):
            o = cell["outputs"][name]
            if o["status"] == "unsupported":
                assert {r for e in o["support"] for r in e["reasons"]} >= {GATE_UNKNOWN}
            else:
                assert o["status"] == "unchanged"
    assert statuses["unchanged"] > 0 and statuses["unsupported"] > 0
    # What made an output unsupported, measured 2026-10-15 on this grid: the
    # closed-loop trim authority (146 closed-loop cells), the undecoded short
    # pulse (16 cells) and the one gate-ambiguous cell. Nothing else.
    assert reasons == {TRIM_AUTHORITY_UNDECODED, SHORT_PULSE, GATE_UNKNOWN, "not_computed"}
    assert out["cells"]["overlay"]["edited_cells"] == []
    # Every pair, from the run the cells came from: whatever was computed is
    # identical on both sides, and BFS was computed everywhere.
    for name in result.outputs:
        computed = np.isfinite(result.base[name]) & np.isfinite(result.candidate[name])
        assert np.array_equal(result.base[name][computed], result.candidate[name][computed])
    assert np.isfinite(result.base["base_fuel_schedule"]).all()
    # Both loops are on the grid, so both sides of the chain were exercised.
    assert {"open_loop", "closed_loop"} <= set(np.unique(result.loop).tolist())


# -- 2. K +5 % --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def k_run(real: Real) -> Run:
    return simulate(real, request([K_PLUS_5], label="K +5 %"))


def test_a_k_edit_moves_bfs_by_the_realised_raw_ratio_at_every_open_loop_pair(
    real: Real, k_run: Run
) -> None:
    """E07.6's known answer (K +5 % -> BFS +5 %), as the realised raw ratio.

    **Bound, derived** (E08.7 item 2's, with this image's Fuel Comp instead of a
    flat 128): Tp_new - r Tp_old lies in (-1 - c, r(1 + c)), c = 9163/65536,
    r = K_new / K_old. BFS = 4 floor(Tp x FC / 128) with FC the same on both
    sides (only K moved; the fill byte is paired), so with f = FC / 128,
    BFS_new - r BFS_old lies in (-4(f(1 + c) + 1), 4r(f(1 + c) + 1)), plus
    E08.7's 0.2-count slack for the quotient's own truncation, scaled by f.
    f is bounded by this image's largest slot-1 Fuel Comp cell."""
    out, result = k_run.out, k_run.result
    (edit,) = out["candidate"]["cells"]
    assert (edit["table"], edit["row"], edit["col"]) == ("k_constant", 0, 0)
    assert (edit["old"], edit["new"]) == (28715, 30151)  # 30150.75, nearest
    r = edit["new"] / edit["old"]
    c = TP_SCALE / (1 << TP_SCALE_SHIFT)
    f = max(max(row) for row in _table(real, "fuel_compensation", 1)["raw"]) / 128
    hi = 4 * r * (f * (1 + c) + 1) + 0.2 * f
    lo = -4 * (f * (1 + c) + 1) - 0.2 * f
    base = result.base["base_fuel_schedule"]
    cand = result.candidate["base_fuel_schedule"]
    ceiling = _table(real, "bfs_ceiling", None)["raw"]
    flat = {v for row in ceiling for v in row}
    assert len(flat) == 1  # a flat curve: the clamp is one constant
    assert cand.max() < flat.pop() << 8  # nothing clamped on this grid
    open_pairs = (result.loop == "open_loop") & result.supported("base_fuel_schedule")
    assert open_pairs.sum() >= 100
    error = cand[open_pairs] - r * base[open_pairs]
    assert (error > lo).all() and (error < hi).all(), (float(error.min()), float(error.max()))
    # Every pair moved (BFS is far above the word at every grid point).
    assert (cand[open_pairs] != base[open_pairs]).all()
    # Per cell: an all-open-loop cell's relative dBFS quantiles are r - 1 to
    # within the bound over that run's smallest open-loop base BFS.
    rel_tol = max(hi, -lo) / float(base[open_pairs].min())
    checked = gated = 0
    for cell in _visited(out):
        if cell["loop_shares"] != {"open_loop": 1.0}:
            continue
        bfs = cell["outputs"]["base_fuel_schedule"]
        if bfs["status"] == "unsupported":  # the gate-ambiguous cell (negative control)
            assert {r for e in bfs["support"] for r in e["reasons"]} >= {GATE_UNKNOWN}
            gated += 1
            continue
        assert bfs["status"] == "changed" and bfs["supported_share"] == 1.0
        for q in ("p05", "p50", "p95"):
            assert abs(bfs["quantiles"]["relative_delta"][q] - (r - 1)) <= rel_tol
        checked += 1
    assert checked >= 20 and gated <= 1


# -- 3. Target AFR +4 counts, the WOT column --------------------------------------------------


@pytest.fixture(scope="module")
def wot_run(real: Real) -> Run:
    req = request(
        [TARGET_PLUS_4_WOT],
        label="Target AFR WOT column +4",
        grid="target_afr",
        rpm_edges=WOT_RPM_EDGES,
        load_edges=WOT_LOAD_EDGES,
        **E076_WOT_ENV,
        **E076_LEARNED,
        draws=WOT_DRAWS,
    )
    return simulate(real, req)


def test_a_target_afr_plus_4_moves_wot_phi_by_e076s_figure_through_e076s_own_path(
    real: Real, wot_run: Run
) -> None:
    """phi +0.03125 at every pair (4 counts of 1/128 on both corners the lookup
    reads), and the editor's pulse and lambda are E07.6's own ``run_paired`` at
    each grid point and drawn fill byte: ECU side exactly, lambda within
    E08.7's tolerance of E07.6's central value and range."""
    out, result, design = wot_run.out, wot_run.result, wot_run.design
    assert [(c["row"], c["col"], c["old"], c["new"]) for c in out["candidate"]["cells"]] == [
        (row, WOT_COLUMN, old, old + 4)
        for row, old in enumerate(r[WOT_COLUMN] for r in _table(real, "fuel_targets", 1)["raw"])
    ]
    phi_delta = result.candidate["target_phi"] - result.base["target_phi"]
    assert (phi_delta == E076_PHI_DELTA).all()
    assert (result.loop == "open_loop").all()
    for name in ("target_phi", "injector_pulse_ms", "delivered_lambda"):
        assert result.supported(name).all(), name
    # Aggregated on the Target AFR grid: one visited cell, the WOT column.
    (cell,) = _visited(out)
    assert cell["col"] == WOT_COLUMN
    statuses = {name: o["status"] for name, o in cell["outputs"].items()}
    assert statuses["target_phi"] == statuses["delivered_lambda"] == "changed"
    assert statuses["base_fuel_schedule"] == "not_reached"
    # E07.6's own path at each point and fill byte.
    for i in range(result.n_points):
        for byte in sorted(set(result.fill_bytes[i].tolist())):
            point = design.operating_point(i, fill_byte=int(byte))
            scenario = dataclasses.replace(wot_run.template, points=(point,))
            ref = run_paired(real.kit, scenario, real.image, wot_run.candidate).points[0]["outputs"]
            at = result.fill_bytes[i] == byte
            for name in ("base_fuel_schedule", "target_phi", "injector_pulse_ms"):
                assert (result.base[name][i][at] == ref[name]["base"]).all(), (i, byte, name)
                assert (result.candidate[name][i][at] == ref[name]["candidate"]).all()
            rel = result.relative_delta("delivered_lambda")[i][at]
            p05, p50, p95 = np.quantile(rel, [0.05, 0.5, 0.95])
            lam = ref["delivered_lambda"]
            lo, hi = lam["relative_delta_range_over_assumptions"]
            assert abs(p50 - lam["relative_delta"]) < P50_VS_CENTRAL, (i, byte, p50)
            assert lo <= p05 <= p95 <= hi, (i, byte, lo, p05, p95, hi)


def _wot_figures(wot_run: Run) -> dict[str, Any]:
    """The highest-BFS point of the WOT run (the form's top fill cell at 4,800 rpm)."""
    result = wot_run.result
    top = int(np.argmax(result.base["base_fuel_schedule"].max(axis=1)))
    pulse = result.candidate["injector_pulse_ms"][top] - result.base["injector_pulse_ms"][top]
    rel = result.relative_delta("delivered_lambda")[top]
    p05, p50, p95 = (float(v) for v in np.quantile(rel, [0.05, 0.5, 0.95]))
    return {
        "bfs": sorted(set(result.base["base_fuel_schedule"][top].tolist())),
        "pulse_delta_ms": sorted({round(float(v), 2) for v in pulse}),
        "lambda_rel": (p05, p50, p95),
    }


@pytest.mark.xfail(
    strict=True,
    reason=(
        "W09.6 finding 2026-10-15: at the form's top fill cell (98.2 %, 4,800 rpm, baro 81) "
        "the steady BFS on ZS44A-20261004 is 29,080, not E07.6's 42,000: pulse +0.28 ms "
        "(not +0.41), lambda p05/p50/p95 -2.507/-2.486/-2.468 % (outside -2.52...-2.49). "
        "E07.6's own run_paired gives the same at that point (the test above); the "
        "figures are BFS-conditional (E08.7 item 4) and no grid point reaches that BFS "
        "(test below)."
    ),
)
def test_e076s_written_wot_figures_at_the_grids_wot_point(wot_run: Run) -> None:
    """E07.6's WOT figures as written, within E07.6's stated tolerance, at the
    grid's highest-BFS WOT point. Kept literal; see the xfail reason."""
    got = _wot_figures(wot_run)
    p05, p50, p95 = got["lambda_rel"]
    lo, hi = E076_LAMBDA_RANGE
    checks = {
        "pulse": got["pulse_delta_ms"] == [E076_PULSE_DELTA_MS],
        "lambda_p50": round(p50, 3) == E076_LAMBDA_REL,
        "lambda_range": lo <= p05 <= p95 <= hi,
    }
    assert all(checks.values()), (checks, got)


def test_no_grid_point_reaches_e076s_wot_bfs_or_the_wot_column_by_x(real: Real) -> None:
    """Why the written figures cannot be reproduced on a grid: a grid takes
    fill as MAP / baro and solves the airflow through the plant's manifold
    model, so even the top of the fill model (99.9-100 %) at E07.6's 4,800 rpm
    and baro 81 stays far below the BFS E07.6's figures were measured at. And
    without the forced-column flag, the Target AFR WOT column is reached by no
    point of the form's grid: X never gets there."""
    top = simulate(
        real,
        request(
            [TARGET_PLUS_4_WOT],
            grid="target_afr",
            rpm_edges=WOT_RPM_EDGES,
            load_edges=[99.9, 100.0],
            **E076_WOT_ENV,
            **E076_LEARNED,
        ),
    )
    assert top.result.base["base_fuel_schedule"].max() < E076_BFS_FOR_PULSE
    no_flag = simulate(real, request([TARGET_PLUS_4_WOT], grid="target_afr"))
    result = no_flag.result
    phi_b, phi_c = result.base["target_phi"], result.candidate["target_phi"]
    computed = np.isfinite(phi_b) & np.isfinite(phi_c)
    assert computed.mean() > 0.99  # all but the gate-ambiguous point
    assert (phi_c[computed] == phi_b[computed]).all()
    assert all(c["col"] < WOT_COLUMN for c in _visited(no_flag.out))
    for cell in _visited(no_flag.out):
        phi = cell["outputs"]["target_phi"]
        # partly_supported: the cell holding the gate-ambiguous point.
        assert phi["status"] in ("unchanged", "unsupported", "partly_supported"), cell["col"]
        assert phi["changed_share"] in (0.0, None)


# -- 4. the Fuel Comp region edit -------------------------------------------------------------


@pytest.fixture(scope="module")
def fc_run(real: Real) -> tuple[Run, rom_editor.SimRequest]:
    spec = load_edit_spec(FIXTURE_SPEC)
    assert spec.base.revision == REVISION
    edits = [e.model_dump(mode="json", exclude_none=True) for e in spec.edits]
    req = request(edits, label=spec.label)
    run = simulate(real, req)
    # The page's spec is the fixture's spec: same hash.
    assert run.out["spec_sha256"] == spec.sha256()
    return run, req


def test_the_fuel_comp_region_edit_marks_its_cells_and_moves_only_what_reads_them(
    real: Real, fc_run: tuple[Run, rom_editor.SimRequest]
) -> None:
    run, _ = fc_run
    out, result, design = run.out, run.result, run.design
    edited = {(c["row"], c["col"]): (c["old"], c["new"]) for c in out["candidate"]["cells"]}
    assert edited == FC_EDITED_CELLS
    assert all(c["table"] == "fuel_compensation" for c in out["candidate"]["cells"])
    # The overlay on the fuel_comp grid: exactly those cells, nowhere else.
    marks = {
        (c["row"], c["col"]): (c["edited"]["old"], c["edited"]["new"])
        for c in out["cells"]["cells"]
        if c["edited"] is not None
    }
    assert marks == FC_EDITED_CELLS
    # Per pair: which edited cells the Fuel Comp lookup gives weight to.
    rpm_axis, fill_axis = _fuel_comp_axes_raw(real)
    reached = np.zeros(result.base["base_fuel_schedule"].shape, dtype=bool)
    for i, point in enumerate(design.points):
        rows = _nonzero_corners(rpm_axis, sim_rpm_word(point.rpm, real.kit.contract))
        for d, byte in enumerate(result.fill_bytes[i].tolist()):
            cols = _nonzero_corners(fill_axis, int(byte))
            reached[i, d] = any((r, c) in FC_EDITED_CELLS for r in rows for c in cols)
    assert 0 < reached.sum() < reached.size
    # Nothing that the lookup does not reach moves, in any output.
    for name in result.outputs:
        computed = np.isfinite(result.base[name]) & np.isfinite(result.candidate[name])
        delta = np.where(computed, result.candidate[name] - result.base[name], 0.0)
        assert (delta[~reached] == 0.0).all(), name
    bfs_moved = result.candidate["base_fuel_schedule"] != result.base["base_fuel_schedule"]
    assert bfs_moved.any() and not (bfs_moved & ~reached).any()
    lam = result.delta("delivered_lambda")
    lam_moved = np.isfinite(lam) & (lam != 0.0)
    assert not (lam_moved & ~reached).any()
    # Measured 2026-10-15: every reaching pair runs closed loop under ENV (the
    # region is light load), where the candidate holds the base's lambda at the
    # trim's fixed point (E07.6), so delta lambda is 0 at every pair and the
    # change is carried by fuel_trim_total_pct (unsupported: trim authority).
    assert (result.loop[reached] == "closed_loop").all()
    assert not lam_moved.any()
    trim = result.delta("fuel_trim_total_pct")
    assert (np.isfinite(trim) & (trim != 0.0) & reached).any()
    # Cells: an output reads changed only in a cell holding a reaching pair.
    order = {(p.cell_row, p.cell_col): i for i, p in enumerate(design.points)}
    for cell in _visited(out):
        i = order[(cell["row"], cell["col"])]
        for name in ("base_fuel_schedule", "delivered_lambda", "fuel_trim_total_pct"):
            status = cell["outputs"][name]["status"]
            if status in ("changed", "partly_supported"):
                assert reached[i].any(), (cell["row"], cell["col"], name, status)
            if not reached[i].any():
                assert status in ("unchanged", "unsupported", "not_reached"), (cell, name)


def test_a_real_result_crosses_run_job_and_pickles(
    real: Real, fc_run: tuple[Run, rom_editor.SimRequest], tmp_path: Path
) -> None:
    """The child's entry on the library's plain file gives the same result,
    and it survives the process boundary."""
    run, req = fc_run
    path = tmp_path / f"{real.sha256}.bin"
    path.write_bytes(real.image)
    params = {
        "image_path": str(path),
        "sha256": real.sha256,
        "revision_match": real.revision_match,
        "profile_dir": str(PROFILE),
        "request": req.model_dump(mode="json"),
    }
    job = rom_editor.run_job("rom_sim", params, kit=real.kit, registry=real.registry)
    assert "refused" not in job
    assert job == run.out
    assert pickle.loads(pickle.dumps(job)) == job
    json.dumps(job)


# -- the route --------------------------------------------------------------------------------


def test_the_route_simulates_the_uploaded_osiris_file(
    real: Real, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's flow: the ``.OsirisROM`` through the library's upload,
    matched by the library itself, then ``POST .../simulate`` through
    ``run_off_loop`` (in-process executor; the child's own ``load_kit``)."""
    config = isolated_config(tmp_path, daemon={"afr_calibration": PROFILE / "afr_calibration.yaml"})
    pool = CountingExecutor()
    monkeypatch.setattr(
        rom_editor_routes,
        "run_off_loop",
        functools.partial(workbench_jobs.run_off_loop, executor=pool),
    )
    monkeypatch.setattr(workbench_jobs, "limit_address_space", lambda nbytes: (nbytes, nbytes))
    source = OPERATOR_ROMS[IMAGE_KEY]
    req = request([K_PLUS_5], label="K +5 %", draws=2)
    try:
        with TestClient(make_app(config)) as client:
            up = client.post(
                "/control/rom/library",
                params={"filename": source.name},
                content=source.read_bytes(),
                headers=OCTET,
            )
            assert up.status_code == 200, up.text
            assert up.json()["sha256"] == real.sha256
            entry = client.get(f"/control/rom/library/{real.sha256}", headers=AUTH).json()
            assert entry["entry"]["revision_match"] == real.revision_match
            response = client.post(
                f"/control/rom/library/{real.sha256}/simulate",
                json=req.model_dump(mode="json"),
                headers=AUTH,
            )
    finally:
        pool.shutdown(wait=True)
    assert response.status_code == 200, response.text
    assert pool.submitted == 1
    expected = rom_editor.simulate_edits(
        real.image, real.revision_match, req, PROFILE, sha256=real.sha256, registry=real.registry
    )
    assert response.json() == json.loads(json.dumps(expected))
    assert response.json()["design"]["points"] == FORM_POINTS
