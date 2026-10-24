"""R16: the dyno's numbers, refusals and stored schema are pinned.

``src/analysis/dyno.py`` was split into owner modules (contract, motion,
altitude noise, squat, driveline, inertia, road load, comparison, power,
profile). A pure move must not change a result, so this test runs the real
estimators over seeded synthetic fixtures -- the successful calculations and
the refused ones -- and compares them with ``tests/fixtures/dyno_golden.json``,
captured from the single-file module before any code moved.

What is compared: result and refusal kind, every field, array dtype, shape,
NaN mask and value (floats to 1e-9 relative), and the order of every list and
mapping. The stored side is pinned the same way: the committed Xterra
``dyno.yaml`` as loaded, a fully populated profile as saved (byte for byte),
and ``DynoProfile``'s JSON schema.

The fixture is regenerated only on purpose, by a change that is meant to move
a number::

    .venv/bin/python -m tests.test_dyno_golden --write
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import BaseModel

from src.analysis import dyno
from src.analysis.dyno import (
    DYNO_FILENAME,
    Driveline,
    DynoCurve,
    DynoProfile,
    EngineInertiaRecord,
    InertiaConditions,
    J1349Correction,
    MotionSources,
    RoadLoadConditions,
    SpeedSource,
    compare_runs,
    correct_specific_force,
    estimate_pitch_offset,
    fit_engine_inertia,
    fit_road_load,
    fit_squat,
    load_dyno_profile,
    measure_altitude_noise,
    record_dyno_floor,
    save_dyno_profile,
)
from src.analysis.dyno_squat import _grade_with_se
from src.config import VehicleParameters
from src.yaml_load import load_yaml
from tests import test_dyno as td
from tests import test_dyno_power as tp

REPO = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "fixtures" / "dyno_golden.json"


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------


def canon(value: Any) -> Any:
    """A JSON-able image of a result that keeps type names, dtypes and NaNs."""
    if isinstance(value, np.ndarray):
        return {
            "__array__": str(value.dtype),
            "shape": list(value.shape),
            "values": [float(v) for v in value.ravel()],
        }
    if isinstance(value, np.floating | np.integer):
        return value.item()
    if isinstance(value, BaseModel):
        return {"__model__": type(value).__name__, **canon(value.model_dump(mode="json"))}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass__": type(value).__name__,
            **{f.name: canon(getattr(value, f.name)) for f in dataclasses.fields(value)},
        }
    if isinstance(value, dict):
        return {"__dict__": [[str(k), canon(v)] for k, v in value.items()]}
    if isinstance(value, list | tuple):
        return {"__list__" if isinstance(value, list) else "__tuple__": [canon(v) for v in value]}
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def differences(path: str, got: Any, want: Any) -> list[str]:
    """Where two canonical images differ (floats to 1e-9 relative)."""
    if isinstance(want, dict) and isinstance(got, dict):
        if list(got) != list(want):
            return [f"{path}: keys {list(got)} != {list(want)}"]
        return [d for k in want for d in differences(f"{path}.{k}", got[k], want[k])]
    if isinstance(want, list) and isinstance(got, list):
        if len(got) != len(want):
            return [f"{path}: length {len(got)} != {len(want)}"]
        return [
            d
            for i, (g, w) in enumerate(zip(got, want, strict=True))
            for d in differences(f"{path}[{i}]", g, w)
        ]
    if isinstance(want, float) and isinstance(got, float):
        if np.isnan(want) or np.isnan(got):
            return [] if np.isnan(want) and np.isnan(got) else [f"{path}: {got} != {want}"]
        if got == want or abs(got - want) <= 1e-12 + 1e-9 * abs(want):
            return []
        return [f"{path}: {got!r} != {want!r}"]
    if type(got) is not type(want) or got != want:
        return [f"{path}: {got!r} != {want!r}"]
    return []


# ---------------------------------------------------------------------------
# The fixed inputs
# ---------------------------------------------------------------------------


def _squat_conditions() -> dyno.SquatConditions:
    return dyno.SquatConditions(
        payload_kg=127.0,
        tire_pressure_front_kpa=240.0,
        tire_pressure_rear_kpa=240.0,
        road="synthetic flat",
        date=dt.date(2026, 9, 26),
        sessions=["synthetic"],
    )


def _road_conditions() -> RoadLoadConditions:
    return RoadLoadConditions(
        road="synthetic flat",
        date=dt.date(2026, 9, 26),
        sessions=["synthetic"],
        payload_kg=127.0,
        tire_pressure_front_kpa=240.0,
        tire_pressure_rear_kpa=240.0,
        ambient_temp_c=18.0,
    )


def _inertia_conditions() -> InertiaConditions:
    return InertiaConditions(
        road="r",
        date=dt.date(2026, 9, 26),
        sessions=["s"],
        payload_kg=127.0,
        tune_revision="ZS44A-20260920",
    )


def _motion_cases() -> dict[str, Any]:
    out: dict[str, Any] = {}
    flat = td._flat_accelerations(np.random.default_rng(1), seconds=60.0)
    squat = td._squat_record()
    out["correct_specific_force_squat_and_offset"] = correct_specific_force(
        flat, squat=squat, pitch_offset=td._stated_offset(0.3)
    )
    out["correct_specific_force_no_squat"] = correct_specific_force(
        flat, squat=None, pitch_offset=td._stated_offset(0.0)
    )
    out["correct_specific_force_frame_mismatch"] = correct_specific_force(
        dataclasses.replace(flat, imu_reference_id="b"),
        squat=squat,
        pitch_offset=td._stated_offset(0.3),
    )
    drive = Driveline.from_vehicle(td.PARAMS, td.INERTIA)
    assert isinstance(drive, Driveline)
    legs = td._coast_pair(drive, 31, noise_f=0.005, noise_a=0.003)
    out["pitch_offset_pair"] = estimate_pitch_offset(legs, squat=squat)
    out["pitch_offset_no_squat"] = estimate_pitch_offset(legs, squat=None)
    out["pitch_offset_single"] = estimate_pitch_offset(legs[:1], squat=squat)
    out["pitch_offset_framed"] = estimate_pitch_offset(
        td._in_frame(legs, "a")[:1] + td._in_frame(legs, "b")[1:], squat=squat
    )
    out["pitch_offset_few"] = estimate_pitch_offset(legs, squat=squat, min_samples=10**6)
    out["pitch_offset_no_legs"] = estimate_pitch_offset([], squat=squat)
    out["driveline"] = drive
    return out


def _squat_cases() -> dict[str, Any]:
    out: dict[str, Any] = {}
    parked = td._parked(np.random.default_rng(5))
    noise = measure_altitude_noise(parked)
    out["altitude_noise"] = noise
    out["altitude_noise_wander"] = measure_altitude_noise(
        td._parked(np.random.default_rng(6), ar_sd=2.0, tau_s=15.0)
    )
    out["altitude_noise_moving"] = measure_altitude_noise(
        td._flat_accelerations(np.random.default_rng(1))
    )
    out["altitude_noise_short"] = measure_altitude_noise(
        td._parked(np.random.default_rng(5), seconds=60.0)
    )
    out["altitude_noise_frozen"] = measure_altitude_noise(
        dataclasses.replace(parked, altitude_m=np.full(len(parked), 1500.0))
    )
    out["altitude_noise_none"] = measure_altitude_noise(
        dataclasses.replace(parked, altitude_m=None)
    )
    flat = td._flat_accelerations(np.random.default_rng(2), seconds=240.0)
    fit = fit_squat(flat, altitude_noise=td._noise())
    out["squat"] = fit
    out["squat_record"] = fit.to_record(_squat_conditions()) if hasattr(fit, "to_record") else None
    out["squat_two_references"] = fit_squat(
        flat, altitude_noise=[td._noise(), td._noise(1.0, 2.0, 10.0)]
    )
    out["squat_constant_grade"] = fit_squat(
        td._flat_accelerations(np.random.default_rng(5), grade=0.003), altitude_noise=td._noise()
    )
    out["squat_not_flat"] = fit_squat(
        td._flat_accelerations(np.random.default_rng(6), grade=0.02), altitude_noise=td._noise()
    )
    out["squat_crest"] = fit_squat(
        td._flat_accelerations(np.random.default_rng(3), grade_of_t=td._crest, seconds=120.0),
        altitude_noise=td._noise(),
    )
    out["squat_unresolvable"] = fit_squat(
        td._flat_accelerations(
            np.random.default_rng(7), seconds=8.0, altitude_noise_m=4.0, amplitude=2.5
        ),
        altitude_noise=td._noise(white_sd=4.0),
    )
    out["squat_no_altitude"] = fit_squat(
        dataclasses.replace(flat, altitude_m=None), altitude_noise=td._noise()
    )
    out["squat_no_reference"] = fit_squat(flat, altitude_noise=[])
    out["squat_steady"] = fit_squat(
        td._flat_accelerations(np.random.default_rng(9), amplitude=0.1), altitude_noise=td._noise()
    )
    out["squat_too_few"] = fit_squat(
        dataclasses.replace(
            flat,
            t_s=flat.t_s[:20],
            speed_mps=flat.speed_mps[:20],
            accel_mps2=flat.accel_mps2[:20],
            sf_lon_mps2=flat.sf_lon_mps2[:20],
            altitude_m=flat.altitude_m[:20] if flat.altitude_m is not None else None,
        ),
        altitude_noise=td._noise(),
    )
    out["grade_with_se"] = _grade_with_se(
        flat.t_s,
        np.concatenate(
            [[0.0], np.cumsum(np.diff(flat.t_s) * 0.5 * (flat.speed_mps[1:] + flat.speed_mps[:-1]))]
        ),
        flat.altitude_m if flat.altitude_m is not None else flat.t_s,
        td._noise(),
    )
    return out


def _driveline_cases() -> dict[str, Any]:
    out: dict[str, Any] = {}
    drive = Driveline.from_vehicle(td.PARAMS, td.INERTIA)
    assert isinstance(drive, Driveline)
    out["effective_mass"] = [drive.effective_mass(td.MASS, g) for g in range(1, 7)]
    out["neutral_effective_mass"] = drive.neutral_effective_mass(td.MASS)
    out["missing_facts"] = Driveline.from_vehicle(VehicleParameters(mass_kg=2000.0), None)
    out["bad_efficiency"] = Driveline.from_vehicle(
        td.PARAMS.model_copy(update={"drivetrain_loss_fraction": 1.5}), td.INERTIA
    )
    pulls = [td._pull(np.random.default_rng(g), drive, g) for g in (2, 3) for _ in range(2)]
    legs = td._coast_pair(drive, 41, noise_f=0.005, noise_a=0.003)
    offset = estimate_pitch_offset(legs, squat=td._squat_record())
    assert isinstance(offset, dyno.PitchOffset)
    kwargs = td._inertia_kwargs(drive)
    fit = fit_engine_inertia(pulls, pitch_offset=offset, **kwargs)
    out["inertia_fit"] = fit
    out["inertia_record"] = (
        fit.to_record(_inertia_conditions()) if hasattr(fit, "to_record") else None
    )
    out["inertia_no_squat"] = fit_engine_inertia(
        pulls, pitch_offset=offset, **{**kwargs, "squat": None}
    )
    out["inertia_same_gear"] = fit_engine_inertia(pulls[:2], pitch_offset=offset, **kwargs)
    out["inertia_three_gears"] = fit_engine_inertia(
        pulls + [td._pull(np.random.default_rng(4), drive, 4, rpm_end=4500.0)],
        pitch_offset=offset,
        **kwargs,
    )
    out["inertia_none"] = fit_engine_inertia([], pitch_offset=offset, **kwargs)
    out["inertia_frame"] = fit_engine_inertia(
        pulls, pitch_offset=dataclasses.replace(offset, reference_id="x"), **kwargs
    )
    out["inertia_alpha"] = fit_engine_inertia(
        pulls, pitch_offset=offset, min_alpha_difference=1e6, **kwargs
    )
    out["inertia_overlap"] = fit_engine_inertia(
        pulls, pitch_offset=offset, min_rpm_overlap=1e6, **kwargs
    )
    if isinstance(fit, dyno.EngineInertiaFit):
        measured = Driveline.from_vehicle(
            td.PARAMS,
            td.INERTIA.model_copy(update={"measured_engine": fit.to_record(_inertia_conditions())}),
        )
        out["driveline_measured"] = measured
        out["driveline_measured_eta_changed"] = Driveline.from_vehicle(
            td.PARAMS.model_copy(update={"drivetrain_loss_fraction": 0.2}),
            td.INERTIA.model_copy(update={"measured_engine": fit.to_record(_inertia_conditions())}),
        )
    return out


def _road_load_cases() -> dict[str, Any]:
    out: dict[str, Any] = {}
    drive = Driveline.from_vehicle(td.PARAMS, td.INERTIA)
    assert isinstance(drive, Driveline)
    squat = td._squat_record()
    legs = td._coast_pair(drive, 51, runs=2, noise_f=0.005, noise_a=0.003)
    fit = fit_road_load(legs, driveline=drive, test_mass_kg=td.MASS, squat=squat)
    out["road_load_pair"] = fit
    out["road_load_record"] = (
        fit.to_record(_road_conditions()) if hasattr(fit, "to_record") else None
    )
    if isinstance(fit, dyno.RoadLoadFit):
        out["road_load_force"] = fit.force(np.linspace(5.0, 30.0, 11))
    out["road_load_no_squat"] = fit_road_load(
        legs, driveline=drive, test_mass_kg=td.MASS, squat=None
    )
    out["road_load_single_refused"] = fit_road_load(
        legs[:1], driveline=drive, test_mass_kg=td.MASS, squat=squat
    )
    out["road_load_single_allowed"] = fit_road_load(
        legs[:1], driveline=drive, test_mass_kg=td.MASS, squat=squat, allow_single_direction=True
    )
    out["road_load_short_span"] = fit_road_load(
        legs, driveline=drive, test_mass_kg=td.MASS, squat=squat, min_speed_span_kph=1e3
    )
    out["road_load_too_few"] = fit_road_load(
        legs, driveline=drive, test_mass_kg=td.MASS, squat=squat, min_samples=10**6
    )
    out["road_load_three_headings"] = fit_road_load(
        legs + [dyno.Leg("eastbound", legs[0].samples)],
        driveline=drive,
        test_mass_kg=td.MASS,
        squat=squat,
    )
    out["road_load_frames"] = fit_road_load(
        td._in_frame(legs[:1], "a") + td._in_frame(legs[1:], "b"),
        driveline=drive,
        test_mass_kg=td.MASS,
        squat=squat,
    )
    out["road_load_none"] = fit_road_load([], driveline=drive, test_mass_kg=td.MASS, squat=squat)
    # One direction measures no pitch offset, so a constant force bias reaches A.
    out["road_load_negative"] = fit_road_load(
        [
            dyno.Leg(
                legs[0].heading,
                dataclasses.replace(legs[0].samples, sf_lon_mps2=legs[0].samples.sf_lon_mps2 + 3.0),
            )
        ],
        driveline=drive,
        test_mass_kg=td.MASS,
        squat=squat,
        allow_single_direction=True,
    )
    return out


def _comparison_cases() -> dict[str, Any]:
    out: dict[str, Any] = {}
    floor = td._floor()
    out["floor"] = floor
    out["floor_matches"] = [
        floor.matches("wheel_power_kw", td._conditions()),
        floor.matches("wheel_torque_nm", td._conditions()),
        floor.matches("wheel_power_kw", td._conditions(gear=2)),
    ]
    out["compare_inside"] = compare_runs(td._curve(0.0), td._curve(2.0), floor)
    out["compare_outside"] = compare_runs(td._curve(0.0), td._curve(4.0), floor)
    out["compare_sigma_override"] = compare_runs(
        td._curve(0.0), td._curve(2.0), floor, sigma_threshold=1.0, grid_step_rpm=100.0
    )
    out["compare_as_dict"] = compare_runs(td._curve(0.0), td._curve(4.0), floor)
    result = out["compare_outside"]
    out["compare_outside_as_dict"] = result.as_dict() if hasattr(result, "as_dict") else None
    partial_rpm = np.arange(2500.0, 3800.0, 25.0)
    partial = DynoCurve(
        "wheel_power_kw", partial_rpm, np.full(len(partial_rpm), 70.0), td._conditions()
    )
    out["compare_partial"] = compare_runs(partial, partial, floor)
    high = DynoCurve(
        "wheel_power_kw", np.array([5600.0, 6000.0]), np.array([80.0, 82.0]), td._conditions()
    )
    out["compare_no_overlap"] = compare_runs(high, high, floor)
    out["compare_no_floor"] = compare_runs(td._curve(), td._curve(5.0), None)
    out["compare_gear"] = compare_runs(td._curve(), td._curve(5.0, gear=2), floor)
    out["compare_induction"] = compare_runs(td._curve(), td._curve(5.0, induction="boosted"), floor)
    out["compare_single"] = compare_runs(td._curve(), td._curve(5.0, direction="single"), floor)
    torque = DynoCurve("wheel_torque_nm", td.RPM, np.full(len(td.RPM), 300.0), td._conditions())
    out["compare_quantity"] = compare_runs(td._curve(), torque, floor)
    out["floor_two"] = record_dyno_floor([td._curve(), td._curve(1.0)], band_edges_rpm=[2500, 3500])
    out["floor_single"] = record_dyno_floor(
        [td._curve(), td._curve(1.0), td._curve(-1.0, direction="single")],
        band_edges_rpm=[2500, 3500],
    )
    out["floor_days"] = record_dyno_floor(
        [td._curve(), td._curve(1.0), td._curve(-1.0, date=dt.date(2026, 9, 28))],
        band_edges_rpm=[2500, 3500],
    )
    out["floor_identical"] = record_dyno_floor([td._curve()] * 3, band_edges_rpm=[2500, 3500])
    out["floor_no_overlap"] = record_dyno_floor(
        [td._curve(), td._curve(1.0), td._curve(-1.0)], band_edges_rpm=[7000, 8000]
    )
    out["floor_one_edge"] = record_dyno_floor(
        [td._curve(), td._curve(1.0), td._curve(-1.0)], band_edges_rpm=[7000]
    )
    out["floor_mixed"] = record_dyno_floor(
        [
            td._curve(),
            td._curve(1.0),
            DynoCurve("wheel_torque_nm", td.RPM, np.full(len(td.RPM), 300.0), td._conditions()),
        ],
        band_edges_rpm=[2500, 3500],
    )
    return out


def _power_cases() -> dict[str, Any]:
    out: dict[str, Any] = {}
    quiet = tp.PAIR
    noisy = [
        tp._pull(heading, noise=tp._Noise(np.random.default_rng(seed)))
        for heading, seed in (("northbound", 61), ("southbound", 62))
    ]
    out["power_exact"] = tp._power(quiet)
    out["power_noisy"] = tp._power(noisy)
    out["power_corrected"] = tp._power(
        noisy,
        correction=J1349Correction(vapour_pressure_kpa=1.0, vapour_pressure_source="synthetic"),
    )
    out["power_single_allowed"] = tp._power(
        quiet[:1], allow_single_direction=True, conditions=tp._conditions(direction="single")
    )
    result = out["power_noisy"]
    if isinstance(result, dyno.DynoPower):
        out["power_dyno_curve_power"] = result.dyno_curve("wheel_power_kw")
        out["power_dyno_curve_torque"] = result.dyno_curve("wheel_torque_nm")
        out["power_scaled"] = result.curve.scaled(1.25)
    out["power_none"] = tp._power([])
    out["power_conditions_gear"] = tp._power(quiet, conditions=tp._conditions(gear=2))
    out["power_single_road_load"] = tp._power(
        quiet, road_load=dataclasses.replace(tp._pair_road_load(), directions="single")
    )
    out["power_boosted_correction"] = tp._power(
        quiet,
        conditions=tp._conditions(induction="boosted"),
        correction=J1349Correction(vapour_pressure_kpa=1.0, vapour_pressure_source="synthetic"),
    )
    out["power_no_dry_air"] = tp._power(
        quiet,
        correction=J1349Correction(vapour_pressure_kpa=90.0, vapour_pressure_source="synthetic"),
    )
    out["power_frame"] = tp._power(quiet, pitch_offset=tp._offset(frame="a"))
    out["power_direction_mismatch"] = tp._power(
        quiet, conditions=tp._conditions(direction="single")
    )
    north, south = quiet
    out["power_one_direction"] = tp._power([north])
    out["power_too_many"] = tp._power(
        [north, south, dyno.PowerPull("eastbound", tp.GEAR, south.samples, south.demand)]
    )
    n = len(north.samples)
    part = np.zeros(n, dtype=bool)
    part[n // 2 : n // 2 + 60] = True
    rpm = north.samples.rpm
    assert rpm is not None
    ratio = tp.GEARBOX[3] / tp.GEARBOX[2]
    out["power_no_rpm"] = tp._power([tp._edit(north, rpm=None), south])
    out["power_no_demand"] = tp._power([tp._edit(north, demand=None), south])
    out["power_gear_change"] = tp._power(
        [tp._edit(north, rpm=np.where(part, rpm * ratio, rpm)), south]
    )
    out["power_clutch_slip"] = tp._power(
        [tp._edit(north, rpm=np.where(part, rpm * 1.08, rpm)), south]
    )
    out["power_ratio_mismatch"] = tp._power(
        [tp._edit(north, speed_mps=north.samples.speed_mps * 1.10), south]
    )
    assert north.demand is not None
    out["power_not_wot"] = tp._power(
        [tp._edit(north, demand=np.where(part, 3.5, north.demand)), south]
    )
    out["power_grade_varies"] = tp._power(
        [
            tp._pull(
                "northbound",
                noise=tp.SILENT,
                grade=lambda x: 0.03 * np.sin(2 * np.pi * np.asarray(x, dtype=float) / 150.0),
            ),
            south,
        ]
    )
    out["power_short_span"] = tp._power(
        [tp._pull("northbound", noise=tp.SILENT, rpm_end=3200.0), south]
    )
    out["power_below_floor"] = tp._power(
        [tp._pull("northbound", noise=tp.SILENT, rpm_start=1500.0, rpm_end=2520.0), south]
    )
    out["power_no_overlap"] = tp._power(
        [
            tp._pull("northbound", noise=tp.SILENT, rpm_end=3700.0),
            tp._pull("southbound", noise=tp.SILENT, rpm_start=3900.0),
        ]
    )
    out["power_low_rate"] = tp._power([tp._pull("northbound", noise=tp.SILENT, hz=10.0), south])
    return out


def cases() -> dict[str, Any]:
    """Every pinned calculation, in a fixed order."""
    out: dict[str, Any] = {}
    for group in (
        _motion_cases,
        _squat_cases,
        _driveline_cases,
        _road_load_cases,
        _comparison_cases,
        _power_cases,
    ):
        out.update(group())
    return out


# ---------------------------------------------------------------------------
# The stored side
# ---------------------------------------------------------------------------


def full_profile() -> DynoProfile:
    """Every section of a ``dyno.yaml`` populated, from constants only."""
    record = EngineInertiaRecord(
        reflected_kg_m2=0.153,
        reflected_kg_m2_se=0.004,
        driveline_efficiency=0.85,
        engine_kg_m2=0.153 / 0.85,
        engine_kg_m2_se=0.004 / 0.85,
        gears=(2, 3),
        rpm_min=2600.0,
        rpm_max=5400.0,
        n_bins=28,
        imu_pitch_offset_deg=0.3,
        imu_reference_id="frame-a",
        squat_corrected=True,
        conditions=_inertia_conditions(),
    )
    load = dyno.RoadLoadFit(
        a_n=182.2,
        b_n_per_mps=5.0,
        c_n_per_mps2=0.55,
        a_se_n=1.5,
        b_se_n_per_mps=0.2,
        c_se_n_per_mps2=0.01,
        directions="pair",
        per_direction={
            "northbound": dyno.DirectionFit(
                a_n=180.0,
                b_n_per_mps=4.0,
                c_n_per_mps2=0.54,
                a_se_n=2.0,
                b_se_n_per_mps=0.3,
                c_se_n_per_mps2=0.02,
                residual_sd_n=9.0,
                n_samples=250,
                speed_min_mps=5.5,
                speed_max_mps=31.0,
                static_pitch_deg=0.5,
            )
        },
        residual_sd_n=9.0,
        test_mass_kg=2311.0,
        neutral_effective_mass_kg=2330.0,
        inertia="estimated",
        squat_corrected=True,
        imu_pitch_offset_deg=0.3,
        imu_reference_id="frame-a",
    )
    return DynoProfile(
        motion_sources=MotionSources(
            speed=[SpeedSource(channel="decoded:SPEED", ratio_to_gps=0.932, evidence="measured")],
            rpm=["decoded:ECM_RPM", "obd:RPM"],
        ),
        inertia=td.INERTIA.model_copy(update={"measured_engine": record}),
        squat=td._squat_record(),
        road_load=load.to_record(_road_conditions()),
        noise_floors=[td._floor()],
    )


def stored_side(tmp: Path) -> dict[str, Any]:
    committed = load_dyno_profile(REPO / "data" / "nissan-xterra-2008" / DYNO_FILENAME)
    assert committed is not None
    path = tmp / DYNO_FILENAME
    save_dyno_profile(path, full_profile())
    reloaded = load_dyno_profile(path)
    assert reloaded is not None
    return {
        "xterra_dyno_yaml": committed.model_dump(mode="json"),
        "full_profile_yaml": path.read_text(),
        "full_profile_reloaded": reloaded.model_dump(mode="json"),
        "schema": DynoProfile.model_json_schema(),
        "floor_lookup": canon(
            [
                full_profile().noise_floor_for("wheel_power_kw", td._conditions()),
                full_profile().noise_floor_for("wheel_torque_nm", td._conditions()),
                full_profile().noise_floor_for("wheel_power_kw", td._conditions(gear=2)),
                DynoProfile().noise_floor_for("wheel_power_kw", td._conditions()),
            ]
        ),
        "refusal_kinds": sorted(dyno.RefusalKind.__args__),  # type: ignore[attr-defined]
        "refusal_as_dict": dyno.DynoRefusal("squat", "not_flat", "why").as_dict(),
        "constants": {
            name: getattr(dyno, name)
            for name in (
                "DYNO_FILENAME",
                "MIN_SAMPLES",
                "SQUAT_MAX_GRADE",
                "SQUAT_GRADE_SEGMENT_M",
                "SQUAT_GRADE_STEP_M",
                "SQUAT_MIN_SEGMENT_SAMPLES",
                "SQUAT_FLAT_FAMILY_ALPHA",
                "SQUAT_GRADE_LEAK_SIGMA",
                "SQUAT_MIN_ACCEL_SPAN_G",
                "COAST_MIN_SPEED_SPAN_KPH",
                "INERTIA_MIN_RPM_OVERLAP",
                "INERTIA_MIN_ALPHA_DIFFERENCE",
                "INERTIA_RPM_BIN",
                "INERTIA_MIN_PER_BIN",
                "MIN_PULL_PAIRS",
                "CURVE_GRID_STEP_RPM",
                "ALTITUDE_NOISE_MIN_REFERENCE_S",
                "ALTITUDE_NOISE_MAX_SPEED_MPS",
                "INERTIA_CONVENTION",
                "QUANTITY_UNITS",
                "FLOOR_MATCH_FIELDS",
                "FLOOR_SAME_DAY_FIELDS",
                "POWER_GEAR_TOLERANCE",
                "POWER_MIN_RPM_SPAN",
                "POWER_MAX_GRADE_RANGE",
                "POWER_GRADE_SMOOTH_S",
                "POWER_MIN_PER_BIN",
                "POWER_MIN_BIN_FILL",
                "ENGINE_ESTIMATE_LABEL",
                "WHEEL_TORQUE_CONVENTION",
                "GPS_SPEED_CHANNEL",
            )
        },
    }


def snapshot(tmp: Path) -> dict[str, Any]:
    """Everything pinned, as one canonical JSON-able mapping."""
    canonical = {"cases": canon(cases()), "stored": stored_side(tmp)}
    snap: dict[str, Any] = json.loads(json.dumps(canonical, allow_nan=True))
    return snap


def _write() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        GOLDEN.write_text(json.dumps(snapshot(Path(tmp)), indent=1, allow_nan=True) + "\n")
    print(f"wrote {GOLDEN}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pinned() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(GOLDEN.read_text())
    return loaded


@pytest.fixture(scope="module")
def current(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return snapshot(tmp_path_factory.mktemp("dyno-golden"))


def _case_names(pinned: dict[str, Any]) -> list[str]:
    return [key for key, _ in pinned["cases"]["__dict__"]]


def test_the_same_calculations_are_pinned(pinned: dict[str, Any], current: dict[str, Any]) -> None:
    assert _case_names(current) == _case_names(pinned)


def test_the_pinned_cases_cover_successes_and_every_refusal_path(pinned: dict[str, Any]) -> None:
    kinds = set()
    ok = 0
    for _name, value in pinned["cases"]["__dict__"]:
        if isinstance(value, dict) and value.get("__dataclass__") == "DynoRefusal":
            kinds.add(value["kind"])
        elif isinstance(value, dict) and ("__dataclass__" in value or "__model__" in value):
            ok += 1
    assert ok >= 25
    assert {
        "not_flat",
        "grade_unresolvable",
        "single_direction",
        "frame_mismatch",
        "non_physical",
        "same_gear",
        "no_floor",
        "conditions_mismatch",
        "gear_change",
        "clutch_slip",
        "gear_ratio_mismatch",
        "not_wot",
        "grade_varies",
        "correction_refused",
        "too_many_directions",
    } <= kinds


def test_every_calculation_matches_its_pin(pinned: dict[str, Any], current: dict[str, Any]) -> None:
    found = differences("cases", current["cases"], pinned["cases"])
    assert found == [], "\n".join(found[:20])


def test_the_stored_side_matches_its_pin(pinned: dict[str, Any], current: dict[str, Any]) -> None:
    found = differences("stored", current["stored"], pinned["stored"])
    assert found == [], "\n".join(found[:20])


def test_the_saved_profile_is_byte_for_byte_the_pinned_text(
    pinned: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / DYNO_FILENAME
    save_dyno_profile(path, full_profile())
    assert path.read_text() == pinned["stored"]["full_profile_yaml"]
    assert load_yaml(path.read_text())["schema_version"] == 1


if __name__ == "__main__":
    if "--write" in sys.argv:
        _write()
    else:
        print(__doc__)
