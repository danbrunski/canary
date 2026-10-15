"""E07.3 remaining 1: fuel-mv-3, the load-dependent VE.

The physics (the ideal-cycle residual-gas factor and the MAP it solves for) is
checked on its limiting cases and its own equation; the profile is checked to
be declared, frozen (identified on the corpus 2026-10-15) and unqualified beside
the two other frozen versions; and the identify -> freeze -> predict path is run
on the committed slice with a synthetic lambda. Nothing here fits the truck: the
frozen baseline is pinned by hash, never recomputed.
"""

from __future__ import annotations

import copy
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from src.analysis.fuel_plant import (
    LOAD_DEPENDENT_READS,
    MAP_ABOVE_AMBIENT,
    MAP_OUTSIDE_VE_FIT,
    R_AIR,
    BaselineParameterSet,
    FuelPlantProfile,
    FuelPlantRefusalError,
    Geometry,
    PlantInputs,
    VeCurve,
    air_per_event_g,
    effective_open_ms,
    flow_factor,
    injector_delta_p_kpa,
    load_b06,
    load_fuel_plant,
    manifold_pressure_load_dependent,
    manifold_pressure_ve_curve,
    part_load_breathing_factor,
    plant_files,
    plant_qualification,
    predict,
    resolve_parameters,
    sensitivity_cases,
)
from src.analysis.fuel_plant_eval import build_samples, identify, plant_inputs

PROFILE = Path("data/nissan-xterra-2008")
MV3 = "fuel-mv-3"
MV3_FILE = "fuel_plant_mv3.yaml"
FIXTURE = Path("tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926")
GEOM = Geometry(cylinders=6, displacement_l=3.954, strokes=4, banks=2)
FORM = dict(compression_ratio=9.7, exhaust_ratio=1.0, exponent=1.3)
#: A VE(rpm) that covers the slice (it is a morning drive below fuel-mv-2's IAT
#: range), for exercising the path only.
CURVE = VeCurve(
    rpm_edges=[500.0, 1000.0, 2000.0, 3000.0, 4000.0, 5000.0, 6500.0],
    envelope=[0.217, 0.677, 0.888, 0.895, 0.906, 0.873],
    n_reads=[10] * 6,
    baro_kpa=(60.0, 105.0),
    iat_c=(-30.0, 70.0),
)


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((PROFILE / MV3_FILE).read_text())
    return raw


def _air_at(cf: float, rpm: float, baro: float = 81.0, iat: float = 30.0) -> float:
    """Air per cylinder event filling ``cf`` of the displacement at ambient."""
    v_m3 = GEOM.cylinder_volume_l / 1000.0
    return cf * baro * 1000.0 * v_m3 / (R_AIR * (iat + 273.15)) * 1000.0


def test_fuel_mv_3_is_frozen_and_unqualified_beside_the_other_versions() -> None:
    assert {v: f.name for v, f in plant_files(PROFILE).items()}[MV3] == MV3_FILE
    mv3, _ = load_fuel_plant(PROFILE, MV3)
    mv2, _ = load_fuel_plant(PROFILE, "fuel-mv-2")
    assert mv3.manifold_model == "speed_density_load_dependent"
    # The full-breathing rule is fuel-mv-2's, unchanged, on the same drives.
    assert mv3.volumetric_efficiency == mv2.volumetric_efficiency
    assert mv3.split.identification == mv2.split.identification
    assert mv3.acceptance.model_dump(exclude={"declared_on"}) == mv2.acceptance.model_dump(
        exclude={"declared_on"}
    )
    assert mv3.acceptance.declared_on == mv3.split.declared_on == date(2026, 10, 13)
    # Identified 2026-10-15 on the corpus (hash pinned: a re-fit is a new
    # baseline, never an edit). Its VE envelope is fuel-mv-2's on the same drives.
    assert mv3.baseline is not None and mv3.baseline.frozen and mv3.baseline.ve_curve
    assert mv3.baseline.sha256() == (
        "8d54217e1f91af57efedab9bba2c323ccf61d5f85245f15dff284ed28677d523"
    )
    assert mv2.baseline is not None
    assert mv3.baseline.fitted_on == mv3.split.identification
    assert mv3.baseline.ve_curve == mv2.baseline.ve_curve
    # No never-scored drive exists: none held out, so it is never qualified.
    assert mv3.spent_held_out == []
    assert mv3.split.held_out == [] and mv3.split.held_out_pending
    q = plant_qualification(mv3)
    assert not q.qualified and q.reason is not None and "no held-out recording" in q.reason
    delay = load_b06(PROFILE, mv3.delay)
    params = resolve_parameters(mv3, delay)
    assert params.values["stoich_air_per_open_ms"] == 0.050994
    assert params.values["compression_ratio"] == 9.7
    assert "exhaust_back_pressure_unmeasured" in params.effects
    # Without its baseline the same declaration refuses to resolve.
    raw = _raw()
    raw.pop("baseline")
    unfrozen = FuelPlantProfile.model_validate(raw)
    assert plant_qualification(unfrozen).reason == "plant_not_qualified: no frozen baseline"
    with pytest.raises(FuelPlantRefusalError) as err:
        resolve_parameters(unfrozen, delay)
    assert err.value.reason == "baseline_not_frozen"


def test_the_residual_gas_factor_and_its_limiting_cases() -> None:
    x = np.linspace(0.05, 1.0, 400)
    f = part_load_breathing_factor(x, **FORM)
    # Normalised at ambient intake pressure: the fitted full-breathing VE holds there.
    assert part_load_breathing_factor([1.0], **FORM)[0] == pytest.approx(1.0, abs=1e-15)
    assert np.all(np.diff(f) > 0) and np.all((f >= 0) & (f <= 1))
    # Zero where the residual alone fills the cylinder, x = p_e / r_c^n.
    x_zero = 1.0 / 9.7**1.3
    assert part_load_breathing_factor([x_zero * 0.999], **FORM)[0] == 0.0
    assert part_load_breathing_factor([x_zero * 1.001], **FORM)[0] > 0.0
    # No clearance volume left to fill (r_c -> infinity): no part-load loss.
    huge = dict(FORM, compression_ratio=1e12)
    assert np.allclose(part_load_breathing_factor(x, **huge), 1.0, atol=1e-9)
    # More back-pressure, less part-load charge; still 1 at ambient.
    back = part_load_breathing_factor(x, **dict(FORM, exhaust_ratio=1.1))
    live = f[:-1] > 0
    assert np.all(back[:-1][live] < f[:-1][live]) and np.all(back <= f + 1e-15)
    assert back[-1] == pytest.approx(1.0)
    with pytest.raises(FuelPlantRefusalError) as err:
        part_load_breathing_factor([0.5], **dict(FORM, exhaust_ratio=1.01 * 9.7**1.3))
    assert err.value.reason == "breathing_form_invalid"


def test_without_residual_gas_it_is_fuel_mv_2s_map() -> None:
    rpm = np.repeat([800.0, 2500.0, 4500.0], 5)
    cf = np.tile([0.1, 0.3, 0.6, 0.85, 0.95], 3)
    air = np.array([_air_at(c, r) for c, r in zip(cf, rpm, strict=True)])
    iat, baro = np.full(15, 30.0), np.full(15, 81.0)
    flat, why_flat = manifold_pressure_ve_curve(air, rpm, iat, baro, GEOM, CURVE, 0.7)
    none, why = manifold_pressure_load_dependent(
        air, rpm, iat, baro, GEOM, CURVE, 0.7, **dict(FORM, compression_ratio=1e12)
    )
    ok = np.isfinite(flat)
    assert np.array_equal(why, why_flat)
    np.testing.assert_allclose(none[ok], flat[ok], rtol=1e-9)


def test_the_map_solves_its_own_equation_and_never_exceeds_ambient() -> None:
    rpm = np.full(60, 2500.0)
    cf = np.linspace(0.05, 0.888, 60)
    air = np.array([_air_at(c, 2500.0) for c in cf])
    iat, baro = np.full(60, 30.0), np.full(60, 81.0)
    p, why = manifold_pressure_load_dependent(air, rpm, iat, baro, GEOM, CURVE, 0.7, **FORM)
    assert np.all(why == "") and np.all(np.diff(p) > 0) and np.all(p <= 81.0 * (1 + 1e-9))
    # The solved MAP reproduces the charge fraction it was solved from.
    ve = CURVE.ve(rpm, 0.7)
    x = p / baro
    np.testing.assert_allclose(ve * part_load_breathing_factor(x, **FORM) * x, cf, rtol=1e-12)
    # Load dependence only raises MAP against the full-breathing VE (f <= 1).
    flat, _ = manifold_pressure_ve_curve(air, rpm, iat, baro, GEOM, CURVE, 0.7)
    assert np.all(p >= flat - 1e-9)
    # At the full-breathing charge fraction MAP is ambient; above it, refused.
    edge = [_air_at(0.888, 2500.0), _air_at(0.95, 2500.0)]
    p2, why2 = manifold_pressure_load_dependent(
        edge, [2500.0] * 2, [30.0] * 2, [81.0] * 2, GEOM, CURVE, 0.7, **FORM
    )
    assert p2[0] == pytest.approx(81.0, rel=1e-9) and why2[0] == ""
    assert np.isnan(p2[1]) and why2[1] == MAP_ABOVE_AMBIENT
    p3, why3 = manifold_pressure_load_dependent(
        [_air_at(0.3, 7000.0)], [7000.0], [30.0], [81.0], GEOM, CURVE, 0.7, **FORM
    )
    assert np.isnan(p3[0]) and why3[0] == MAP_OUTSIDE_VE_FIT


def test_idle_map_moves_toward_a_warm_na_idle() -> None:
    """fuel-mv-2's known limitation: warm idle (charge fraction 0.151 at
    762 rpm, baro 81 kPa) sits at MAP 17.5 kPa at the 0.7 floor. The factor
    moves it to ~22 kPa; a warm NA idle is ~28-35 kPa, so part of the gap
    stays (the floor's bracket still carries it)."""
    air = [_air_at(0.151, 762.0)]
    flat, _ = manifold_pressure_ve_curve(air, [762.0], [30.0], [81.0], GEOM, CURVE, 0.7)
    p, _ = manifold_pressure_load_dependent(air, [762.0], [30.0], [81.0], GEOM, CURVE, 0.7, **FORM)
    back, _ = manifold_pressure_load_dependent(
        air, [762.0], [30.0], [81.0], GEOM, CURVE, 0.7, **dict(FORM, exhaust_ratio=1.1)
    )
    assert flat[0] == pytest.approx(81.0 * 0.151 / 0.7, rel=1e-9)  # 17.47 kPa
    assert 21.0 < p[0] < 23.0
    assert p[0] < back[0] < 24.0
    # Injector dP at a 350 kPa rail: the overstatement against fuel-mv-2 shrinks.
    dp_flat, dp = 350.0 + 81.0 - flat[0], 350.0 + 81.0 - p[0]
    assert 0.0 < dp_flat / dp - 1.0 < 0.015


def test_the_breathing_form_is_declared_never_fitted() -> None:
    raw = _raw()
    for pid in LOAD_DEPENDENT_READS:
        missing = copy.deepcopy(raw)
        missing["registry"]["parameters"] = [
            p for p in missing["registry"]["parameters"] if p["id"] != pid
        ]
        with pytest.raises(ValidationError, match="does not declare"):
            FuelPlantProfile.model_validate(missing)
    fitted = copy.deepcopy(raw)
    cr = next(p for p in fitted["registry"]["parameters"] if p["id"] == "compression_ratio")
    cr.update(status="identifiable", source="log_fit", value=None)
    with pytest.raises(ValidationError, match="status known"):
        FuelPlantProfile.model_validate(fitted)
    filled = copy.deepcopy(raw)
    pe = next(p for p in filled["registry"]["parameters"] if p["id"] == "exhaust_pressure_ratio")
    pe["working_assumption"].update(value=1.0, bracket=[1.0, 40.0])
    with pytest.raises(ValidationError, match="fill the cylinder"):
        FuelPlantProfile.model_validate(filled)
    mv3 = FuelPlantProfile.model_validate(raw)
    cases = sensitivity_cases(mv3, load_b06(PROFILE, mv3.delay))
    assert cases["exhaust_pressure_ratio=hi"] == {"exhaust_pressure_ratio": 1.1}
    assert not any(k.startswith(("compression_ratio", "residual_expansion")) for k in cases)


def test_identification_then_a_frozen_baseline_predicts() -> None:
    """The identify -> freeze -> predict path a corpus run will take, on the
    committed slice relabelled as identification with a synthetic lambda (a
    plant whose scale is 0.05 g/ms): it recovers the scale, a frozen baseline
    then resolves, and its MAP is the load-dependent solve."""
    mv3, _ = load_fuel_plant(PROFILE, MV3)
    delay = load_b06(PROFILE, mv3.delay)
    params = resolve_parameters(mv3, delay, for_identification=True, ve_curve=CURVE)
    frame = build_samples(FIXTURE, mv3, delay)
    frame = frame[frame["state"] == "eligible"].copy()
    out1 = predict(plant_inputs(frame), params, mv3.geometry)
    assert out1.manifold_refusal is not None
    keep = (out1.manifold_refusal == "") & np.isfinite(out1.lambda_delivered)
    frame = frame[keep].copy()
    frame["lambda_obs"] = out1.lambda_delivered[keep] / 0.05
    frame["session"] = mv3.split.identification[0]
    frame["region_id"] = frame["session"] + "#" + frame["region"].astype(str)
    fit = identify([frame], mv3, delay, sessions_root=FIXTURE.parent, ve_curve=CURVE)
    assert fit["value"] == pytest.approx(0.05) and fit["n_samples"] == len(frame) > 50
    raw = _raw()
    raw["baseline"] = BaselineParameterSet(
        id="test-only.mv3",
        frozen=True,
        frozen_on=date(2026, 10, 13),
        plant_version=MV3,
        hardware_configuration=mv3.hardware_configuration,
        fitted_on=list(mv3.split.identification),
        revisions=["ZS44A-20260920"],
        values={"stoich_air_per_open_ms": fit["value"]},
        intervals={"stoich_air_per_open_ms": fit["interval"]},
        method="test: identify on a relabelled slice",
        ve_curve=CURVE,
    ).model_dump(mode="json")
    frozen = FuelPlantProfile.model_validate(raw)
    resolved = resolve_parameters(frozen, delay)
    inputs = plant_inputs(frame)
    out = predict(inputs, resolved, frozen.geometry)
    np.testing.assert_allclose(out.lambda_delivered, frame["lambda_obs"].to_numpy(), rtol=1e-9)
    # Units: lambda = air / (s x f(dP) x Te), every term recomputed apart.
    air = air_per_event_g(inputs.maf_gps, inputs.rpm, frozen.geometry)
    map_kpa, _ = manifold_pressure_load_dependent(
        air, inputs.rpm, inputs.iat_c, inputs.baro_kpa, frozen.geometry, CURVE, 0.7, **FORM
    )
    np.testing.assert_allclose(out.map_kpa, map_kpa, rtol=1e-12)
    rail = np.where(np.isfinite(inputs.rail_kpa), inputs.rail_kpa, 350.0)
    ff = flow_factor(injector_delta_p_kpa(rail, inputs.baro_kpa, map_kpa), 350.0, 0.5)
    te = effective_open_ms(inputs.pulse_ms, inputs.ecu_dead_time_ms)
    np.testing.assert_allclose(out.lambda_delivered, air / (fit["value"] * ff * te), rtol=1e-12)
    assert isinstance(inputs, PlantInputs)
