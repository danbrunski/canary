"""E07.5: dynamic plant stages -- stage 1 (manifold mass/energy balance and
cylinder breathing), every stage's derived standing, and the claims a result
may not make.

The real profile's contract and plants are loaded; acceptance clauses are
shown on them, and the refusals by mutating the contract in memory.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from src.analysis.engine_dynamics import (
    CaseConvergence,
    ConvergenceReport,
    SubsystemStanding,
    breathing_bound,
    charge_fraction,
    check_internal_models,
    check_reference,
    claim_refusals,
    consistency,
    contract_step_s,
    convergence_study,
    integrate,
    load_kit,
    replay_manifold,
    require_claim,
    resolve_manifold,
    rollout_blockers,
    standings,
    subsystem_standing,
)
from src.analysis.fuel_plant import (
    KELVIN,
    air_per_event_g,
    manifold_pressure_speed_density,
)
from src.analysis.sim_contract import (
    CONTRACT_FILE,
    SimContract,
    SimulationRefusalError,
)

PROFILE = Path(__file__).resolve().parents[1] / "data" / "nissan-xterra-2008"
HW = "na_stock_injectors_2026_10"


@pytest.fixture(scope="module")
def kit() -> Any:
    return load_kit(PROFILE)


@pytest.fixture(scope="module")
def study(kit: Any) -> ConvergenceReport:
    """The real refinement study (seconds): only the two study tests use it."""
    contract, manifold, fuel = kit
    return convergence_study(contract, manifold, fuel)


def _passing_stub(contract: SimContract, *, passed: bool = True) -> ConvergenceReport:
    """A report standing in for the real study where the study is not what
    is under test (it says passed for the contract's own step and check)."""
    sub = contract.subsystem("manifold_breathing")
    assert sub.numerical is not None
    case = CaseConvergence("stub", 6500.0, 0.01, (0.0,), (0.0,), (), 4.0, 0.0, 0.0, 0.0, passed, ())
    return ConvergenceReport(
        contract_step_s(contract),
        contract.solver.method,
        tuple(sub.numerical.refinement),
        sub.numerical,
        (case,),
    )


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / CONTRACT_FILE).read_text())
    return raw


def _sub(raw: dict[str, Any], sub_id: str) -> dict[str, Any]:
    sub: dict[str, Any] = next(s for s in raw["subsystems"] if s["id"] == sub_id)
    return sub


def _all_validated(contract: SimContract) -> dict[str, SubsystemStanding]:
    return {s.id: SubsystemStanding(s.id, True, None, True, ()) for s in contract.subsystems}


# -- the contract's stages ------------------------------------------------------------


def test_every_plant_quantity_belongs_to_exactly_one_declared_stage(kit: Any) -> None:
    contract, _, _ = kit
    owned = [q for s in contract.subsystems for q in s.computes]
    plant = {q.id for q in contract.quantities if q.kind in ("plant", "actuator")}
    assert sorted(owned) == sorted(plant)
    owner = contract.subsystem_of("manifold_pressure")
    assert owner is not None and owner.id == "manifold_breathing"
    raw = _raw()
    raw["subsystems"] = [s for s in raw["subsystems"] if s["id"] != "manifold_breathing"]
    with pytest.raises(ValidationError, match="belong to no subsystem"):
        SimContract.model_validate(raw)
    raw = _raw()
    _sub(raw, "fuel_rail")["computes"].append("manifold_pressure")
    with pytest.raises(ValidationError, match="computed by both"):
        SimContract.model_validate(raw)
    raw = _raw()
    _sub(raw, "fuel_rail")["computes"].append("target_phi_word")
    with pytest.raises(ValidationError, match="not a declared plant or actuator"):
        SimContract.model_validate(raw)


def test_a_built_stage_declares_its_envelope_and_a_stateful_one_its_numerics() -> None:
    raw = _raw()
    del _sub(raw, "manifold_breathing")["applicability"]
    with pytest.raises(ValidationError, match="applicability is required"):
        SimContract.model_validate(raw)
    raw = _raw()
    del _sub(raw, "manifold_breathing")["numerical"]
    with pytest.raises(ValidationError, match="step-size refinement"):
        SimContract.model_validate(raw)
    raw = _raw()
    sub = _sub(raw, "engine_thermal")
    sub["references"], sub["required_measurements"] = [], []
    with pytest.raises(ValidationError, match="name what would validate it"):
        SimContract.model_validate(raw)


# -- stage 1 physics ---------------------------------------------------------------------


def test_the_steady_manifold_is_the_fuel_plants_speed_density_at_the_same_ve(kit: Any) -> None:
    contract, manifold, fuel = kit
    params = resolve_manifold(manifold, fuel)
    # One assumption, read from the fuel plant's registry, never restated.
    wa = fuel.registry.get("manifold_pressure").working_assumption
    assert params.volumetric_efficiency == wa.value
    maf, rpm, iat, baro = 30.0, 2500.0, 30.0, 80.0
    trace = integrate(
        np.full(400, maf), rpm, iat, [params], step_s=0.00128, method=contract.solver.method
    )
    air = air_per_event_g(maf, rpm, fuel.geometry)
    sd = manifold_pressure_speed_density(
        air, iat, baro, fuel.geometry, params.volumetric_efficiency
    )
    assert trace.map_kpa[-1, 0] == pytest.approx(float(sd), rel=1e-12)
    assert trace.cylinder_gps[-1, 0] == pytest.approx(maf, rel=1e-12)
    assert trace.temp_k[-1, 0] == pytest.approx(iat + KELVIN, rel=1e-12)
    with pytest.raises(SimulationRefusalError, match="override_outside_bracket"):
        resolve_manifold(manifold, fuel, overrides={"volumetric_efficiency": 1.2})


def test_limiting_cases_no_inflow_empties_at_tau_and_isothermal_holds_temperature(
    kit: Any,
) -> None:
    contract, manifold, fuel = kit
    adia = resolve_manifold(manifold, fuel)
    iso = resolve_manifold(manifold, fuel, energy_balance="isothermal")
    rpm, h = 2000.0, 0.00128
    tau = float(adia.tau_s(rpm))
    n = int(3 * tau / h)
    trace = integrate(
        np.zeros(n),
        rpm,
        25.0,
        [adia, iso],
        step_s=h,
        method="rk4",
        initial_mass_g=[10.0, 10.0],
        initial_temp_k=[298.15, 298.15],
    )
    exact = 10.0 * np.exp(-trace.t_s / tau)
    # The mass equation is the same in both forms and exact to rk4's order.
    assert np.max(np.abs(trace.mass_g[:, 0] - exact)) < 1e-7
    assert np.max(np.abs(trace.mass_g[:, 1] - exact)) < 1e-7
    assert np.all(trace.temp_k[:, 1] == pytest.approx(298.15))
    # An emptying adiabatic manifold expands and cools.
    assert trace.temp_k[-1, 0] < 298.15 - 10.0


def test_consistency_holds_a_correct_trace_and_fails_a_mutated_integrator(kit: Any) -> None:
    """The check re-steps every stored state with its own code and holds the
    reported cylinder flow to the breathing law: it is not an identity of the
    integrator. Mutations emulate the integrator drawing 1.7x the breathing
    law's air, and booking 0.6x the energy rate."""
    _, manifold, fuel = kit
    params = resolve_manifold(manifold, fuel)
    maf = np.r_[np.full(50, 8.0), np.full(300, 60.0), np.full(300, 12.0)]
    for method in ("rk4", "fixed_step_euler"):
        trace = integrate(maf, 2500.0, 30.0, [params], step_s=0.00128, method=method)
        ok = consistency(trace)
        assert ok.equation_residual_rel[0] < 1e-12, method
        assert ok.breathing_excess[0] <= 1.0, method
        assert not ok.nonphysical.any()
    trace = integrate(maf, 2500.0, 30.0, [params], step_s=0.00128, method="rk4")
    # Outflow x1.7: integrated with 1.7x the breathing, reported as the law.
    greedy = dataclasses.replace(params, volumetric_efficiency=1.7 * params.volumetric_efficiency)
    drew_more = integrate(maf, 2500.0, 30.0, [greedy], step_s=0.00128, method="rk4")
    mutated = consistency(dataclasses.replace(drew_more, params=(params,)))
    assert mutated.equation_residual_rel[0] > 1e-3
    assert mutated.breathing_excess[0] > 10.0
    # Energy rate x0.6: every energy increment booked at 0.6.
    e0 = trace.energy_j[0]
    short = dataclasses.replace(
        trace,
        energy_j=e0 + 0.6 * (trace.energy_j - e0),
        final_energy_j=e0 + 0.6 * (trace.final_energy_j - e0),
    )
    assert consistency(short).equation_residual_rel[0] > 1e-6
    # Reading MAF as the cylinder charge through a tip-in breaks the law.
    as_maf = dataclasses.replace(trace, cylinder_gps=trace.inflow_gps)
    assert consistency(as_maf).breathing_excess[0] > 10.0
    # One nonphysical sample cannot switch the check off for the rest: 1.3x
    # the law plus a negative mass at step 3 is still inconsistent, and the
    # bad step is named.
    hungry = dataclasses.replace(params, volumetric_efficiency=1.3 * params.volumetric_efficiency)
    over = integrate(maf, 2500.0, 30.0, [hungry], step_s=0.00128, method="rk4")
    mass = over.mass_g.copy()
    mass[3] = -1.0
    broken = consistency(dataclasses.replace(over, params=(params,), mass_g=mass))
    assert broken.nonphysical[3, 0] and broken.nonphysical.sum() == 1
    assert broken.equation_residual_rel[0] > 1e-3
    assert broken.breathing_excess[0] > 10.0


def test_a_nonphysical_sample_does_not_hide_an_inconsistent_replay(
    kit: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verifier's repro: breathe at 1.3x the law with one negative-mass
    step at k = 3. The replay used to accept it (residual 0.34, excess 8e7)."""
    import src.analysis.engine_dynamics as ed

    contract, manifold, fuel = kit
    params = resolve_manifold(manifold, fuel)
    real = ed.integrate

    def mutated(inflow: Any, rpm: Any, iat: Any, plist: Any, **kwargs: Any) -> Any:
        (p,) = plist
        hungry = dataclasses.replace(p, volumetric_efficiency=1.3 * p.volumetric_efficiency)
        trace = real(inflow, rpm, iat, [hungry], **kwargs)
        mass = trace.mass_g.copy()
        mass[3] = -1.0
        return dataclasses.replace(trace, params=(p,), mass_g=mass)

    monkeypatch.setattr(ed, "integrate", mutated)
    times, values = _replay_inputs(maf=20.0, rpm=2000.0)
    with pytest.raises(SimulationRefusalError, match="integration_inconsistent"):
        replay_manifold(
            contract,
            HW,
            params,
            times_s=times,
            values=values,
            convergence=_passing_stub(contract),
        )


def test_standing_reads_a_report_only_for_the_contracts_own_step(kit: Any) -> None:
    contract, _, _ = kit
    sub = contract.subsystem("manifold_breathing")
    stub = _passing_stub(contract)
    assert subsystem_standing(
        contract, sub, profile_dir=PROFILE, convergence=stub
    ).numerically_verified
    for other, reason in (
        (dataclasses.replace(stub, step_s=0.00064), "numerics_unverified"),
        (dataclasses.replace(stub, method="fixed_step_euler"), "numerics_unverified"),
        (dataclasses.replace(stub, cases=()), "numerics_unconverged"),
    ):
        st = subsystem_standing(contract, sub, profile_dir=PROFILE, convergence=other)
        assert st.numerically_verified is False
        assert f"{reason}:manifold_breathing" in st.reasons


# -- numerics: the contract's solver step is established, not assumed ----------------------


def test_the_contract_solver_converges_at_every_applicability_corner(
    kit: Any, study: ConvergenceReport
) -> None:
    contract, _, _ = kit
    assert study.method == contract.solver.method == "rk4"
    assert study.step_s == pytest.approx(0.00128)
    assert study.passed, study.to_dict()["failed"]
    assert len(study.cases) == 18  # 9 parameter cases x 2 rpm corners
    summary = study.to_dict()
    lo, hi = summary["observed_order_range"]
    assert 3.85 <= lo <= hi <= 4.15
    assert summary["worst_rpm"] == 6500.0
    assert summary["worst_flow_error"][0] < 1e-5
    assert summary["max_equation_residual_rel"] < 1e-12
    assert summary["max_breathing_excess"] <= 1.0


def test_the_refinement_study_can_fail_euler_at_the_same_step(kit: Any) -> None:
    contract, manifold, fuel = kit
    report = convergence_study(contract, manifold, fuel, method="fixed_step_euler")
    summary = report.to_dict()
    assert not report.passed
    # Converging at first order, but too coarse at the high-rpm corners.
    assert 0.95 <= summary["observed_order_range"][0] <= summary["observed_order_range"][1] <= 1.15
    assert all(":rpm=6500" in case for case in summary["failed"])
    assert summary["worst_flow_error"][0] > 0.05
    # Against the rk4 contract this report is of another method; against a
    # contract that still solved with Euler it is that contract's, and failed.
    standing = subsystem_standing(
        contract, contract.subsystem("manifold_breathing"), profile_dir=PROFILE, convergence=report
    )
    assert standing.numerically_verified is False
    assert "numerics_unverified:manifold_breathing" in standing.reasons
    raw = _raw()
    raw["solver"]["method"] = "fixed_step_euler"
    euler = SimContract.model_validate(raw)
    standing = subsystem_standing(
        euler, euler.subsystem("manifold_breathing"), profile_dir=PROFILE, convergence=report
    )
    assert standing.numerically_verified is False
    assert "numerics_unconverged:manifold_breathing" in standing.reasons


# -- replay: bounded, never clipped, rpm prescribed ------------------------------------------


def _replay_inputs(maf: float, rpm: float) -> tuple[dict[str, Any], dict[str, Any]]:
    t = np.arange(0.0, 2.0, 0.25)
    times = {q: t for q in ("mass_air_flow", "engine_speed", "intake_air_temperature")}
    times["barometric_pressure"] = t
    values = {
        "mass_air_flow": np.r_[np.full(4, maf / 4), np.full(4, maf)],
        "engine_speed": np.full(len(t), rpm),
        "intake_air_temperature": np.full(len(t), 30.0),
        "barometric_pressure": np.full(len(t), 80.0),
    }
    return times, values


def test_replay_refuses_outside_its_envelope_and_never_clips_above_ambient(kit: Any) -> None:
    contract, manifold, fuel = kit
    params = resolve_manifold(manifold, fuel)
    verified = _passing_stub(contract)
    times, values = _replay_inputs(maf=20.0, rpm=2000.0)
    result = replay_manifold(
        contract, HW, params, times_s=times, values=values, convergence=verified
    )
    assert not result.refused.any()
    assert result.prescribed == ("engine_speed", "mass_air_flow")
    assert "not a prediction of speed or acceleration" in result.labels["engine_speed"]
    assert np.allclose(result.resolved_period_s, 120.0 / 2000.0)
    assert result.consistency.equation_residual_rel[0] < 1e-12
    assert result.consistency.breathing_excess[0] <= 1.0
    assert result.step_s == contract_step_s(contract)
    assert "manifold_volume_unmeasured" in result.effects
    # Charge fraction above the working VE: the steady manifold would sit
    # above ambient. Those samples are refused, not clipped to baro.
    fill = 80.0 * 1000 / (287.05 * 303.15) * 3.954 * 2000.0 / 120.0
    times, values = _replay_inputs(maf=0.9 * fill, rpm=2000.0)
    result = replay_manifold(
        contract, HW, params, times_s=times, values=values, convergence=verified
    )
    assert result.refused.any()
    assert np.isnan(result.map_kpa[result.refused]).all()
    assert result.refusal_reasons() == {"manifold_above_ambient": int(result.refused.sum())}
    times, values = _replay_inputs(maf=5.0, rpm=300.0)
    with pytest.raises(SimulationRefusalError, match="outside_applicability.*engine_speed"):
        replay_manifold(contract, HW, params, times_s=times, values=values, convergence=verified)
    times, values = _replay_inputs(maf=20.0, rpm=2000.0)
    with pytest.raises(SimulationRefusalError, match="new hardware configuration"):
        replay_manifold(
            contract, "supercharged", params, times_s=times, values=values, convergence=verified
        )


def test_a_coarse_step_or_an_unverified_study_is_refused(kit: Any) -> None:
    """The verifier's repro (2 L, VE 1.0, 6500 rpm, MAF 40 -> 120 g/s) gave
    -3.9e6 kPa at a 20 ms step. The step is now the contract's, and only with a
    passing study of exactly that step and method."""
    contract, manifold, fuel = kit
    params = dataclasses.replace(
        resolve_manifold(manifold, fuel), volume_l=2.0, volumetric_efficiency=1.0
    )
    t = np.arange(0.0, 2.0, 0.25)
    times = {
        q: t
        for q in ("mass_air_flow", "engine_speed", "intake_air_temperature", "barometric_pressure")
    }
    values = {
        "mass_air_flow": np.r_[np.full(4, 40.0), np.full(4, 120.0)],
        "engine_speed": np.full(len(t), 6500.0),
        "intake_air_temperature": np.full(len(t), 30.0),
        "barometric_pressure": np.full(len(t), 80.0),
    }
    stub = _passing_stub(contract)

    def run(c: SimContract, report: ConvergenceReport) -> Any:
        return replay_manifold(c, HW, params, times_s=times, values=values, convergence=report)

    for other in (
        dataclasses.replace(stub, step_s=0.02),
        dataclasses.replace(stub, method="fixed_step_euler"),
    ):
        with pytest.raises(SimulationRefusalError, match="numerics_unverified"):
            run(contract, other)
    for failing in (_passing_stub(contract, passed=False), dataclasses.replace(stub, cases=())):
        with pytest.raises(SimulationRefusalError, match="numerics_unconverged"):
            run(contract, failing)
    # A contract whose solver step is one whole ECU tick fails its own study,
    # so its replay is refused.
    raw = _raw()
    next(c for c in raw["clocks"] if c["id"] == raw["solver"]["clock"])["period_s"] = 0.01024
    coarse = SimContract.model_validate(raw)
    report = convergence_study(coarse, manifold, fuel)
    assert not report.passed
    with pytest.raises(SimulationRefusalError, match="numerics_unconverged"):
        run(coarse, report)
    # On the contract's own step the same boundary is finite and physical.
    result = run(contract, stub)
    assert not result.refused.any()
    assert np.all(np.isfinite(result.map_kpa)) and np.nanmin(result.map_kpa) > 0
    # The 20 ms Euler run the verifier found: its states are named nonphysical.
    held = np.r_[np.full(50, 40.0), np.full(50, 120.0)]
    blown = integrate(held, 6500.0, 30.0, [params], step_s=0.02, method="fixed_step_euler")
    assert consistency(blown).nonphysical.any()


def test_the_fuel_plants_own_map_is_a_declared_exception(kit: Any) -> None:
    """fuel-mv-1 (frozen) computes MAP itself in plant replay; the contract
    says so, where the two differ, and who owes the reconciliation."""
    contract, _, _ = kit
    assert check_internal_models(contract) == []
    (im,) = contract.subsystem("fuel_delivery").internal_models
    assert (im.quantity, im.owner, im.modes) == (
        "manifold_pressure",
        "manifold_breathing",
        ["plant_replay"],
    )
    assert "caps MAP at baro" in im.differs and "plant_version" in im.owed_by
    raw = _raw()
    _sub(raw, "fuel_delivery")["internal_models"] = []
    assert check_internal_models(SimContract.model_validate(raw)) == [
        "fuel_delivery: src.analysis.fuel_plant computes manifold_pressure undeclared"
    ]
    raw = _raw()
    _sub(raw, "fuel_delivery")["internal_models"][0]["owner"] = "fuel_rail"
    with pytest.raises(ValidationError, match="internal model of 'manifold_pressure'"):
        SimContract.model_validate(raw)
    st = standings(contract, PROFILE, convergence=_passing_stub(contract))
    assert (
        "internal_model:manifold_pressure:owed_by_next_plant_version" in st["fuel_delivery"].reasons
    )


# -- validation: independent references only ----------------------------------------------


def test_bfs_and_the_ambient_ve_cannot_validate_a_manifold_driven_by_maf(kit: Any) -> None:
    contract, _, _ = kit
    sub = contract.subsystem("manifold_breathing")
    refs = {r.id: r for r in sub.references}
    assert any(
        r.startswith("reference_not_independent:bfs_air_charge:") and "mass_air_flow" in r
        for r in check_reference(contract, sub, refs["bfs_air_charge"])
    )
    assert any(
        r.startswith("reference_not_independent:ve_ambient:") and "mass_air_flow" in r
        for r in check_reference(contract, sub, refs["ve_ambient"])
    )
    # A MAP sensor would be independent; it does not exist yet.
    assert check_reference(contract, sub, refs["map_sensor"]) == [
        "reference_requires_measurement:map_sensor:map_sensor"
    ]


def test_torque_is_never_calibrated_against_the_fuel_energy_estimate(kit: Any) -> None:
    contract, _, _ = kit
    sub = contract.subsystem("torque_production")
    refs = {r.id: r for r in sub.references}
    fuel_ref = check_reference(contract, sub, refs["fuel_energy_torque"])
    assert len(fuel_ref) == 1 and fuel_ref[0].startswith(
        "reference_not_independent:fuel_energy_torque:"
    )
    assert {"mass_air_flow", "delivered_lambda"} <= set(fuel_ref[0].split(":")[2].split(","))
    # Even marked available it stays refused: the refusal is the lineage.
    raw = _raw()
    for ref in _sub(raw, "torque_production")["references"]:
        if ref["id"] == "h07_wheel_torque":
            ref["status"], ref["requires"] = "available", None
    mutated = SimContract.model_validate(raw)
    sub2 = mutated.subsystem("torque_production")
    refs2 = {r.id: r for r in sub2.references}
    # H07's road-dyno torque shares only the declared rpm index: independent
    # once H07 validates it.
    assert check_reference(mutated, sub2, refs2["h07_wheel_torque"]) == []
    assert check_reference(mutated, sub2, refs2["fuel_energy_torque"]) == fuel_ref
    assert check_reference(contract, sub, refs["h07_wheel_torque"]) == [
        "reference_not_validated:h07_wheel_torque:h07_na_baseline"
    ]


def test_the_manifold_is_built_and_converged_but_not_validated(kit: Any) -> None:
    contract, _, _ = kit
    st = standings(contract, PROFILE, convergence=_passing_stub(contract))
    manifold = st["manifold_breathing"]
    assert manifold.built and manifold.numerically_verified and not manifold.validated
    assert "requires_measurement:map_sensor" in manifold.reasons
    assert "no_passing_validation:manifold_breathing" in manifold.reasons
    # The fuel stage reads its own recorded held-out verdicts (E07.3: fail).
    assert not st["fuel_delivery"].validated
    assert any(r.startswith("plant_not_qualified") for r in st["fuel_delivery"].reasons)
    assert not any(s.validated for s in st.values())
    assert "manifold_breathing" in rollout_blockers(contract, st)


def test_only_a_pass_against_an_available_independent_reference_validates() -> None:
    raw = _raw()
    study = _passing_stub(SimContract.model_validate(_raw()))
    sub = _sub(raw, "manifold_breathing")
    sub["validations"] = [
        {
            "reference": "bfs_air_charge",
            "recordings": ["_capture-x-2026-11-01T120000"],
            "verdict": "pass",
            "scored_on": "2026-11-01",
            "evidence": "agrees with the ECU's own filter",
        }
    ]
    contract = SimContract.model_validate(raw)
    st = subsystem_standing(
        contract, contract.subsystem("manifold_breathing"), profile_dir=PROFILE, convergence=study
    )
    assert not st.validated  # a circular reference cannot validate
    for ref in sub["references"]:
        if ref["id"] == "map_sensor":
            ref["status"], ref["requires"] = "available", None
    for m in sub["required_measurements"]:
        m["status"] = "available"
    sub["validations"][0]["reference"] = "map_sensor"
    contract = SimContract.model_validate(raw)
    st = subsystem_standing(
        contract, contract.subsystem("manifold_breathing"), profile_dir=PROFILE, convergence=study
    )
    assert st.validated and st.reasons == ()
    sub["validations"][0]["verdict"] = "fail"
    contract = SimContract.model_validate(raw)
    st = subsystem_standing(
        contract, contract.subsystem("manifold_breathing"), profile_dir=PROFILE, convergence=study
    )
    assert not st.validated


# -- claims ------------------------------------------------------------------------------


def test_prescribed_rpm_is_never_an_acceleration_prediction(kit: Any) -> None:
    contract, _, _ = kit
    every_stage_validated = _all_validated(contract)
    for mode in ("plant_replay", "conditional_rom_comparison"):
        reasons = claim_refusals(contract, every_stage_validated, mode, "acceleration")
        assert reasons and reasons[0].startswith("prescribed_rpm_not_acceleration"), mode
        with pytest.raises(SimulationRefusalError, match="prescribed_rpm_not_acceleration"):
            require_claim(contract, every_stage_validated, mode, "acceleration")
    # A rollout computes speed: it may claim acceleration only once every
    # stage upstream of speed is validated.
    assert claim_refusals(contract, every_stage_validated, "coupled_rollout", "acceleration") == []
    unvalidated = {
        k: dataclasses.replace(v, validated=False) for k, v in every_stage_validated.items()
    }
    reasons = claim_refusals(contract, unvalidated, "coupled_rollout", "acceleration")
    assert "subsystem_not_validated:rotational_dynamics" in reasons
    assert "subsystem_not_validated:manifold_breathing" in reasons


def test_spark_edits_cannot_produce_qualified_power_or_knock(kit: Any) -> None:
    contract, _, _ = kit
    ok = _all_validated(contract)
    for claim in ("power", "torque"):
        reasons = claim_refusals(contract, ok, "coupled_rollout", claim, changed_controls=["spark"])
        assert reasons and reasons[0].startswith("spark_response_unmeasured"), claim
        assert "controlled_spark_sweep" in reasons[0]
    assert claim_refusals(contract, ok, "coupled_rollout", "knock")[0].startswith(
        "knock_unmodelled"
    )
    assert claim_refusals(contract, ok, "coupled_rollout", "knock", changed_controls=["spark"])[
        0
    ].startswith("knock_unmodelled")
    # In a rollout speed is computed, so a torque claim also rests on the
    # inertia stage; where rpm is prescribed torque is not computed at all.
    reasons = claim_refusals(
        contract,
        {
            **ok,
            "rotational_dynamics": dataclasses.replace(ok["rotational_dynamics"], validated=False),
        },
        "coupled_rollout",
        "torque",
    )
    assert reasons == ["subsystem_not_validated:rotational_dynamics"]
    assert claim_refusals(contract, ok, "plant_replay", "torque")[0].startswith(
        "engine_torque_not_computed"
    )
    # With a controlled sweep available, the spark refusal goes; knock does not.
    raw = _raw()
    for m in _sub(raw, "torque_production")["required_measurements"]:
        if m["id"] == "controlled_spark_sweep":
            m["status"] = "available"
    swept = SimContract.model_validate(raw)
    assert claim_refusals(swept, ok, "coupled_rollout", "power", changed_controls=["spark"]) == []
    assert claim_refusals(swept, ok, "coupled_rollout", "knock", changed_controls=["spark"])
    with pytest.raises(SimulationRefusalError, match="claim_undeclared"):
        claim_refusals(contract, ok, "coupled_rollout", "economy")


# -- the corpus bound ------------------------------------------------------------------------


def test_the_breathing_bound_counts_where_the_shared_ve_is_contradicted(kit: Any) -> None:
    _, _, fuel = kit
    rpm = np.array([800.0, 2500.0, 4500.0, 4500.0, 300.0])
    fill = 80.0 * 1000 / (287.05 * 303.15) * fuel.geometry.displacement_l * rpm / 120.0
    cf_wanted = np.array([0.15, 0.5, 0.85, 1.2, 0.2])
    frame = pd.DataFrame(
        {
            "maf_gps": cf_wanted * fill,
            "rpm": rpm,
            "iat_c": 30.0,
            "baro_kpa": 80.0,
            "coolant_c": [90.0, 90.0, 90.0, 90.0, 90.0],
            "session": "_sim-test",
        }
    )
    frame["charge_fraction"] = charge_fraction(
        frame["maf_gps"], frame["rpm"], frame["iat_c"], frame["baro_kpa"], fuel.geometry
    )
    assert np.allclose(frame["charge_fraction"], cf_wanted)
    out = breathing_bound(frame, fuel)
    assert out["states"]["eligible"] == 4  # 300 rpm is outside the envelope
    assert out["all"]["above_working_ve"] == 2
    assert out["all"]["above_one"] == 1
    assert out["by_rpm_band"]["4000-5000"]["n"] == 2
