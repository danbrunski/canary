"""E07.1: controller / plant / scenario contracts.

The Xterra's ``engine_sim_contract.yaml`` is loaded and checked against the
E05 application model; then each acceptance clause is shown to be enforced by
mutating that real file in memory until the schema refuses it by name:

- every one of the four modes owns every quantity exactly once;
- no same-step cycle can hide among the quantities a mode computes;
- a predicted output (or an alias channel of it) cannot enter as a measured
  input, except in a mode labelled ``state_estimation``, which names each
  assimilated observation and scores in its own group.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from src.analysis.ecu_application import load_application_model
from src.analysis.sim_contract import (
    CONTRACT_FILE,
    FOUR_MODES,
    EcuIdentity,
    PlantIdentity,
    Scenario,
    SimContract,
    SimulationRefusalError,
    SuppliedValue,
    check_against_application_model,
    check_scenario,
    load_contract,
    score_group,
)

PROFILE = Path(__file__).resolve().parents[1] / "data" / "nissan-xterra-2008"
SHA_A = "a" * 64
SHA_B = "b" * 64


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / CONTRACT_FILE).read_text())
    return raw


def _mode(raw: dict[str, Any], mode_id: str) -> dict[str, Any]:
    mode: dict[str, Any] = next(m for m in raw["modes"] if m["id"] == mode_id)
    return mode


def _quantity(raw: dict[str, Any], quantity_id: str) -> dict[str, Any]:
    q: dict[str, Any] = next(q for q in raw["quantities"] if q["id"] == quantity_id)
    return q


def _refused(raw: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        SimContract.model_validate(raw)


# -- the profile's contract ---------------------------------------------------------------


def test_the_profile_contract_loads_and_agrees_with_the_application_model() -> None:
    contract, sha = load_contract(PROFILE)
    model, _ = load_application_model(PROFILE)
    assert len(sha) == 64
    assert check_against_application_model(contract, model) == []
    assert contract.ecu_schedule.application_model_task == model.schedule.function == 0x0575EC
    # The ECU tick is the decoded 10.24 ms table; the solver steps a whole
    # number of times inside it.
    clocks = {c.id: c for c in contract.clocks}
    assert clocks[contract.ecu_schedule.clock].period_s == pytest.approx(0.01024)
    assert [t.function for t in contract.ecu_schedule.tasks][:4] == [
        0x0575EC,
        0x057144,
        0x053B8C,
        0x0527CC,
    ]


def test_all_four_modes_own_every_quantity_exactly_once() -> None:
    contract, _ = load_contract(PROFILE)
    assert sorted(m.kind for m in contract.modes) == sorted(FOUR_MODES)
    ids = {q.id for q in contract.quantities}
    for mode in contract.modes:
        assert set(mode.roles) == ids, mode.id
        assert score_group(contract, mode.id) == "open_loop"
    raw = _raw()
    del _mode(raw, "plant_replay")["roles"]["engine_speed"]
    _refused(raw, "every quantity has exactly one role")


def test_ecu_quantities_carry_integer_widths_and_plant_quantities_do_not() -> None:
    contract, _ = load_contract(PROFILE)
    for q in contract.quantities:
        assert (q.integer is not None) == (q.kind in ("ecu", "sensor")), q.id
    raw = _raw()
    _quantity(raw, "mass_air_flow")["integer"] = {
        "width": 16,
        "signed": False,
        "scale": "x",
        "confidence": "code",
    }
    _refused(raw, "every other kind is continuous")


def test_the_first_slice_holds_airflow_and_rpm_as_boundaries_and_predicts_lambda() -> None:
    contract, _ = load_contract(PROFILE)
    mode = contract.mode("conditional_rom_comparison")
    assert mode.role("engine_speed") == mode.role("mass_air_flow") == "prescribed_boundary"
    assert mode.role("delivered_lambda") == "predicted_output"
    assert mode.role("target_phi_word") == "predicted_output"
    assert mode.role("base_fuel_schedule") == "predicted_output"
    # Target AFR is an objective: delivered lambda reads air and fuel, never it.
    deps = {d.quantity for d in contract.quantity("delivered_lambda").depends_on}
    assert deps == {"mass_air_flow", "fuel_mass_flow"}
    # The rollout feeds no recorded BFS, trim, airflow or rpm back.
    rollout = contract.mode("coupled_rollout")
    for q in ("base_fuel_schedule", "stft_b1", "ltft_b1", "mass_air_flow", "engine_speed"):
        assert rollout.role(q) in ("predicted_output", "inferred_state"), q
    assert rollout.roles["ltft_b1"].init is not None
    assert "ecu_reflash" in rollout.roles["ltft_b1"].init.reset_on


# -- hidden same-step cycles ----------------------------------------------------------------


def test_a_hidden_same_step_cycle_is_refused() -> None:
    """The trim reads last tick's lambda reading; reading this tick's would
    close injector -> fuel -> lambda -> sensor -> trim -> injector in one step."""
    raw = _raw()
    deps = _quantity(raw, "stft_b1")["depends_on"]
    for dep in deps:
        if dep["quantity"] == "lambda_sensor_b1":
            dep["timing"] = "same_step"
    _refused(raw, r"same-step cycle .*stft_b1")


def test_the_cycle_is_named_in_the_mode_that_computes_all_of_it() -> None:
    """Only the rollout computes the whole loop (the others supply the trim,
    the injector command or the lambda reading), so it is the mode refused."""
    raw = _raw()
    for dep in _quantity(raw, "stft_b1")["depends_on"]:
        if dep["quantity"] == "lambda_sensor_b1":
            dep["timing"] = "same_step"
    with pytest.raises(ValidationError, match="mode coupled_rollout: same-step cycle"):
        SimContract.model_validate(raw)


def test_a_quantity_cannot_read_itself_in_the_same_step() -> None:
    raw = _raw()
    _quantity(raw, "engine_speed")["depends_on"][0]["timing"] = "same_step"
    _refused(raw, "depends on itself in the same step")


def test_a_previous_step_read_needs_an_initial_value() -> None:
    raw = _raw()
    del _mode(raw, "ecu_replay")["roles"]["target_afr_x"]["init"]
    _refused(raw, "target_afr_x is read from the previous step, so it needs an init")


# -- predicted-as-measured ------------------------------------------------------------------


def test_a_predicted_output_cannot_enter_as_a_measured_input() -> None:
    raw = _raw()
    # ECU replay predicts the command word; PID 0x44 is an alias of it.
    _mode(raw, "ecu_replay")["roles"]["pedal_flag"]["source"] = "obd:COMMANDED_EQUIV_RATIO"
    _refused(raw, "predicted-as-measured")
    raw = _raw()
    _mode(raw, "plant_replay")["roles"]["fuel_rail_pressure"] = {
        "role": "measured_input",
        "source": "obd:O2_WR_LAMBDA_S1",
    }
    _refused(raw, r"observes lambda_sensor_b1")


def test_a_state_estimation_mode_may_assimilate_and_scores_separately() -> None:
    raw = _raw()
    estimation = copy.deepcopy(_mode(raw, "plant_replay"))
    estimation["id"] = "plant_state_estimation"
    estimation["score_group"] = "state_estimation"
    estimation["roles"]["fuel_rail_pressure"] = {
        "role": "inferred_state",
        "init": {"method": "observed_checkpoint", "note": "rail pressure at the start"},
    }
    # The rail model reads last step's fuel flow, so that needs a start too.
    estimation["roles"]["fuel_mass_flow"] = {
        "role": "predicted_output",
        "init": {"method": "steady_state", "note": "settled at the start"},
    }
    estimation["assimilates"] = [
        {
            "quantity": "lambda_sensor_b1",
            "channel": "obd:O2_WR_LAMBDA_S1",
            "corrects": "fuel_rail_pressure",
        }
    ]
    raw["modes"].append(estimation)
    contract = SimContract.model_validate(raw)
    assert score_group(contract, "plant_state_estimation") == "state_estimation"
    assert score_group(contract, "plant_replay") == "open_loop"
    # Even there, a measured input may not read a computed quantity's channel.
    leaking = copy.deepcopy(raw)
    _mode(leaking, "plant_state_estimation")["roles"]["battery_voltage"]["source"] = (
        "obd:O2_WR_LAMBDA_S1"
    )
    _refused(leaking, "predicted-as-measured")


def test_an_open_loop_mode_cannot_assimilate() -> None:
    raw = _raw()
    _mode(raw, "plant_replay")["assimilates"] = [
        {"quantity": "lambda_sensor_b1", "channel": "obd:O2_WR_LAMBDA_S1", "corrects": "x"}
    ]
    _refused(raw, "only a state_estimation mode assimilates")


def test_a_comparison_or_rollout_takes_no_measured_input() -> None:
    for mode_id in ("conditional_rom_comparison", "coupled_rollout"):
        raw = _raw()
        _mode(raw, mode_id)["roles"]["intake_air_temperature"] = {
            "role": "measured_input",
            "source": "obd:INTAKE_TEMP",
        }
        _refused(raw, "takes no measured input")


# -- ownership and domains ------------------------------------------------------------------


def test_an_unused_dependency_is_an_undeclared_input() -> None:
    raw = _raw()
    _mode(raw, "conditional_rom_comparison")["roles"]["pedal_flag"] = {"role": "predicted_output"}
    _refused(raw, "reads requested_throttle_position, which has no role here")


def test_replay_modes_stay_on_their_side() -> None:
    raw = _raw()
    roles = _mode(raw, "ecu_replay")["roles"]
    roles["battery_voltage"] = {"role": "prescribed_boundary", "source": "scenario"}
    roles["injector_open_time_b1"] = {"role": "predicted_output"}
    _refused(raw, "ECU replay computes only ECU quantities")
    raw = _raw()
    roles = _mode(raw, "plant_replay")["roles"]
    roles["battery_voltage"] = {"role": "prescribed_boundary", "source": "scenario"}
    roles["battery_voltage_counts"] = {"role": "predicted_output"}
    roles["dead_time"] = {"role": "predicted_output"}
    _refused(raw, "plant replay computes no ECU quantity")


def test_a_supplied_value_cannot_be_read_from_the_ecus_own_reading_of_it() -> None:
    """The comparison predicts the ECU's baro reading from the prescribed
    ambient pressure; taking that pressure from the ECU's reported baro would
    feed the prediction its own observation."""
    raw = _raw()
    _mode(raw, "conditional_rom_comparison")["roles"]["barometric_pressure"]["source"] = (
        "obd:BAROMETRIC_PRESSURE"
    )
    _refused(raw, "observes barometric_counts")


def test_the_ecu_reads_the_plant_only_through_a_sensor() -> None:
    raw = _raw()
    _quantity(raw, "base_fuel_schedule")["depends_on"].append(
        {"quantity": "mass_air_flow", "timing": "same_step"}
    )
    _refused(raw, "through a declared sensor or actuator")


def test_a_held_fixed_internal_quantity_states_its_assumption() -> None:
    raw = _raw()
    del _mode(raw, "conditional_rom_comparison")["roles"]["mass_air_flow"]["assumption"]
    _refused(raw, "states the assumption it is held fixed under")


def test_the_solver_step_divides_the_ecu_tick() -> None:
    raw = _raw()
    next(c for c in raw["clocks"] if c["kind"] == "solver")["period_s"] = 0.003
    _refused(raw, "a whole number of times")


def test_disagreement_with_the_application_model_is_named() -> None:
    contract, _ = load_contract(PROFILE)
    model, _ = load_application_model(PROFILE)
    moved = contract.model_copy(
        update={"ecu_schedule": contract.ecu_schedule.model_copy(update={"cycle_start": 0x1234})}
    )
    assert any("cycle_start" in p for p in check_against_application_model(moved, model))
    raw = _raw()
    _quantity(raw, "target_afr_x")["integer"]["width"] = 16
    widened = SimContract.model_validate(raw)
    assert any("target_afr_x" in p for p in check_against_application_model(widened, model))


# -- scenarios ------------------------------------------------------------------------------


def _comparison(contract: SimContract, sha: str, **changes: Any) -> Scenario:
    mode = contract.mode("conditional_rom_comparison")
    boundaries = {
        q: SuppliedValue(value=1.0, source="operating point 3,000 rpm warm")
        for q, a in mode.roles.items()
        if a.source == "scenario"
    }
    hardware = contract.hardware_configurations[0]
    fields: dict[str, Any] = {
        "contract_sha256": sha,
        "mode": mode.id,
        "ecu": [
            EcuIdentity(program_id="p", image_sha256=SHA_A, slot=1, slot_source="default_slot"),
            EcuIdentity(program_id="p", image_sha256=SHA_B, slot=1, slot_source="default_slot"),
        ],
        "plant": PlantIdentity(
            version=contract.plant_version,
            hardware_configuration=hardware.id,
            parameter_evidence={"displacement": "profile"},
        ),
        "boundaries": boundaries,
        **changes,
    }
    return Scenario(**fields)


def test_a_complete_comparison_scenario_is_accepted_and_hashed_canonically() -> None:
    contract, sha = load_contract(PROFILE)
    scenario = _comparison(contract, sha)
    assert check_scenario(contract, sha, scenario).kind == "conditional_rom_comparison"
    same = Scenario.model_validate_json(scenario.model_dump_json())
    assert same.scenario_hash() == scenario.scenario_hash()
    provenance = scenario.provenance()
    assert provenance["scenario_sha256"] == scenario.scenario_hash()
    assert [e["image_sha256"] for e in provenance["ecu"]] == [SHA_A, SHA_B]
    other = scenario.model_copy(
        update={
            "boundaries": {
                **scenario.boundaries,
                "engine_speed": SuppliedValue(value=2.0, source="another point"),
            }
        }
    )
    assert other.scenario_hash() != scenario.scenario_hash()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"contract_sha256": "0" * 64}, "contract_mismatch"),
        (
            {
                "ecu": [
                    EcuIdentity(
                        program_id="p", image_sha256=SHA_A, slot=1, slot_source="default_slot"
                    )
                ]
            },
            "ecu_identity_count",
        ),
        (
            {
                "ecu": [
                    EcuIdentity(
                        program_id="p", image_sha256=SHA_A, slot=1, slot_source="default_slot"
                    ),
                    EcuIdentity(program_id="p", image_sha256=SHA_B, slot_source="unresolved"),
                ]
            },
            "active_slot_unresolved",
        ),
        (
            {
                "ecu": [
                    EcuIdentity(
                        program_id="p", image_sha256=SHA_A, slot=1, slot_source="default_slot"
                    ),
                    EcuIdentity(
                        program_id="q", image_sha256=SHA_B, slot=1, slot_source="default_slot"
                    ),
                ]
            },
            "program_mismatch",
        ),
        ({"plant": None}, "plant_identity_missing"),
        ({"boundaries": {}}, "boundary_missing"),
    ],
)
def test_a_scenario_its_mode_cannot_evaluate_is_refused_by_name(
    change: dict[str, Any], reason: str
) -> None:
    contract, sha = load_contract(PROFILE)
    fields = {**_comparison(contract, sha).__dict__, **change}
    with pytest.raises(SimulationRefusalError) as refused:
        check_scenario(contract, sha, Scenario(**fields))
    assert refused.value.reason == reason


def test_the_rom_comparison_admits_its_declared_plant_versions_and_no_other() -> None:
    """E08.11: the paired comparison admits fuel-mv-1 (the contract's, which
    E07.4/E07.6 run) and fuel-mv-2 (the simulator's); fuel-mv-3, declared by
    the fuel stage but not admitted, is refused by name, as is an undeclared
    version. A mode with no list admits the contract's version alone."""
    contract, sha = load_contract(PROFILE)
    assert contract.plant_version == "fuel-mv-1"
    rom_mode = contract.mode("conditional_rom_comparison")
    assert contract.admitted_plant_versions(rom_mode) == ["fuel-mv-1", "fuel-mv-2"]
    hardware = contract.hardware_configurations[0].id
    for version in ("fuel-mv-1", "fuel-mv-2"):
        plant = PlantIdentity(version=version, hardware_configuration=hardware)
        assert check_scenario(contract, sha, _comparison(contract, sha, plant=plant)) == rom_mode
    for version in ("fuel-mv-3", "fuel-mv-9"):
        plant = PlantIdentity(version=version, hardware_configuration=hardware)
        with pytest.raises(SimulationRefusalError) as refused:
            check_scenario(contract, sha, _comparison(contract, sha, plant=plant))
        assert refused.value.reason == "plant_version_mismatch"
        assert "['fuel-mv-1', 'fuel-mv-2']" in str(refused.value)
    others = [m for m in contract.modes if m.id != rom_mode.id]
    assert all(contract.admitted_plant_versions(m) == ["fuel-mv-1"] for m in others)
    raw = _raw()
    _mode(raw, "conditional_rom_comparison")["plant_versions"] = ["fuel-mv-1", "fuel-mv-9"]
    _refused(raw, "admits plant versions .'fuel-mv-9'. that no stage implementation declares")
    raw = _raw()
    _mode(raw, "conditional_rom_comparison")["plant_versions"] = ["fuel-mv-2", "fuel-mv-2"]
    _refused(raw, "duplicate mode conditional_rom_comparison plant version")
    # Admitting fuel-mv-3 needs its MAP model declared for the comparison too.
    raw = _raw()
    _mode(raw, "conditional_rom_comparison")["plant_versions"] = ["fuel-mv-1", "fuel-mv-3"]
    _refused(raw, "declared for conditional_rom_comparison in plant fuel-mv-3")


def test_missing_history_never_defaults_to_a_state() -> None:
    """ECU replay starts X and the command from observed checkpoints; without
    them the scenario is refused rather than started cold or steady."""
    contract, sha = load_contract(PROFILE)
    mode = contract.mode("ecu_replay")
    boundaries = {
        q: SuppliedValue(value=0.0, source="key-on read")
        for q, a in mode.roles.items()
        if a.source == "scenario"
    }
    ecu = [EcuIdentity(program_id="p", image_sha256=SHA_A, slot=1, slot_source="registry")]
    scenario = Scenario(contract_sha256=sha, mode="ecu_replay", ecu=ecu, boundaries=boundaries)
    with pytest.raises(SimulationRefusalError) as refused:
        check_scenario(contract, sha, scenario)
    assert refused.value.reason == "initial_state_missing"
    state = {
        "target_afr_x": SuppliedValue(
            channel="mem:ECM_FUEL_TARGET_MAP_X_TRACE", source="first read"
        ),
        "target_phi_word": SuppliedValue(channel="mem:ECM_TARGET_EQUIV_RATIO", source="first read"),
    }
    started = scenario.model_copy(update={"initial_state": state})
    assert check_scenario(contract, sha, started).id == "ecu_replay"


# -- verifier findings, 2026-10-05 ------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode_id", "quantity", "source"),
    [
        ("coupled_rollout", "base_fuel_schedule", "mem:ECM_AIR_CHARGE_SLOT_103A"),
        ("conditional_rom_comparison", "stft_b1", "obd:SHORT_FUEL_TRIM_1"),
        ("conditional_rom_comparison", "engine_speed", "obd:RPM"),
        ("conditional_rom_comparison", "mass_air_flow", "obd:MAF"),
    ],
)
def test_a_relabelled_recording_cannot_feed_a_comparison_or_rollout(
    mode_id: str, quantity: str, source: str
) -> None:
    """A recorded channel stays a recording when it is called a prescribed
    boundary: those modes take stated values only."""
    raw = _raw()
    _mode(raw, mode_id)["roles"][quantity] = {
        "role": "prescribed_boundary",
        "source": source,
        "assumption": "relabelled",
    }
    with pytest.raises(ValidationError, match=r"relabelled measured input|predicted-as-measured"):
        SimContract.model_validate(raw)
    # A channel no computed quantity carries is refused for the same reason.
    raw = _raw()
    _mode(raw, "conditional_rom_comparison")["roles"]["intake_air_temperature"]["source"] = (
        "obd:INTAKE_TEMP"
    )
    _refused(raw, "relabelled measured input")


def test_a_sensor_carries_what_it_measures_even_when_the_mode_ignores_it() -> None:
    """Plant replay predicts delivered lambda; the wideband channel is its
    reading, so it cannot feed the replay even with the sensor model unused."""
    raw = _raw()
    roles = _mode(raw, "plant_replay")["roles"]
    roles["lambda_sensor_b1"] = {"role": "unused"}
    roles["fuel_rail_pressure"] = {"role": "measured_input", "source": "obd:O2_WR_LAMBDA_S1"}
    _refused(raw, "observes delivered_lambda")


def test_a_comparison_scenario_takes_stated_values_only() -> None:
    contract, sha = load_contract(PROFILE)
    scenario = _comparison(contract, sha)
    for field, quantity, channel in (
        ("boundaries", "engine_speed", "obd:RPM"),
        ("boundaries", "stft_b1", "obd:SHORT_FUEL_TRIM_1"),
        ("initial_state", "target_phi_word", "mem:ECM_TARGET_EQUIV_RATIO"),
    ):
        values = {
            **getattr(scenario, field),
            quantity: SuppliedValue(channel=channel, source="drive"),
        }
        with pytest.raises(SimulationRefusalError) as refused:
            check_scenario(contract, sha, scenario.model_copy(update={field: values}))
        assert refused.value.reason == "recorded_value_refused", (quantity, channel)


def test_a_replay_boundary_cannot_be_read_from_a_computed_quantitys_channel() -> None:
    contract, sha = load_contract(PROFILE)
    mode = contract.mode("ecu_replay")
    boundaries = {
        q: SuppliedValue(value=0.0, source="key-on read")
        for q, a in mode.roles.items()
        if a.source == "scenario"
    }
    boundaries["idle_air_learned"] = SuppliedValue(
        channel="mem:ECM_FUEL_COMP_X_INPUT", source="wrong window"
    )
    scenario = Scenario(
        contract_sha256=sha,
        mode="ecu_replay",
        ecu=[EcuIdentity(program_id="p", image_sha256=SHA_A, slot=1, slot_source="registry")],
        boundaries=boundaries,
    )
    with pytest.raises(SimulationRefusalError) as refused:
        check_scenario(contract, sha, scenario)
    assert refused.value.reason == "predicted_as_measured"


def test_pid_0x44_is_a_leak_only_alias_of_phi() -> None:
    """PID 0x44 = phi x (A/F target / 14.7): never scored as phi, never fed to
    a mode computing phi."""
    contract, _ = load_contract(PROFILE)
    phi = contract.quantity("target_phi_word")
    assert phi.observed_as == ["mem:ECM_TARGET_EQUIV_RATIO"]
    assert phi.leak_aliases == ["obd:COMMANDED_EQUIV_RATIO"]
    raw = _raw()
    _quantity(raw, "target_phi_word")["observed_as"].append("obd:COMMANDED_EQUIV_RATIO")
    _refused(raw, "either scored or a leak-only alias")


def test_the_ecu_tick_is_indexed_in_the_main_loop_table_and_its_period_unverified() -> None:
    contract, _ = load_contract(PROFILE)
    schedule = contract.ecu_schedule
    assert schedule.task_table == 0x0219D0
    assert [(t.entry, t.function) for t in schedule.tasks] == [
        (37, 0x0575EC),
        (41, 0x057144),
        (42, 0x053B8C),
        (44, 0x0527CC),
    ]
    clocks = {c.id: c for c in contract.clocks}
    assert clocks[schedule.clock].period_status == "unverified"
    assert clocks[contract.solver.clock].period_status == "chosen"
    raw = _raw()
    del next(c for c in raw["clocks"] if c["kind"] == "ecu_time_task")["period_status"]
    _refused(raw, "period_status goes with")


def test_undecoded_facts_are_absent_or_labelled_inferred() -> None:
    contract, _ = load_contract(PROFILE)
    for q_id in ("maf_reading", "pedal_sensor", "barometric_counts", "lambda_sensor_b1"):
        sensor = contract.quantity(q_id).sensor
        assert sensor is not None and sensor.quantization is None, q_id
    pulse = contract.quantity("injector_pulse_index0").integer
    assert pulse is not None and pulse.confidence == "inferred"
    assert pulse.note is not None and "0xFFFF2710" in pulse.note
    assert "inferred" in pulse.scale
    for q in contract.quantities:
        if q.integer is not None and "not decoded" in q.integer.scale:
            assert q.integer.confidence == "inferred", q.id
