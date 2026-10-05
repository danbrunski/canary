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
    PLANT_FILE,
    B06Rules,
    FuelPlantProfile,
    FuelPlantRefusalError,
    Geometry,
    ParameterRegistry,
    PlantInputs,
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
    predict,
    resolve_parameters,
)
from src.analysis.fuel_plant_eval import (
    build_samples,
    check_drive_independence,
    evaluate,
    identify,
    plant_inputs,
)
from src.analysis.sim_contract import COMPUTED, leak_channels, load_contract

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
    raw: dict[str, Any] = yaml.safe_load((PROFILE / PLANT_FILE).read_text())
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
        evaluate([fixture_samples], unfrozen, delay, sessions_root=ROOT)
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
        evaluate([fitted], profile, delay, sessions_root=ROOT)
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
        evaluate([fixture_samples], profile, leaky, sessions_root=ROOT)
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
        evaluate([fixture_samples], leaky, b06, sessions_root=tmp_path)
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
    result = evaluate([fixture_samples], profile, b06, sessions_root=ROOT)
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
        evaluate([fixture_samples], revised, b06, sessions_root=ROOT)
    assert err.value.reason == "spent_held_out"


def test_the_lambda_delay_is_b06s_frozen_one_never_refitted(
    plant: tuple[FuelPlantProfile, B06Rules], tmp_path: Path
) -> None:
    profile, delay = plant
    assert (delay.seconds, delay.bracket) == (0.1, (0.0, 0.5))
    with pytest.raises(FuelPlantRefusalError) as err:
        resolve_parameters(profile, replace(delay, seconds=0.25))
    assert err.value.reason == "delay_mismatch"
    declared = yaml.safe_load((PROFILE / profile.delay.file).read_text())
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
    result = evaluate([fixture_samples], profile, delay, sessions_root=ROOT)
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
