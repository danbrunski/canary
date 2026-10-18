"""E08.10: every result names the working assumptions it rests on, and a
reason reaches only the outputs downstream of the node it names.

Found by W09.6's real-image runs (``tests/test_rom_editor_private_rom.py``):
the VE a grid's numbers rest on (fuel-mv-1's working 0.7, bracket [0.4, 1.0])
was named nowhere, and a Target AFR lookup gate marked steady BFS unsupported
although BFS is computed before the lookup. Both are checked here on E07.6's
synthetic image; the real-image case is the private test's (8, 15) cell.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from src.analysis import paired_scenarios, rom_editor, twin_runner
from src.analysis.fuel_plant import ParameterRegistry, plant_reads
from src.analysis.paired_scenarios import (
    OUTPUTS,
    PLANT_ASSUMPTION_PREFIX,
    ReasonScope,
    SimKit,
    computed_node,
    rpm_word,
    run_paired,
)
from src.analysis.sim_montecarlo import (
    DESIGN_ASSUMPTION_PREFIX,
    FILL_BYTE,
    LoadedUncertainty,
    kit_for_plant,
)
from src.analysis.sim_report import render_report
from src.vehicle.tune_revisions import TuneRevisionRegistry
from tests import test_rom_editor, test_rom_sim_cli, test_sim_montecarlo
from tests.rom_fixtures import PROFILE
from tests.test_paired_scenarios import CRUISE, WOT, _edit_all, _scenario
from tests.test_rom_editor import request
from tests.test_sim_montecarlo import P_CRUISE, P_MODEL, P_WOT, _design, _run, _template

# The fixtures these tests share, bound here so pytest finds them: the
# synthetic kit, the CLI's synthetic world and grid run, the editor's registry.
kit_and_image = test_sim_montecarlo.kit_and_image
uncertainty = test_sim_montecarlo.uncertainty
world = test_rom_sim_cli.world
grid_design = test_rom_sim_cli.grid_design
spec_run = test_rom_sim_cli.spec_run
registry = test_rom_editor.registry
matched = test_rom_editor.matched

TARGET_GATE = "target_lookup:enrichment_gate_state_unknown"
VE = "manifold_pressure"
RAIL = "rail_pressure"


def _named(assumptions: list[str]) -> dict[str, str]:
    """The plant working assumptions a result names, by parameter id."""
    return {
        a[len(PLANT_ASSUMPTION_PREFIX) :].split(" ", 1)[0]: a
        for a in assumptions
        if a.startswith(PLANT_ASSUMPTION_PREFIX)
    }


def _registry_working_assumptions(kit: SimKit) -> set[str]:
    """What the registry itself says the plant runs on a working value for."""
    out = set()
    for pid in plant_reads(kit.plant.manifold_model):
        p = kit.plant.registry.get(pid)
        if p.working_assumption is not None and p.status != "known":
            out.add(pid)
    return out


def _with_registry(kit: SimKit, edit: Any) -> SimKit:
    raw = kit.plant.registry.model_dump(mode="json")
    for p in raw["parameters"]:
        edit(p)
    registry_ = ParameterRegistry.model_validate(raw)
    return dataclasses.replace(kit, plant=kit.plant.model_copy(update={"registry": registry_}))


def test_run_paired_names_each_working_assumption_the_registry_resolves(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """Parameter, value, bracket, source file and held/drawn, from the
    registry; VE only where the plant's manifold model sets MAP, the rail
    only where no rail reading was given. Moving the registry moves it."""
    kit, image = kit_and_image
    cand = _edit_all(image, kit, "fuel_compensation", 1, 3)
    model_wot = dataclasses.replace(
        WOT, map_kpa_source="plant_model", rail_kpa_source="working_assumption", map_kpa=0.0
    )
    result = run_paired(kit, _scenario(kit, image, cand, model_wot, CRUISE), image, cand)
    named = _named(result.assumptions)
    assert set(named) == _registry_working_assumptions(kit)
    assert {VE, RAIL} <= set(named)
    assert named[VE] == (
        f"{PLANT_ASSUMPTION_PREFIX}manifold_pressure (volumetric_efficiency) = 0.7, bracket "
        "[0.4, 1.0], held at that value (nissan-xterra-2008/fuel_plant.yaml registry, plant "
        "fuel-mv-1; unresolved effect manifold_pressure_unmeasured)"
    )
    # Stated MAP and rail: the plant reads neither working value.
    stated = run_paired(kit, _scenario(kit, image, cand, WOT, CRUISE), image, cand)
    assert set(_named(stated.assumptions)) == _registry_working_assumptions(kit) - {VE, RAIL}
    # A drawn parameter says so; the string carries no per-draw value.
    drawn = run_paired(
        kit,
        _scenario(kit, image, cand, model_wot),
        image,
        cand,
        plant_overrides={VE: 0.55},
    )
    assert "drawn inside its bracket in this run" in _named(drawn.assumptions)[VE]
    assert "0.55" not in _named(drawn.assumptions)[VE]

    # The registry moves: the result follows, value, bracket and membership.
    def move(p: dict[str, Any]) -> None:
        if p["id"] == VE:
            p["working_assumption"].update(value=0.65, bracket=[0.45, 0.95])
        if p["id"] == "fuel_stoich_afr":
            p.update(status="known", source="datasheet", value=14.7, working_assumption=None)

    moved = _with_registry(kit, move)
    again = _named(
        run_paired(moved, _scenario(moved, image, cand, model_wot), image, cand).assumptions
    )
    assert "= 0.65, bracket [0.45, 0.95]" in again[VE]
    assert "fuel_stoich_afr" not in again
    assert set(again) == _registry_working_assumptions(moved)


def test_a_monte_carlo_names_the_drawn_and_held_assumptions_and_the_designs(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    kit, image = kit_and_image
    cand = _edit_all(image, kit, "fuel_compensation", 1, 3)
    design = dataclasses.replace(
        _design(image, (P_MODEL, 190, 195), (P_CRUISE, 50, 55)),
        assumptions=("a stated design fact",),
        maf_frozen_points=1,
    )
    result = _run(kit, image, cand, design, uncertainty, draws=3, vary=[VE])
    named = _named(result.assumptions)
    assert set(named) == _registry_working_assumptions(kit)
    assert "drawn inside its bracket" in named[VE]
    assert all("held at that value" in a for pid, a in named.items() if pid != VE)
    assert f"{DESIGN_ASSUMPTION_PREFIX}a stated design fact" in result.assumptions
    frozen = [a for a in result.assumptions if "frozen there" in a]
    assert frozen and "the MAF of 1 of 2 points" in frozen[0] and "VE (drawn)" in frozen[0]
    assert all(a in result.caveats for a in result.assumptions)
    # A value change in the registry (bracket kept, so the declaration still
    # matches) reaches the Monte Carlo's string too.
    moved = _with_registry(
        kit, lambda p: p["working_assumption"].update(value=0.65) if p["id"] == VE else None
    )
    again = _run(moved, image, cand, design, uncertainty, draws=2)
    assert "= 0.65, bracket [0.4, 1.0]" in _named(again.assumptions)[VE]


def test_a_reason_reaches_only_the_outputs_downstream_of_its_node(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, _ = kit_and_image
    twin = computed_node(kit.model, "target_equiv_ratio")
    bfs = computed_node(kit.model, "base_fuel_schedule_word")
    assert (twin, bfs) == ("enrichment_gate", "base_fuel_schedule")
    scope = ReasonScope.of(
        kit,
        [
            ("rom:target_equiv_ratio_unevaluable", twin),
            ("rom:base_fuel_schedule_word_unevaluable", bfs),
            # Raised by the twin's trace, each naming its own node.
            ("rom:unmodelled:correction_sum", twin),
            ("rom:unmodelled:fill_blend", twin),
            ("rom:observed_not_predicted:load_input_x", twin),
            ("conditioned_on_changed_path:fuel_comp_load_byte", "fuel_comp_load_byte"),
            ("rom:unknown_bytes_changed", None),
        ],
    )
    below_target = set(OUTPUTS) - {"base_fuel_schedule"}
    for reason, reaches in (
        (TARGET_GATE, below_target),
        ("rom:target_equiv_ratio_unevaluable", below_target),
        ("rom:unmodelled:correction_sum", below_target),
        ("rom:observed_not_predicted:load_input_x", below_target),
        # Upstream of BFS: the twin raised it, but BFS rests on that node too.
        ("rom:unmodelled:fill_blend", set(OUTPUTS)),
        ("steady_bfs:outside_axis", set(OUTPUTS)),
        ("rom:base_fuel_schedule_word_unevaluable", set(OUTPUTS)),
        ("conditioned_on_changed_path:fuel_comp_load_byte", set(OUTPUTS)),
        # No node: every output, conservatively.
        ("rom:unknown_bytes_changed", set(OUTPUTS)),
        ("plant:manifold_above_ambient", set(OUTPUTS)),
        ("base_target_table:slot_unresolved", set(OUTPUTS)),
    ):
        assert {o for o in OUTPUTS if scope.reaches(reason, o)} == reaches, reason


def test_a_target_lookup_gate_leaves_bfs_supported_in_run_paired_and_the_fast_path(
    kit_and_image: tuple[SimKit, bytes],
    uncertainty: LoadedUncertainty,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The (8, 15) case on the synthetic image: the lookup at the WOT point
    lands in the enrichment gate's band. BFS is computed before it and stays
    supported; target phi, the pulse, lambda, fuel and the trim keep the
    reason. The fast path's own run_paired self-check passes."""
    kit, image = kit_and_image
    cand = _edit_all(image, kit, "fuel_compensation", 1, 3)
    # The lookup is the twin runner's target-phi stage (E09.2c): one patch
    # reaches run_paired and the fast path alike.
    real_predict = twin_runner.predict_phi
    gated_rpm = rpm_word(P_WOT.rpm, kit.contract)

    def in_the_band(*args: Any, **kw: Any) -> Any:
        out = real_predict(*args, **kw)
        if kw["rpm_raw"] == gated_rpm:
            return dataclasses.replace(
                out, word=None, phi=None, reason="enrichment_gate_state_unknown"
            )
        return out

    monkeypatch.setattr(twin_runner, "predict_phi", in_the_band)
    design = _design(image, (P_WOT, 190, 200), (P_CRUISE, 50, 70))
    result = _run(kit, image, cand, design, uncertainty, draws=4)
    assert result.supported("base_fuel_schedule")[0].all()
    assert np.isfinite(result.delta("base_fuel_schedule")[0]).all()
    for name in set(OUTPUTS) - {"base_fuel_schedule"}:
        for d in range(result.draws):
            assert TARGET_GATE in result.reasons(name, 0, d), name
    # And run_paired itself, pair by pair, at the drawn bytes.
    template = _template(kit, image, cand)
    for d in range(result.draws):
        points = tuple(
            dataclasses.replace(dp.point, conditioned={FILL_BYTE: float(result.fill_bytes[i, d])})
            for i, dp in enumerate(design.points)
        )
        plain = run_paired(
            kit,
            dataclasses.replace(template, points=points),
            image,
            cand,
            plant_overrides={pid: float(v[d]) for pid, v in result.parameters.items()},
        )
        bfs = plain.points[0]["outputs"]["base_fuel_schedule"]
        assert bfs["status"] == "supported" and bfs["reasons"] == []
        assert bfs["delta"] == result.delta("base_fuel_schedule")[0, d]
        for i, p in enumerate(plain.points):
            for name in OUTPUTS:
                assert result.reasons(name, i, d) == tuple(p["outputs"][name]["reasons"])


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def test_a_grid_run_names_ve_and_the_frozen_maf_through_the_cli_and_its_report(
    spec_run: dict[str, Any],
) -> None:
    """``tools/rom_sim.py run`` on a grid, now on the simulator's fuel-mv-2
    (E08.11): assumptions stay a list of strings, name the drawn VE floor 0.7
    [0.4, 1.0] and the bands it is the floor of, the held VE(rpm) fit with its
    bands and baseline, and the frozen MAF; the report's caveat block shows
    them before the first chart."""
    result = spec_run["result"]
    assert _strings(result["assumptions"]) and _strings(result["caveats"])
    ve = _named(result["assumptions"])[VE]
    assert "manifold_pressure (volumetric_efficiency) = 0.7, the floor under" in ve
    assert "rpm bands [500, 1000), [1000, 2000)" in ve and "bracket [0.4, 1.0], drawn" in ve
    fit = next(a for a in result["assumptions"] if "volumetric_efficiency(rpm) is the frozen" in a)
    assert fit.startswith(PLANT_ASSUMPTION_PREFIX)
    assert "na-stock-2026-10-08.mv2" in fit and "held at the fit in this run, never drawn" in fit
    assert "[2000, 3000) 0.888395 fit" in fit and "[500, 1000) 0.7 floor" in fit
    design = [a for a in result["assumptions"] if a.startswith(DESIGN_ASSUMPTION_PREFIX)]
    solved = next(a for a in design if "MAF solved on the frozen baseline" in a)
    frozen = next(a for a in design if "frozen there" in a)
    assert "na-stock-2026-10-08.mv2" in solved and "bracket [0.4, 1.0]" in solved
    page = (Path(spec_run["out"]) / "report.html").read_text()
    first_svg = page.index("<svg")
    for text in (ve, fit, solved, frozen):
        escaped = text.replace("&", "&amp;").replace("'", "&#x27;")
        assert 0 <= page.index(escaped) < first_svg
    assert page.index('id="working-assumptions"') < first_svg
    # The cell table carries them as caveats: the page is rebuilt from it alone.
    rebuilt = render_report(
        result["cell_table"], run=result["run"], timing=result["ignition_timing"]
    )
    assert rebuilt == page
    assert json.loads(json.dumps(result["assumptions"])) == result["assumptions"]


def test_assumptions_stay_strings_through_the_rom_editor(
    kit_and_image: tuple[SimKit, bytes],
    matched: dict[str, Any],
    registry: TuneRevisionRegistry,
) -> None:
    """``rom_editor.simulate_edits`` (called, not edited) passes the result's
    assumptions through as strings, VE and the frozen MAF among them."""
    kit, image = kit_and_image
    req = rom_editor.SimRequest.model_validate(request(draws=2))
    out = rom_editor.simulate_edits(image, matched, req, PROFILE, kit=kit, registry=registry)
    assert _strings(out["assumptions"]) and _strings(out["caveats"])
    assert "= 0.7, bracket [0.4, 1.0]" in _named(out["assumptions"])[VE]
    assert any("frozen there" in a for a in out["assumptions"])
    # The design's own ride in ``assumptions`` with their prefix (W09, ae534b60).
    assert any(a.startswith(DESIGN_ASSUMPTION_PREFIX) for a in out["assumptions"])


# -- E08.11: a VE(rpm) plant, and the verifier's minors ---------------------------------------


def test_a_ve_rpm_plant_names_its_held_fit_and_its_drawn_or_held_floor(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """On fuel-mv-2 a model-MAP result names the held VE(rpm) fit (band
    edges, the VE each band runs at, the baseline id) and the floor, as drawn
    or held, with the bands it is the floor of. A stated MAP names neither."""
    kit, _ = kit_and_image
    mv2 = kit_for_plant(kit, PROFILE, "fuel-mv-2")
    for drawn, how in (({VE}, "drawn inside its bracket"), (set(), "held at that value")):
        lines = paired_scenarios.plant_assumption_lines(mv2, [P_MODEL], drawn=drawn)
        floor = _named(lines)[VE]
        assert how in floor and "bracket [0.4, 1.0]" in floor
        assert "rpm bands [500, 1000), [1000, 2000)" in floor
        assert "fuel_plant_mv2.yaml registry, plant fuel-mv-2" in floor
        fit = _named(lines)["volumetric_efficiency(rpm)"]
        assert "frozen fit of baseline na-stock-2026-10-08.mv2" in fit
        assert "held at the fit in this run, never drawn" in fit
        assert (
            "[500, 1000) 0.7 floor, [1000, 2000) 0.7 floor, [2000, 3000) 0.888395 fit, "
            "[3000, 4000) 0.894657 fit, [4000, 5000) 0.905535 fit, [5000, 6500] 0.872824 fit"
        ) in fit
        assert "baro 79-82 kPa or IAT 23-50 C" in fit
    stated = paired_scenarios.plant_assumption_lines(mv2, [P_WOT], drawn={VE})
    assert VE not in _named(stated) and "volumetric_efficiency(rpm)" not in _named(stated)
    # fuel-mv-1 has one working VE and no fit to name.
    mv1 = paired_scenarios.plant_assumption_lines(kit, [P_MODEL], drawn={VE})
    assert "volumetric_efficiency(rpm)" not in _named(mv1)
    assert "floor" not in _named(mv1)[VE]


def test_the_manifold_models_parameters_are_named_only_where_map_is_the_models(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """A stated MAP reads none of the manifold model's parameters, so a
    fuel-mv-3 result on stated MAP names neither VE nor exhaust_pressure_ratio;
    on model MAP it names both (verifier minor, E08.11)."""
    kit, _ = kit_and_image
    mv3 = kit_for_plant(kit, PROFILE, "fuel-mv-3")
    at_stated = dataclasses.replace(P_MODEL, map_kpa_source="stated", map_kpa=79.0)
    stated = {a.parameter for a in paired_scenarios.plant_assumptions(mv3, [at_stated], drawn=())}
    model = {a.parameter for a in paired_scenarios.plant_assumptions(mv3, [P_MODEL], drawn=())}
    assert "exhaust_pressure_ratio" not in stated and VE not in stated
    assert {"exhaust_pressure_ratio", VE} <= model
    assert model - stated == {"exhaust_pressure_ratio", VE}


def test_a_parameter_is_a_working_assumption_only_at_its_registry_value(
    kit_and_image: tuple[SimKit, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``plant_assumptions`` labels a parameter a working assumption only when
    the number the run resolved is the registry's ``working_assumption.value``
    (verifier minor, E08.11); B06's restated delay is named from the registry
    like any other, not by a hand-kept exception."""
    kit, _ = kit_and_image
    named = {a.parameter for a in paired_scenarios.plant_assumptions(kit, [P_WOT], drawn=())}
    assert "lambda_observation_delay" in named and "bank_air_split" in named
    real = paired_scenarios.resolve_parameters

    def moved(*args: Any, **kw: Any) -> Any:
        out = real(*args, **kw)
        return dataclasses.replace(out, values={**out.values, "bank_air_split": 0.51})

    monkeypatch.setattr(paired_scenarios, "resolve_parameters", moved)
    after = {a.parameter for a in paired_scenarios.plant_assumptions(kit, [P_WOT], drawn=())}
    assert after == named - {"bank_air_split"}


def test_a_run_level_gate_withholds_only_the_outputs_it_reaches(
    kit_and_image: tuple[SimKit, bytes], uncertainty: LoadedUncertainty
) -> None:
    """The verifier's case: a Load Point Scaler edit. The scaler turns BFS into
    the X byte the Target AFR lookup reads, so its run-level gate reasons
    (``rom:observed_not_predicted:load_input_x`` and the rest) reach target phi
    and everything downstream, whose candidate is withheld, and not BFS, which
    is computed before it: BFS stays supported with its candidate, in
    run_paired and in the Monte Carlo's fast path alike."""
    from src.analysis.rom_effects import CellEdit, make_candidate

    kit, image = kit_and_image
    scaler = int.from_bytes(image[0x9416:0x9418], "big")
    cand = make_candidate(
        image, kit.rom_map, [CellEdit("load_point_scaler", 0, 0, scaler + 2000)], base_label="s"
    ).image
    plain = run_paired(kit, _scenario(kit, image, cand, WOT, CRUISE), image, cand)
    gate = "rom:observed_not_predicted:load_input_x"
    for point in plain.points:
        bfs = point["outputs"]["base_fuel_schedule"]
        assert bfs["status"] == "supported" and bfs["candidate"] is not None
        assert gate not in bfs["reasons"]
        for name in set(OUTPUTS) - {"base_fuel_schedule"}:
            out = point["outputs"][name]
            assert gate in out["reasons"] and out["candidate"] is None, name
    result = _run(kit, image, cand, _design(image, (P_WOT, 190, 200)), uncertainty, draws=3)
    assert result.supported("base_fuel_schedule").all()
    assert np.isfinite(result.candidate["base_fuel_schedule"]).all()
    for name in set(OUTPUTS) - {"base_fuel_schedule"}:
        assert np.isnan(result.candidate[name]).all(), name
        assert all(gate in result.reasons(name, 0, d) for d in range(result.draws))
