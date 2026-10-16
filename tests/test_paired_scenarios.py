"""E07.6: paired baseline/candidate scenarios without leaking observations.

Synthetic images (``tests.rom_fixtures`` noise in the Xterra's layout, with
every constant the decodes check written in, and the air-charge calibration at
the 20260920 tune's values so steady BFS lands where the truck's does) and the
real application model re-pinned to them. The real frozen plant runs both
sides: it is not qualified, so nothing here can be ranked as an improvement
whatever it shows.
"""

from __future__ import annotations

import dataclasses
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.analysis.ecu_application import ApplicationModel, program_sha256
from src.analysis.fuel_plant import PLANT_FILE, FuelPlantProfile
from src.analysis.fuel_plant_eval import identify
from src.analysis.paired_scenarios import (
    OUTPUTS,
    TOOL_VERSION,
    LearnedState,
    OperatingPoint,
    PairedScenario,
    SimKit,
    load_kit,
    optimise,
    rank_as_improvement,
    run_paired,
)
from src.analysis.paired_steady import steady_coverage
from src.analysis.plant_validation import InterventionVerdict
from src.analysis.rom_effects import CellEdit, make_candidate
from src.analysis.rom_image import sha256_hex
from src.analysis.rom_map import load_rom_map
from src.analysis.sim_contract import (
    CONTRACT_FILE,
    EcuIdentity,
    PlantIdentity,
    SimContract,
    SimulationRefusalError,
    SuppliedValue,
    check_against_application_model,
)
from src.evidence.ledger import MemoryLedger, SyntheticIsolationError
from src.evidence.records import (
    Conditions,
    Evidence,
    Provenance,
    Quality,
    Quantity,
    Result,
    SourceRef,
    VehicleIdentity,
)
from tests.rom_fixtures import PROFILE, synthetic_image


def _image(model: ApplicationModel, kit_plant: Any) -> bytes:
    """A synthetic image every decode the slice uses describes: the twin's final-
    command constants and Load Point Scaler, the model's constants and
    conditions, UpRev's patch, and the pulse chain's published words."""
    image = bytearray(synthetic_image())
    image[0x8B39] = 130
    image[0x8B3D] = 129
    image[0xD247 : 0xD247 + 8] = bytes([128] * 8)
    struct.pack_into(">H", image, 0x9416, 44019)
    for marker in model.program.patch.markers:
        raw = bytes.fromhex(marker.bytes)
        image[marker.offset : marker.offset + len(raw)] = raw
    for node in model.nodes:
        for ref in node.rom:
            if ref.offset is not None:
                assert ref.width is not None and ref.value is not None
                width = ref.width // 8
                image[ref.offset : ref.offset + width] = ref.value.to_bytes(width, "big")
        for c in node.calibration_conditions:
            for k in range(c.count):
                image[c.offset + k] = (image[c.offset + k] & ~c.mask & 0xFF) | c.equals
    chain = kit_plant.pulse_chain
    for word in (
        chain.dead_time_base,
        chain.dead_time_slope,
        chain.rail_reference,
        chain.flow_scaler,
    ):
        image[word.address : word.address + word.width] = word.value.to_bytes(word.width, "big")
    _air_charge_calibration(image)
    return bytes(image)


def _air_charge_calibration(image: bytearray) -> None:
    """The steady-BFS calibration at the 20260920 tune's values (K 28715, flow
    and speed multipliers 226/3, MAF report scale 149, BFS ceiling 255:
    docs/ecu-firmware-analysis.md, tests/test_air_charge.py), flat Fuel Comp
    128 on rising axes, so WOT BFS lands near the truck's ~38-40k."""
    rom_map = load_rom_map(PROFILE)

    def put(table_id: str, slot: int | None, values: list[int], width: int = 1) -> None:
        at = rom_map.table_address(rom_map.table(table_id), slot)
        for k, v in enumerate(values):
            image[at + k * width : at + (k + 1) * width] = v.to_bytes(width, "big")

    put("fuel_comp_engine_speed_axis", None, [8 * (i + 1) for i in range(16)])
    put("fuel_comp_flow_axis", None, [16 * i for i in range(16)])
    put("fuel_compensation", 1, [128] * 256)
    put("k_constant", 1, [28715], width=2)
    put("bfs_ceiling", None, [255] * 16)
    image[0x89D2], image[0x88AD], image[0x83E9] = 226, 3, 149


@pytest.fixture(scope="module")
def kit_and_image() -> tuple[SimKit, bytes]:
    kit = load_kit(PROFILE)
    image = _image(kit.model, kit.plant)
    identity = kit.model.program.identity.model_copy(
        update={"sha256": program_sha256(image, kit.model)}
    )
    model = kit.model.model_copy(
        update={"program": kit.model.program.model_copy(update={"identity": identity})}
    )
    return dataclasses.replace(kit, model=model), image


WOT = OperatingPoint(
    rpm=4800.0,
    maf_gps=150.0,
    baro_kpa=81.0,
    iat_c=25.0,
    coolant_c=88.0,
    battery_v=14.0,
    rail_kpa=350.0,
    map_kpa=79.0,
    throttle_pct=90.0,
    pedal_flag=0,
    conditioned={"fuel_comp_load_byte": 200.0},
    source="design: WOT point, E07.6 test grid",
)
CRUISE = dataclasses.replace(
    WOT,
    rpm=2000.0,
    maf_gps=18.0,
    map_kpa=40.0,
    throttle_pct=10.0,
    conditioned={"fuel_comp_load_byte": 60.0},
    source="design: cruise point, E07.6 test grid",
)
LEARNED = LearnedState(
    stft_pct=0.0,
    ltft_pct=2.34,
    throttle_closed_learned=0.0,
    idle_air_learned=0.0,
    evap_purge_area=0.0,
    source="design: stated trims",
)


def _identity(image: bytes) -> EcuIdentity:
    return EcuIdentity(
        program_id="ZS44A", image_sha256=sha256_hex(image), slot=1, slot_source="default_slot"
    )


def _scenario(
    kit: SimKit, base: bytes, candidate: bytes, *points: OperatingPoint, **over: Any
) -> PairedScenario:
    plant = PlantIdentity(
        version=kit.plant.plant_version,
        hardware_configuration=kit.plant.hardware_configuration,
    )
    fields: dict[str, Any] = dict(
        mode="conditional_rom_comparison",
        base=_identity(base),
        candidate=_identity(candidate),
        plant=plant,
        points=points or (WOT, CRUISE),
        learned=LEARNED,
    )
    fields.update(over)
    return PairedScenario(**fields)


def _edit_all(image: bytes, kit: SimKit, table_id: str, slot: int | None, step: int) -> bytes:
    table = kit.rom_map.table(table_id)
    start = kit.rom_map.table_address(table, slot)
    edits = []
    for r in range(table.rows):
        for c in range(table.cols):
            raw = image[start + r * table.cols + c]
            edits.append(CellEdit(table_id, r, c, min(255, max(0, raw + step)), slot=slot))
    edits = [e for e in edits if e.raw != image[start + e.row * table.cols + e.col]]
    return make_candidate(image, kit.rom_map, edits, base_label="synthetic").image


def test_the_negative_control_rom_reproduces_the_baseline_exactly(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    result = run_paired(kit, _scenario(kit, image, bytes(image)), image, bytes(image))
    assert result.outcome == "identical"
    assert result.label == "simulation" and result.synthetic is True
    assert result.affected_cells == []
    wot = result.points[0]["outputs"]
    for name in OUTPUTS:
        assert wot[name]["base"] is not None
        assert wot[name]["base"] == wot[name]["candidate"]
        assert wot[name]["delta"] == 0.0
    # The WOT point is open loop and supported by the slice (unqualified plant
    # aside); the cruise point is closed loop: computed at the trim's fixed
    # point, but not supported while the trim authority is undecoded.
    assert wot["delivered_lambda"]["status"] == "supported"
    assert result.points[0]["loop"] == "open_loop" and result.points[1]["loop"] == "closed_loop"
    cruise = result.points[1]["outputs"]
    assert cruise["delivered_lambda"]["reasons"] == ["closed_loop_trim_authority_undecoded"]
    # BFS is computed per ROM at its steady fixed point (no stated BFS).
    assert 38000 < wot["base_fuel_schedule"]["base"] < 42000
    assert wot["base_fuel_schedule"]["status"] == "supported"
    lo, hi = wot["base_fuel_schedule"]["interval"]["base"]
    assert lo <= wot["base_fuel_schedule"]["base"] <= hi
    assert result.provenance["tool_version"] == TOOL_VERSION == "3"
    again = run_paired(kit, _scenario(kit, image, bytes(image)), image, bytes(image))
    assert again.to_dict() == result.to_dict()


def test_a_target_afr_edit_is_computed_through_the_pulse_and_the_plant(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_targets", 1, 4)
    result = run_paired(kit, _scenario(kit, image, candidate), image, candidate)
    assert {c["table"] for c in result.affected_cells} == {"fuel_targets"}
    wot = result.points[0]["outputs"]
    assert wot["target_phi"]["status"] == "supported"
    assert wot["target_phi"]["delta"] != 0.0
    lam = wot["delivered_lambda"]
    assert lam["status"] == "supported"
    # More commanded fuel, less lambda: same sign through pulse and plant.
    assert (wot["target_phi"]["delta"] > 0) == (lam["delta"] < 0)
    assert wot["injector_pulse_ms"]["delta"] * wot["target_phi"]["delta"] > 0
    assert lam["relative_delta_range_over_assumptions"][0] <= lam["relative_delta"]
    lo, hi = lam["parameter_band"]
    assert lo < lam["base"] < hi
    assert any("slew and cap" in a for a in result.assumptions)
    # Ignition and BFS are traced, not computed: unchanged by this edit.
    assert {v["status"] for v in result.traced_outputs.values()} == {"unchanged"}
    # Still not rankable: the plant failed its bounds and no intervention passed.
    verdict = rank_as_improvement(result, intervention=None)
    assert not verdict["rankable"]
    assert any(r.startswith("plant_not_qualified") for r in verdict["reasons"])
    assert "intervention_test_not_passed:none" in verdict["reasons"]
    assert any("LTFT acts in open loop" in a for a in result.assumptions)
    assert any("STFT is neutral" in a and "assumed" in a for a in result.assumptions)
    assert any("@point1" in r and "closed_loop" in r for r in verdict["reasons"])


def test_a_fuel_comp_edit_is_computed_through_steady_bfs(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """Fuel Comp acts inside BFS. BFS is computed per ROM at its steady fixed
    point, so the edit reaches X, phi, the pulse and lambda as numbers, with
    the fill dynamics the fixed point absorbs stated, never silently held."""
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    result = run_paired(kit, _scenario(kit, image, candidate), image, candidate)
    assert {c["table"] for c in result.affected_cells} == {"fuel_compensation"}
    wot = result.points[0]["outputs"]
    bfs = wot["base_fuel_schedule"]
    assert bfs["status"] == "supported"
    # Fuel Comp 128 -> 131 scales BFS by 131/128 (within the word quantisation).
    assert bfs["candidate"] / bfs["base"] == pytest.approx(131 / 128, rel=2e-3)
    pulse, lam = wot["injector_pulse_ms"], wot["delivered_lambda"]
    assert pulse["status"] == lam["status"] == "supported"
    assert pulse["delta"] > 0 and lam["delta"] < 0
    # Open loop: more base fuel through the same plant, lambda down by ~2.3 %.
    assert lam["relative_delta"] == pytest.approx(-0.0228, abs=0.002)
    assert any("absorbed" in a and "manifold_fill_filter" in a for a in result.assumptions)
    assert any("MAF reading is the prescribed airflow" in a for a in result.assumptions)
    assert not any(r.startswith("conditioned_on_changed_path") for r in result.unresolved)
    # Ignition reads the moved X through trims the slice does not compute:
    # traced, unevaluable, so the change still cannot be ranked.
    assert result.traced_outputs["ignition_timing"]["status"] == "unevaluable"
    assert "base_fuel_schedule_word" not in result.traced_outputs
    verdict = rank_as_improvement(result, intervention=_minted(result, "pass"))
    assert not verdict["rankable"]
    assert "unsupported:ignition_timing:unevaluable" in verdict["reasons"]


def test_a_k_edit_moves_bfs_one_for_one(kit_and_image: tuple[SimKit, bytes]) -> None:
    kit, image = kit_and_image
    k = CellEdit("k_constant", 0, 0, 28715 * 105 // 100, slot=1)
    candidate = make_candidate(image, kit.rom_map, [k], base_label="synthetic").image
    result = run_paired(kit, _scenario(kit, image, candidate, WOT), image, candidate)
    bfs = result.points[0]["outputs"]["base_fuel_schedule"]
    assert bfs["status"] == "supported"
    assert bfs["candidate"] / bfs["base"] == pytest.approx(1.05, rel=2e-3)


def test_an_edit_reaching_the_stated_fill_byte_is_unresolved(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """The fill byte is conditioned on (not computed): a fill-curve edit
    reaches it, so nothing downstream is given as if the byte were fixed."""
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fill_curve_low", None, 2)
    result = run_paired(kit, _scenario(kit, image, candidate), image, candidate)
    for point in result.points:
        for name in OUTPUTS:
            o = point["outputs"][name]
            assert o["status"] == "unsupported"
            assert "conditioned_on_changed_path:fuel_comp_load_byte" in o["reasons"]
            # Unsupported is not unchanged: no candidate value, no delta.
            assert o["candidate"] is None and o["delta"] is None
    # The edited node is not absorbed by the fixed point: its reason stays.
    assert "rom:unmodelled:fuel_comp_load_byte" in result.unresolved
    assert result.coverage == dict.fromkeys(OUTPUTS, 0.0)


def test_a_computed_quantity_cannot_be_stated(kit_and_image: tuple[SimKit, bytes]) -> None:
    kit, image = kit_and_image
    for quantity in ("base_fuel_schedule", "target_afr_x"):
        stated = dataclasses.replace(
            WOT, conditioned={"fuel_comp_load_byte": 200.0, quantity: 42000.0}
        )
        with pytest.raises(SimulationRefusalError) as err:
            run_paired(kit, _scenario(kit, image, image, stated), image, bytes(image))
        assert err.value.reason == "conditioning_not_allowed"
        assert "computed here" in str(err.value)
    missing = dataclasses.replace(WOT, conditioned={})
    result = run_paired(kit, _scenario(kit, image, image, missing), image, bytes(image))
    out = result.points[0]["outputs"]["base_fuel_schedule"]
    assert "conditioning_missing:fuel_comp_load_byte" in out["reasons"]


def test_closed_loop_runs_at_the_trims_fixed_point(kit_and_image: tuple[SimKit, bytes]) -> None:
    """At a closed-loop point the trim absorbs a Fuel Comp edit: the same
    metered open time, so lambda and fuel do not move and the total trim
    carries the change, k_c = k_b x G_b / G_c."""
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_compensation", 1, 3)
    result = run_paired(
        kit,
        _scenario(kit, image, candidate, CRUISE, candidate_events=("ecu_reflash",)),
        image,
        candidate,
    )
    point = result.points[0]
    assert point["loop"] == "closed_loop"
    out = point["outputs"]
    assert out["target_phi"]["delta"] == 0.0 and out["target_phi"]["status"] == "supported"
    assert out["base_fuel_schedule"]["delta"] > 0
    for name in ("injector_pulse_ms", "delivered_lambda", "fuel_per_event_g"):
        assert out[name]["delta"] == 0.0
    trim = out["fuel_trim_total_pct"]
    # The base runs at its stated trims (0 + 2.34 %); the candidate's total
    # falls by about the BFS increase net of the ECM's pressure correction.
    assert trim["base"] == pytest.approx(2.34, abs=0.01)
    assert -2.4 < trim["delta"] < -1.5
    for name in ("injector_pulse_ms", "fuel_trim_total_pct", "delivered_lambda"):
        assert out[name]["reasons"] == ["closed_loop_trim_authority_undecoded"]
    # The flash reset LTFT to 0: the STFT carries the whole total until it relearns.
    assert point["candidate_stft_at_its_ltft_pct"] == pytest.approx(trim["candidate"])
    assert any(a.startswith("closed loop:") for a in result.assumptions)


def test_a_moved_closed_loop_setpoint_is_not_computed(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    """A target edit at a closed-loop cell moves the commanded phi; the A/F
    feedback setpoint's relation to it is undecoded, so the candidate's fuel
    side is refused by name instead of computed at the stated trims."""
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_targets", 1, 4)
    result = run_paired(kit, _scenario(kit, image, candidate, CRUISE), image, candidate)
    point = result.points[0]
    assert point["loop"] == "closed_loop_setpoint_moved"
    out = point["outputs"]
    assert out["target_phi"]["delta"] != 0.0
    for name in ("injector_pulse_ms", "fuel_trim_total_pct", "delivered_lambda"):
        assert "closed_loop_setpoint_moved" in out[name]["reasons"]
        assert out[name]["candidate"] is None and out[name]["delta"] is None


def test_the_steady_fixed_point_covers_only_unedited_absorbed_nodes(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, _ = kit_and_image
    cov = steady_coverage(kit.contract, "conditional_rom_comparison", ["fuel_comp_lookup"])
    assert cov.absorbed >= {"manifold_fill_filter", "fill_transient", "airflow_qa"}
    assert cov.covers("unmodelled:fill_blend")
    assert cov.covers("observed_not_predicted:load_input_x")
    assert not cov.covers("unmodelled:fuel_comp_load_byte")
    assert not cov.covers("unmodelled:final_command_word")
    assert not cov.covers("outside_decode:fill_blend")
    edited = steady_coverage(kit.contract, "conditional_rom_comparison", ["fill_coefficient"])
    assert not edited.covers("unmodelled:fill_coefficient")


def test_absorbed_nodes_must_feed_the_fixed_point(kit_and_image: tuple[SimKit, bytes]) -> None:
    """The contract's absorbs list is checked against the manifest: an absorbed
    node off BFS's upstream graph is a claim with no computation behind it."""
    kit, _ = kit_and_image
    assert check_against_application_model(kit.contract, kit.model) == []
    raw = yaml.safe_load((PROFILE / CONTRACT_FILE).read_text())
    mode = next(m for m in raw["modes"] if m["id"] == "conditional_rom_comparison")
    mode["roles"]["base_fuel_schedule"]["init"]["absorbs"].append("timing_rate_limit")
    contract = SimContract.model_validate(raw)
    assert check_against_application_model(contract, kit.model) == [
        "conditional_rom_comparison.base_fuel_schedule: absorbed 'timing_rate_limit' is not "
        "upstream of base_fuel_schedule"
    ]
    mode["roles"]["base_fuel_schedule"]["init"]["method"] = "prescribed"
    with pytest.raises(ValueError, match="only a steady_state initialisation absorbs"):
        SimContract.model_validate(raw)


def test_the_flash_resets_the_learned_trim_on_the_candidate_only(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    scenario = _scenario(kit, image, bytes(image), WOT, candidate_events=("ecu_reflash",))
    result = run_paired(kit, scenario, image, bytes(image))
    assert result.learned_state["resets"] == {"ltft_b1": "ecu_reflash"}
    assert result.learned_state["base"]["ltft_pct"] == 2.34
    assert result.learned_state["candidate"]["ltft_pct"] == 0.0
    # Same image, but the reset trim alone changes the pulse: reported as such.
    pulse = result.points[0]["outputs"]["injector_pulse_ms"]
    assert pulse["delta"] < 0
    with pytest.raises(SimulationRefusalError) as err:
        run_paired(
            kit,
            _scenario(kit, image, bytes(image), WOT, candidate_events=("battery_disconnect",)),
            image,
            bytes(image),
        )
    assert err.value.reason == "reset_unmodelled"


def test_recorded_or_computed_values_cannot_be_fed_back(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image

    def reason(scenario: PairedScenario) -> str:
        with pytest.raises(SimulationRefusalError) as err:
            run_paired(kit, scenario, image, bytes(image))
        return err.value.reason

    copied = dataclasses.replace(WOT, source="median of _capture-tuning-2026-10-05T120000")
    assert reason(_scenario(kit, image, image, copied)) == "recorded_value_refused"
    from_channel = {"evap_purge_area": SuppliedValue(channel="did:ECM_AIR_CHARGE", source="x")}
    # A channel is refused by the contract itself in a comparison.
    assert (
        reason(_scenario(kit, image, image, WOT, extra_boundaries=from_channel))
        == "recorded_value_refused"
    )
    bfs = {"base_fuel_schedule": SuppliedValue(value=42000.0, source="design")}
    assert (
        reason(_scenario(kit, image, image, WOT, extra_boundaries=bfs))
        == "boundary_not_supplied_here"
    )
    # A rollout computes rpm, airflow and the trims: a point that prescribes them is refused.
    assert reason(_scenario(kit, image, image, WOT, mode="coupled_rollout")) == (
        "boundary_not_supplied_here"
    )
    # Conditioning is only for computed, unmodelled ECU quantities.
    odd = dataclasses.replace(WOT, conditioned={"mass_air_flow": 150.0})
    assert reason(_scenario(kit, image, image, odd)) == "conditioning_not_allowed"


def test_synthetic_traces_stay_out_of_evidence_and_baseline_data(
    kit_and_image: tuple[SimKit, bytes], tmp_path: Path
) -> None:
    kit, image = kit_and_image
    result = run_paired(kit, _scenario(kit, image, bytes(image)), image, bytes(image))
    traces = result.traces()
    assert traces.attrs["synthetic"] is True
    session = result.session
    assert session.startswith("_sim-") and set(traces["session"]) == {session}
    # Never plant identification data...
    frame = traces.rename(columns={"point": "t"}).assign(state="eligible", region_id="x")
    with pytest.raises(Exception) as err:
        identify([frame], kit.plant, kit.delay, sessions_root=tmp_path)
    assert getattr(err.value, "reason", None) == "synthetic_as_observed"
    # ...and never evidence on any memory ledger, the replay ledgers included.
    stamp = datetime(2030, 1, 1, tzinfo=UTC)
    record = Evidence(
        vehicle=VehicleIdentity(
            profile="nissan-xterra-2008", vin=None, program_id=None, calibration=None
        ),
        provenance=Provenance(
            producer="paired_scenarios", producer_kind="tool", version="1", code_version="c"
        ),
        conditions=Conditions(regime="simulated WOT point", vehicle_state=None),
        valid_from=stamp,
        subject=f"paired_scenarios:{session}",
        sources=(
            SourceRef(
                session=session,
                window_start=stamp,
                window_end=stamp,
                channels=("sim:delivered_lambda",),
                n_samples=1,
            ),
        ),
        input_hashes={"scenario": result.scenario_sha256},
        policy={"mode": "conditional_rom_comparison"},
        values={"delivered_lambda": Quantity(value=0.85, unit="1")},
        quality=Quality(n_observations=1, timestamp_basis=None),
        result=Result(status="evaluated", residual=Quantity(value=0.0, unit="1")),
    )
    for replay_id in (None, "2030-01-01T000000"):
        ledger = MemoryLedger(
            tmp_path / f"memory-{replay_id}.jsonl",
            vehicle="nissan-xterra-2008",
            replay_id=replay_id,
        )
        with pytest.raises(SyntheticIsolationError):
            ledger.append(record)


def test_optimisation_is_refused() -> None:
    with pytest.raises(SimulationRefusalError) as err:
        optimise()
    assert err.value.reason == "optimisation_deferred"


# -- ranking needs a derived verdict and a qualified, recorded plant ------------------------


def _minted(result: Any, verdict: str, **over: Any) -> InterventionVerdict:
    """A verdict as evaluate_intervention mints it (the test stands in for a run)."""
    fields: dict[str, Any] = dict(
        case="test",
        verdict=verdict,
        synthetic=False,
        base_image_sha256=result.base_image_sha256,
        candidate_image_sha256=result.candidate_image_sha256,
        baseline_sha256=result.plant["baseline_sha256"],
        plant_version="fuel-mv-1",
        qualified_regimes=("closed_loop", "enrichment"),
    )
    fields.update(over)
    return InterventionVerdict._mint(**fields)


def _plant_with(verdict: str, *, synthetic: bool = False) -> FuelPlantProfile:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / PLANT_FILE).read_text())
    raw["synthetic"] = synthetic
    for rec in raw["spent_held_out"]:
        rec["verdict"] = verdict
    return FuelPlantProfile.model_validate(raw)


def test_ranking_needs_a_sealed_matching_verdict_and_a_recorded_qualified_plant(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_targets", 1, 4)
    qualified = dataclasses.replace(kit, plant=_plant_with("pass"))
    result = run_paired(qualified, _scenario(kit, image, candidate, WOT), image, candidate)
    # Everything holds: a supported WOT edit, a qualified recorded plant and a
    # passing test of this change on this baseline.
    assert rank_as_improvement(result, intervention=_minted(result, "pass")) == {
        "rankable": True,
        "reasons": [],
    }

    def reasons(intervention: InterventionVerdict | None, r: Any = result) -> list[str]:
        out = rank_as_improvement(r, intervention=intervention)
        assert not out["rankable"]
        return list(out["reasons"])

    # The verdict is derived, never stated: an edited one fails its seal.
    forged = dataclasses.replace(_minted(result, "pending"), verdict="pass")
    assert reasons(forged) == ["intervention_verdict_unsealed: not minted by evaluate_intervention"]
    assert reasons(_minted(result, "pass", synthetic=True)) == ["intervention_test_synthetic"]
    assert reasons(_minted(result, "pass", candidate_image_sha256="0" * 64)) == [
        "intervention_images_differ: the test was of another change"
    ]
    assert reasons(_minted(result, "pass", baseline_sha256="0" * 64)) == [
        "intervention_plant_differs: the test ran another baseline"
    ]
    # A relabelled synthetic plant never ranks, whatever its verdicts say.
    synthetic = dataclasses.replace(kit, plant=_plant_with("pass", synthetic=True))
    relabelled = run_paired(synthetic, _scenario(kit, image, candidate, WOT), image, candidate)
    assert relabelled.plant["synthetic"] is True
    assert any(
        r.startswith("synthetic_plant") for r in reasons(_minted(relabelled, "pass"), relabelled)
    )


def test_a_closed_loop_only_qualification_cannot_rank_wot_deltas(
    kit_and_image: tuple[SimKit, bytes],
) -> None:
    kit, image = kit_and_image
    candidate = _edit_all(image, kit, "fuel_targets", 1, 4)
    closed_only = dataclasses.replace(kit, plant=_plant_with("pass_closed_loop_only"))
    result = run_paired(closed_only, _scenario(kit, image, candidate, WOT), image, candidate)
    wot = result.points[0]["outputs"]
    for name in ("delivered_lambda", "fuel_per_event_g"):
        assert wot[name]["status"] == "unsupported"
        assert "regime_not_qualified:enrichment" in wot[name]["reasons"]
    # The ECU-side outputs do not rest on the plant.
    assert wot["target_phi"]["status"] == "supported"
    out = rank_as_improvement(
        result, intervention=_minted(result, "pass", qualified_regimes=("closed_loop",))
    )
    assert not out["rankable"]
    assert any("regime_not_qualified:enrichment" in r for r in out["reasons"])
