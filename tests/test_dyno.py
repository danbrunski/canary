"""Driving-dyno building blocks (H07.4, H07.5, H07.6, H07.8).

Every estimator is shown to recover a known answer from synthetic data
generated forward from stated coefficients (with measurement noise and the
nuisances the estimator claims to handle: grade, wind, squat), and to refuse
each degenerate input with its named cause.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import math
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from src.analysis.dyno import (
    DYNO_FILENAME,
    AltitudeNoise,
    Driveline,
    DynoCurve,
    DynoNoiseFloor,
    DynoProfile,
    DynoRefusal,
    EngineInertiaFit,
    EngineInertiaRecord,
    GearPull,
    InertiaConditions,
    InertiaData,
    Leg,
    MotionSamples,
    PitchOffset,
    RoadLoadConditions,
    RoadLoadFit,
    RunComparison,
    RunConditions,
    SquatCoefficient,
    SquatConditions,
    SquatFit,
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
from src.signals.committees import STANDARD_GRAVITY as G
from src.vehicle.profiles import read_registry_file

REPO = Path(__file__).resolve().parents[1]

# Known answers the synthetic data is generated from. None of them is read
# from a vehicle profile: the estimators must recover them, not echo them.
MASS = 2311.0
TRUE_SQUAT_DEG_PER_G = 1.2
TRUE_MOUNT_OFFSET_DEG = 0.3
TRUE_A, TRUE_B, TRUE_C = 180.0, 5.0, 0.55
TRUE_ENGINE_INERTIA = 0.18  # the driveline below *estimates* 0.25
ETA = 0.85

PARAMS = VehicleParameters(
    mass_kg=2184.4,
    wheel_tire_assembly_kg=40.3,
    final_drive=4.08,
    gearbox_ratios=(4.368, 2.518, 1.743, 1.283, 1.0, 0.769),
    tire_loaded_circumference_m=2.609,
    driveline_evidence="synthetic test vehicle",
    drivetrain_loss_fraction=1.0 - ETA,
)
INERTIA = InertiaData(
    road_wheels=4,
    wheel_shape_factor=0.7,
    driveline_output_kg_m2=1.0,
    engine_side_kg_m2=0.25,
    evidence="synthetic estimate",
)


@pytest.fixture
def driveline() -> Driveline:
    built = Driveline.from_vehicle(PARAMS, INERTIA)
    assert isinstance(built, Driveline)
    return built


def _refused(result: object, kind: str) -> DynoRefusal:
    assert isinstance(result, DynoRefusal), result
    assert result.kind == kind, result
    assert result.reason
    return result


# ---------------------------------------------------------------------------
# Synthetic generators
# ---------------------------------------------------------------------------


def _pitch_sf(accel: np.ndarray, grade: float | np.ndarray, deg_per_g: float) -> np.ndarray:
    """Specific force a tilted IMU reads: a + g·sin(θ) + g·sin(δ(a)) + mounting."""
    pitch = np.radians(deg_per_g * accel / G + TRUE_MOUNT_OFFSET_DEG)
    return accel + G * np.sin(np.arctan(grade)) + G * np.sin(pitch)


def _flat_accelerations(
    rng: np.random.Generator,
    *,
    grade: float = 0.0,
    seconds: float = 90.0,
    amplitude: float = 2.5,
    altitude_noise_m: float = 1.0,
    hz: float = 10.0,
    noise_f: float = 0.03,
    noise_a: float = 0.02,
    grade_of_t: Callable[[np.ndarray], np.ndarray] | None = None,
    altitude_ar_sd_m: float = 0.0,
    altitude_tau_s: float = 10.0,
    altitude_error_m: np.ndarray | None = None,
) -> MotionSamples:
    """Accelerate and brake (period 20 s) on a road of constant or varying grade.

    ``grade_of_t`` makes the grade a function of time, i.e. of where the
    vehicle is when; the IMU sees it and GPS altitude integrates it. Altitude
    error is white (``altitude_noise_m``) plus AR(1) wander
    (``altitude_ar_sd_m``, ``altitude_tau_s``), or a given series
    (``altitude_error_m``, e.g. real parked GNSS altitude).
    """
    t = np.arange(0.0, seconds, 1.0 / hz)
    period = 20.0
    accel = amplitude * np.sin(2 * math.pi * t / period)
    speed = 15.0 + amplitude * period / (2 * math.pi) * (1 - np.cos(2 * math.pi * t / period))
    road = np.full(len(t), grade) if grade_of_t is None else grade_of_t(t)
    rise = np.diff(t) * 0.5 * (speed[1:] * road[1:] + speed[:-1] * road[:-1])
    altitude = 1500.0 + np.concatenate([[0.0], np.cumsum(rise)])
    sf = _pitch_sf(accel, road, TRUE_SQUAT_DEG_PER_G)
    return MotionSamples(
        t_s=t,
        speed_mps=speed,
        accel_mps2=accel + rng.normal(0, noise_a, len(t)),
        sf_lon_mps2=sf + rng.normal(0, noise_f, len(t)),
        altitude_m=altitude
        + (
            _altitude_error(rng, t, altitude_noise_m, altitude_ar_sd_m, altitude_tau_s)
            if altitude_error_m is None
            else altitude_error_m[: len(t)]
        ),
    )


def _altitude_error(
    rng: np.random.Generator, t: np.ndarray, white_sd: float, ar_sd: float, tau_s: float
) -> np.ndarray:
    """White plus exactly discretised AR(1) (Ornstein-Uhlenbeck) altitude error."""
    error = rng.normal(0.0, white_sd, len(t)) if white_sd > 0 else np.zeros(len(t))
    if ar_sd > 0:
        wander = np.empty(len(t))
        wander[0] = rng.normal(0.0, ar_sd)
        for i in range(1, len(t)):
            phi = math.exp(-(t[i] - t[i - 1]) / tau_s)
            wander[i] = phi * wander[i - 1] + math.sqrt(1 - phi**2) * rng.normal(0.0, ar_sd)
        error = error + wander
    return error


def _parked(
    rng: np.random.Generator,
    *,
    seconds: float = 300.0,
    hz: float = 10.0,
    white_sd: float = 1.0,
    ar_sd: float = 0.0,
    tau_s: float = 10.0,
) -> MotionSamples:
    """A stationary reference: the truck parked, GNSS altitude doing what it does."""
    t = np.arange(0.0, seconds, 1.0 / hz)
    zeros = np.zeros(len(t))
    return MotionSamples(
        t_s=t,
        speed_mps=zeros,
        accel_mps2=zeros,
        sf_lon_mps2=zeros,
        altitude_m=1500.0 + _altitude_error(rng, t, white_sd, ar_sd, tau_s),
    )


@functools.cache
def _noise(white_sd: float = 1.0, ar_sd: float = 0.0, tau_s: float = 10.0) -> AltitudeNoise:
    """The noise model measured from a parked reference with these statistics."""
    measured = measure_altitude_noise(
        _parked(np.random.default_rng(999), white_sd=white_sd, ar_sd=ar_sd, tau_s=tau_s)
    )
    assert isinstance(measured, AltitudeNoise), measured
    return measured


#: For the noise-free algebra tests only: a model that resolves anything.
_NEGLIGIBLE_NOISE = AltitudeNoise(
    white_var_m2=1e-8, correlated_var_m2=0.0, tau_s=1.0, reference_s=60.0, n_reference=600
)


def _coastdown(
    rng: np.random.Generator,
    driveline: Driveline,
    *,
    heading_sign: float,
    grade: float = 0.005,
    wind_mps: float = 2.0,
    v0: float = 31.0,
    v_end: float = 5.5,
    squat_deg_per_g: float = TRUE_SQUAT_DEG_PER_G,
    hz: float = 10.0,
    noise_f: float = 0.03,
    noise_a: float = 0.02,
) -> MotionSamples:
    """Clutch-in coast on a graded road in wind; uphill and headwind one way."""
    m_eff = driveline.neutral_effective_mass(MASS)
    slope = grade * heading_sign
    wind = wind_mps * heading_sign
    theta = math.atan(slope)
    dt_sim, every = 0.01, int(round(1.0 / (hz * 0.01)))
    v, t, altitude = v0, 0.0, 1500.0
    rows: list[tuple[float, float, float, float]] = []
    step = 0
    while v > v_end:
        air = v + wind
        force = TRUE_A + TRUE_B * v + TRUE_C * air * abs(air)
        accel = -(force + MASS * G * math.sin(theta)) / m_eff
        if step % every == 0:
            rows.append((t, v, accel, altitude))
        assert t < 600.0, "the coast never reaches v_end (downhill faster than drag)"
        v += accel * dt_sim
        altitude += slope * v * dt_sim
        t += dt_sim
        step += 1
    arr = np.array(rows)
    accel_col = arr[:, 2]
    sf = _pitch_sf(accel_col, slope, squat_deg_per_g)
    return MotionSamples(
        t_s=arr[:, 0],
        speed_mps=arr[:, 1],
        accel_mps2=accel_col + rng.normal(0, noise_a, len(arr)),
        sf_lon_mps2=sf + rng.normal(0, noise_f, len(arr)),
        altitude_m=arr[:, 3],
    )


def _engine_torque(rpm: float) -> float:
    return 280.0 + 40.0 * (1.0 - ((rpm - 4000.0) / 2000.0) ** 2)


def _pull(
    rng: np.random.Generator,
    driveline: Driveline,
    gear: int,
    *,
    grade: float = 0.01,
    rpm_start: float = 2500.0,
    rpm_end: float = 5500.0,
    hz: float = 100.0,
    eta_at_crank: float = ETA,
) -> GearPull:
    """A WOT pull in one gear from a known torque curve, road load and inertia."""
    ratio = driveline.overall_ratio(gear)
    r = driveline.tire_radius_m
    # The crank inertia is upstream of the lossy gearbox: the wheel sees
    # ETA·N/r·(T − I_e·α), so the engine's share of m_eff is ETA·I_e·N²/r².
    # Written out here, independently of Driveline.effective_mass.
    m_eff = (
        MASS
        + driveline.wheel_side_kg_m2 / r**2
        + eta_at_crank * TRUE_ENGINE_INERTIA * ratio**2 / r**2
    )
    theta = math.atan(grade)
    dt_sim = 1.0 / hz
    v = rpm_start * 2 * math.pi / 60 * r / ratio
    t = 0.0
    rows: list[tuple[float, float, float, float]] = []
    while True:
        rpm = v * ratio / r * 60 / (2 * math.pi)
        if rpm > rpm_end:
            break
        drive = _engine_torque(rpm) * eta_at_crank * ratio / r
        load = TRUE_A + TRUE_B * v + TRUE_C * v**2
        accel = (drive - load - MASS * G * math.sin(theta)) / m_eff
        rows.append((t, v, accel, rpm))
        assert t < 60.0, f"gear {gear} never reaches {rpm_end} rpm: pick a reachable band"
        v += accel * dt_sim
        t += dt_sim
    arr = np.array(rows)
    accel_col = arr[:, 2]
    sf = _pitch_sf(accel_col, grade, TRUE_SQUAT_DEG_PER_G)
    return GearPull(
        gear=gear,
        samples=MotionSamples(
            t_s=arr[:, 0],
            speed_mps=arr[:, 1],
            accel_mps2=accel_col + rng.normal(0, 0.02, len(arr)),
            sf_lon_mps2=sf + rng.normal(0, 0.03, len(arr)),
            rpm=arr[:, 3] + rng.normal(0, 5.0, len(arr)),
        ),
    )


def _squat_record() -> SquatCoefficient:
    fit = SquatFit(
        deg_per_g=TRUE_SQUAT_DEG_PER_G,
        deg_per_g_se=0.0,
        offset_deg=0.0,
        residual_sd_deg=0.1,
        n_samples=500,
        accel_span_g=0.5,
        grade=0.0,
        grade_se=0.001,
        max_segment_grade=0.002,
        grade_leak_deg_per_g=0.0,
        grade_leak_se_deg_per_g=0.1,
        altitude_noise=[_NEGLIGIBLE_NOISE],
    )
    return fit.to_record(
        SquatConditions(
            payload_kg=127.0,
            tire_pressure_front_kpa=240.0,
            tire_pressure_rear_kpa=240.0,
            road="synthetic flat",
            date=dt.date(2026, 9, 26),
            sessions=["synthetic"],
        )
    )


def _true_road_load() -> RoadLoadFit:
    return RoadLoadFit(
        a_n=TRUE_A,
        b_n_per_mps=TRUE_B,
        c_n_per_mps2=TRUE_C,
        a_se_n=0.0,
        b_se_n_per_mps=0.0,
        c_se_n_per_mps2=0.0,
        directions="pair",
        per_direction={},
        residual_sd_n=0.0,
        test_mass_kg=MASS,
        neutral_effective_mass_kg=MASS,
        inertia="estimated",
        squat_corrected=True,
        imu_pitch_offset_deg=TRUE_MOUNT_OFFSET_DEG,
    )


# ---------------------------------------------------------------------------
# MotionSamples
# ---------------------------------------------------------------------------


def _loose_motion(**columns: Any) -> MotionSamples:
    """``MotionSamples`` from plain lists, which its constructor accepts at runtime."""
    return MotionSamples(**columns)


def test_motion_samples_refuse_mismatched_and_non_finite_columns() -> None:
    with pytest.raises(ValueError, match="differ in length"):
        _loose_motion(t_s=[0, 1, 2], speed_mps=[1, 1], accel_mps2=[0, 0, 0], sf_lon_mps2=[0, 0, 0])
    with pytest.raises(ValueError, match="non-finite"):
        _loose_motion(
            t_s=[0, 1], speed_mps=[1, float("nan")], accel_mps2=[0, 0], sf_lon_mps2=[0, 0]
        )
    with pytest.raises(ValueError, match="strictly increasing"):
        _loose_motion(t_s=[0, 0], speed_mps=[1, 1], accel_mps2=[0, 0], sf_lon_mps2=[0, 0])


# ---------------------------------------------------------------------------
# H07.4 — squat
# ---------------------------------------------------------------------------


def test_squat_is_exact_without_noise() -> None:
    fit = fit_squat(
        _flat_accelerations(
            np.random.default_rng(0), noise_f=0.0, noise_a=0.0, altitude_noise_m=0.0
        ),
        altitude_noise=_NEGLIGIBLE_NOISE,
    )
    assert isinstance(fit, SquatFit), fit
    assert fit.deg_per_g == pytest.approx(TRUE_SQUAT_DEG_PER_G, rel=1e-3)
    assert fit.offset_deg == pytest.approx(TRUE_MOUNT_OFFSET_DEG, abs=1e-3)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_squat_recovers_known_coefficient_on_flat_ground(seed: int) -> None:
    # Four minutes of +/-0.25 g accelerations on a flat road, 10 Hz, with
    # 0.03 m/s^2 specific-force and 0.02 m/s^2 acceleration noise: the slope
    # scatters ~2 % (3.3 % for a 90 s window), so 5 % is ~2.5 sigma.
    fit = fit_squat(
        _flat_accelerations(np.random.default_rng(seed), seconds=240.0), altitude_noise=_noise()
    )
    assert isinstance(fit, SquatFit), fit
    assert fit.deg_per_g == pytest.approx(TRUE_SQUAT_DEG_PER_G, rel=0.05)
    assert abs(fit.deg_per_g - TRUE_SQUAT_DEG_PER_G) <= 3.5 * fit.deg_per_g_se
    assert fit.offset_deg == pytest.approx(TRUE_MOUNT_OFFSET_DEG, abs=0.05)


def test_squat_constant_small_grade_goes_to_the_offset_not_the_slope() -> None:
    fit = fit_squat(
        _flat_accelerations(np.random.default_rng(5), grade=0.003, noise_f=0.0, noise_a=0.0),
        altitude_noise=_noise(),
    )
    assert isinstance(fit, SquatFit), fit
    assert fit.deg_per_g == pytest.approx(TRUE_SQUAT_DEG_PER_G, rel=1e-3)
    expected_offset = TRUE_MOUNT_OFFSET_DEG + math.degrees(math.atan(0.003))
    assert fit.offset_deg == pytest.approx(expected_offset, abs=1e-3)


def _crest(t: np.ndarray) -> np.ndarray:
    """+1.2 % while accelerating, −1.2 % while braking: a crest, mean grade ~0."""
    return np.where(t < 10.0, 0.012, -0.012)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_squat_refuses_a_crest_whose_mean_grade_is_flat(seed: int) -> None:
    # One 20 s accelerate-then-brake cycle over a ~5 m crest: the window's mean
    # grade is ~0, so a mean-only gate passes it, and the pull uphill and the
    # lift downhill read as ~4.5 deg/g of "squat" against a true 1.2.
    samples = _flat_accelerations(np.random.default_rng(seed), seconds=20.0, grade_of_t=_crest)
    refusal = _refused(fit_squat(samples, altitude_noise=_noise()), "not_flat")
    assert "though the window's mean is" in refusal.reason
    assert "over 0-200 m" in refusal.reason


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_squat_refuses_a_rolling_road_in_phase_with_the_acceleration(seed: int) -> None:
    def rolling(t: np.ndarray) -> np.ndarray:
        return 0.015 * np.sin(2 * math.pi * t / 20.0)

    samples = _flat_accelerations(np.random.default_rng(seed), grade_of_t=rolling)
    _refused(fit_squat(samples, altitude_noise=_noise()), "not_flat")


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_squat_refuses_sub_gate_grade_that_moves_with_the_acceleration(seed: int) -> None:
    # ±0.4 % in phase with ±0.25 g: every segment is inside the 0.5 % gate, yet
    # it would read ~2.1 deg/g (true 1.2). The leak check refuses it and says
    # how much it would have added.
    def gentle(t: np.ndarray) -> np.ndarray:
        return 0.004 * np.sin(2 * math.pi * t / 20.0)

    samples = _flat_accelerations(np.random.default_rng(seed), grade_of_t=gentle)
    refusal = _refused(fit_squat(samples, altitude_noise=_noise()), "grade_correlated")
    assert "deg/g" in refusal.reason


def test_squat_grade_checks_rarely_refuse_a_flat_road_and_report_an_honest_leak() -> None:
    fits = [
        fit_squat(_flat_accelerations(np.random.default_rng(s)), altitude_noise=_noise())
        for s in range(20)
    ]
    passed = [f for f in fits if isinstance(f, SquatFit)]
    # White 1 m altitude noise, 90 s windows: measured 1 of 200 refused.
    assert len(passed) >= 19
    leaks = np.array([f.grade_leak_deg_per_g for f in passed])
    ses = np.array([f.grade_leak_se_deg_per_g for f in passed])
    assert abs(leaks.mean()) < 0.06
    # The recorded se is neither inflated nor optimistic.
    assert 0.7 < ses.mean() / leaks.std(ddof=1) < 1.5
    slopes = np.array([f.deg_per_g for f in passed])
    slope_ses = np.array([f.deg_per_g_se for f in passed])
    assert 0.7 < slope_ses.mean() / slopes.std(ddof=1) < 1.4


@pytest.mark.parametrize(("ar_sd", "tau_s"), [(0.5, 10.0), (1.0, 10.0), (1.0, 2.0)])
def test_flat_road_under_wandering_altitude_is_unresolvable_not_not_flat(
    ar_sd: float, tau_s: float
) -> None:
    # AR(1) GNSS altitude wander, measured from a parked reference with the
    # same statistics. A white-noise se refused 200 of 200 of these as
    # not_flat; the road is flat, and what fails is GPS altitude's ability to
    # show it at 0.5 % per 200 m, so every one is grade_unresolvable (measured:
    # 100/100 at 90 s and 50/50 at 240 s for each of the three).
    noise = _noise(white_sd=0.0, ar_sd=ar_sd, tau_s=tau_s)
    for seed in range(3):
        samples = _flat_accelerations(
            np.random.default_rng(seed),
            altitude_noise_m=0.0,
            altitude_ar_sd_m=ar_sd,
            altitude_tau_s=tau_s,
        )
        refusal = _refused(fit_squat(samples, altitude_noise=noise), "grade_unresolvable")
        assert "GPS altitude cannot show it" in refusal.reason or "resolves" in refusal.reason


_REAL_ALTITUDE = REPO / "tests" / "fixtures" / "gnss_altitude" / "parked-2026-09-20T152758.json"


def _real_parked() -> tuple[np.ndarray, np.ndarray]:
    raw = json.loads(_REAL_ALTITUDE.read_text())
    return np.asarray(raw["t_s"], dtype=float), np.asarray(raw["altitude_m"], dtype=float)


def _real_reference(t: np.ndarray, z: np.ndarray, lo: float, hi: float) -> AltitudeNoise:
    keep = (t >= lo) & (t <= hi)
    zeros = np.zeros(int(keep.sum()))
    measured = measure_altitude_noise(
        MotionSamples(
            t_s=t[keep], speed_mps=zeros, accel_mps2=zeros, sf_lon_mps2=zeros, altitude_m=z[keep]
        )
    )
    assert isinstance(measured, AltitudeNoise), measured
    return measured


def test_real_parked_gnss_altitude_is_not_white_and_the_model_says_so() -> None:
    t, z = _real_parked()
    noise = _real_reference(t, z, 0.0, 183.0)
    # A smooth wander (variogram ~h^1.5), not white noise and not a plain AR(1).
    assert noise.alpha >= 1.25
    # The model's se for a 200 m segment at 23 m/s against the scatter of that
    # same estimator slid along the real reference: it must describe the data.
    seg_t = np.arange(0.0, 200.0 / 23.0, 0.1)
    seg_s = 23.0 * seg_t
    _, model_se = _grade_with_se(seg_t, seg_s, np.zeros_like(seg_t), noise)
    grid = np.arange(0.0, 183.0, 0.1)
    zi = np.interp(grid, t, z)
    weights = (seg_s - seg_s.mean()) / float((seg_s - seg_s.mean()) @ (seg_s - seg_s.mean()))
    slid = [weights @ zi[k : k + len(weights)] for k in range(0, len(zi) - len(weights), 5)]
    empirical = float(np.std(slid))
    assert 0.7 < model_se / empirical < 1.4
    # The defect this replaced: the textbook se from the segment's own
    # residuals (independent samples) understates the real scatter several-fold.
    design = np.column_stack([np.ones_like(seg_s), seg_s])
    naive = [
        float(np.sqrt(np.diag(np.linalg.pinv(design.T @ design)))[1])
        * float(
            np.std(
                zi[k : k + len(weights)]
                - design @ np.linalg.lstsq(design, zi[k : k + len(weights)], rcond=None)[0],
                ddof=2,
            )
        )
        for k in range(0, len(zi) - len(weights), 25)
    ]
    assert float(np.mean(naive)) * 3.0 < empirical


def test_squat_on_real_parked_altitude_noise_never_reads_as_not_flat() -> None:
    # The committed slice (chosen by rule: the first 720 s after 15 minutes
    # parked), bracketed by references before and after, its four 90 s middle
    # windows used as the altitude error of a flat synthetic run. Measured
    # over every such window on 2026-09-20/21 (bracketed): 42 of 90 scored,
    # 47 grade_unresolvable, 1 not_flat.
    t, z = _real_parked()
    before = _real_reference(t, z, 0.0, 183.0)
    after = _real_reference(t, z, 537.0, 720.0)
    outcomes = []
    for window in range(4):
        grid = 180.0 + 90.0 * window + np.arange(0.0, 90.0, 0.1)
        error = np.interp(grid, t, z)
        samples = _flat_accelerations(
            np.random.default_rng(window), altitude_error_m=error - error.mean()
        )
        fit = fit_squat(samples, altitude_noise=[before, after])
        if isinstance(fit, SquatFit):
            assert fit.deg_per_g == pytest.approx(TRUE_SQUAT_DEG_PER_G, rel=0.05)
            outcomes.append("scored")
        else:
            outcomes.append(fit.kind)
    assert "not_flat" not in outcomes
    assert set(outcomes) <= {"scored", "grade_unresolvable"}
    assert outcomes.count("scored") >= 3


def test_each_standard_error_takes_the_noisier_reference() -> None:
    # A white-heavy and a wander-heavy reference: each se (window, leak) must
    # be the larger of the two computed separately, not whichever reference
    # is noisier at one lag.
    white_heavy = _noise(white_sd=1.0)
    wander_heavy = _noise(white_sd=0.05, ar_sd=0.3, tau_s=60.0)
    samples = _flat_accelerations(np.random.default_rng(3), seconds=240.0, altitude_noise_m=0.3)
    both = fit_squat(samples, altitude_noise=[white_heavy, wander_heavy])
    alone = [fit_squat(samples, altitude_noise=ref) for ref in (white_heavy, wander_heavy)]
    assert isinstance(both, SquatFit), both
    assert all(isinstance(fit, SquatFit) for fit in alone), alone
    first, second = alone
    assert isinstance(first, SquatFit) and isinstance(second, SquatFit)
    # They rank the other way round for the two quantities: the wander
    # dominates the window's grade, the white noise the leak.
    assert second.grade_se > first.grade_se
    assert first.grade_leak_se_deg_per_g > second.grade_leak_se_deg_per_g
    assert both.grade_se == pytest.approx(max(first.grade_se, second.grade_se))
    assert both.grade_leak_se_deg_per_g == pytest.approx(
        max(first.grade_leak_se_deg_per_g, second.grade_leak_se_deg_per_g)
    )
    assert both.altitude_noise == [white_heavy, wander_heavy]
    _refused(
        fit_squat(_flat_accelerations(np.random.default_rng(3)), altitude_noise=[]),
        "inputs_missing",
    )


_AR_1M: dict[str, Any] = {"altitude_noise_m": 0.0, "altitude_ar_sd_m": 1.0, "altitude_tau_s": 10.0}


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_steep_grade_is_not_flat_however_noisy_the_altitude(seed: int) -> None:
    # Under AR(1) 1 m / τ 10 s every flat window is grade_unresolvable; a 4 %
    # grade still reads ~4.05 ± 0.30 %, 12 se above the gate. The refusal must
    # say not_flat: "the road may be flat" would be false.
    noise = _noise(white_sd=0.0, ar_sd=1.0, tau_s=10.0)
    samples = _flat_accelerations(np.random.default_rng(seed), grade=0.04, **_AR_1M)
    refusal = _refused(fit_squat(samples, altitude_noise=noise), "not_flat")
    assert "over" in refusal.reason and "may be flat" not in refusal.reason


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_steep_crest_is_not_flat_however_noisy_the_altitude(seed: int) -> None:
    # ±5 % crest (segment reads ~5.0 ± 0.7 %) under the same noise.
    def crest(t: np.ndarray) -> np.ndarray:
        return np.where(t < 10.0, 0.05, -0.05)

    noise = _noise(white_sd=0.0, ar_sd=1.0, tau_s=10.0)
    samples = _flat_accelerations(
        np.random.default_rng(seed), seconds=20.0, grade_of_t=crest, **_AR_1M
    )
    _refused(fit_squat(samples, altitude_noise=noise), "not_flat")


def test_segment_threshold_is_family_wise_not_two_se_per_segment() -> None:
    # A constant 0.4 % grade is inside the gate. A 240 s window holds ~27
    # independent segments; at 2 se each, 15 of 40 such windows were refused
    # not_flat, and with the family-wise z (3.1 here) none were.
    for seed in range(10):
        fit = fit_squat(
            _flat_accelerations(np.random.default_rng(seed), seconds=240.0, grade=0.004),
            altitude_noise=_noise(),
        )
        assert not (isinstance(fit, DynoRefusal) and fit.kind == "not_flat"), (seed, fit)


def test_window_mean_gate_allows_for_its_own_standard_error() -> None:
    # A grade exactly at the gate (0.5 %) over a window shorter than one
    # segment (6.5 s, ~150 m; 0.3 m white altitude noise, se ~0.09 %): the
    # estimate lands above 0.5 % about half the time. Only a mean beyond
    # 0.5 % + 2 se is shown to exceed the gate.
    for seed in range(10):
        fit = fit_squat(
            _flat_accelerations(
                np.random.default_rng(seed), seconds=6.5, grade=0.005, altitude_noise_m=0.3
            ),
            altitude_noise=_noise(white_sd=0.3),
        )
        assert not (isinstance(fit, DynoRefusal) and fit.kind == "not_flat"), (seed, fit)


def test_an_unresolvable_window_says_so_about_the_window() -> None:
    # Shorter than one segment (5 s, ~100 m) under 4 m of altitude noise:
    # the window itself cannot be resolved, and the refusal says the window.
    samples = _flat_accelerations(np.random.default_rng(7), seconds=5.0)
    refusal = _refused(
        fit_squat(samples, altitude_noise=_noise(white_sd=4.0)), "grade_unresolvable"
    )
    assert "the window's grade" in refusal.reason
    assert "segment takes" in refusal.reason  # names γ at the segment lag, not a sill


def test_altitude_too_sparse_for_segments_is_unresolvable() -> None:
    # 0.5 Hz altitude: a 200 m segment holds at most 7 samples, fewer than a
    # segment needs, so no crest check is possible and the fit must say so
    # rather than skip it.
    samples = _flat_accelerations(np.random.default_rng(2), seconds=120.0, hz=0.5)
    refusal = _refused(fit_squat(samples, altitude_noise=_noise()), "grade_unresolvable")
    assert "too sparse" in refusal.reason


def test_altitude_noise_reference_refusals() -> None:
    parked = _parked(np.random.default_rng(1))
    _refused(measure_altitude_noise(replace(parked, altitude_m=None)), "inputs_missing")
    moving = replace(parked, speed_mps=np.full(len(parked), 3.0))
    _refused(measure_altitude_noise(moving), "not_stationary")
    short = _parked(np.random.default_rng(1), seconds=100.0)
    refusal = _refused(measure_altitude_noise(short), "too_few_samples")
    assert "99.9 s parked" in refusal.reason
    frozen = replace(parked, altitude_m=np.full(len(parked), 1500.0))
    _refused(measure_altitude_noise(frozen), "no_spread")
    with pytest.raises(ValidationError, match="zero variance"):
        AltitudeNoise(
            white_var_m2=0.0, correlated_var_m2=0.0, tau_s=1.0, reference_s=60.0, n_reference=10
        )


def test_squat_refuses_a_stretch_that_is_not_flat() -> None:
    refusal = _refused(
        fit_squat(
            _flat_accelerations(np.random.default_rng(6), grade=0.02), altitude_noise=_noise()
        ),
        "not_flat",
    )
    assert "grade" in refusal.reason


def test_squat_refuses_when_gps_altitude_cannot_resolve_the_grade() -> None:
    samples = _flat_accelerations(
        np.random.default_rng(7), seconds=8.0, altitude_noise_m=4.0, amplitude=2.5
    )
    _refused(fit_squat(samples, altitude_noise=_noise(white_sd=4.0)), "grade_unresolvable")


def test_squat_refuses_without_altitude_too_few_points_or_no_acceleration() -> None:
    samples = _flat_accelerations(np.random.default_rng(8))
    _refused(
        fit_squat(replace(samples, altitude_m=None), altitude_noise=_noise()), "inputs_missing"
    )
    short = MotionSamples(
        t_s=samples.t_s[:20],
        speed_mps=samples.speed_mps[:20],
        accel_mps2=samples.accel_mps2[:20],
        sf_lon_mps2=samples.sf_lon_mps2[:20],
        altitude_m=samples.altitude_m[:20] if samples.altitude_m is not None else None,
    )
    _refused(fit_squat(short, altitude_noise=_noise()), "too_few_samples")
    steady = _flat_accelerations(np.random.default_rng(9), amplitude=0.1)
    _refused(fit_squat(steady, altitude_noise=_noise()), "accel_span_too_small")


def test_squat_record_requires_conditions() -> None:
    fit = fit_squat(_flat_accelerations(np.random.default_rng(4)), altitude_noise=_noise())
    assert isinstance(fit, SquatFit)
    with pytest.raises(ValidationError):
        SquatConditions(payload_kg=127.0, road="x", date=dt.date(2026, 9, 26), sessions=["s"])  # type: ignore[call-arg]
    record = _squat_record()
    accel = np.array([G * 0.1])
    one = _loose_motion(t_s=[0.0], speed_mps=[20.0], accel_mps2=accel, sf_lon_mps2=accel)
    corrected = correct_specific_force(one, squat=record, pitch_offset=_stated_offset(0.0))
    assert not isinstance(corrected, DynoRefusal), corrected
    # 0.1 g of acceleration pitches 0.12 deg: sin(0.12 deg)·g removed.
    assert corrected[0] == pytest.approx(G * 0.1 - G * math.sin(math.radians(0.12)))
    both = correct_specific_force(one, squat=record, pitch_offset=_stated_offset(0.3))
    assert not isinstance(both, DynoRefusal), both
    assert both[0] == pytest.approx(G * 0.1 - G * math.sin(math.radians(0.42)))
    # The public correction takes the offset with its frame, and refuses
    # samples resolved in another one.
    framed = replace(one, imu_reference_id="frame-b")
    refusal = _refused(
        correct_specific_force(framed, squat=record, pitch_offset=_stated_offset(0.3)),
        "frame_mismatch",
    )
    assert "frame-b" in refusal.reason


# ---------------------------------------------------------------------------
# H07.5 — road load
# ---------------------------------------------------------------------------


def _coast_pair(driveline: Driveline, seed: int, *, runs: int = 1, **kwargs: float) -> list[Leg]:
    rng = np.random.default_rng(seed)
    legs: list[Leg] = []
    for _ in range(runs):
        legs.append(Leg("northbound", _coastdown(rng, driveline, heading_sign=+1.0, **kwargs)))
        legs.append(Leg("southbound", _coastdown(rng, driveline, heading_sign=-1.0, **kwargs)))
    return legs


#: A steady wind leaves C·w² in the pair's A (2.2 N at 2 m/s); B and C are exact.
WIND = 2.0
PAIR_A = TRUE_A + TRUE_C * WIND**2


def test_road_load_is_exact_without_noise(driveline: Driveline) -> None:
    # 0.5 % grade, 2 m/s wind (headwind northbound), 1.2 deg/g squat, 0.3 deg
    # IMU pitch offset; no measurement noise: the algebra alone.
    fit = fit_road_load(
        _coast_pair(driveline, 10, noise_f=0.0, noise_a=0.0),
        driveline=driveline,
        test_mass_kg=MASS,
        squat=_squat_record(),
    )
    assert isinstance(fit, RoadLoadFit), fit
    assert fit.a_n == pytest.approx(PAIR_A, rel=1e-3)
    assert fit.b_n_per_mps == pytest.approx(TRUE_B, rel=1e-3)
    assert fit.c_n_per_mps2 == pytest.approx(TRUE_C, rel=1e-3)


@pytest.mark.parametrize("seed", [11, 12, 13, 14, 15])
def test_road_load_recovers_known_coefficients_within_5_percent(
    driveline: Driveline, seed: int
) -> None:
    # Same road, wind, squat and offset; three coast-downs per heading, with
    # 0.005 m/s^2 specific-force and 0.003 m/s^2 acceleration noise per 10 Hz
    # sample (H07.3's smoothed inputs). B's scatter is ~1.9 % here, so 5 % is
    # ~2.7 sigma on the weakest coefficient.
    fit = fit_road_load(
        _coast_pair(driveline, seed, runs=3, noise_f=0.005, noise_a=0.003),
        driveline=driveline,
        test_mass_kg=MASS,
        squat=_squat_record(),
    )
    assert isinstance(fit, RoadLoadFit), fit
    assert fit.a_n == pytest.approx(TRUE_A, rel=0.05)
    assert fit.b_n_per_mps == pytest.approx(TRUE_B, rel=0.05)
    assert fit.c_n_per_mps2 == pytest.approx(TRUE_C, rel=0.05)
    speeds = np.linspace(20 / 3.6, 110 / 3.6, 10)
    truth = TRUE_A + TRUE_B * speeds + TRUE_C * speeds**2
    np.testing.assert_allclose(fit.force(speeds), truth, rtol=0.02)
    assert fit.directions == "pair"
    assert fit.squat_corrected is True
    assert fit.inertia == "estimated"
    # The IMU's 0.3 deg pitch offset was measured from the pair and removed:
    # left in, it is m·g·sin(0.3 deg) = 118.7 N of phantom rolling resistance.
    assert fit.imu_pitch_offset_deg == pytest.approx(TRUE_MOUNT_OFFSET_DEG, abs=0.02)
    # Each direction alone carries the wind in B (±2·C·w = ±2.2 N/(m/s)); the pair cancels it.
    north, south = fit.per_direction["northbound"], fit.per_direction["southbound"]
    assert north.b_n_per_mps - south.b_n_per_mps == pytest.approx(4 * TRUE_C * WIND, rel=0.15)
    grade_deg = math.degrees(math.atan(0.005))
    assert north.static_pitch_deg - south.static_pitch_deg == pytest.approx(2 * grade_deg, abs=0.03)


def test_road_load_standard_errors_cover_the_truth_at_higher_noise(
    driveline: Driveline,
) -> None:
    # One pair at 0.03 m/s^2 per 10 Hz sample: B scatters ~19 %, and the
    # reported se must say so rather than the point estimate implying 5 %.
    for seed in range(30, 40):
        fit = fit_road_load(
            _coast_pair(driveline, seed),
            driveline=driveline,
            test_mass_kg=MASS,
            squat=_squat_record(),
        )
        assert isinstance(fit, RoadLoadFit), fit
        assert abs(fit.a_n - PAIR_A) <= 3.5 * fit.a_se_n
        assert abs(fit.b_n_per_mps - TRUE_B) <= 3.5 * fit.b_se_n_per_mps
        assert abs(fit.c_n_per_mps2 - TRUE_C) <= 3.5 * fit.c_se_n_per_mps2
        assert fit.b_se_n_per_mps / TRUE_B > 0.08  # B is not resolved to 5 % by one pair


def test_road_load_standard_errors_are_not_inflated(driveline: Driveline) -> None:
    # Reported se against the seed-to-seed scatter of 20 independent pairs
    # (measured ratio 0.83-0.85 for A, B and C): a doubled se would read ~1.7.
    fits = [
        fit_road_load(
            _coast_pair(driveline, seed),
            driveline=driveline,
            test_mass_kg=MASS,
            squat=_squat_record(),
        )
        for seed in range(30, 50)
    ]
    assert all(isinstance(f, RoadLoadFit) for f in fits)
    for value, se in (
        ("a_n", "a_se_n"),
        ("b_n_per_mps", "b_se_n_per_mps"),
        ("c_n_per_mps2", "c_se_n_per_mps2"),
    ):
        scatter = np.std([getattr(f, value) for f in fits], ddof=1)
        reported = np.mean([getattr(f, se) for f in fits])
        assert 0.6 < reported / scatter < 1.4, (value, reported, scatter)


def test_pitch_offset_separates_from_grade_only_with_both_directions(
    driveline: Driveline,
) -> None:
    pair = _coast_pair(driveline, 16, grade=0.01, v_end=8.0)
    offset = estimate_pitch_offset(pair, squat=_squat_record())
    assert isinstance(offset, PitchOffset), offset
    # Known answer 0.3 deg, on a 1 % grade that is 0.57 deg each way.
    assert offset.deg == pytest.approx(TRUE_MOUNT_OFFSET_DEG, abs=0.02)
    assert offset.heading_pitch_deg["northbound"] == pytest.approx(
        TRUE_MOUNT_OFFSET_DEG + math.degrees(math.atan(0.01)), abs=0.02
    )
    _refused(estimate_pitch_offset(pair[:1], squat=None), "single_direction")
    _refused(estimate_pitch_offset([], squat=None), "inputs_missing")


def _in_frame(legs: list[Leg], frame: str | None) -> list[Leg]:
    return [Leg(leg.heading, replace(leg.samples, imu_reference_id=frame)) for leg in legs]


def test_pitch_offset_is_bound_to_the_frame_it_was_measured_in(driveline: Driveline) -> None:
    # A pitch offset is a property of one IMU rotation (imu_frame.json's
    # reference_id). It carries that id, and nothing applies it to samples
    # resolved in another frame.
    pair = _in_frame(_coast_pair(driveline, 16), "frame-a")
    offset = estimate_pitch_offset(pair, squat=_squat_record())
    assert isinstance(offset, PitchOffset), offset
    assert offset.reference_id == "frame-a"
    mixed = [pair[0], *_in_frame(pair[1:], "frame-b")]
    refusal = _refused(estimate_pitch_offset(mixed, squat=None), "frame_mismatch")
    assert "frame-a" in refusal.reason and "frame-b" in refusal.reason
    fit = fit_road_load(pair, driveline=driveline, test_mass_kg=MASS, squat=_squat_record())
    assert isinstance(fit, RoadLoadFit), fit
    assert fit.imu_reference_id == "frame-a"
    _refused(
        fit_road_load(
            mixed,
            driveline=driveline,
            test_mass_kg=MASS,
            squat=None,
            allow_single_direction=True,
        ),
        "frame_mismatch",
    )
    # The two-gear fit applies the offset: pulls from another frame are refused,
    # pulls from the same frame are fitted and record it.
    rng = np.random.default_rng(3)
    pulls = [_pull(rng, driveline, 2), _pull(rng, driveline, 3)]
    framed = [GearPull(p.gear, replace(p.samples, imu_reference_id="frame-a")) for p in pulls]
    kwargs = _inertia_kwargs(driveline)
    refusal = _refused(fit_engine_inertia(pulls, pitch_offset=offset, **kwargs), "frame_mismatch")
    assert "frame-a" in refusal.reason
    fitted = fit_engine_inertia(framed, pitch_offset=offset, **kwargs)
    assert isinstance(fitted, EngineInertiaFit), fitted
    assert fitted.imu_reference_id == "frame-a"
    assert (
        fitted.to_record(
            InertiaConditions(
                road="r",
                date=dt.date(2026, 9, 26),
                sessions=["s"],
                payload_kg=127.0,
                tune_revision="ZS44A-20260920",
            )
        ).imu_reference_id
        == "frame-a"
    )


def test_road_load_single_direction_is_refused_unless_asked_and_then_labelled(
    driveline: Driveline,
) -> None:
    one_way = _coast_pair(driveline, 12)[:1]
    _refused(
        fit_road_load(one_way, driveline=driveline, test_mass_kg=MASS, squat=None),
        "single_direction",
    )
    fit = fit_road_load(
        one_way, driveline=driveline, test_mass_kg=MASS, squat=None, allow_single_direction=True
    )
    assert isinstance(fit, RoadLoadFit)
    assert fit.directions == "single"
    assert fit.squat_corrected is False
    assert fit.imu_pitch_offset_deg is None
    record = fit.to_record(
        RoadLoadConditions(
            road="r",
            date=dt.date(2026, 9, 26),
            sessions=["s"],
            payload_kg=127.0,
            tire_pressure_front_kpa=240.0,
            tire_pressure_rear_kpa=240.0,
            ambient_temp_c=20.0,
        )
    )
    with pytest.raises(ValidationError, match="coast-down pair"):
        DynoProfile(road_load=record)


def test_road_load_refuses_degenerate_coastdowns(driveline: Driveline) -> None:
    pair = _coast_pair(driveline, 13)
    three = [*pair, Leg("eastbound", pair[0].samples)]
    _refused(
        fit_road_load(three, driveline=driveline, test_mass_kg=MASS, squat=None),
        "too_many_directions",
    )
    narrow = _coast_pair(driveline, 14, v0=25.0, v_end=20.0)
    refusal = _refused(
        fit_road_load(narrow, driveline=driveline, test_mass_kg=MASS, squat=None),
        "speed_span_too_small",
    )
    assert "km/h" in refusal.reason
    tiny = [Leg(c.heading, _slice(c.samples, 30)) for c in _coast_pair(driveline, 15)]
    _refused(
        fit_road_load(tiny, driveline=driveline, test_mass_kg=MASS, squat=None),
        "too_few_samples",
    )
    _refused(
        fit_road_load([], driveline=driveline, test_mass_kg=MASS, squat=None), "inputs_missing"
    )
    # Specific force of the wrong sign (e.g. an engaged-clutch pull fed in as a
    # coast): negative drag is not a road load.
    flipped = [Leg(c.heading, replace(c.samples, sf_lon_mps2=-c.samples.sf_lon_mps2)) for c in pair]
    _refused(
        fit_road_load(flipped, driveline=driveline, test_mass_kg=MASS, squat=None),
        "non_physical",
    )


def _slice(samples: MotionSamples, n: int) -> MotionSamples:
    return MotionSamples(
        t_s=samples.t_s[:n],
        speed_mps=samples.speed_mps[:n],
        accel_mps2=samples.accel_mps2[:n],
        sf_lon_mps2=samples.sf_lon_mps2[:n],
    )


# ---------------------------------------------------------------------------
# H07.6 — rotating inertia
# ---------------------------------------------------------------------------


def test_driveline_from_vehicle_names_every_missing_fact() -> None:
    refusal = _refused(Driveline.from_vehicle(VehicleParameters(), None), "not_configured")
    for fact in (
        "final_drive",
        "gearbox_ratios",
        "tire_loaded_circumference_m",
        "wheel_tire_assembly_kg",
        "inertia",
    ):
        assert fact in refusal.reason


def test_driveline_effective_mass_from_stated_estimates(driveline: Driveline) -> None:
    r = 2.609 / (2 * math.pi)
    wheel_side = 4 * 0.7 * 40.3 * r**2 + 1.0
    assert driveline.wheel_side_kg_m2 == pytest.approx(wheel_side)
    assert driveline.inertia == "estimated"
    assert driveline.neutral_effective_mass(MASS) == pytest.approx(MASS + wheel_side / r**2)
    third = 1.743 * 4.08
    # The engine's share is η·I_e·N²/r²: its inertia torque comes off at the
    # crank, upstream of the 15 % driveline loss.
    assert driveline.efficiency == pytest.approx(ETA)
    assert driveline.effective_mass(MASS, 3) == pytest.approx(
        MASS + wheel_side / r**2 + ETA * 0.25 * third**2 / r**2
    )
    with pytest.raises(ValueError, match="gear 7"):
        driveline.overall_ratio(7)


def _stated_offset(deg: float = TRUE_MOUNT_OFFSET_DEG) -> PitchOffset:
    """A known pitch offset in the synthetic data's frame (``reference_id`` None)."""
    return PitchOffset(deg=deg, heading_pitch_deg={}, n_samples=0, reference_id=None)


def _inertia_kwargs(driveline: Driveline) -> dict[str, Any]:
    return {
        "driveline": driveline,
        "test_mass_kg": MASS,
        "road_load": _true_road_load(),
        "squat": _squat_record(),
    }


def test_two_gear_fit_is_exact_without_noise(driveline: Driveline) -> None:
    pulls = [_pull(_Silent(), driveline, 2), _pull(_Silent(), driveline, 3)]  # type: ignore[arg-type]
    fit = fit_engine_inertia(pulls, pitch_offset=_stated_offset(), **_inertia_kwargs(driveline))
    assert isinstance(fit, EngineInertiaFit), fit
    assert fit.engine_kg_m2 == pytest.approx(TRUE_ENGINE_INERTIA, rel=0.005)


class _Silent:
    def normal(self, loc: float, scale: float, size: int) -> np.ndarray:
        return np.zeros(size)


def test_inertia_convention_is_upstream_of_driveline_losses(driveline: Driveline) -> None:
    # The generator takes the inertia torque off at the crank and applies η =
    # 0.85 to what is left (the physical arrangement). The fit measures η·I_e
    # and divides by the driveline's η: the crank inertia comes back, and the
    # product is what m_eff uses.
    pulls = [_pull(_Silent(), driveline, 2), _pull(_Silent(), driveline, 3)]  # type: ignore[arg-type]
    kwargs = {**_inertia_kwargs(driveline), "pitch_offset": _stated_offset()}
    fit = fit_engine_inertia(pulls, **kwargs)
    assert isinstance(fit, EngineInertiaFit), fit
    assert fit.reflected_kg_m2 == pytest.approx(ETA * TRUE_ENGINE_INERTIA, rel=0.005)
    assert fit.driveline_efficiency == pytest.approx(ETA)
    assert fit.engine_kg_m2 == pytest.approx(TRUE_ENGINE_INERTIA, rel=0.005)
    # A driveline that assumes no loss reads the reflected product as the crank
    # inertia (0.153 for 0.18): the assumed η is part of the answer, recorded.
    lossless = replace(driveline, efficiency=1.0)
    naive = fit_engine_inertia(pulls, **{**kwargs, "driveline": lossless})
    assert isinstance(naive, EngineInertiaFit)
    assert naive.engine_kg_m2 == pytest.approx(ETA * TRUE_ENGINE_INERTIA, rel=0.005)
    assert naive.reflected_kg_m2 == pytest.approx(fit.reflected_kg_m2)


def test_inertia_record_holds_one_convention(driveline: Driveline) -> None:
    pulls = [_pull(_Silent(), driveline, 2), _pull(_Silent(), driveline, 3)]  # type: ignore[arg-type]
    fit = fit_engine_inertia(pulls, pitch_offset=_stated_offset(), **_inertia_kwargs(driveline))
    assert isinstance(fit, EngineInertiaFit)
    conditions = InertiaConditions(
        road="r",
        date=dt.date(2026, 9, 26),
        sessions=["s"],
        payload_kg=127.0,
        tune_revision="ZS44A-20260920",
    )
    record = fit.to_record(conditions)
    raw = record.model_dump()
    with pytest.raises(ValidationError, match="reflected_kg_m2 / driveline_efficiency"):
        EngineInertiaRecord.model_validate({**raw, "engine_kg_m2": raw["reflected_kg_m2"]})
    with pytest.raises(ValidationError, match="another convention"):
        EngineInertiaRecord.model_validate({**raw, "assumption": "eta applied at the wheels"})
    # A profile whose loss fraction has changed since the measurement is refused,
    # naming the measured product, rather than silently mixing two η.
    changed = PARAMS.model_copy(update={"drivetrain_loss_fraction": 0.10})
    refusal = _refused(
        Driveline.from_vehicle(changed, INERTIA.model_copy(update={"measured_engine": record})),
        "conditions_mismatch",
    )
    assert "reflected_kg_m2" in refusal.reason


@pytest.mark.parametrize("seed", [21, 22, 23])
def test_two_gear_pulls_recover_a_known_engine_inertia(driveline: Driveline, seed: int) -> None:
    # Three pulls in each of 2nd and 3rd, 2,500-5,500 rpm on a 1 % grade, at
    # 100 Hz with 0.03 m/s^2 specific-force noise; one pull per gear scatters
    # ~3 %, three ~1.7 %.
    rng = np.random.default_rng(seed)
    pulls = [_pull(rng, driveline, gear) for gear in (2, 3) for _ in range(3)]
    # The session's pitch offset, from its coast-down pair (same IMU mounting).
    offset = estimate_pitch_offset(_coast_pair(driveline, seed + 100), squat=_squat_record())
    assert isinstance(offset, PitchOffset)
    kwargs = _inertia_kwargs(driveline)
    fit = fit_engine_inertia(pulls, pitch_offset=offset, **kwargs)
    assert isinstance(fit, EngineInertiaFit), fit
    # Known answer 0.18 kg m^2 (the driveline's estimate is 0.25) within 5 %.
    assert fit.engine_kg_m2 == pytest.approx(TRUE_ENGINE_INERTIA, rel=0.05)
    assert abs(fit.engine_kg_m2 - TRUE_ENGINE_INERTIA) <= 3.5 * fit.engine_kg_m2_se
    assert fit.imu_pitch_offset_deg == offset.deg
    # Why the offset is a required argument: left in, 0.3 deg moves it ~44 %.
    blind = fit_engine_inertia(pulls, pitch_offset=_stated_offset(0.0), **kwargs)
    assert isinstance(blind, EngineInertiaFit)
    assert abs(blind.engine_kg_m2 / TRUE_ENGINE_INERTIA - 1.0) > 0.15
    assert fit.gears == (2, 3)
    assert fit.n_bins >= 20
    record = fit.to_record(
        InertiaConditions(
            road="r",
            date=dt.date(2026, 9, 26),
            sessions=["s"],
            payload_kg=127.0,
            tune_revision="ZS44A-20260920",
        )
    )
    measured = Driveline.from_vehicle(
        PARAMS, INERTIA.model_copy(update={"measured_engine": record})
    )
    assert isinstance(measured, Driveline)
    assert measured.inertia == "measured_engine"
    assert measured.engine_side_kg_m2 == pytest.approx(fit.engine_kg_m2)


def test_two_gear_standard_error_is_not_inflated(driveline: Driveline) -> None:
    # One pull per gear, 12 seeds: reported se against the seed-to-seed
    # scatter (measured ratio 1.19); a doubled se would read ~2.4.
    fits = []
    for seed in range(12):
        rng = np.random.default_rng(seed)
        pulls = [_pull(rng, driveline, 2), _pull(rng, driveline, 3)]
        fit = fit_engine_inertia(pulls, pitch_offset=_stated_offset(), **_inertia_kwargs(driveline))
        assert isinstance(fit, EngineInertiaFit), fit
        fits.append(fit)
    scatter = np.std([f.engine_kg_m2 for f in fits], ddof=1)
    reported = np.mean([f.engine_kg_m2_se for f in fits])
    assert 0.7 < reported / scatter < 1.7


def test_two_gear_fit_refuses_degenerate_pulls(driveline: Driveline) -> None:
    rng = np.random.default_rng(22)
    second = _pull(rng, driveline, 2)
    third = _pull(rng, driveline, 3)
    kwargs = {**_inertia_kwargs(driveline), "pitch_offset": _stated_offset()}
    _refused(fit_engine_inertia([], **kwargs), "inputs_missing")
    _refused(fit_engine_inertia([second, second], **kwargs), "same_gear")
    no_rpm = GearPull(3, replace(third.samples, rpm=None))
    _refused(fit_engine_inertia([second, no_rpm], **kwargs), "inputs_missing")
    high = _pull(rng, driveline, 3, rpm_start=4800.0, rpm_end=5500.0)
    low = _pull(rng, driveline, 2, rpm_start=2500.0, rpm_end=3500.0)
    _refused(fit_engine_inertia([low, high], **kwargs), "rpm_overlap_too_small")
    # 5th and 6th accelerate the engine at nearly the same (low) rate.
    fifth = _pull(rng, driveline, 5, rpm_start=1500.0, rpm_end=2600.0)
    sixth = _pull(rng, driveline, 6, rpm_start=1500.0, rpm_end=2600.0)
    refusal = _refused(fit_engine_inertia([fifth, sixth], **kwargs), "alpha_difference_too_small")
    assert "rad/s^2" in refusal.reason
    _refused(fit_engine_inertia([second, third, fifth], **kwargs), "invalid_input")
    _refused(fit_engine_inertia([second, GearPull(9, third.samples)], **kwargs), "invalid_input")
    short = GearPull(3, _slice(third.samples, 30))
    _refused(fit_engine_inertia([second, short], **kwargs), "too_few_samples")


def test_xterra_profile_builds_a_driveline_with_estimated_inertia() -> None:
    registry = read_registry_file(REPO / "profiles.yaml")
    (xterra,) = [p for p in registry if p.slug == "nissan-xterra-2008"]
    profile = load_dyno_profile(REPO / "data" / "nissan-xterra-2008" / DYNO_FILENAME)
    assert profile is not None
    built = Driveline.from_vehicle(xterra.parameters, profile.inertia)
    assert isinstance(built, Driveline)
    assert built.inertia == "estimated"
    # The file's own evidence: ~119 kg wheel side + ~65 kg engine side (η·I_e,
    # η = 0.85 from the profile's drivetrain_loss_fraction) in 3rd, at the
    # 2.559 m rolling circumference (H07.11; ~62 kg at the old 2.609 m).
    test_mass = xterra.parameters.mass_kg + (xterra.parameters.usual_payload_kg or 0.0)
    rotating = built.effective_mass(test_mass, 3) - test_mass
    assert rotating == pytest.approx(184.0, abs=2.0)


# ---------------------------------------------------------------------------
# H07.8 — noise floor
# ---------------------------------------------------------------------------


def _conditions(**overrides: object) -> RunConditions:
    base: dict[str, object] = {
        "gear": 3,
        "direction": "pair",
        "tune_revision": "ZS44A-20260920",
        "induction": "na",
        "baro_kpa": 80.1,
        "intake_temp_c": 25.0,
        "ambient_temp_c": 18.0,
        "road": "flat straight",
        "date": dt.date(2026, 9, 27),
        "payload_kg": 127.0,
        "sessions": ["_capture-tuning-2026-09-27T150000"],
    }
    base.update(overrides)
    return RunConditions.model_validate(base)


RPM = np.arange(2500.0, 6001.0, 25.0)


def _curve(offset_kw: float = 0.0, **overrides: object) -> DynoCurve:
    power = 60.0 + 30.0 * np.sin((RPM - 2500.0) / 3500.0 * math.pi * 0.8)
    return DynoCurve("wheel_power_kw", RPM, power + offset_kw, _conditions(**overrides))


def _floor() -> DynoNoiseFloor:
    floor = record_dyno_floor(
        [_curve(0.0), _curve(1.0), _curve(-1.0)], band_edges_rpm=[2500, 3500, 4500, 5500]
    )
    assert isinstance(floor, DynoNoiseFloor), floor
    return floor


def test_floor_records_per_band_spread_of_three_pull_pairs() -> None:
    floor = _floor()
    assert floor.n_pull_pairs == 3
    assert floor.unit == "kW"
    assert [(b.rpm_min, b.rpm_max) for b in floor.bands] == [
        (2500, 3500),
        (3500, 4500),
        (4500, 5500),
    ]
    for band in floor.bands:
        assert band.sd == pytest.approx(1.0)  # offsets 0, +1, -1: sample sd 1.0
        assert band.sd_of_difference == pytest.approx(math.sqrt(2.0))
    assert floor.conditions.tune_revision == "ZS44A-20260920"


def test_floor_refuses_too_few_pairs_singles_and_mixed_days_or_roms() -> None:
    _refused(
        record_dyno_floor([_curve(), _curve(1.0)], band_edges_rpm=[2500, 3500]),
        "too_few_pull_pairs",
    )
    _refused(
        record_dyno_floor(
            [_curve(), _curve(1.0), _curve(-1.0, direction="single")], band_edges_rpm=[2500, 3500]
        ),
        "direction_mismatch",
    )
    refusal = _refused(
        record_dyno_floor(
            [_curve(), _curve(1.0), _curve(-1.0, tune_revision="ZS44A-20261001")],
            band_edges_rpm=[2500, 3500],
        ),
        "not_same_day_same_rom",
    )
    assert "tune_revision" in refusal.reason
    _refused(
        record_dyno_floor(
            [_curve(), _curve(1.0), _curve(-1.0, date=dt.date(2026, 9, 28))],
            band_edges_rpm=[2500, 3500],
        ),
        "not_same_day_same_rom",
    )
    _refused(record_dyno_floor([_curve()] * 3, band_edges_rpm=[2500, 3500]), "no_spread")
    _refused(
        record_dyno_floor([_curve(), _curve(1.0), _curve(-1.0)], band_edges_rpm=[7000, 8000]),
        "no_overlap",
    )


def test_dyno_loader_refuses_malformed_files(tmp_path: Path) -> None:
    path = tmp_path / DYNO_FILENAME
    path.write_text("")
    with pytest.raises(ValueError, match="empty"):
        load_dyno_profile(path)
    path.write_text("- 1\n- 2\n")
    with pytest.raises(ValueError, match="not a mapping"):
        load_dyno_profile(path)
    path.write_text("schema_version: 1\nnoise_floor: []\n")  # misspelt key
    with pytest.raises(ValidationError, match="noise_floor"):
        load_dyno_profile(path)
    path.write_text("schema_version: 2\n")
    with pytest.raises(ValidationError, match="schema_version"):
        load_dyno_profile(path)
    path.write_text("schema_version: 1\ninertia: {road_wheels: 4\n")
    with pytest.raises(yaml.YAMLError):
        load_dyno_profile(path)
    raw = DynoProfile(noise_floors=[_floor()]).model_dump(mode="json")
    raw["noise_floors"][0]["bands"][0]["sd"] = -1.0
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError, match="sd"):
        load_dyno_profile(path)


def test_floor_loader_round_trips_and_the_schema_requires_conditions(tmp_path: Path) -> None:
    path = tmp_path / DYNO_FILENAME
    assert load_dyno_profile(path) is None  # no file: no floor, never an invented one
    save_dyno_profile(path, DynoProfile(inertia=INERTIA, noise_floors=[_floor()]))
    loaded = load_dyno_profile(path)
    assert loaded is not None
    assert loaded.noise_floors == [_floor()]
    raw = _floor().model_dump(mode="json")
    del raw["conditions"]
    with pytest.raises(ValidationError, match="conditions"):
        DynoNoiseFloor.model_validate(raw)
    raw = _floor().model_dump(mode="json")
    raw["n_pull_pairs"] = 2
    with pytest.raises(ValidationError, match="n_pull_pairs"):
        DynoNoiseFloor.model_validate(raw)
    with pytest.raises(ValidationError, match="same regime"):
        DynoProfile(noise_floors=[_floor(), _floor()])


def test_committed_xterra_dyno_file_loads_and_has_no_floor_yet() -> None:
    profile = load_dyno_profile(REPO / "data" / "nissan-xterra-2008" / DYNO_FILENAME)
    assert profile is not None
    assert profile.inertia is not None and profile.inertia.measured_engine is None
    _refused(profile.noise_floor_for("wheel_power_kw", _conditions()), "no_floor")


def test_profile_floor_lookup_names_the_recorded_regimes() -> None:
    profile = DynoProfile(noise_floors=[_floor()])
    assert profile.noise_floor_for("wheel_power_kw", _conditions()) == _floor()
    refusal = _refused(
        profile.noise_floor_for("wheel_power_kw", _conditions(gear=2)), "conditions_mismatch"
    )
    assert "gear 3" in refusal.reason and "gear 2" in refusal.reason


def test_compare_refuses_to_call_a_difference_inside_the_floor() -> None:
    floor = _floor()  # sd_of_difference = 1.414 kW; 2 sigma = 2.83 kW
    inside = compare_runs(_curve(0.0), _curve(2.0), floor)
    assert isinstance(inside, RunComparison), inside
    assert not inside.differs
    assert {b.verdict for b in inside.bands} == {"within_floor"}
    outside = compare_runs(_curve(0.0), _curve(4.0), floor)
    assert isinstance(outside, RunComparison)
    assert outside.differs
    assert all(b.verdict == "differs" for b in outside.bands)
    assert outside.bands[0].sigma == pytest.approx(4.0 / math.sqrt(2.0))


def test_compare_refusals_name_their_cause() -> None:
    floor = _floor()
    _refused(compare_runs(_curve(), _curve(5.0), None), "no_floor")
    _refused(compare_runs(_curve(), _curve(5.0, gear=2), floor), "conditions_mismatch")
    _refused(compare_runs(_curve(), _curve(5.0, induction="boosted"), floor), "conditions_mismatch")
    _refused(compare_runs(_curve(), _curve(5.0, direction="single"), floor), "direction_mismatch")
    torque = DynoCurve("wheel_torque_nm", RPM, np.full(len(RPM), 300.0), _conditions())
    _refused(compare_runs(_curve(), torque, floor), "conditions_mismatch")
    high = DynoCurve(
        "wheel_power_kw", np.array([5600.0, 6000.0]), np.array([80.0, 82.0]), _conditions()
    )
    _refused(compare_runs(high, high, floor), "no_overlap")


def test_compare_marks_bands_the_runs_do_not_cover() -> None:
    floor = _floor()
    partial_rpm = np.arange(2500.0, 3800.0, 25.0)
    partial = DynoCurve(
        "wheel_power_kw", partial_rpm, np.full(len(partial_rpm), 70.0), _conditions()
    )
    result = compare_runs(partial, partial, floor)
    assert isinstance(result, RunComparison)
    assert [b.verdict for b in result.bands] == ["within_floor", "within_floor", "not_covered"]
