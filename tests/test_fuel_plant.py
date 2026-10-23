"""E07.3: the first fuel-response plant (mean-value fuel and mixture).

Unit and conservation checks and limiting cases on the physics; the refusals
that keep a fit honest (a lumped log fit split into "measured" parts, the
target fed in as an input, an unfrozen baseline, a leaked drive, a refitted
delay); and the held-out evaluation on a committed slice of a held-out drive,
pinned at what it measured — including that it misses the predeclared bounds.
"""

from __future__ import annotations

import copy
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from src.analysis.fuel_plant import (
    KELVIN,
    MAP_ABOVE_AMBIENT,
    MAP_OUTSIDE_VE_FIT,
    PLANT_FILE,
    R_AIR,
    B06Rules,
    FuelPlantProfile,
    FuelPlantRefusalError,
    Geometry,
    ParameterRegistry,
    PlantInputs,
    SpentRecord,
    air_per_event_g,
    bootstrap_median,
    check_against_contract,
    delivered_lambda,
    effective_open_ms,
    flow_factor,
    injector_delta_p_kpa,
    load_b06,
    load_fuel_plant,
    manifold_pressure_speed_density,
    mass_balance_residual,
    plant_files,
    plant_qualification,
    predict,
    resolve_parameters,
)
from src.analysis.fuel_plant_eval import (
    build_samples,
    check_drive_independence,
    compare_versions,
    evaluate,
    identify,
    identify_ve,
    plant_inputs,
)
from src.analysis.sim_contract import COMPUTED, leak_channels, load_contract
from src.yaml_load import load_yaml

PROFILE = Path("data/nissan-xterra-2008")
FIXTURE = Path("tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926")
ROOT = FIXTURE.parent
GEOM = Geometry(cylinders=6, displacement_l=3.954, strokes=4, banks=2)


@pytest.fixture(scope="module")
def plant() -> tuple[FuelPlantProfile, B06Rules]:
    profile, _ = load_fuel_plant(PROFILE)
    return profile, load_b06(PROFILE, profile.delay)


@pytest.fixture(scope="module")
def fixture_samples(plant: tuple[FuelPlantProfile, B06Rules]) -> pd.DataFrame:
    profile, delay = plant
    return build_samples(FIXTURE, profile, delay)


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = load_yaml((PROFILE / PLANT_FILE).read_text())
    return raw


def _inputs(n: int = 1, **over: float) -> PlantInputs:
    base = dict(
        pulse_ms=5.0,
        ecu_dead_time_ms=0.5,
        rpm=3000.0,
        maf_gps=60.0,
        baro_kpa=81.0,
        iat_c=25.0,
        rail_kpa=350.0,
    )
    base.update(over)
    return PlantInputs(**{k: np.full(n, v, dtype=float) for k, v in base.items()})  # type: ignore[arg-type]


# -- profile, contract -------------------------------------------------------------


def test_the_profile_agrees_with_the_e07_1_contract(
    plant: tuple[FuelPlantProfile, B06Rules],
) -> None:
    profile, _ = plant
    contract, _ = load_contract(PROFILE)
    (config,) = [
        h for h in contract.hardware_configurations if h.id == profile.hardware_configuration
    ]
    diffs = check_against_contract(
        profile, {p.id: p.status for p in config.parameters}, contract.plant_version
    )
    # Every contract parameter is declared with the same status, and the
    # contract names this plant's version.
    assert diffs == []


def test_the_plant_reads_no_channel_its_replay_mode_computes_and_scores_a_real_one(
    plant: tuple[FuelPlantProfile, B06Rules],
) -> None:
    profile, _ = plant
    contract, _ = load_contract(PROFILE)
    (mode,) = [m for m in contract.modes if m.kind == "plant_replay"]
    by_id = {q.id: q for q in contract.quantities}
    computed = {q for q, a in mode.roles.items() if a.role in COMPUTED}
    leaks = leak_channels(by_id, computed)
    s = profile.specs
    inputs = [
        s.pulse,
        s.rpm,
        s.maf,
        s.baro,
        s.intake_temp,
        s.coolant,
        s.battery_voltage,
        s.rail_pressure,
    ]
    assert [c for c in inputs if c in leaks] == []
    scored = [q for q in computed if s.lambda_observed in by_id[q].observed_as]
    assert scored == ["lambda_sensor_b1"]  # observed_as, never a leak-only alias


def test_feeding_the_target_to_the_plant_is_refused() -> None:
    raw = _raw()
    raw["specs"]["lambda_observed"] = "mem:ECM_TARGET_EQUIV_RATIO"
    with pytest.raises(ValidationError, match="target_as_input"):
        FuelPlantProfile.model_validate(raw)


def test_the_reference_phi_cannot_move_a_prediction(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame
) -> None:
    profile, delay = plant
    params = resolve_parameters(profile, delay)
    a = predict(plant_inputs(fixture_samples), params, profile.geometry).lambda_delivered
    moved = fixture_samples.assign(reference_phi=fixture_samples["reference_phi"] * 1.3)
    b = predict(plant_inputs(moved), params, profile.geometry).lambda_delivered
    np.testing.assert_array_equal(a, b)


# -- registry refusals --------------------------------------------------------------


def test_a_lumped_fit_cannot_be_split_into_independently_measured_parts() -> None:
    raw = _raw()
    params = copy.deepcopy(raw["registry"]["parameters"])
    for p in params:
        if p["id"] == "injector_static_flow":
            p.update(status="identifiable", source="log_fit")
    with pytest.raises(ValidationError, match="MAF bias would be absorbed into injector flow"):
        ParameterRegistry.model_validate({"parameters": params})


def test_a_component_may_be_derived_only_when_every_other_one_is_known() -> None:
    raw = _raw()
    params = copy.deepcopy(raw["registry"]["parameters"])
    for p in params:
        if p["id"] == "injector_static_flow":
            p.update(status="identifiable", source="log_fit")
        if p["id"] in ("fuel_stoich_afr", "maf_transfer_bias", "lambda_sensor_gain"):
            p.update(status="known", source="measurement", value=1.0)
            p.pop("working_assumption", None)
    ParameterRegistry.model_validate({"parameters": params})


def test_an_unmeasured_parameter_carries_no_number() -> None:
    raw = _raw()
    params = copy.deepcopy(raw["registry"]["parameters"])
    for p in params:
        if p["id"] == "maf_transfer_bias":
            p["value"] = 0.0
    with pytest.raises(ValidationError, match="requires_measurement carries no value"):
        ParameterRegistry.model_validate({"parameters": params})


def test_a_missing_plant_parameter_is_refused_by_name() -> None:
    raw = _raw()
    params = [p for p in raw["registry"]["parameters"] if p["id"] != "manifold_pressure"]
    with pytest.raises(ValidationError, match="manifold_pressure"):
        ParameterRegistry.model_validate({"parameters": params})


def test_nothing_evaluates_without_a_frozen_baseline(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame
) -> None:
    profile, delay = plant
    unfrozen = profile.model_copy(
        update={"baseline": profile.baseline.model_copy(update={"frozen": False})}  # type: ignore[union-attr]
    )
    with pytest.raises(FuelPlantRefusalError) as err:
        resolve_parameters(unfrozen, delay)
    assert err.value.reason == "baseline_not_frozen"
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([fixture_samples], unfrozen, delay, sessions_root=ROOT, profile_dir=PROFILE)
    assert err.value.reason == "baseline_not_frozen"


def test_identification_and_evaluation_stay_on_their_own_drives(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame
) -> None:
    profile, delay = plant
    with pytest.raises(FuelPlantRefusalError) as err:
        identify([fixture_samples], profile, delay, sessions_root=ROOT)  # a held-out drive
    assert err.value.reason == "not_identification"
    fitted = fixture_samples.assign(session=profile.split.identification[0])
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([fitted], profile, delay, sessions_root=ROOT, profile_dir=PROFILE)
    assert err.value.reason == "not_held_out"
    raw = _raw()
    raw["split"]["held_out"].append(raw["baseline"]["fitted_on"][0])
    raw["split"]["identification"].remove(raw["baseline"]["fitted_on"][0])
    with pytest.raises(ValidationError, match="fitted on held-out"):
        FuelPlantProfile.model_validate(raw)


def test_a_recording_that_informed_b06_is_not_evidence(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame
) -> None:
    profile, b06 = plant
    # Any regime's informed_by, not only the delay's fitted_on.
    assert set(b06.fitted_on) < set(b06.informed_by)
    leaky = replace(b06, informed_by=(*b06.informed_by, FIXTURE.name))
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([fixture_samples], profile, leaky, sessions_root=ROOT, profile_dir=PROFILE)
    assert err.value.reason == "leaked_b06"


def test_the_steady_rule_takes_b06s_band_and_holdoffs_from_its_schema(
    plant: tuple[FuelPlantProfile, B06Rules],
) -> None:
    _, b06 = plant
    assert (b06.closed_loop_band, b06.holdoff_closed_loop_s, b06.holdoff_enrichment_s) == (
        0.01,
        15.0,
        6.0,
    )
    raw = _raw()
    raw["steady"]["closed_loop_band"] = 0.01
    with pytest.raises(ValidationError, match="closed_loop_band"):
        FuelPlantProfile.model_validate(raw)


def test_a_drive_on_both_sides_of_the_split_is_refused(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame, tmp_path: Path
) -> None:
    profile, b06 = plant
    # The same activity recorded under two directory names is one drive.
    twin = "_capture-tuning-2026-09-23T143000"
    for name in (FIXTURE.name, twin):
        (tmp_path / name).mkdir()
        shutil.copy(FIXTURE / "telemetry-00000.parquet", tmp_path / name)
    split = profile.split.model_copy(
        update={"identification": [*profile.split.identification, twin]}
    )
    leaky = profile.model_copy(update={"split": split})
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([fixture_samples], leaky, b06, sessions_root=tmp_path, profile_dir=PROFILE)
    assert err.value.reason == "leaked_drive"
    with pytest.raises(FuelPlantRefusalError) as err:
        check_drive_independence(leaky, tmp_path)
    assert err.value.reason == "leaked_drive"
    # The declared split itself is seven separate drives.
    drives = check_drive_independence(profile, tmp_path)
    assert len(drives) == 7


def test_a_spent_held_out_drive_cannot_score_another_baseline(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame
) -> None:
    profile, b06 = plant
    spent = profile.spent()
    assert set(spent) == set(profile.split.held_out)
    assert {r.baseline_sha256 for r in spent.values()} == {
        "6aec1db556989cc693dcf226a3017478c58a19c6c54abf251d9fe9a916e09220"
    }
    assert profile.baseline is not None
    assert profile.baseline.sha256() == spent[FIXTURE.name].baseline_sha256
    # Scoring again under the same baseline is allowed, and names what to record.
    result = evaluate([fixture_samples], profile, b06, sessions_root=ROOT, profile_dir=PROFILE)
    assert result["spent_records"] == [
        {
            "recording": FIXTURE.name,
            "baseline_id": profile.baseline.id,
            "baseline_sha256": profile.baseline.sha256(),
            "plant_version": "fuel-mv-1",
            "verdict": "fail",
        }
    ]
    # A revised baseline cannot reuse them: refused at load...
    raw = _raw()
    raw["baseline"]["values"]["stoich_air_per_open_ms"] = 0.0505
    raw["baseline"]["intervals"]["stoich_air_per_open_ms"] = [0.05, 0.051]
    with pytest.raises(ValidationError, match="never-scored held-out drives"):
        FuelPlantProfile.model_validate(raw)
    raw = _raw()
    raw["plant_version"] = raw["baseline"]["plant_version"] = "fuel-mv-2"
    with pytest.raises(ValidationError, match="spent_held_out"):
        FuelPlantProfile.model_validate(raw)
    # ...and by evaluate itself, for a profile built without the validator.
    revised = profile.model_copy(
        update={"baseline": profile.baseline.model_copy(update={"id": "na-stock-revised"})}
    )
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([fixture_samples], revised, b06, sessions_root=ROOT, profile_dir=PROFILE)
    assert err.value.reason == "spent_held_out"


def test_the_lambda_delay_is_b06s_frozen_one_never_refitted(
    plant: tuple[FuelPlantProfile, B06Rules], tmp_path: Path
) -> None:
    profile, delay = plant
    assert (delay.seconds, delay.bracket) == (0.1, (0.0, 0.5))
    with pytest.raises(FuelPlantRefusalError) as err:
        resolve_parameters(profile, replace(delay, seconds=0.25))
    assert err.value.reason == "delay_mismatch"
    declared = load_yaml((PROFILE / profile.delay.file).read_text())
    declared["eligibility"]["regimes"]["enrichment"]["transport_delays"]["ram_final_target"] = {
        "status": "not_estimated",
        "seconds": None,
        "note": "a test: not frozen",
    }
    (tmp_path / profile.delay.file).write_text(yaml.safe_dump(declared))
    with pytest.raises(FuelPlantRefusalError) as err:
        load_b06(tmp_path, profile.delay)
    assert err.value.reason == "delay_not_frozen"


def test_a_sensitivity_override_stays_inside_its_bracket(
    plant: tuple[FuelPlantProfile, B06Rules],
) -> None:
    profile, delay = plant
    resolve_parameters(profile, delay, overrides={"manifold_pressure": 1.0})
    for bad in ({"manifold_pressure": 1.2}, {"stoich_air_per_open_ms": 0.06}, {"nope": 1.0}):
        with pytest.raises(FuelPlantRefusalError):
            resolve_parameters(profile, delay, overrides=bad)


# -- physics: units, conservation, limiting cases ------------------------------------


def test_air_per_cylinder_event_has_the_right_units() -> None:
    # 6 g/s at 600 rpm, 6 cylinders, 4-stroke: 30 events/s -> 0.2 g each.
    assert air_per_event_g([6.0], [600.0], GEOM)[0] == pytest.approx(0.2)
    # A stopped engine has no event, not an infinite charge.
    assert np.isnan(air_per_event_g([6.0], [0.0], GEOM)[0])
    # Full charge at 81 kPa, 25 degC in 0.659 L is ~0.62 g; speed density
    # inverts it and never exceeds baro.
    full = 81000 * (3.954 / 6 / 1000) / (287.05 * 298.15) * 1000
    assert manifold_pressure_speed_density([full], [25.0], [81.0], GEOM, 1.0)[0] == pytest.approx(
        81.0, rel=1e-9
    )
    assert manifold_pressure_speed_density([2 * full], [25.0], [81.0], GEOM, 1.0)[0] == 81.0


def test_mass_is_conserved_and_stoichiometry_only_moves_fuel_grams(
    plant: tuple[FuelPlantProfile, B06Rules],
) -> None:
    profile, delay = plant
    params = resolve_parameters(profile, delay)
    out = predict(_inputs(5), params, profile.geometry)
    exhaust = out.air_g + out.fuel_g
    np.testing.assert_allclose(mass_balance_residual(out.air_g, out.fuel_g, exhaust), 0.0)
    np.testing.assert_allclose(
        out.fuel_g * params.values["fuel_stoich_afr"], out.stoich_air_equiv_g
    )
    e0 = resolve_parameters(profile, delay, overrides={"fuel_stoich_afr": 14.7})
    out0 = predict(_inputs(5), e0, profile.geometry)
    np.testing.assert_allclose(out0.lambda_delivered, out.lambda_delivered)
    assert (out0.fuel_g < out.fuel_g).all()


def test_limiting_cases(plant: tuple[FuelPlantProfile, B06Rules]) -> None:
    profile, delay = plant
    params = resolve_parameters(profile, delay)
    g = profile.geometry
    base = predict(_inputs(), params, g).lambda_delivered[0]
    # Pulse equal to the dead time meters nothing: no fuel, no lambda.
    cut = predict(_inputs(pulse_ms=0.5), params, g)
    assert cut.fuel_g[0] == 0.0 and np.isnan(cut.lambda_delivered[0])
    assert effective_open_ms([0.3], [0.5])[0] == 0.0  # never negative
    # Twice the effective open time halves lambda; twice the air doubles it.
    longer = predict(_inputs(pulse_ms=0.5 + 2 * 4.5), params, g).lambda_delivered[0]
    assert longer == pytest.approx(base / 2)
    more_air = predict(_inputs(maf_gps=120.0, rpm=3000.0), params, g).lambda_delivered[0]
    # (MAP moves with the charge, and dP with it: compare at no vacuum.)
    assert more_air / base == pytest.approx(
        2.0
        * flow_factor(
            injector_delta_p_kpa(
                350,
                81,
                manifold_pressure_speed_density(
                    air_per_event_g([60.0], [3000.0], g), [25.0], [81.0], g, 0.7
                ),
            ),
            350,
            0.5,
        )[0]
        / flow_factor(
            injector_delta_p_kpa(
                350,
                81,
                manifold_pressure_speed_density(
                    air_per_event_g([120.0], [3000.0], g), [25.0], [81.0], g, 0.7
                ),
            ),
            350,
            0.5,
        )[0]
    )
    # At the reference dP the flow factor is exactly 1; vacuum raises flow.
    assert flow_factor([350.0], 350.0, 0.5)[0] == 1.0
    assert flow_factor([350.0 + 60.0], 350.0, 0.5)[0] > 1.0
    assert np.isnan(flow_factor([0.0], 350.0, 0.5)[0])
    assert np.isnan(delivered_lambda([1.0], [0.0])[0])
    # A measured rail droop delivers less fuel, so leaner, by sqrt(dP).
    droop = predict(_inputs(rail_kpa=330.0), params, g)
    assert droop.lambda_delivered[0] > base
    unmeasured = predict(_inputs(rail_kpa=float("nan")), params, g)
    assert not unmeasured.rail_measured[0] and unmeasured.rail_effect == "rail_pressure_unmeasured"


def test_bootstrap_resamples_regions_and_is_deterministic() -> None:
    groups = [np.array([1.0, 1.0, 1.0, 9.0]), np.array([2.0]), np.array([3.0, 3.0])]
    a = bootstrap_median(groups, resamples=500, level=0.9, seed=1)
    assert a == bootstrap_median(groups, resamples=500, level=0.9, seed=1)
    assert a[0] == 2.0  # median of region medians, not of the pooled samples
    assert a[1] <= a[0] <= a[2]


def test_identification_recovers_a_known_scale(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame
) -> None:
    profile, delay = plant
    params = resolve_parameters(profile, delay, for_identification=True)
    frame = fixture_samples[fixture_samples["state"] == "eligible"].copy()
    unit = predict(plant_inputs(frame), params, profile.geometry).lambda_delivered
    frame["lambda_obs"] = unit / 0.05  # a synthetic plant whose scale is 0.05 g/ms
    frame["session"] = profile.split.identification[0]
    frame["region_id"] = frame["session"] + "#" + frame["region"].astype(str)
    out = identify([frame], profile, delay, sessions_root=ROOT)
    assert out["value"] == pytest.approx(0.05)
    assert out["interval"][0] == pytest.approx(0.05) and out["interval"][1] == pytest.approx(0.05)


# -- held-out evaluation on a committed slice ---------------------------------------


def test_every_pulse_read_names_its_state(fixture_samples: pd.DataFrame) -> None:
    from src.analysis.fuel_plant_eval import STATES

    assert set(fixture_samples["state"]) <= set(STATES)
    assert fixture_samples["state"].value_counts().to_dict() == {
        "transient": 859,
        "post_fuel_cut": 462,
        "eligible": 152,
        "fuel_cut": 106,
        "lambda_rail": 16,
        "missing_input": 1,
    }
    eligible = fixture_samples[fixture_samples["state"] == "eligible"]
    assert eligible["region"].nunique() == 38
    assert (eligible["coolant_c"] >= 70).all()


def test_the_held_out_slice_is_scored_against_the_predeclared_bounds(
    plant: tuple[FuelPlantProfile, B06Rules], fixture_samples: pd.DataFrame
) -> None:
    """Measured, not hoped: the frozen plant misses two of the bounds here, as
    it does on the full held-out drives (sample |error| p90 and region pass
    fraction, both from short idle pulses; see the E07.3 notes)."""
    profile, delay = plant
    result = evaluate([fixture_samples], profile, delay, sessions_root=ROOT, profile_dir=PROFILE)
    lam = result["lambda"]
    assert (lam["n_samples"], lam["n_regions"]) == (152, 38)
    assert lam["median_pct"] == pytest.approx(1.90, abs=0.01)
    assert lam["abs_p90_pct"] == pytest.approx(8.90, abs=0.01)
    lo, hi = lam["region_interval_pct"]
    assert lo < lam["region_median_of_medians_pct"] < hi
    checks = result["checks"]
    assert checks["pooled_median_abs_pct"]["pass"] is True
    assert checks["sample_abs_p90_pct"]["pass"] is False
    assert checks["region_pass_fraction"]["pass"] is False
    # One enrichment region on the slice: too little to judge, said by name.
    assert checks["enrichment_median_abs_pct"]["pass"] is None
    assert result["verdict"] == "fail"
    # No rail pressure on this drive: the effect is named, never silently nominal.
    assert result["coverage"]["rail_measured_fraction"] == 0.0
    assert "rail_pressure_unmeasured" in result["unresolved_effects"]
    assert {"injector_latency_unmeasured", "manifold_pressure_unmeasured"} <= set(
        result["unresolved_effects"]
    )
    # Fuel error is the same information inverted, never a second test.
    assert result["fuel"]["median_pct"] == pytest.approx(
        100 * (1 / (1 + lam["median_pct"] / 100) - 1), abs=0.2
    )
    assert set(result["sensitivity_median_shift_pct"]) >= {
        "lambda_observation_delay=lo",
        "lambda_observation_delay=hi",
        "rail_pressure=lo",
        "injector_dead_time=hi",
    }


# -- fuel-mv-2: VE(rpm) and a MAP that refuses instead of capping (2026-10-08) --------

MV2 = "fuel-mv-2"
MV2_FILE = "fuel_plant_mv2.yaml"


@pytest.fixture(scope="module")
def plant_mv2() -> FuelPlantProfile:
    profile, _ = load_fuel_plant(PROFILE, MV2)
    return profile


def _maf_at_charge_fraction(cf: float, rpm: float, baro: float = 81.0, iat: float = 25.0) -> float:
    """The MAF that fills the displacement to ``cf`` of the ambient fill."""
    return cf * baro * 1000 / (R_AIR * (iat + 273.15)) * 3.954 * rpm / 120


def test_fuel_mv_1_stays_frozen_and_fuel_mv_2_is_declared_beside_it(
    plant: tuple[FuelPlantProfile, B06Rules], plant_mv2: FuelPlantProfile
) -> None:
    mv1, _ = plant
    assert {v: f.name for v, f in plant_files(PROFILE).items()} == {
        "fuel-mv-1": PLANT_FILE,
        MV2: MV2_FILE,
        "fuel-mv-3": "fuel_plant_mv3.yaml",
    }
    # fuel-mv-1: the same baseline hash its spent records were scored under.
    assert mv1.plant_version == "fuel-mv-1" and mv1.manifold_model == "speed_density_capped"
    assert mv1.volumetric_efficiency is None and mv1.baseline is not None
    assert mv1.baseline.ve_curve is None
    assert mv1.baseline.sha256() == (
        "6aec1db556989cc693dcf226a3017478c58a19c6c54abf251d9fe9a916e09220"
    )
    # fuel-mv-2: its own frozen baseline (hash pinned: a re-fit is a new
    # baseline, never an edit), identified on the same drives.
    mv2 = plant_mv2
    assert mv2.manifold_model == "speed_density_refusing" and mv2.volumetric_efficiency
    assert mv2.baseline is not None and mv2.baseline.frozen and mv2.baseline.ve_curve
    assert mv2.baseline.sha256() == (
        "0180c207492beed438df3b7dbe25e6061eeb9490d3d4e38741829033acf21576"
    )
    assert mv2.baseline.fitted_on == mv1.baseline.fitted_on == mv2.split.identification
    assert mv2.baseline.ve_curve.rpm_edges == mv2.volumetric_efficiency.rpm_edges
    # No never-scored drive exists: none held out, so it is never qualified.
    assert mv2.split.held_out == [] and mv2.split.held_out_pending
    q = plant_qualification(mv2)
    assert not q.qualified and q.reason is not None and "no held-out recording" in q.reason
    with pytest.raises(FuelPlantRefusalError) as err:
        load_fuel_plant(PROFILE, "fuel-mv-9")
    assert err.value.reason == "plant_version_undeclared"


def test_the_revised_map_refuses_above_ambient_where_fuel_mv_1_caps(
    plant: tuple[FuelPlantProfile, B06Rules], plant_mv2: FuelPlantProfile
) -> None:
    mv1, delay = plant
    p1 = resolve_parameters(mv1, delay)
    p2 = resolve_parameters(plant_mv2, delay)
    curve = plant_mv2.baseline.ve_curve if plant_mv2.baseline else None
    assert curve is not None
    ve_4500 = float(curve.ve([4500.0], 0.7, fitted_at=0.7)[0])
    assert ve_4500 == pytest.approx(0.905535, abs=1e-6)
    # Charge fraction 0.8 at 4500 rpm: above VE 0.7, below VE(4500).
    part = _inputs(rpm=4500.0, maf_gps=_maf_at_charge_fraction(0.8, 4500.0))
    capped = predict(part, p1, GEOM)
    assert capped.map_kpa[0] == 81.0  # fuel-mv-1 caps, silently
    revised = predict(part, p2, GEOM)
    assert revised.manifold_refusal is not None and revised.manifold_refusal[0] == ""
    assert revised.map_kpa[0] == pytest.approx(81.0 * 0.8 / ve_4500, rel=1e-9)
    assert np.isfinite(revised.lambda_delivered[0])
    # Charge fraction 0.95: above VE(4500) too. Refused and named, never baro.
    over = _inputs(rpm=4500.0, maf_gps=_maf_at_charge_fraction(0.95, 4500.0))
    assert predict(over, p1, GEOM).map_kpa[0] == 81.0
    refused = predict(over, p2, GEOM)
    assert np.isnan(refused.map_kpa[0]) and np.isnan(refused.lambda_delivered[0])
    assert refused.manifold_refusal is not None
    assert refused.manifold_refusal[0] == MAP_ABOVE_AMBIENT
    # Outside the fit (rpm beyond the bands, or an altitude it never saw).
    for case in (
        _inputs(rpm=7000.0, maf_gps=50.0),
        _inputs(rpm=3000.0, maf_gps=30.0, baro_kpa=65.0),
        _inputs(rpm=3000.0, maf_gps=30.0, iat_c=21.0),  # a cold morning: IAT is bounded too
    ):
        out = predict(case, p2, GEOM)
        assert out.manifold_refusal is not None
        assert out.manifold_refusal[0] == MAP_OUTSIDE_VE_FIT and np.isnan(out.map_kpa[0])
    # Idle (a band the drives never filled above 0.7) runs at the 0.7 floor:
    # fuel-mv-1's MAP, to rounding.
    idle = _inputs(rpm=700.0, maf_gps=_maf_at_charge_fraction(0.15, 700.0), pulse_ms=2.5)
    assert predict(idle, p2, GEOM).map_kpa[0] == pytest.approx(
        predict(idle, p1, GEOM).map_kpa[0], rel=1e-12
    )


def test_ve_is_fitted_on_identification_drives_only_as_a_lower_envelope(
    plant_mv2: FuelPlantProfile,
) -> None:
    """VE >= charge fraction on every naturally aspirated read: the fit is the
    largest steady warm charge fraction per band; cold, unsteady and sparse
    bands do not count, and a held-out drive cannot be fitted on."""
    ident = plant_mv2.split.identification[0]
    rows = []
    for rpm, cf, steady, coolant in [
        (2500.0, 0.85, True, 90.0),
        (2600.0, 0.88, True, 90.0),
        (2700.0, 0.97, False, 90.0),  # a tip-in: MAF filling the manifold
        (2800.0, 0.95, True, 40.0),  # cold
        *[(4500.0, 0.90, True, 90.0)] * 2,  # too few reads for the band
        *[(800.0, 0.2, True, 90.0)] * 5,
        *[(2400.0, 0.5, True, 90.0)] * 3,
    ]:
        rows.append(
            dict(
                session=ident,
                rpm=rpm,
                charge_fraction=cf,
                steady=steady,
                coolant_c=coolant,
                baro_kpa=81.0,
                iat_c=25.0,
            )
        )
    samples = pd.DataFrame(rows)
    out = identify_ve(samples, plant_mv2)
    curve = out["ve_curve"]
    assert curve.envelope[2] == pytest.approx(0.88)  # 2000-3000: the steady warm max
    assert curve.envelope[0] == pytest.approx(0.2)
    assert curve.envelope[4] is None  # 2 reads < min_reads
    assert float(curve.ve([2500.0], 0.7, fitted_at=0.7)[0]) == pytest.approx(0.88)
    assert float(curve.ve([800.0], 0.7, fitted_at=0.7)[0]) == 0.7  # never filled: the floor
    assert float(curve.ve([4500.0], 0.7, fitted_at=0.7)[0]) == 0.7
    assert curve.baro_kpa == (81.0, 81.0)
    held = samples.assign(session="_capture-tuning-2026-09-22T170459")
    with pytest.raises(FuelPlantRefusalError) as err:
        identify_ve(held, plant_mv2)
    assert err.value.reason == "not_identification"
    broken = pd.concat([samples, samples.iloc[:1].assign(charge_fraction=1.2)])
    with pytest.raises(FuelPlantRefusalError) as err:
        identify_ve(broken, plant_mv2)
    assert err.value.reason == "charge_fraction_above_one"


def test_the_ve_curve_belongs_to_a_refusing_plant_only() -> None:
    raw: dict[str, Any] = load_yaml((PROFILE / MV2_FILE).read_text())
    capped = copy.deepcopy(raw)
    capped["manifold_model"] = "speed_density_capped"
    with pytest.raises(ValidationError, match="volumetric_efficiency"):
        FuelPlantProfile.model_validate(capped)
    no_curve = copy.deepcopy(raw)
    del no_curve["baseline"]["ve_curve"]
    with pytest.raises(ValidationError, match="freezes its ve_curve"):
        FuelPlantProfile.model_validate(no_curve)
    mv1 = _raw()
    mv1["baseline"]["ve_curve"] = raw["baseline"]["ve_curve"]
    with pytest.raises(ValidationError, match="no ve_curve"):
        FuelPlantProfile.model_validate(mv1)
    unpending = copy.deepcopy(raw)
    del unpending["split"]["held_out_pending"]
    with pytest.raises(ValidationError, match="held_out_pending"):
        FuelPlantProfile.model_validate(unpending)


def test_fuel_mv_2_cannot_hold_out_a_drive_fuel_mv_1_spent(
    plant_mv2: FuelPlantProfile,
    plant: tuple[FuelPlantProfile, B06Rules],
    fixture_samples: pd.DataFrame,
    tmp_path: Path,
) -> None:
    _, delay = plant
    # Its own ledger is empty, so the sibling's is read at load.
    raw: dict[str, Any] = load_yaml((PROFILE / MV2_FILE).read_text())
    raw["split"]["held_out"] = [FIXTURE.name]
    del raw["split"]["held_out_pending"]
    FuelPlantProfile.model_validate(raw)  # the file alone cannot see it...
    shutil.copy(PROFILE / PLANT_FILE, tmp_path / PLANT_FILE)
    (tmp_path / MV2_FILE).write_text(yaml.safe_dump(raw))
    with pytest.raises(FuelPlantRefusalError) as err:  # ...the profile can
        load_fuel_plant(tmp_path, MV2)
    assert err.value.reason == "spent_held_out"
    # As declared, evaluate refuses by name rather than failing on no frames.
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([fixture_samples], plant_mv2, delay, sessions_root=ROOT, profile_dir=PROFILE)
    assert err.value.reason == "held_out_pending"
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([], plant_mv2, delay, sessions_root=ROOT, profile_dir=PROFILE)
    assert err.value.reason == "held_out_pending"
    # A profile built in memory, past every loader, is still refused by
    # evaluate itself: it reads every plant file's ledger.
    in_memory = FuelPlantProfile.model_validate(raw)
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate([fixture_samples], in_memory, delay, sessions_root=ROOT, profile_dir=PROFILE)
    assert err.value.reason == "spent_held_out"
    # ...and cannot dodge it with a directory that does not declare it.
    with pytest.raises(FuelPlantRefusalError) as err:
        evaluate(
            [fixture_samples], in_memory, delay, sessions_root=ROOT, profile_dir=tmp_path / "x"
        )
    assert err.value.reason == "plant_version_undeclared"


def test_a_plant_without_held_out_drives_never_qualifies(plant_mv2: FuelPlantProfile) -> None:
    """A verdict recorded on a recording outside held_out cannot qualify a
    plant whose held_out is empty (or stand in for its held-out drives)."""
    assert plant_mv2.baseline is not None
    record = {
        "recording": "_capture-tuning-2026-09-21T223734",
        "baseline_id": plant_mv2.baseline.id,
        "baseline_sha256": plant_mv2.baseline.sha256(),
        "plant_version": MV2,
        "scored_on": "2026-10-08",
        "verdict": "pass",
    }
    raw: dict[str, Any] = load_yaml((PROFILE / MV2_FILE).read_text())
    raw["spent_held_out"] = [record]
    with pytest.raises(ValidationError, match="not in split.held_out"):
        FuelPlantProfile.model_validate(raw)
    # Built past the validator, qualification still refuses.
    smuggled = plant_mv2.model_copy(update={"spent_held_out": [SpentRecord.model_validate(record)]})
    q = plant_qualification(smuggled)
    assert not q.qualified and q.reason is not None and "no held-out recording" in q.reason
    failed = plant_mv2.model_copy(
        update={"spent_held_out": [SpentRecord.model_validate(dict(record, verdict="fail"))]}
    )
    assert not plant_qualification(failed).qualified
    # With a held-out drive declared and passed, a stray verdict still refuses.
    declared = smuggled.model_copy(
        update={
            "split": smuggled.split.model_copy(
                update={"held_out": ["_capture-x-2026-11-01T000000"], "held_out_pending": None}
            ),
            "spent_held_out": [
                SpentRecord.model_validate(dict(record, recording="_capture-x-2026-11-01T000000")),
                SpentRecord.model_validate(dict(record, verdict="fail")),
            ],
        }
    )
    q = plant_qualification(declared)
    assert not q.qualified and q.reason is not None and "undeclared recordings" in q.reason


def test_versions_are_compared_per_sample_on_spent_drives_only(
    plant: tuple[FuelPlantProfile, B06Rules],
    plant_mv2: FuelPlantProfile,
    fixture_samples: pd.DataFrame,
) -> None:
    mv1, delay = plant
    out = compare_versions([fixture_samples], [mv1, plant_mv2], delay)
    assert "verdict" not in out and "spent_records" not in out
    assert out["sessions"] == {FIXTURE.name: "spent_held_out"}
    block = out["spent_held_out"]
    n1 = block["versions"]["fuel-mv-1"]["n_samples"]
    assert n1 == block["versions"][MV2]["n_samples"] == out["common_samples"]
    # The slice's 152 eligible reads, less what fuel-mv-2 refuses by name.
    refused = sum(out["refused_by_version"][MV2].values())
    assert out["refused_by_version"]["fuel-mv-1"] == {}
    assert n1 + refused == 152
    change = block[f"abs_error_change_{MV2}_minus_fuel-mv-1_pct"]
    assert change["n"] == n1
    # Per MAF band the change is still per sample; no band medians side by side.
    by_band = block[f"abs_error_change_{MV2}_minus_fuel-mv-1_pct_by_maf_band"]
    assert sum(b["n"] for b in by_band.values()) == n1
    assert "median_error_by_maf_band_pct" not in block
    assert change["fraction_better"] + change["fraction_worse"] <= 1.0
    # A never-scored recording is not looked at.
    fresh = fixture_samples.assign(session="_capture-tuning-2026-10-09T120000")
    with pytest.raises(FuelPlantRefusalError) as err:
        compare_versions([fresh], [mv1, plant_mv2], delay)
    assert err.value.reason == "unspent_recording"
    with pytest.raises(FuelPlantRefusalError) as err:
        compare_versions([fixture_samples], [mv1], delay)
    assert err.value.reason == "one_version"


def test_one_plenum_feeds_both_banks_so_map_reads_the_total_air_at_any_split() -> None:
    """E08.11 (operator, 2026-10-16): the VQ40 has one plenum for both banks,
    so MAP comes from the engine's total air (the mean cylinder charge), and
    the bank split acts on per-bank fuelling and lambda only. Hand-worked at
    100 g/s, 3,000 rpm, 30 C, baro 81 kPa on fuel-mv-1's VE 0.7: 150 intake
    events/s (3,000/60 x 6 cylinders / 2 revolutions), mean charge 0.666667 g;
    bank shares at split 0.52 and 0.48 are 0.693333 g and 0.64 g, summing to
    twice the mean; MAP = m R T / (VE V_cyl) = 0.666667e-3 x 287.05 x 303.15 /
    (0.7 x 0.659e-3) = 125.8 kPa -> capped at 81, so the check runs at 40 g/s
    too (0.266667 g, MAP 50.30 kPa)."""
    profile, _ = load_fuel_plant(PROFILE)
    delay = load_b06(PROFILE, profile.delay)
    geom = profile.geometry
    assert (geom.cylinders, geom.banks) == (6, 2)
    v_cyl_m3 = 3.954 / 6 / 1000.0

    def at(split: float, maf: float) -> Any:
        params = resolve_parameters(profile, delay, overrides={"bank_air_split": split})
        return predict(
            PlantInputs(
                pulse_ms=np.array([4.0]),
                ecu_dead_time_ms=np.array([0.5]),
                rpm=np.array([3000.0]),
                maf_gps=np.array([maf]),
                baro_kpa=np.array([81.0]),
                iat_c=np.array([30.0]),
                rail_kpa=np.array([350.0]),
            ),
            params,
            geom,
        )

    for maf, mean in ((100.0, 100.0 / 150.0), (40.0, 40.0 / 150.0)):
        hi, lo, even = at(0.52, maf), at(0.48, maf), at(0.5, maf)
        # Each bank's cylinder gets split x banks of the mean; the two shares
        # sum to the total (two banks' worth of the mean) at any split.
        assert float(hi.air_g[0]) == pytest.approx(mean * 1.04, rel=1e-12)
        assert float(lo.air_g[0]) == pytest.approx(mean * 0.96, rel=1e-12)
        assert float(hi.air_g[0] + lo.air_g[0]) == pytest.approx(2.0 * mean, rel=1e-12)
        assert float(even.air_g[0]) == pytest.approx(mean, rel=1e-12)
        # MAP is the plenum's: the same at every split, from the mean charge.
        expected = mean / 1000.0 * R_AIR * (30.0 + KELVIN) / (0.7 * v_cyl_m3) / 1000.0
        assert float(even.map_kpa[0]) == pytest.approx(min(expected, 81.0), rel=1e-9)
        assert hi.map_kpa[0] == lo.map_kpa[0] == even.map_kpa[0]
        # The split still moves this bank's lambda, as the bank's own air.
        assert float(hi.lambda_delivered[0]) == pytest.approx(
            float(even.lambda_delivered[0]) * 1.04, rel=1e-12
        )
    assert float(at(0.5, 40.0).map_kpa[0]) == pytest.approx(50.30, abs=0.01)
    # On the refusing plants too: 131 g/s at 4,800 rpm is a charge fraction
    # of ~0.88, MAP ~0.97 x baro at fuel-mv-2's VE 0.9055. A bank's share at
    # split 0.52 (x 1.04) would have read ~1.005 x baro and refused; the plenum
    # sees the total, so the split cannot push MAP above ambient.
    for version in ("fuel-mv-2", "fuel-mv-3"):
        prof, _ = load_fuel_plant(PROFILE, version)
        maps = []
        for split in (0.48, 0.5, 0.52):
            out = predict(
                PlantInputs(
                    pulse_ms=np.array([12.0]),
                    ecu_dead_time_ms=np.array([0.5]),
                    rpm=np.array([4800.0]),
                    maf_gps=np.array([131.0]),
                    baro_kpa=np.array([81.0]),
                    iat_c=np.array([25.0]),
                    rail_kpa=np.array([350.0]),
                ),
                resolve_parameters(prof, delay, overrides={"bank_air_split": split}),
                prof.geometry,
            )
            assert out.manifold_refusal is not None and out.manifold_refusal[0] == ""
            maps.append(float(out.map_kpa[0]))
        assert len(set(maps)) == 1, (version, maps)
