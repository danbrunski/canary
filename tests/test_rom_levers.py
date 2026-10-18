"""E09.14a: ``src.analysis.rom_levers`` names the lever and what blocks a number.

The world is E07.2's synthetic image (``tests.test_rom_effects.modelled_image``,
which every node's decode describes) against the committed Xterra profile.
Nothing real is read. What these pin:

- the three accepted strings (timing trim: the torque stage's blockers, "no
  torque plant" while it is unbuilt and, built, "torque production built, not
  validated (owed H07.10, ...)" with "spark response unmeasured"; a MAF-curve
  byte: "not mapped"; a Fuel Comp edit: "closed loop: trim authority
  undecoded");
- the edit's own torque/power claim (E07.5 item 8): the controls are derived
  from the comparison, and the claim is refused by name;
- that every blocker is *derived*: change the manifest, the contract or the
  plant's qualification and the blocker goes, so no hand-kept table can hide
  behind a passing test;
- that the runner's output names it joins to are the ones ``paired_scenarios``
  uses (the one place a name is restated).

Coupling created here: ``tests.test_rom_effects.modelled_image`` and ``_bump``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis import paired_scenarios
from src.analysis.ecu_application import ApplicationModel, load_application_model, program_sha256
from src.analysis.fuel_plant import Qualification, load_fuel_plant, plant_qualification
from src.analysis.paired_steady import (
    BFS_OUTPUT,
    ECU_RUNNER_OUTPUTS,
    FILL_BYTE,
    TRIM_AUTHORITY_UNDECODED,
    TWIN_OUTPUT,
    conditionable_quantities,
    steady_coverage,
)
from src.analysis.rom_effects import EffectReport, compare_roms, make_candidate
from src.analysis.rom_levers import (
    LEVER_LABELS,
    SIMULATED_OUTPUTS,
    TEXT_CLOSED_LOOP,
    TEXT_NO_TORQUE_PLANT,
    TEXT_NOT_MAPPED,
    LeverClassification,
    changed_controls,
    classify_edit,
    edit_claim_refusals,
    require_edit_claim,
)
from src.analysis.rom_map import RomMap
from src.analysis.sim_contract import SimContract, SimulationRefusalError, load_contract
from tests.rom_fixtures import PROFILE, xterra_map
from tests.test_rom_effects import _bump, modelled_image

MODE = "conditional_rom_comparison"
#: An image offset the application model does not read and ``rom_map.yaml``
#: does not map: the MAF curve (E09.8 maps it).
MAF_CURVE = 0xE27E
#: What the built, unvalidated torque stage says (E07.5 stage 4, 2026-10-17).
TEXT_TORQUE_UNVALIDATED = (
    "torque production built, not validated (owed H07.10, H07.5, H07.12, H07.8, N01.2"
)
TORQUE_CODES = {
    "engine_torque_not_computed",
    "subsystem_not_validated",
    "spark_response_unmeasured",
}


def _raw_contract() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / "engine_sim_contract.yaml").read_text())
    return raw


def _torque(raw: dict[str, Any]) -> dict[str, Any]:
    sub: dict[str, Any] = next(s for s in raw["subsystems"] if s["id"] == "torque_production")
    return sub


class World:
    def __init__(self) -> None:
        model, self.model_sha = load_application_model(PROFILE)
        self.image = modelled_image(model)
        identity = model.program.identity.model_copy(
            update={"sha256": program_sha256(self.image, model)}
        )
        self.model: ApplicationModel = model.model_copy(
            update={"program": model.program.model_copy(update={"identity": identity})}
        )
        self.rom_map: RomMap = xterra_map()
        self.contract: SimContract = load_contract(PROFILE)[0]
        self.plant_q: Qualification = plant_qualification(load_fuel_plant(PROFILE)[0])

    def candidate(self, table: str, slot: int | None = 1) -> bytes:
        edit = _bump(self.image, self.rom_map, table, slot, 0, 0)
        return make_candidate(self.image, self.rom_map, [edit], base_label="x").image

    def report(
        self, image: bytes, *, model: ApplicationModel | None = None, mode: bool = True
    ) -> EffectReport:
        extra: dict[str, Any] = {"contract": self.contract, "mode": MODE} if mode else {}
        return compare_roms(
            self.image,
            image,
            self.rom_map,
            model or self.model,
            self.model_sha,
            base_slot=1,
            candidate_slot=1,
            slot_source="default_slot",
            **extra,
        )

    def classify(
        self,
        image: bytes,
        *,
        model: ApplicationModel | None = None,
        contract: SimContract | None = None,
        qualification: Qualification | None = None,
        **kwargs: Any,
    ) -> LeverClassification:
        model = model or self.model
        return classify_edit(
            self.report(image, model=model),
            rom_map=self.rom_map,
            model=model,
            contract=contract or self.contract,
            qualification=qualification or self.plant_q,
            **kwargs,
        )


@pytest.fixture(scope="module")
def world() -> World:
    return World()


def _joined(result: LeverClassification) -> str:
    return "\n".join(result.messages)


def _blockers(result: LeverClassification, output: str) -> set[str]:
    return {b.code for o in result.outputs if o.output == output for b in o.blockers}


def _maf_image(world: World) -> bytes:
    image = bytearray(world.image)
    image[MAF_CURVE] ^= 0x01
    return bytes(image)


# -- the three accepted strings -----------------------------------------------------------------


def test_a_timing_trim_edit_names_the_torque_stage_and_what_it_owes(world: World) -> None:
    result = world.classify(world.candidate("timing_trim_a"))
    assert [e.lever for e in result.levers] == ["timing_trim_a"]
    assert TEXT_TORQUE_UNVALIDATED in _joined(result)
    assert TEXT_NO_TORQUE_PLANT not in _joined(result)  # the stage is built
    for output in ("ignition_trim_selected", "ignition_timing"):
        assert TORQUE_CODES <= _blockers(result, output)
        assert "no_torque_plant" not in _blockers(result, output)
    assert (
        "Timing trim A -> ignition_trim_selected: engine torque not computed in "
        "conditional_rom_comparison; " + TEXT_TORQUE_UNVALIDATED
    ) in _joined(result)
    assert "spark response unmeasured" in _joined(result)
    assert result.messages[-1].startswith("torque/power claim for this edit (spark): refused;")
    # The timing trim reaches no fuel output, so none is blamed.
    reached = {o.output for o in result.outputs if o.reached}
    assert reached == {"ignition_timing", "ignition_trim_selected"}


def test_a_maf_curve_byte_is_not_mapped(world: World) -> None:
    result = world.classify(_maf_image(world))
    (lever,) = result.levers
    assert lever.lever == "unmapped" and (lever.start, lever.end) == (MAF_CURVE, MAF_CURVE)
    assert TEXT_NOT_MAPPED == "not mapped"
    assert any(f"0x{MAF_CURVE:X}" in m and TEXT_NOT_MAPPED in m for m in result.messages)
    # Unknown bytes make every output unknown, never unchanged.
    assert all(o.status == "unknown" for o in result.outputs)
    assert all("not_mapped" in {b.code for b in o.blockers} for o in result.outputs)
    assert not any(o.number for o in result.outputs)


def test_a_fuel_comp_edit_says_closed_loop_trim_authority_is_undecoded(world: World) -> None:
    result = world.classify(world.candidate("fuel_compensation"))
    assert [e.lever for e in result.levers] == ["fuel_compensation"]
    assert TEXT_CLOSED_LOOP == "closed loop: trim authority undecoded"
    assert TEXT_CLOSED_LOOP in _joined(result)
    for output in SIMULATED_OUTPUTS:
        assert TRIM_AUTHORITY_UNDECODED in _blockers(result, output)
    # The ECU-side outputs are not gated by the loop; the plant's standing is its own.
    assert TRIM_AUTHORITY_UNDECODED not in _blockers(result, "base_fuel_schedule_word")
    # The steady fixed point covers what a Fuel Comp edit passes through (the
    # absorbed filters, X computed from BFS): the run computes both ECU outputs,
    # so neither is blocked (E09.14a verifier, 2026-10-16).
    assert _blockers(result, "base_fuel_schedule_word") == set()
    assert _blockers(result, "target_equiv_ratio") == set()
    ecu = {o.output: o for o in result.outputs if o.output in ECU_RUNNER_OUTPUTS.values()}
    assert all(o.number for o in ecu.values())
    for output in ("base_fuel_schedule_word", "target_equiv_ratio"):
        assert f"-> {output}: a number can be given" in _joined(result)
    # Ignition is not computed by any run: it keeps every reason.
    assert "unmodelled: " in " ".join(m for m in result.messages if "ignition_timing" in m)
    assert "plant_not_qualified" in _blockers(result, "delivered_lambda")


def test_a_regime_other_than_closed_loop_does_not_carry_the_closed_loop_blocker(
    world: World,
) -> None:
    candidate = world.candidate("fuel_compensation")
    assert TEXT_CLOSED_LOOP in _joined(world.classify(candidate, regime="closed_loop"))
    assert TEXT_CLOSED_LOOP in _joined(world.classify(candidate))  # not stated: listed
    assert TEXT_CLOSED_LOOP not in _joined(world.classify(candidate, regime="fuel_cut"))
    with pytest.raises(ValueError, match="regime"):
        world.classify(candidate, regime="wot")  # type: ignore[arg-type]


# -- everything is derived, nothing is a table kept here -----------------------------------------


def test_a_node_the_manifest_calls_implemented_stops_blocking(world: World, tmp_path: Path) -> None:
    """A scratch copy of the application model with one node flipped to
    ``implemented``: the blocker naming it is gone from the same edit. Since
    E09.7 implemented the timing chain a trim edit names no unmodelled node,
    so a K edit (which reaches ignition timing through X) carries it."""
    candidate = world.candidate("k_constant")
    before = world.classify(candidate)
    assert "unmodelled: " in _joined(before)
    assert "knock_trim_difference" in _joined(before)

    text = (PROFILE / "ecu_application_model.yaml").read_text()
    data = yaml.safe_load(text)
    (node,) = [n for n in data["nodes"] if n["id"] == "knock_trim_difference"]
    assert node["twin"] == "not_modelled"
    node["twin"] = "implemented"
    node["implemented_by"] = "src.decode.air_charge.rpm_load_lookup"
    # The loader holds each output's blockers to its unresolved nodes.
    for output in data["outputs"]:
        for blocker in output.get("blockers", []):
            blocker["nodes"] = [n for n in blocker["nodes"] if n != "knock_trim_difference"]
        # A blocker left naming no node is closed.
        output["blockers"] = [b for b in output.get("blockers", []) if b["nodes"]]
    scratch = tmp_path / "profile"
    scratch.mkdir()
    (scratch / "ecu_application_model.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    mutated, _ = load_application_model(scratch)
    mutated = mutated.model_copy(update={"program": world.model.program})
    after = world.classify(candidate, model=mutated)
    assert "knock_trim_difference" not in _joined(after)
    # The rest of the chain still blocks, and the torque stage is still unvalidated.
    assert "fill_blend" in _joined(after) and TEXT_TORQUE_UNVALIDATED in _joined(after)


def test_a_closed_loop_override_qualified_on_the_vehicle_lifts_the_closed_loop_blocker(
    world: World,
) -> None:
    candidate = world.candidate("fuel_compensation")
    assert TEXT_CLOSED_LOOP in _joined(world.classify(candidate))
    overrides = [
        o.model_copy(update={"qualification": "vehicle", "qualified_by": ["tests/x.py::t"]})
        if o.regime == "closed_loop"
        else o
        for o in world.model.overrides
    ]
    qualified = world.model.model_copy(update={"overrides": overrides})
    assert TEXT_CLOSED_LOOP not in _joined(world.classify(candidate, model=qualified))


def test_the_torque_blocker_follows_the_stages_derived_standing(world: World) -> None:
    """Unbuilt: "no torque plant". Built (the profile): not validated, naming
    what it owes. A recorded pass against an available, independent reference
    lifts the stage's blocker; the spark sweep lifts the spark one; the ROM
    comparison still computes no torque."""
    candidate = world.candidate("timing_trim_a")
    raw = _raw_contract()
    sub = _torque(raw)
    for key in ("implementation", "applicability"):
        sub.pop(key)
    sub["status"] = "unbuilt"
    unbuilt = SimContract.model_validate(raw)
    result = world.classify(candidate, contract=unbuilt)
    assert TEXT_NO_TORQUE_PLANT in _joined(result)
    assert "no_torque_plant" in _blockers(result, "ignition_trim_selected")
    assert "no_torque_plant" in _blockers(result, "ignition_timing")

    raw = _raw_contract()
    sub = _torque(raw)
    for ref in sub["references"]:
        if ref["id"] == "h07_wheel_torque":
            ref["status"], ref["requires"] = "available", None
    sub["validations"] = [
        {
            "reference": "h07_wheel_torque",
            "recordings": ["_capture-h07-2026-11-01T120000"],
            "verdict": "pass",
            "scored_on": "2026-11-01",
            "evidence": "a stand-in pass for the test",
        }
    ]
    for m in sub["required_measurements"]:
        m["status"] = "available" if m["id"] == "controlled_spark_sweep" else m["status"]
    validated = SimContract.model_validate(raw)
    result = world.classify(candidate, contract=validated)
    codes = _blockers(result, "ignition_timing")
    assert "spark_response_unmeasured" not in codes
    assert TEXT_TORQUE_UNVALIDATED not in _joined(result)
    assert "torque production" not in _joined(result)
    # Still blocked: the fuel stage the mode computes lambda with is not
    # validated, and the comparison does not compute torque at all.
    assert {"engine_torque_not_computed", "subsystem_not_validated"} <= codes
    assert "fuel delivery built, not validated (plant not qualified)" in _joined(result)


def test_an_output_no_contract_quantity_carries_says_no_plant_consumes_it(world: World) -> None:
    quantities = [
        q.model_copy(update={"manifest_node": None}) if q.id == "ignition_timing_byte" else q
        for q in world.contract.quantities
    ]
    uncarried = world.contract.model_copy(update={"quantities": quantities})
    result = world.classify(world.candidate("timing_trim_a"), contract=uncarried)
    assert "no_plant_consumer" in _blockers(result, "ignition_trim_selected")
    assert "no_plant_consumer" in _blockers(result, "ignition_timing")


def test_a_contract_quantity_that_carries_an_output_lifts_its_plant_blocker(
    world: World,
) -> None:
    """Join a contract quantity to the timing writer, and the output has a
    consumer: neither plant blocker is produced."""
    quantities = [
        q.model_copy(update={"manifest_node": "timing_trim_lookup"})
        if q.manifest_node == "base_fuel_schedule"
        else q
        for q in world.contract.quantities
    ]
    carried = world.contract.model_copy(update={"quantities": quantities})
    result = world.classify(world.candidate("timing_trim_a"), contract=carried)
    assert _blockers(result, "ignition_trim_selected") == set()


def test_a_qualified_plant_lifts_plant_not_qualified(world: World) -> None:
    candidate = world.candidate("fuel_compensation")
    assert "plant not qualified" in _joined(world.classify(candidate))
    qualified = Qualification(True, ("closed_loop", "enrichment"), None, "x", "fuel-mv-1", False)
    result = world.classify(candidate, qualification=qualified)
    assert "plant not qualified" not in _joined(result)
    assert TEXT_CLOSED_LOOP in _joined(result)  # a separate blocker, a separate cause


def test_the_runner_names_it_joins_are_the_ones_paired_scenarios_uses() -> None:
    """One mapping, in ``paired_steady``; the run re-exports it and derives its
    own ECU/plant split from it."""
    assert paired_scenarios.ECU_RUNNER_OUTPUTS is ECU_RUNNER_OUTPUTS
    assert set(ECU_RUNNER_OUTPUTS) == set(paired_scenarios._ECU_OUTPUTS)
    assert ECU_RUNNER_OUTPUTS["base_fuel_schedule"] == paired_scenarios._BFS_OUTPUT == BFS_OUTPUT
    assert ECU_RUNNER_OUTPUTS["target_phi"] == paired_scenarios._TWIN_OUTPUT == TWIN_OUTPUT
    assert set(SIMULATED_OUTPUTS) == set(paired_scenarios.OUTPUTS) - paired_scenarios._ECU_OUTPUTS
    model, _ = load_application_model(PROFILE)
    assert set(ECU_RUNNER_OUTPUTS.values()) <= {o.id for o in model.outputs}


def test_levers_are_the_maps_kinds_and_a_label_never_names_a_missing_kind() -> None:
    rom_map = xterra_map()
    kinds = {t.kind for t in rom_map.tables if t.kind}
    assert set(LEVER_LABELS) <= kinds


def test_an_axis_edit_is_its_tables_lever(world: World) -> None:
    result = world.classify(world.candidate("fuel_comp_engine_speed_axis", None))
    assert [e.lever for e in result.levers] == ["fuel_compensation"]
    assert result.levers[0].tables == ("fuel_comp_engine_speed_axis",)


def test_every_mapped_table_is_a_lever_with_a_label(world: World) -> None:
    """A table added to the map (E09.7-E09.9) becomes a lever with no change here."""
    for table in world.rom_map.tables:
        for slot in world.rom_map.table_slots(table):
            if table.slot_offset is not None and slot != 1:
                continue
            address = world.rom_map.table_address(table, slot)
            image = bytearray(world.image)
            image[address] ^= 0x01
            result = world.classify(bytes(image))
            assert result.levers, table.id
            assert all(e.label for e in result.levers), table.id
            assert result.messages


# -- shape --------------------------------------------------------------------------------------


def test_the_result_is_plain_stable_json_and_forbids_extra_fields(world: World) -> None:
    candidate = world.candidate("fuel_compensation")
    cells = [{"table": "fuel_compensation", "slot": 1, "row": 0, "col": 0}]
    a = world.classify(candidate, cells=cells)
    b = world.classify(candidate, cells=cells)
    assert json.dumps(a.to_dict(), sort_keys=True) == json.dumps(b.to_dict(), sort_keys=True)
    assert a.to_dict()["messages"] == list(a.messages)
    assert a.levers[0].changed_cells == 1
    assert world.classify(candidate).levers[0].changed_cells is None
    reached = {o["output"]: o["reached"] for o in a.to_dict()["outputs"]}
    pulse = next(o for o in a.to_dict()["outputs"] if o["output"] == "injector_pulse_ms")
    assert reached["injector_pulse_ms"] and not pulse["number"]
    with pytest.raises(ValueError):
        LeverClassification.model_validate({**a.model_dump(), "surprise": 1})


def test_an_unusable_comparison_is_a_message_not_a_crash(world: World) -> None:
    refused = compare_roms(
        world.image[:100],
        world.image,
        world.rom_map,
        world.model,
        world.model_sha,
        base_slot=1,
        candidate_slot=1,
        slot_source="default_slot",
    )
    assert refused.reason is not None
    result = classify_edit(
        refused,
        rom_map=world.rom_map,
        model=world.model,
        contract=world.contract,
        qualification=world.plant_q,
    )
    assert result.reason == refused.reason and result.levers == ()
    assert refused.reason in result.messages[0]


def test_an_identical_image_changes_nothing(world: World) -> None:
    result = world.classify(world.image)
    assert result.outcome == "identical" and result.levers == ()
    assert result.messages == ("no change: the candidate is the baseline image",)
    assert not any(o.reached for o in result.outputs)


def test_a_checksum_only_change_is_bookkeeping_and_reaches_nothing(world: World) -> None:
    image = bytearray(world.image)
    image[world.rom_map.checksums.sum_offset] ^= 0xFF
    result = world.classify(bytes(image))
    assert [e.lever for e in result.levers] == ["checksum"]
    assert not any(o.reached for o in result.outputs)


# -- no ECU in the import chain -----------------------------------------------------------------

_WRITE_PATHS = ("src.control", "src.daemon", "src.dashboard", "src.mcp_server", "can", "serial")
_ALLOWED_COLLECTION = {"src.collection", "src.collection.kline", "src.collection.kline.names"}


def test_classifying_an_edit_imports_nothing_that_talks_to_the_ecu() -> None:
    script = textwrap.dedent(
        """
        import json, sys
        from tests.test_rom_levers import World
        w = World()
        result = w.classify(w.candidate("timing_trim_a"))
        assert "spark response unmeasured" in "\\n".join(result.messages)
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
    reached = [
        m
        for m in loaded
        if any(m == p or m.startswith(p + ".") for p in _WRITE_PATHS)
        or (m.startswith("src.collection") and m not in _ALLOWED_COLLECTION)
    ]
    assert reached == []


# -- the run's own rule, cross-checked against run_paired -----------------------------------


class RunWorld:
    """test_paired_scenarios' synthetic image and re-pinned real model, which
    ``run_paired`` can compute on (E07.6)."""

    def __init__(self) -> None:
        import dataclasses

        from src.analysis.paired_scenarios import load_kit
        from tests import test_paired_scenarios as tp

        kit = load_kit(PROFILE)
        self.image = tp._image(kit.model, kit.plant)
        identity = kit.model.program.identity.model_copy(
            update={"sha256": program_sha256(self.image, kit.model)}
        )
        model = kit.model.model_copy(
            update={"program": kit.model.program.model_copy(update={"identity": identity})}
        )
        self.kit = dataclasses.replace(kit, model=model)
        self.tp = tp

    def edit_all(self, table: str, slot: int | None, step: int) -> bytes:
        return self.tp._edit_all(self.image, self.kit, table, slot, step)

    def run(self, candidate: bytes, kit: Any = None) -> Any:
        kit = kit or self.kit
        scenario = self.tp._scenario(kit, self.image, candidate, self.tp.WOT, self.tp.CRUISE)
        return paired_scenarios.run_paired(kit, scenario, self.image, candidate)

    def classify(self, candidate: bytes, kit: Any = None, **kwargs: Any) -> LeverClassification:
        kit = kit or self.kit
        report = compare_roms(
            self.image,
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
        return classify_edit(
            report,
            rom_map=kit.rom_map,
            model=kit.model,
            contract=kit.contract,
            qualification=plant_qualification(kit.plant),
            **kwargs,
        )


@pytest.fixture(scope="module")
def run_world() -> RunWorld:
    return RunWorld()


_NODE_REASONS = ("unmodelled", "observed_not_predicted", "held_fixed", "outside_decode")


def _run_rom_reasons(result: Any, runner_output: str) -> set[str]:
    """The ``rom:<code>:<node>`` reasons the run gave one output at the first
    (WOT) point, which no per-point value changes."""
    reasons = result.points[0]["outputs"][runner_output]["reasons"]
    return {r for r in reasons if r.startswith("rom:") and r.split(":")[1] in _NODE_REASONS}


@pytest.mark.parametrize(
    ("table", "slot", "step"),
    [
        ("fuel_compensation", 1, 5),  # covered: the run computes BFS and the word
        ("fuel_comp_engine_speed_axis", None, 1),
        ("fill_curve_low", None, 2),  # edits an uncovered node itself
        ("fill_time_constant_rising", None, 3),  # a filter's own table
    ],
)
def test_each_ecu_outputs_blockers_are_the_runs_gate(
    run_world: RunWorld, table: str, slot: int | None, step: int
) -> None:
    candidate = run_world.edit_all(table, slot, step)
    result = run_world.run(candidate)
    classified = run_world.classify(candidate)
    for runner_output, model_output in ECU_RUNNER_OUTPUTS.items():
        blockers = {
            f"rom:{b.code}:{b.node}"
            for o in classified.outputs
            if o.output == model_output
            for b in o.blockers
            if b.code in _NODE_REASONS
        }
        assert blockers == _run_rom_reasons(result, runner_output), (table, model_output)
    # And the verdict agrees: supported at WOT exactly when nothing blocks it.
    for runner_output, model_output in ECU_RUNNER_OUTPUTS.items():
        status = result.points[0]["outputs"][runner_output]["status"]
        row = next(o for o in classified.outputs if o.output == model_output)
        assert (status == "supported") == (not row.blockers), (table, model_output)


def test_an_edit_to_a_filter_node_itself_keeps_its_uncovered_blocker(
    run_world: RunWorld,
) -> None:
    candidate = run_world.edit_all("fill_curve_low", None, 2)
    classified = run_world.classify(candidate)
    assert "unmodelled: " in _joined(classified)
    row = next(o for o in classified.outputs if o.output == "base_fuel_schedule_word")
    assert any(b.code == "unmodelled" and b.node == "fuel_comp_load_byte" for b in row.blockers)
    # The covered nodes on the same path do not appear; the edited one does.
    coverage = steady_coverage(
        run_world.kit.contract, MODE, ("fuel_comp_load_byte", "fill_coefficient")
    )
    assert not coverage.covers("unmodelled:fuel_comp_load_byte")
    # Downstream plant outputs inherit what the run withholds.
    pulse = next(o for o in classified.outputs if o.output == "injector_pulse_ms")
    assert any(b.node == "fuel_comp_load_byte" for b in pulse.blockers)


def test_the_closed_loop_function_drives_both_the_run_and_the_classifier(
    run_world: RunWorld,
) -> None:
    candidate = run_world.edit_all("fuel_compensation", 1, 5)

    def run_reasons(kit: Any) -> set[str]:
        out = run_world.run(candidate, kit).points[1]["outputs"]  # the cruise point
        return {r for name in SIMULATED_OUTPUTS for r in out[name]["reasons"]}

    assert TRIM_AUTHORITY_UNDECODED in run_reasons(run_world.kit)
    assert TEXT_CLOSED_LOOP in _joined(run_world.classify(candidate))
    # Qualify every closed-loop override on the vehicle (they are read together).
    overrides = [
        o.model_copy(update={"qualification": "vehicle", "qualified_by": ["tests/x.py::t"]})
        if o.regime == "closed_loop"
        else o
        for o in run_world.kit.model.overrides
    ]
    import dataclasses

    qualified = dataclasses.replace(
        run_world.kit, model=run_world.kit.model.model_copy(update={"overrides": overrides})
    )
    assert TRIM_AUTHORITY_UNDECODED not in run_reasons(qualified)
    assert TEXT_CLOSED_LOOP not in _joined(run_world.classify(candidate, qualified))
    # One override left pending keeps it, in both.
    partial = [
        o.model_copy(update={"qualification": "vehicle", "qualified_by": ["tests/x.py::t"]})
        if o.id == "closed_loop_phi_one"
        else o
        for o in run_world.kit.model.overrides
    ]
    kept = dataclasses.replace(
        run_world.kit, model=run_world.kit.model.model_copy(update={"overrides": partial})
    )
    assert TRIM_AUTHORITY_UNDECODED in run_reasons(kept)
    assert TEXT_CLOSED_LOOP in _joined(run_world.classify(candidate, kept))


def test_a_report_made_without_the_runs_mode_is_refused(world: World) -> None:
    candidate = world.candidate("fuel_compensation")
    with pytest.raises(ValueError, match="contract mode"):
        classify_edit(
            world.report(candidate, mode=False),
            rom_map=world.rom_map,
            model=world.model,
            contract=world.contract,
            qualification=world.plant_q,
        )


# -- wording that does not over-claim (verifier, 2026-10-16) ------------------------------------


def test_a_mapped_table_no_node_reads_says_what_the_unknown_reason_means(world: World) -> None:
    """``unknown_bytes_changed`` is one run reason for two causes. A mapped
    table no node reads is not "not mapped", and it keeps its lever label."""
    result = world.classify(world.candidate("load_point_scaler_slot_copy"))
    assert [e.lever for e in result.levers] == ["load_point_scaler"]
    lines = [m for m in result.messages if " -> " in m]
    assert lines and all(m.startswith("Load Point Scaler -> ") for m in lines)
    assert all("a mapped table no node reads (load_point_scaler_slot_copy)" in m for m in lines)
    assert not any(TEXT_NOT_MAPPED in m for m in lines)
    for o in result.outputs:
        if o.reached:
            assert o.levers == ("Load Point Scaler",)
            assert {b.code for b in o.blockers} & {"mapped_unmodelled"}
            assert "not_mapped" not in {b.code for b in o.blockers}
    # The unmapped case is unchanged.
    maf = world.classify(_maf_image(world))
    assert any(m.endswith("-> target_equiv_ratio: not mapped") for m in maf.messages)


def _stated(rw: RunWorld, candidate: bytes, **conditioned: float) -> Any:
    import dataclasses

    point = dataclasses.replace(rw.tp.WOT, conditioned=conditioned, source="design: test")
    scenario = rw.tp._scenario(rw.kit, rw.image, candidate, point)
    return paired_scenarios.run_paired(rw.kit, scenario, rw.image, candidate).points[0]["outputs"]


def test_a_number_is_only_claimed_for_points_that_state_the_fill_byte_and_nothing_it_reaches(
    run_world: RunWorld,
) -> None:
    candidate = run_world.edit_all("fuel_compensation", 1, 5)
    result = run_world.classify(candidate)
    ecu = [o for o in result.outputs if o.output in ECU_RUNNER_OUTPUTS.values()]
    assert ecu and all(o.number for o in ecu)
    expect = "a number can be given (for points that state only the fill byte)"
    for o in ecu:
        assert f"-> {o.output}: {expect}" in _joined(result)
        assert o.assumes_conditioned == (FILL_BYTE,)
    refused_by = {o.output: o.refused_if_conditioned for o in ecu}
    # Stating the final command word withholds the Target AFR word, which sits
    # downstream of it, and not BFS, which does not.
    assert "target_phi_word" in refused_by["target_equiv_ratio"]
    assert "target_phi_word" not in refused_by["base_fuel_schedule_word"]
    plain = result.to_dict()
    row = next(o for o in plain["outputs"] if o["output"] == "target_equiv_ratio")
    assert row["design_dependent"] is True and row["assumes_conditioned"] == [FILL_BYTE]
    # Derived from the contract's mode, not a literal.
    assert set(conditionable_quantities(run_world.kit.contract, MODE)) >= {
        FILL_BYTE,
        "target_phi_word",
    }
    # An output with a blocker makes no claim, so it has no caveat.
    pulse = next(o for o in result.outputs if o.output == "injector_pulse_ms")
    assert not pulse.design_dependent and pulse.assumes_conditioned == ()

    # The run agrees with every clause of the wording.
    ok = _stated(run_world, candidate, **{FILL_BYTE: 60.0})
    assert ok["base_fuel_schedule"]["status"] == "supported"
    assert ok["target_phi"]["status"] == "supported"
    missing = _stated(run_world, candidate)
    assert f"conditioning_missing:{FILL_BYTE}" in missing["base_fuel_schedule"]["reasons"]
    assert missing["base_fuel_schedule"]["status"] == "unsupported"
    for model_output, runner_output in ((m, r) for r, m in ECU_RUNNER_OUTPUTS.items()):
        for quantity in ("target_phi_word",):
            refused = _stated(run_world, candidate, **{FILL_BYTE: 60.0, quantity: 16000.0})
            said = quantity in refused_by[model_output]
            gave = f"conditioned_on_changed_path:{quantity}" in refused[runner_output]["reasons"]
            assert said == gave, (model_output, quantity)
            assert (refused[runner_output]["status"] == "unsupported") == said


# -- the edit's claim (E07.5 item 8) -----------------------------------------------------------


def test_the_controls_an_edit_changes_are_read_from_the_comparison(world: World) -> None:
    """A timing edit is spark; Fuel Targets is lambda; Fuel Comp and K move BFS,
    which indexes the timing tables (load input X), so they are both; bytes no
    node reads leave the controls unresolved."""

    def controls(image: bytes) -> tuple[tuple[str, ...], tuple[str, ...]]:
        edit = changed_controls(world.report(image), world.model, world.contract)
        return edit.controls, edit.unresolved

    assert controls(world.candidate("timing_trim_a")) == (("spark",), ())
    assert controls(world.candidate("fuel_targets")) == (("lambda",), ())
    assert controls(world.candidate("fuel_compensation")) == (("lambda", "spark"), ())
    assert controls(world.candidate("k_constant")) == (("lambda", "spark"), ())
    _, unresolved = controls(_maf_image(world))
    assert "unknown_bytes_changed" in unresolved
    assert controls(world.image) == ((), ())
    # Bytes changed but no output and no contract quantity reached: nothing
    # says what the edit moves, so the controls are unresolved, not empty.
    report = world.report(world.candidate("timing_trim_a"))
    silent = dataclasses.replace(
        report,
        changed_nodes=(),
        outputs=tuple(
            dataclasses.replace(o, status="unchanged", path=(), reasons=()) for o in report.outputs
        ),
    )
    edit = changed_controls(silent, world.model, world.contract)
    assert edit.controls == () and edit.unresolved == ("no_contract_quantity_reached",)


def test_a_torque_or_power_claim_for_an_edit_is_refused_by_name(world: World) -> None:
    def refusals(image: bytes, claim: str = "power") -> list[str]:
        return edit_claim_refusals(
            world.report(image),
            model=world.model,
            contract=world.contract,
            qualification=world.plant_q,
            claim=claim,
        )

    timing = refusals(world.candidate("timing_trim_a"))
    assert timing[0].startswith("engine_torque_not_computed")
    assert "subsystem_not_validated:torque_production" in timing
    assert any(r.startswith("spark_response_unmeasured") for r in timing)
    fuel = refusals(world.candidate("fuel_targets"), "torque")
    assert any(r.startswith("lambda_response_unmeasured") for r in fuel)
    assert not any(r.startswith("spark_response_unmeasured") for r in fuel)
    assert refusals(_maf_image(world))[0].startswith("controls_unresolved: unknown_bytes_changed")
    assert refusals(world.candidate("timing_trim_a"), "knock")[-1].startswith("knock_unmodelled")
    with pytest.raises(SimulationRefusalError) as refused:
        require_edit_claim(
            world.report(world.candidate("timing_trim_a")),
            model=world.model,
            contract=world.contract,
            qualification=world.plant_q,
        )
    assert refused.value.reason == "engine_torque_not_computed"
    assert "spark_response_unmeasured" in str(refused.value)
    # The error names what the torque stage still owes, by task item.
    for owed in ("vehicle_mass (H07.10)", "h07_road_load (H07.5)", "h07_na_baseline (H07.12)"):
        assert owed in str(refused.value)
    assert "h07_repeatability (H07.8)" in str(refused.value)
    assert "spark_trim_unit (N01.2)" in str(refused.value)
    # The line every front end shows names the controls and the blockers.
    line = world.classify(world.candidate("fuel_targets")).messages[-1]
    assert line.startswith("torque/power claim for this edit (lambda): refused;")
    assert "lambda response unmeasured" in line and TEXT_TORQUE_UNVALIDATED in line
