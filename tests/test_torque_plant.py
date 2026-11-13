"""E07.5 stage 4: the minimum torque model (E09.13) as a declared structure.

What this pins, all offline and on synthetic known answers (nothing here is a
torque measurement, and nothing is fitted -- queue entry Q27):

- the structure's own shape: eta(spark) is 1 at MBT, symmetric and flat there;
  eta(lambda) peaks at its declared peak and is 1 at stoich; indicated torque
  scales with the air per event; a hand-computed case is reproduced;
- MBT is a parameter, never assumed: an unstated MBT is refused by name;
- the registry follows E07.3's rules (a literature bracket names its
  citation, nothing is ``identifiable``), and agrees with the contract and the
  fuel plant (``check_registry``);
- the contract: spark is an actuator commanded by the ECU timing byte, the
  stage is built with bounded applicability and stays not validated, naming
  what it owes.
"""

from __future__ import annotations

import copy
import functools
import math
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from src.analysis.engine_dynamics import check_internal_models, subsystem_standing
from src.analysis.sim_contract import SimContract, SimulationRefusalError, load_contract
from src.analysis.torque_plant import (
    INPUTS,
    PLANT_FILE,
    READS,
    TorqueParameters,
    TorquePlantProfile,
    check_registry,
    evaluate,
    lambda_efficiency,
    load_torque_plant,
    parameter_case,
    retard_from_mbt,
    spark_efficiency,
)
from src.yaml_load import load_yaml

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "data" / "nissan-xterra-2008"

#: One bracket case: every unmeasured parameter stated inside its bracket.
CASE: dict[str, float] = {
    "fuel_lower_heating_value": 42.5,
    "fuel_stoich_afr": 14.1,
    "indicated_efficiency_peak": 0.36,
    "lambda_peak": 0.9,
    "lambda_peak_gain": 0.04,
    "lambda_rich_curvature": 1.5,
    "spark_mbt_advance": 30.0,
    "spark_efficiency_curvature": 0.0005,
    "friction_fmep_0": 97.0,
    "friction_fmep_1": 15.0,
    "friction_fmep_2": 5.0,
    "exhaust_back_pressure": 5.0,
}


@functools.cache
def _parsed_plant() -> dict[str, Any]:
    parsed: dict[str, Any] = load_yaml((PROFILE / PLANT_FILE).read_text())
    return parsed


def _raw_plant() -> dict[str, Any]:
    return copy.deepcopy(_parsed_plant())


def _param(raw: dict[str, Any], pid: str) -> dict[str, Any]:
    found: dict[str, Any] = next(p for p in raw["registry"]["parameters"] if p["id"] == pid)
    return found


@pytest.fixture(scope="module")
def world() -> tuple[SimContract, TorquePlantProfile, str]:
    contract, _ = load_contract(PROFILE)
    profile, sha = load_torque_plant(PROFILE)
    return contract, profile, sha


def _samples(n: int = 1, **over: Any) -> dict[str, Any]:
    base = {
        "engine_speed": 3000.0,
        "cylinder_air_flow": 60.0,
        "delivered_lambda": 1.0,
        "spark_advance": 30.0,
        "manifold_pressure": 60.0,
        "barometric_pressure": 80.0,
        "intake_air_temperature": 30.0,
    }
    base.update(over)
    return {k: np.broadcast_to(np.asarray(v, dtype=float), (n,)).copy() for k, v in base.items()}


# -- the shape ------------------------------------------------------------------------


def test_spark_efficiency_is_one_at_mbt_symmetric_and_flat_there() -> None:
    c = 0.0005
    assert spark_efficiency(0.0, c) == 1.0
    for d in (1.0, 5.0, 12.0):
        assert spark_efficiency(d, c) == pytest.approx(spark_efficiency(-d, c), abs=0)
        assert spark_efficiency(d, c) == pytest.approx(1.0 - c * d * d)
    # 10 deg from MBT at c = 5e-4 costs 5 %.
    assert spark_efficiency(10.0, c) == pytest.approx(0.95)
    assert retard_from_mbt(25.0, 30.0) == 5.0  # spark later than MBT: retarded


def test_mbt_is_never_assumed() -> None:
    with pytest.raises(SimulationRefusalError) as refused:
        retard_from_mbt(25.0, None)
    assert refused.value.reason == "spark_mbt_unstated"


def test_lambda_efficiency_peaks_at_its_peak_and_is_one_at_stoich() -> None:
    peak, gain, k = 0.9, 0.04, 1.5
    assert lambda_efficiency(1.0, peak, gain, k) == pytest.approx(1.0)
    assert lambda_efficiency(peak, peak, gain, k) == pytest.approx(1.0 + gain)
    grid = np.linspace(0.75, 1.2, 4501)
    eta = lambda_efficiency(grid, peak, gain, k)
    assert grid[int(np.argmax(eta))] == pytest.approx(peak, abs=1e-3)
    # Lean of stoich the charge is fuel-limited: 1 / lambda.
    assert lambda_efficiency(1.1, peak, gain, k) == pytest.approx(1.0 / 1.1)
    # Continuous at the peak and at stoich; positive over the declared range
    # at every bracket corner.
    for x in (peak, 1.0):
        assert lambda_efficiency(x - 1e-9, peak, gain, k) == pytest.approx(
            lambda_efficiency(x + 1e-9, peak, gain, k), abs=1e-6
        )
    for p in (0.85, 0.95):
        for g in (0.02, 0.08):
            for kk in (0.5, 3.0):
                assert (lambda_efficiency(grid, p, g, kk) > 0.5).all()


def test_a_hand_computed_case_is_reproduced(world: Any) -> None:
    contract, profile, sha = world
    r = evaluate(profile, sha, contract, CASE, _samples())
    air = 60.0 * 120.0 / (3000.0 * 6.0)  # g per event
    cycle_j = 6.0 * air * 42.5 * 1000.0 / 14.1 * 0.36  # eta(1) = eta(MBT) = 1
    friction_kpa = 97.0 + 15.0 * 3.0 + 5.0 * 9.0
    pumping_kpa = 80.0 + 5.0 - 60.0
    four_pi = 4.0 * math.pi
    assert r.air_per_event_g[0] == pytest.approx(air)
    assert r.indicated_nm[0] == pytest.approx(cycle_j / four_pi)
    assert r.friction_nm[0] == pytest.approx(friction_kpa * 3.954 / four_pi)
    assert r.pumping_nm[0] == pytest.approx(pumping_kpa * 3.954 / four_pi)
    assert r.brake_nm[0] == pytest.approx(r.indicated_nm[0] - r.friction_nm[0] - r.pumping_nm[0])
    # A bracket case, never a prediction: it names what it rests on.
    assert r.kind == "bracket_case" and r.plant_sha256 == sha
    # M01.19: the back-pressure bracket's limit rides on every result.
    assert any("exhaust_back_pressure bracket" in t for t in r.limitations)
    assert set(r.unmeasured) == set(CASE)
    assert "spark_mbt_unmeasured" in r.effects and "pumping_from_unmeasured_map" in r.effects
    assert r.refused == {}


def test_indicated_torque_scales_with_air_and_spark_has_no_slope_at_mbt(world: Any) -> None:
    contract, profile, sha = world
    one = evaluate(profile, sha, contract, CASE, _samples(cylinder_air_flow=40.0))
    two = evaluate(profile, sha, contract, CASE, _samples(cylinder_air_flow=80.0))
    assert two.indicated_nm[0] == pytest.approx(2.0 * one.indicated_nm[0])
    # Friction and pumping do not read air: brake moves by the indicated change.
    assert two.brake_nm[0] - one.brake_nm[0] == pytest.approx(one.indicated_nm[0])
    h = 1e-3

    def brake(spark: float) -> float:
        return float(
            evaluate(profile, sha, contract, CASE, _samples(spark_advance=spark)).brake_nm[0]
        )

    mbt = CASE["spark_mbt_advance"]
    at_mbt = (brake(mbt + h) - brake(mbt - h)) / (2 * h)
    retarded = (brake(mbt - 10 + h) - brake(mbt - 10 - h)) / (2 * h)
    assert abs(at_mbt) < 1e-6
    assert retarded > 0.5  # advancing toward MBT from 10 deg retarded gains torque


def test_samples_outside_the_stage_are_named_never_clipped(world: Any) -> None:
    contract, profile, sha = world
    s = _samples(4)
    s["engine_speed"][1] = 7000.0
    s["manifold_pressure"][2] = 90.0  # above the 80 kPa baro
    s["spark_advance"][3] = 5.0  # 25 deg from MBT 30: outside the declared shape
    r = evaluate(profile, sha, contract, CASE, s)
    assert np.isfinite(r.brake_nm[0])
    assert np.isnan(r.brake_nm[1:]).all()
    assert r.refused["outside_applicability:engine_speed"].tolist() == [False, True, False, False]
    assert r.refused["manifold_above_ambient"].tolist() == [False, False, True, False]
    assert r.refused["spark_outside_shape"].tolist() == [False, False, False, True]
    with pytest.raises(SimulationRefusalError, match="input_missing"):
        evaluate(profile, sha, contract, CASE, {k: v for k, v in s.items() if k != "spark_advance"})


# -- the registry ----------------------------------------------------------------------


def test_a_case_states_every_unmeasured_parameter_inside_its_bracket(world: Any) -> None:
    _, profile, _ = world
    without_mbt = {k: v for k, v in CASE.items() if k != "spark_mbt_advance"}
    with pytest.raises(SimulationRefusalError) as refused:
        parameter_case(profile, without_mbt)
    assert refused.value.reason == "spark_mbt_unstated"
    with pytest.raises(SimulationRefusalError, match="parameter_unstated"):
        parameter_case(profile, {k: v for k, v in CASE.items() if k != "friction_fmep_2"})
    with pytest.raises(SimulationRefusalError, match="outside_bracket"):
        parameter_case(profile, {**CASE, "spark_mbt_advance": 50.0})
    with pytest.raises(SimulationRefusalError, match="parameter_known"):
        parameter_case(profile, {**CASE, "displacement": 4.0})
    with pytest.raises(SimulationRefusalError, match="parameter_undeclared"):
        parameter_case(profile, {**CASE, "boost": 1.0})


def test_the_registry_follows_e07_3s_rules() -> None:
    TorquePlantProfile.model_validate(_raw_plant())
    breaks: list[tuple[str, Any, str]] = [
        ("spark_efficiency_curvature", {"citation": None}, "names its citation"),
        ("spark_mbt_advance", {"citation": "a book"}, "only a literature prior"),
        ("lambda_peak", {"status": "identifiable"}, "fits nothing"),
        ("displacement", {"bracket": [3.9, 4.0]}, "no bracket or measurer"),
        ("friction_fmep_0", {"measured_by": []}, "names its bracket"),
        ("friction_fmep_0", {"value": 97.0}, "carries no value"),
        ("lambda_peak", {"bracket": [0.95, 0.85]}, "is empty"),
    ]
    for pid, change, match in breaks:
        raw = _raw_plant()
        _param(raw, pid).update(change)
        with pytest.raises(ValidationError, match=match):
            TorquePlantProfile.model_validate(raw)
    raw = _raw_plant()
    raw["registry"]["parameters"] = [
        p for p in raw["registry"]["parameters"] if p["id"] != "cylinders"
    ]
    with pytest.raises(ValidationError, match="does not declare"):
        TorquePlantProfile.model_validate(raw)
    raw = _raw_plant()
    del raw["limitations"]  # M01.19: the stated limits are required
    with pytest.raises(ValidationError, match="limitations"):
        TorquePlantProfile.model_validate(raw)
    assert set(READS) == {p["id"] for p in _parsed_plant()["registry"]["parameters"]}


def test_the_registry_agrees_with_the_contract_and_the_fuel_plant(world: Any) -> None:
    contract, profile, _ = world
    assert check_registry(profile, contract, PROFILE) == []
    # Every unmeasured parameter waits on a measurement the contract names.
    for p in profile.registry.parameters:
        assert (p.status == "known") != bool(p.measured_by), p.id
    raw = _raw_plant()
    _param(raw, "lambda_peak")["measured_by"] = ["controlled_spark_sweep"]
    _param(raw, "fuel_stoich_afr")["bracket"] = [14.0, 15.0]
    _param(raw, "exhaust_back_pressure")["measured_by"] = ["dyno_day"]
    problems = check_registry(TorquePlantProfile.model_validate(raw), contract, PROFILE)
    assert any("lambda_peak: controlled_spark_sweep does not list it" in x for x in problems)
    assert any(x.startswith("fuel_stoich_afr: bracket") for x in problems)
    assert any("'dyno_day' is not a required measurement" in x for x in problems)
    assert check_internal_models(contract, PROFILE) == []


# -- the contract ------------------------------------------------------------------------


def test_spark_is_an_actuator_commanded_by_the_ecus_timing_byte(world: Any) -> None:
    contract, _, _ = world
    spark = contract.quantity("spark_advance")
    assert spark.kind == "actuator" and spark.actuator is not None
    assert spark.actuator.control == "spark" and spark.units == "deg_btdc"
    byte = contract.quantity(spark.actuator.commanded_by)
    assert byte.kind == "ecu" and byte.integer is not None
    assert (byte.integer.ram, byte.integer.width, byte.manifest_node) == (
        0xFFFF11FF,
        8,
        "timing_final_byte",
    )
    # The catalog's decode of the same byte (DID 0x110A): degrees = 110 - raw.
    dids = load_yaml((REPO / "data" / "dids.yaml").read_text())["dids"]
    (timing,) = [d for d in dids if d["did"] == 0x110A]
    assert (timing["scale"], timing["offset"]) == (-1.0, 110.0)
    assert spark.actuator.maximum == 110.0 and spark.actuator.minimum == 110.0 - 255
    torque = contract.quantity("engine_torque")
    assert "spark_advance" in {d.quantity for d in torque.depends_on}
    assert {d.quantity for d in torque.depends_on} <= set(INPUTS)


def test_the_torque_stage_is_built_bounded_and_not_validated(world: Any) -> None:
    contract, _, _ = world
    sub = contract.subsystem("torque_production")
    assert sub.status == "built" and sub.applicability is not None
    assert sub.applicability.hardware_configurations == ["na_stock_injectors_2026_10"]
    bounded = {b.quantity for b in sub.applicability.bounds}
    assert {"engine_speed", "barometric_pressure", "intake_air_temperature"} <= bounded
    st = subsystem_standing(contract, sub)  # nothing read from the profile
    assert st.built and not st.validated
    owed = {r.removeprefix("requires_measurement:") for r in st.reasons if r.startswith("requires")}
    assert {
        "vehicle_mass",
        "h07_road_load",
        "h07_na_baseline",
        "h07_repeatability",
        "spark_trim_unit",
        "controlled_spark_sweep",
        "map_sensor",
    } <= owed
    tasks = {m.task for m in sub.required_measurements if m.id in owed}
    assert {"H07.10", "H07.5", "H07.12", "H07.8", "N01.2"} <= tasks
    assert "no_passing_validation:torque_production" in st.reasons


def test_a_pass_against_the_fuel_energy_estimate_does_not_validate_torque() -> None:
    raw = load_yaml((PROFILE / "engine_sim_contract.yaml").read_text())
    sub = next(s for s in raw["subsystems"] if s["id"] == "torque_production")
    sub["validations"] = [
        {
            "reference": "fuel_energy_torque",
            "recordings": ["_capture-x-2026-11-01T120000"],
            "verdict": "pass",
            "scored_on": "2026-11-01",
            "evidence": "a pass against the estimate built from the stage's own inputs",
        }
    ]
    contract = SimContract.model_validate(raw)
    st = subsystem_standing(contract, contract.subsystem("torque_production"))
    assert not st.validated
    assert any(r.startswith("reference_not_independent:fuel_energy_torque") for r in st.reasons)


def test_a_case_cannot_be_built_around_the_registry(world: Any) -> None:
    """The verifier's repro (2026-10-17): a hand-built case with MBT at 5 deg
    and an indicated efficiency of 0.9 ran as a bracket case naming nothing.
    Now a case exists only through ``parameter_case``, and ``evaluate`` makes
    it itself."""
    contract, profile, sha = world
    values = {"displacement": 3.954, "cylinders": 6.0, **CASE}
    values.update(spark_mbt_advance=5.0, indicated_efficiency_peak=0.9)
    with pytest.raises(SimulationRefusalError) as refused:
        TorqueParameters(values, (), profile.plant_version)
    assert refused.value.reason == "parameters_not_from_registry"
    with pytest.raises(SimulationRefusalError) as refused:
        evaluate(profile, sha, contract, {**CASE, "indicated_efficiency_peak": 0.9}, _samples())
    assert refused.value.reason == "outside_bracket"
    without_mbt = {k: v for k, v in CASE.items() if k != "spark_mbt_advance"}
    with pytest.raises(SimulationRefusalError) as refused:
        evaluate(profile, sha, contract, without_mbt, _samples())
    assert refused.value.reason == "spark_mbt_unstated"
    case = parameter_case(profile, CASE)
    with pytest.raises(SimulationRefusalError) as refused:
        evaluate(profile, sha, contract, case, _samples())  # type: ignore[arg-type]
    assert refused.value.reason == "parameters_not_stated"


def test_a_pull_identifies_the_energy_product_never_its_parts(world: Any) -> None:
    contract, profile, _ = world
    (product,) = profile.registry.products
    assert set(product.components) == {
        "fuel_lower_heating_value",
        "indicated_efficiency_peak",
        "fuel_stoich_afr",
    }
    baseline = next(
        m
        for m in contract.subsystem("torque_production").required_measurements
        if m.id == "h07_na_baseline"
    )
    assert product.id in baseline.identifies
    assert not set(product.components) & set(baseline.identifies)
    # Listing a part as identified by the pull is refused.
    raw = load_yaml((PROFILE / "engine_sim_contract.yaml").read_text())
    sub = next(s for s in raw["subsystems"] if s["id"] == "torque_production")
    m = next(m for m in sub["required_measurements"] if m["id"] == "h07_na_baseline")
    m["identifies"].append("indicated_efficiency_peak")
    problems = check_registry(profile, SimContract.model_validate(raw), PROFILE)
    assert any("lists the product, not the part" in x for x in problems)


def test_a_measurement_two_stages_name_is_one_fact() -> None:
    """vehicle_mass and h07_road_load are named by torque_production and
    rotational_dynamics: their status and task cannot drift apart."""
    raw = load_yaml((PROFILE / "engine_sim_contract.yaml").read_text())
    SimContract.model_validate(raw)
    sub = next(s for s in raw["subsystems"] if s["id"] == "rotational_dynamics")
    m = next(m for m in sub["required_measurements"] if m["id"] == "vehicle_mass")
    m["status"] = "available"
    with pytest.raises(ValidationError, match="vehicle_mass: torque_production and"):
        SimContract.model_validate(raw)
