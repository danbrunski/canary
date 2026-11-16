"""The dyno's synthetic vehicle: parameters, trajectories, noise and pulls (R30.2).

Moved out of the ``tests/test_*.py`` files that provided them, bodies unchanged; private helpers
got public names."""

from __future__ import annotations

import datetime as dt
import functools
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from src.analysis.dyno import (
    AltitudeNoise,
    DirectionFit,
    Driveline,
    DynoCurve,
    DynoNoiseFloor,
    DynoPower,
    DynoRefusal,
    GearPull,
    InertiaData,
    Leg,
    MotionSamples,
    PitchOffset,
    PowerPull,
    RoadLoadCoefficients,
    RoadLoadConditions,
    RoadLoadFit,
    RunConditions,
    SquatCoefficient,
    SquatConditions,
    SquatFit,
    compute_power,
    measure_altitude_noise,
    record_dyno_floor,
)
from src.config import VehicleParameters
from src.signals.committees import STANDARD_GRAVITY as G

# Known answers. None is read from a profile; the computation must recover the
# torque curve, not echo it.
MASS = 2311.0


A, B, C = 180.0, 5.0, 0.55


WIND = 2.0  # m/s, headwind northbound


SQUAT_DEG_PER_G = 1.5


MOUNT_OFFSET_DEG = 0.3


ETA = 0.85


ENGINE_INERTIA = 0.18  # kg m^2 at the crank


WHEEL_MASS = 40.3


TIRE_CIRCUMFERENCE = 2.609


RADIUS = TIRE_CIRCUMFERENCE / (2 * math.pi)


GEARBOX = (4.368, 2.518, 1.743, 1.283, 1.0, 0.769)


FINAL_DRIVE = 4.08


#: Written out independently of Driveline: 4 wheels at 0.7 m r^2, plus 1.0
#: kg m^2 of output driveline, all downstream of the losses.
WHEEL_SIDE_KG_M2 = 4 * 0.7 * WHEEL_MASS * RADIUS**2 + 1.0


GEAR = 3


PARAMS = VehicleParameters(
    mass_kg=2184.4,
    wheel_tire_assembly_kg=WHEEL_MASS,
    final_drive=FINAL_DRIVE,
    gearbox_ratios=GEARBOX,
    tire_loaded_circumference_m=TIRE_CIRCUMFERENCE,
    driveline_evidence="synthetic test vehicle",
    drivetrain_loss_fraction=1.0 - ETA,
)


def torque(rpm: np.ndarray | float) -> np.ndarray:
    """The engine's WOT torque curve (N m): 280 at 2,000, peak 320 at 4,000."""
    return 280.0 + 40.0 * (1.0 - ((np.asarray(rpm, dtype=float) - 4000.0) / 2000.0) ** 2)


def road_grade(x_m: np.ndarray | float) -> np.ndarray:
    """Grade (fraction, uphill northbound) at distance x along the road."""
    return 0.004 + 0.003 * np.sin(2 * math.pi * np.asarray(x_m, dtype=float) / 600.0)


def dyno_imu(accel: np.ndarray, theta: np.ndarray, squat: float, offset_deg: float) -> np.ndarray:
    """What a longitudinal accelerometer pitched by φ = squat·a/g + μ against
    the road reads on a grade θ (nose-up positive): a·cos φ + g·sin(θ + φ)."""
    phi = np.radians(squat * accel / G + offset_deg)
    reading: np.ndarray = accel * np.cos(phi) + G * np.sin(theta + phi)
    return reading


class DynoNoise:
    def __init__(self, rng: np.random.Generator | None) -> None:
        self.rng = rng

    def __call__(self, sd: float, n: int) -> np.ndarray:
        return np.zeros(n) if self.rng is None else self.rng.normal(0.0, sd, n)


@dataclass(frozen=True)
class DynoTrajectory:
    """A pull at the simulation's 1 kHz: time, speed, acceleration, rpm, grade angle."""

    heading: str
    gear: int
    t: np.ndarray
    v: np.ndarray
    a: np.ndarray
    rpm: np.ndarray
    theta: np.ndarray

    def sf(self, at: np.ndarray) -> np.ndarray:
        accel = np.interp(at, self.t, self.a)
        return dyno_imu(accel, np.interp(at, self.t, self.theta), SQUAT_DEG_PER_G, MOUNT_OFFSET_DEG)


def dyno_trajectory(
    heading: str,
    *,
    gear: int = GEAR,
    rpm_start: float = 2450.0,
    rpm_end: float = 5500.0,
    grade: Callable[[np.ndarray | float], np.ndarray] = road_grade,
    x_start: float | None = None,
) -> DynoTrajectory:
    """A WOT pull from a known torque curve. Northbound runs up the road from
    x = 0; southbound runs back down from x = 350 m, seeing the grade negated
    and the wind behind it."""
    sign = 1.0 if heading == "northbound" else -1.0
    ratio = GEARBOX[gear - 1] * FINAL_DRIVE
    # The crank's inertia torque comes off before the lossy gears: the wheels
    # see ETA·N/r·(T − I_e·α), α = N·a/r. Downstream inertia sees no loss.
    m_total = MASS + WHEEL_SIDE_KG_M2 / RADIUS**2 + ETA * ENGINE_INERTIA * ratio**2 / RADIUS**2
    dt_sim = 0.001
    x = (0.0 if sign > 0 else 350.0) if x_start is None else x_start
    v = rpm_start * 2 * math.pi / 60 * RADIUS / ratio
    rows: list[tuple[float, ...]] = []
    step = 0
    while True:
        rpm = v * ratio / RADIUS * 60 / (2 * math.pi)
        if rpm > rpm_end:
            break
        theta = math.atan(sign * float(grade(x)))
        air = v + sign * WIND
        load = A + B * v + C * air * abs(air)
        accel = (ETA * ratio / RADIUS * float(torque(rpm)) - MASS * G * math.sin(theta) - load) / (
            m_total
        )
        rows.append((step * dt_sim, v, accel, rpm, theta))
        assert step * dt_sim < 60.0, "unreachable band"
        v += accel * dt_sim
        x += sign * v * dt_sim
        step += 1
    t, speed, accel, rpm_, theta_ = np.array(rows).T
    return DynoTrajectory(heading, gear, t, speed, accel, rpm_, theta_)


def dyno_pull(
    heading: str,
    *,
    noise: DynoNoise,
    hz: float = 100.0,
    **trajectory: object,
) -> PowerPull:
    """The trajectory sampled at ``hz`` with measurement noise, as MotionSamples."""
    path = dyno_trajectory(heading, **trajectory)  # type: ignore[arg-type]
    every = round(1000.0 / hz)
    t, speed, accel, rpm = path.t[::every], path.v[::every], path.a[::every], path.rpm[::every]
    n = len(t)
    return PowerPull(
        heading=heading,
        gear=path.gear,
        samples=MotionSamples(
            t_s=t,
            speed_mps=speed + noise(0.01, n),
            accel_mps2=accel + noise(0.02, n),
            sf_lon_mps2=path.sf(t) + noise(0.03, n),
            rpm=rpm + noise(5.0, n),
        ),
        demand=4.10 + noise(0.005, n),
    )


SILENT = DynoNoise(None)


PAIR = [dyno_pull("northbound", noise=SILENT), dyno_pull("southbound", noise=SILENT)]


@dataclass(frozen=True)
class Squat:
    deg_per_g: float = SQUAT_DEG_PER_G


class _Wot:
    threshold_value = 4.00
    rpm_floor = 2500.0


def dyno_driveline(engine_side: float = ENGINE_INERTIA) -> Driveline:
    built = Driveline.from_vehicle(
        PARAMS,
        InertiaData(
            road_wheels=4,
            wheel_shape_factor=0.7,
            driveline_output_kg_m2=1.0,
            engine_side_kg_m2=engine_side,
            evidence="synthetic",
        ),
    )
    assert isinstance(built, Driveline)
    return built


def road_conditions(**overrides: object) -> RoadLoadConditions:
    """The coast-downs' conditions: the same air, load and tires as :func:`_conditions`."""
    base: dict[str, object] = {
        "road": "synthetic road",
        "date": dt.date(2026, 9, 26),
        "sessions": ["synthetic"],
        "payload_kg": 127.0,
        "tire_pressure_front_kpa": 240.0,
        "tire_pressure_rear_kpa": 240.0,
        "ambient_temp_c": 22.0,
        "baro_kpa": 80.0,
    }
    base.update(overrides)
    return RoadLoadConditions.model_validate(base)


def pair_road_load(**conditions: object) -> RoadLoadCoefficients:
    """What a coast-down pair measures in this wind: C·w² lands in A."""
    return pair_road_load_fit().to_record(road_conditions(**conditions))


def pair_road_load_fit() -> RoadLoadFit:
    return RoadLoadFit(
        a_n=A + C * WIND**2,
        b_n_per_mps=B,
        c_n_per_mps2=C,
        a_se_n=0.0,
        b_se_n_per_mps=0.0,
        c_se_n_per_mps2=0.0,
        directions="pair",
        per_direction={
            heading: DirectionFit(
                a_n=A + C * WIND**2,
                b_n_per_mps=B + sign * 2 * C * WIND,
                c_n_per_mps2=C,
                a_se_n=0.0,
                b_se_n_per_mps=0.0,
                c_se_n_per_mps2=0.0,
                residual_sd_n=0.0,
                n_samples=500,
                speed_min_mps=6.0,
                speed_max_mps=31.0,
                static_pitch_deg=MOUNT_OFFSET_DEG,
            )
            for heading, sign in (("northbound", 1.0), ("southbound", -1.0))
        },
        residual_sd_n=0.0,
        test_mass_kg=MASS,
        neutral_effective_mass_kg=MASS + WHEEL_SIDE_KG_M2 / RADIUS**2,
        inertia="estimated",
        squat_corrected=True,
        imu_pitch_offset_deg=MOUNT_OFFSET_DEG,
    )


def dyno_offset(deg: float = MOUNT_OFFSET_DEG, frame: str | None = None) -> PitchOffset:
    return PitchOffset(deg=deg, heading_pitch_deg={}, n_samples=0, reference_id=frame)


def dyno_conditions(**overrides: object) -> RunConditions:
    base: dict[str, object] = {
        "gear": GEAR,
        "direction": "pair",
        "tune_revision": "synthetic",
        "induction": "na",
        "baro_kpa": 80.0,
        "intake_temp_c": 25.0,
        "ambient_temp_c": 22.0,
        "road": "synthetic road",
        "date": dt.date(2026, 9, 26),
        "payload_kg": 127.0,
        "sessions": ["synthetic"],
    }
    base.update(overrides)
    return RunConditions.model_validate(base)


def dyno_power(pulls: list[PowerPull], **overrides: object) -> DynoPower | DynoRefusal:
    kwargs: dict[str, object] = {
        "driveline": dyno_driveline(),
        "test_mass_kg": MASS,
        "road_load": pair_road_load(),
        "squat": Squat(),
        "pitch_offset": dyno_offset(),
        "wot": _Wot(),
        "conditions": dyno_conditions(),
    }
    kwargs.update(overrides)
    return compute_power(pulls, **kwargs)  # type: ignore[arg-type]


def dyno_edit(pull: PowerPull, **changes: object) -> PowerPull:
    kept: dict[str, Any] = {k: v for k, v in changes.items() if k != "demand"}
    samples = replace(pull.samples, **kept)
    demand = changes.get("demand", pull.demand)
    return PowerPull(pull.heading, pull.gear, samples, demand)  # type: ignore[arg-type]


# Known answers the synthetic data is generated from. None of them is read
# from a vehicle profile: the estimators must recover them, not echo them.
FLAT_MASS = 2311.0


TRUE_SQUAT_DEG_PER_G = 1.2


TRUE_MOUNT_OFFSET_DEG = 0.3


TRUE_A, TRUE_B, TRUE_C = 180.0, 5.0, 0.55


TRUE_ENGINE_INERTIA = 0.18  # the driveline below *estimates* 0.25


FLAT_ETA = 0.85


FLAT_PARAMS = VehicleParameters(
    mass_kg=2184.4,
    wheel_tire_assembly_kg=40.3,
    final_drive=4.08,
    gearbox_ratios=(4.368, 2.518, 1.743, 1.283, 1.0, 0.769),
    tire_loaded_circumference_m=2.609,
    driveline_evidence="synthetic test vehicle",
    drivetrain_loss_fraction=1.0 - FLAT_ETA,
)


INERTIA = InertiaData(
    road_wheels=4,
    wheel_shape_factor=0.7,
    driveline_output_kg_m2=1.0,
    engine_side_kg_m2=0.25,
    evidence="synthetic estimate",
)


def pitch_sf(accel: np.ndarray, grade: float | np.ndarray, deg_per_g: float) -> np.ndarray:
    """Specific force a tilted IMU reads: a + g·sin(θ) + g·sin(δ(a)) + mounting."""
    pitch = np.radians(deg_per_g * accel / G + TRUE_MOUNT_OFFSET_DEG)
    return accel + G * np.sin(np.arctan(grade)) + G * np.sin(pitch)


def flat_accelerations(
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
    sf = pitch_sf(accel, road, TRUE_SQUAT_DEG_PER_G)
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


def flat_parked(
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
def flat_noise(white_sd: float = 1.0, ar_sd: float = 0.0, tau_s: float = 10.0) -> AltitudeNoise:
    """The noise model measured from a parked reference with these statistics."""
    measured = measure_altitude_noise(
        flat_parked(np.random.default_rng(999), white_sd=white_sd, ar_sd=ar_sd, tau_s=tau_s)
    )
    assert isinstance(measured, AltitudeNoise), measured
    return measured


#: For the noise-free algebra tests only: a model that resolves anything.
NEGLIGIBLE_NOISE = AltitudeNoise(
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
    m_eff = driveline.neutral_effective_mass(FLAT_MASS)
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
        accel = -(force + FLAT_MASS * G * math.sin(theta)) / m_eff
        if step % every == 0:
            rows.append((t, v, accel, altitude))
        assert t < 600.0, "the coast never reaches v_end (downhill faster than drag)"
        v += accel * dt_sim
        altitude += slope * v * dt_sim
        t += dt_sim
        step += 1
    arr = np.array(rows)
    accel_col = arr[:, 2]
    sf = pitch_sf(accel_col, slope, squat_deg_per_g)
    return MotionSamples(
        t_s=arr[:, 0],
        speed_mps=arr[:, 1],
        accel_mps2=accel_col + rng.normal(0, noise_a, len(arr)),
        sf_lon_mps2=sf + rng.normal(0, noise_f, len(arr)),
        altitude_m=arr[:, 3],
    )


def _engine_torque(rpm: float) -> float:
    return 280.0 + 40.0 * (1.0 - ((rpm - 4000.0) / 2000.0) ** 2)


def flat_pull(
    rng: np.random.Generator,
    driveline: Driveline,
    gear: int,
    *,
    grade: float = 0.01,
    rpm_start: float = 2500.0,
    rpm_end: float = 5500.0,
    hz: float = 100.0,
    eta_at_crank: float = FLAT_ETA,
    c_n_per_mps2: float = TRUE_C,
) -> GearPull:
    """A WOT pull in one gear from a known torque curve, road load and inertia.

    ``c_n_per_mps2`` is the aero term in the pull's own air (``TRUE_C`` is
    the coast-downs' air, :data:`ROAD_LOAD_FIT_BARO_KPA`).
    """
    ratio = driveline.overall_ratio(gear)
    r = driveline.tire_radius_m
    # The crank inertia is upstream of the lossy gearbox: the wheel sees
    # ETA·N/r·(T − I_e·α), so the engine's share of m_eff is ETA·I_e·N²/r².
    # Written out here, independently of Driveline.effective_mass.
    m_eff = (
        FLAT_MASS
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
        load = TRUE_A + TRUE_B * v + c_n_per_mps2 * v**2
        accel = (drive - load - FLAT_MASS * G * math.sin(theta)) / m_eff
        rows.append((t, v, accel, rpm))
        assert t < 60.0, f"gear {gear} never reaches {rpm_end} rpm: pick a reachable band"
        v += accel * dt_sim
        t += dt_sim
    arr = np.array(rows)
    accel_col = arr[:, 2]
    sf = pitch_sf(accel_col, grade, TRUE_SQUAT_DEG_PER_G)
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


def flat_squat_record() -> SquatCoefficient:
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
        altitude_noise=[NEGLIGIBLE_NOISE],
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


#: The air the synthetic road load (``TRUE_C``) was coasted in.
ROAD_LOAD_FIT_BARO_KPA = 80.0


ROAD_LOAD_FIT_AMBIENT_C = 18.0


def _true_road_load() -> RoadLoadCoefficients:
    """The exact synthetic road load, recorded at :data:`ROAD_LOAD_FIT_BARO_KPA`."""
    direction = DirectionFit(
        a_n=TRUE_A,
        b_n_per_mps=TRUE_B,
        c_n_per_mps2=TRUE_C,
        a_se_n=0.0,
        b_se_n_per_mps=0.0,
        c_se_n_per_mps2=0.0,
        residual_sd_n=0.0,
        n_samples=1000,
        speed_min_mps=4.0,
        speed_max_mps=32.0,
        static_pitch_deg=TRUE_MOUNT_OFFSET_DEG,
    )
    fit = RoadLoadFit(
        a_n=TRUE_A,
        b_n_per_mps=TRUE_B,
        c_n_per_mps2=TRUE_C,
        a_se_n=0.0,
        b_se_n_per_mps=0.0,
        c_se_n_per_mps2=0.0,
        directions="pair",
        per_direction={"northbound": direction, "southbound": direction},
        residual_sd_n=0.0,
        test_mass_kg=FLAT_MASS,
        neutral_effective_mass_kg=FLAT_MASS,
        inertia="estimated",
        squat_corrected=True,
        imu_pitch_offset_deg=TRUE_MOUNT_OFFSET_DEG,
    )
    return fit.to_record(
        RoadLoadConditions(
            road="synthetic flat",
            date=dt.date(2026, 9, 26),
            sessions=["synthetic"],
            payload_kg=127.0,
            tire_pressure_front_kpa=240.0,
            tire_pressure_rear_kpa=240.0,
            ambient_temp_c=ROAD_LOAD_FIT_AMBIENT_C,
            baro_kpa=ROAD_LOAD_FIT_BARO_KPA,
        )
    )


def flat_crest(t: np.ndarray) -> np.ndarray:
    """+1.2 % while accelerating, −1.2 % while braking: a crest, mean grade ~0."""
    return np.where(t < 10.0, 0.012, -0.012)


def flat_coast_pair(
    driveline: Driveline, seed: int, *, runs: int = 1, **kwargs: float
) -> list[Leg]:
    rng = np.random.default_rng(seed)
    legs: list[Leg] = []
    for _ in range(runs):
        legs.append(Leg("northbound", _coastdown(rng, driveline, heading_sign=+1.0, **kwargs)))
        legs.append(Leg("southbound", _coastdown(rng, driveline, heading_sign=-1.0, **kwargs)))
    return legs


def flat_in_frame(legs: list[Leg], frame: str | None) -> list[Leg]:
    return [Leg(leg.heading, replace(leg.samples, imu_reference_id=frame)) for leg in legs]


def flat_stated_offset(deg: float = TRUE_MOUNT_OFFSET_DEG) -> PitchOffset:
    """A known pitch offset in the synthetic data's frame (``reference_id`` None)."""
    return PitchOffset(deg=deg, heading_pitch_deg={}, n_samples=0, reference_id=None)


def flat_inertia_kwargs(driveline: Driveline) -> dict[str, Any]:
    return {
        "driveline": driveline,
        "test_mass_kg": FLAT_MASS,
        "road_load": _true_road_load(),
        # The pulls run in the coast-downs' air unless a test says otherwise.
        "baro_kpa": ROAD_LOAD_FIT_BARO_KPA,
        "ambient_temp_c": ROAD_LOAD_FIT_AMBIENT_C,
        "squat": flat_squat_record(),
    }


def flat_conditions(**overrides: object) -> RunConditions:
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


def flat_curve(offset_kw: float = 0.0, **overrides: object) -> DynoCurve:
    power = 60.0 + 30.0 * np.sin((RPM - 2500.0) / 3500.0 * math.pi * 0.8)
    return DynoCurve("wheel_power_kw", RPM, power + offset_kw, flat_conditions(**overrides))


def flat_floor() -> DynoNoiseFloor:
    floor = record_dyno_floor(
        [flat_curve(0.0), flat_curve(1.0), flat_curve(-1.0)],
        band_edges_rpm=[2500, 3500, 4500, 5500],
    )
    assert isinstance(floor, DynoNoiseFloor), floor
    return floor
