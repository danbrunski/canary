"""Wheel and engine power from WOT pulls (H07.7).

The generator drives a truck forward from a known engine torque curve through
every physical effect the computation claims to handle, each written out here
and not taken from ``src.analysis.dyno``: grade that changes along the road,
a steady wind (headwind one way), body squat, the IMU's residual pitch offset,
driveline losses taken *after* the crank's inertia (``η`` at the crank),
wheel-side and engine-side inertia, and measurement noise. The computation
must recover the torque curve within 2 % over the band, and each refusal must
name its cause.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytest
from pydantic import ValidationError

from src.analysis.dyno import (
    ENGINE_ESTIMATE_LABEL,
    DirectionFit,
    Driveline,
    DynoPower,
    DynoRefusal,
    InertiaData,
    J1349Correction,
    Leg,
    MotionSamples,
    MotionSources,
    PitchOffset,
    PowerPull,
    RoadLoadCoefficients,
    RoadLoadConditions,
    RoadLoadFit,
    RunConditions,
    SpeedSource,
    compute_power,
    estimate_pitch_offset,
    fit_road_load,
    record_dyno_floor,
)
from src.analysis.dyno_inputs import (
    DEFAULT_HZ,
    SPEED_SCALE_TOLERANCE,
    DynoInputs,
    GpsSpeedCheck,
    ImuSpecificForce,
    build_motion_inputs,
)
from src.analysis.dyno_road_load import ROAD_LOAD_BARO_MAX_KPA, ROAD_LOAD_BARO_MIN_KPA
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


@dataclass(frozen=True)
class _Squat:
    deg_per_g: float = SQUAT_DEG_PER_G


class _Wot:
    threshold_value = 4.00
    rpm_floor = 2500.0


PARAMS = VehicleParameters(
    mass_kg=2184.4,
    wheel_tire_assembly_kg=WHEEL_MASS,
    final_drive=FINAL_DRIVE,
    gearbox_ratios=GEARBOX,
    tire_loaded_circumference_m=TIRE_CIRCUMFERENCE,
    driveline_evidence="synthetic test vehicle",
    drivetrain_loss_fraction=1.0 - ETA,
)


def _driveline(engine_side: float = ENGINE_INERTIA) -> Driveline:
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


def torque(rpm: np.ndarray | float) -> np.ndarray:
    """The engine's WOT torque curve (N m): 280 at 2,000, peak 320 at 4,000."""
    return 280.0 + 40.0 * (1.0 - ((np.asarray(rpm, dtype=float) - 4000.0) / 2000.0) ** 2)


def road_grade(x_m: np.ndarray | float) -> np.ndarray:
    """Grade (fraction, uphill northbound) at distance x along the road."""
    return 0.004 + 0.003 * np.sin(2 * math.pi * np.asarray(x_m, dtype=float) / 600.0)


def _imu(accel: np.ndarray, theta: np.ndarray, squat: float, offset_deg: float) -> np.ndarray:
    """What a longitudinal accelerometer pitched by φ = squat·a/g + μ against
    the road reads on a grade θ (nose-up positive): a·cos φ + g·sin(θ + φ)."""
    phi = np.radians(squat * accel / G + offset_deg)
    reading: np.ndarray = accel * np.cos(phi) + G * np.sin(theta + phi)
    return reading


class _Noise:
    def __init__(self, rng: np.random.Generator | None) -> None:
        self.rng = rng

    def __call__(self, sd: float, n: int) -> np.ndarray:
        return np.zeros(n) if self.rng is None else self.rng.normal(0.0, sd, n)


@dataclass(frozen=True)
class _Trajectory:
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
        return _imu(accel, np.interp(at, self.t, self.theta), SQUAT_DEG_PER_G, MOUNT_OFFSET_DEG)


def _trajectory(
    heading: str,
    *,
    gear: int = GEAR,
    rpm_start: float = 2450.0,
    rpm_end: float = 5500.0,
    grade: Callable[[np.ndarray | float], np.ndarray] = road_grade,
    x_start: float | None = None,
) -> _Trajectory:
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
    return _Trajectory(heading, gear, t, speed, accel, rpm_, theta_)


def _pull(
    heading: str,
    *,
    noise: _Noise,
    hz: float = 100.0,
    **trajectory: object,
) -> PowerPull:
    """The trajectory sampled at ``hz`` with measurement noise, as MotionSamples."""
    path = _trajectory(heading, **trajectory)  # type: ignore[arg-type]
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


#: The producer's frame id for the chain test's synthetic IMU.
CHAIN_FRAME = "synthetic-frame"
SPEED_RATIO = 0.932


def _through_the_producer(path: _Trajectory, rng: np.random.Generator) -> PowerPull:
    """Each instrument at its own rate (speed and rpm at 100 Hz in their
    broadcast resolution, the IMU at 208 Hz, GPS at 1 Hz), through H07.3's
    ``build_motion_inputs`` at its default 20 Hz and smoothing."""
    origin = pd.Timestamp("2026-09-26T12:00:00Z")

    def series(t: np.ndarray, values: np.ndarray) -> pd.Series:
        return pd.Series(values, index=origin + pd.to_timedelta(t, unit="s"))

    duration = float(path.t[-1])
    t_can = np.arange(0.0, duration, 0.01) + rng.uniform(-0.0005, 0.0005)
    t_can = np.clip(t_can, 0.0, duration)
    t_imu = np.arange(0.0, duration, 1.0 / 208.0)
    t_gps = np.arange(0.0, duration, 1.0)
    v_can = np.interp(t_can, path.t, path.v)
    channels = {
        "decoded:SPEED": series(t_can, np.round(v_can * SPEED_RATIO * 3.6, 1)),
        "decoded:RPM": series(
            t_can,
            np.round(np.interp(t_can, path.t, path.rpm) * 8) / 8 + rng.normal(0, 5, len(t_can)),
        ),
        "decoded:PEDAL": series(t_can, np.full(len(t_can), 4.10)),
        "gps:speed": series(t_gps, np.interp(t_gps, path.t, path.v) * 3.6),
        "gps:num_satellites": series(t_gps, np.full(len(t_gps), 12.0)),
        "gps:hdop": series(t_gps, np.full(len(t_gps), 0.8)),
    }
    imu = ImuSpecificForce(
        series=series(t_imu, path.sf(t_imu) + rng.normal(0.0, 0.05, len(t_imu))),
        reference_id=CHAIN_FRAME,
    )
    inputs = build_motion_inputs(
        channels,
        start=origin + pd.Timedelta(seconds=0.4),
        end=origin + pd.Timedelta(seconds=duration - 0.4),
        sources=MotionSources(
            speed=[SpeedSource(channel="decoded:SPEED", ratio_to_gps=SPEED_RATIO, evidence="t")],
            rpm=["decoded:RPM"],
        ),
        imu=imu,
        demand_channel="decoded:PEDAL",
    )
    assert isinstance(inputs, DynoInputs), inputs
    assert inputs.hz == DEFAULT_HZ
    motion = inputs.motion()
    assert isinstance(motion, MotionSamples), motion
    return PowerPull(
        path.heading,
        path.gear,
        motion,
        inputs.demand,
        speed_scale_residual=inputs.gps_check.speed_scale_residual,
    )


def _cruise_pair(noise: _Noise, speed: float = 22.0) -> list[Leg]:
    """Steady speed over x = 0-600 m both ways: grade flips sign, μ does not."""
    legs = []
    for heading, sign in (("northbound", 1.0), ("southbound", -1.0)):
        x = np.linspace(0.0, 600.0, 600)
        theta = np.arctan(sign * road_grade(x))
        n = len(x)
        zeros = np.zeros(n)
        legs.append(
            Leg(
                heading,
                MotionSamples(
                    t_s=cast(npt.NDArray[np.float64], np.arange(n) * (600.0 / speed / n)),
                    speed_mps=np.full(n, speed),
                    accel_mps2=zeros + noise(0.02, n),
                    sf_lon_mps2=_imu(zeros, theta, SQUAT_DEG_PER_G, MOUNT_OFFSET_DEG)
                    + noise(0.03, n),
                ),
            )
        )
    return legs


def _coast(heading: str, noise: _Noise, *, v0: float = 31.0, v_end: float = 6.0) -> Leg:
    """Clutch in, on the same road and wind: only the wheel side turns."""
    sign = 1.0 if heading == "northbound" else -1.0
    m_total = MASS + WHEEL_SIDE_KG_M2 / RADIUS**2
    dt_sim, x, v = 0.01, (0.0 if sign > 0 else 1500.0), v0
    rows = []
    step = 0
    while v > v_end:
        theta = math.atan(sign * float(road_grade(x)))
        air = v + sign * WIND
        accel = -(A + B * v + C * air * abs(air) + MASS * G * math.sin(theta)) / m_total
        if step % 10 == 0:
            rows.append((step * dt_sim, v, accel, theta))
        v += accel * dt_sim
        x += sign * v * dt_sim
        step += 1
    t, speed, accel, theta = np.array(rows).T
    n = len(t)
    sf = _imu(accel, theta, SQUAT_DEG_PER_G, MOUNT_OFFSET_DEG)
    return Leg(
        heading,
        MotionSamples(
            t_s=t,
            speed_mps=speed,
            accel_mps2=accel + noise(0.005, n),
            sf_lon_mps2=sf + noise(0.005, n),
        ),
    )


def _road_conditions(**overrides: object) -> RoadLoadConditions:
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


def _pair_road_load(**conditions: object) -> RoadLoadCoefficients:
    """What a coast-down pair measures in this wind: C·w² lands in A."""
    return _pair_road_load_fit().to_record(_road_conditions(**conditions))


def _pair_road_load_fit() -> RoadLoadFit:
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


def _offset(deg: float = MOUNT_OFFSET_DEG, frame: str | None = None) -> PitchOffset:
    return PitchOffset(deg=deg, heading_pitch_deg={}, n_samples=0, reference_id=frame)


def _conditions(**overrides: object) -> RunConditions:
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


def _power(pulls: list[PowerPull], **overrides: object) -> DynoPower | DynoRefusal:
    kwargs: dict[str, object] = {
        "driveline": _driveline(),
        "test_mass_kg": MASS,
        "road_load": _pair_road_load(),
        "squat": _Squat(),
        "pitch_offset": _offset(),
        "wot": _Wot(),
        "conditions": _conditions(),
    }
    kwargs.update(overrides)
    return compute_power(pulls, **kwargs)  # type: ignore[arg-type]


def _ok(result: DynoPower | DynoRefusal) -> DynoPower:
    assert isinstance(result, DynoPower), result
    return result


def _refused(result: object, kind: str) -> DynoRefusal:
    assert isinstance(result, DynoRefusal), result
    assert result.kind == kind, result
    assert result.reason
    return result


def _torque_error(power: DynoPower) -> np.ndarray:
    """Relative error of the engine torque estimate against the true curve."""
    error: np.ndarray = power.curve.engine_torque_nm_estimate / torque(power.curve.rpm) - 1.0
    return error


SILENT = _Noise(None)
PAIR = [_pull("northbound", noise=SILENT), _pull("southbound", noise=SILENT)]


# ---------------------------------------------------------------------------
# Known answers
# ---------------------------------------------------------------------------


def test_power_is_exact_without_noise() -> None:
    power = _ok(_power(PAIR))
    # The algebra alone: every term the generator applied is removed.
    assert np.max(np.abs(_torque_error(power))) < 1e-3
    omega = power.curve.rpm * 2 * math.pi / 60
    np.testing.assert_allclose(
        power.curve.wheel_power_kw, ETA * torque(power.curve.rpm) * omega / 1000.0, rtol=1e-3
    )
    assert power.curve.rpm[0] >= 2500.0 and power.curve.rpm[-1] <= 5500.0
    assert power.curve.rpm[-1] - power.curve.rpm[0] >= 2500.0
    # Each heading alone carries the wind's ±2·C·w·v: into the headwind the
    # pair-averaged road load under-reads the drag, so that heading reads low
    # (and the tailwind one high). The pair cancels it.
    north = power.per_heading["northbound"].engine_torque_nm_estimate / torque(power.curve.rpm)
    south = power.per_heading["southbound"].engine_torque_nm_estimate / torque(power.curve.rpm)
    assert np.max(north - 1.0) < -0.004 and np.min(south - 1.0) > 0.004
    assert power.direction == "pair" and power.n_pulls == 2


@pytest.mark.parametrize("seed", [31, 32, 33])
def test_a_known_torque_curve_is_recovered_within_2_percent_end_to_end(seed: int) -> None:
    # Everything measured from the synthetic drive itself: the road load from
    # a coast-down pair (fit_road_load), the pitch offset from a steady-cruise
    # pair (estimate_pitch_offset), two noisy pulls each way.
    rng = np.random.default_rng(seed)
    noise = _Noise(rng)
    driveline = _driveline()
    coasts = [_coast(h, noise) for h in ("northbound", "southbound") for _ in range(3)]
    fit = fit_road_load(coasts, driveline=driveline, test_mass_kg=MASS, squat=_Squat())
    assert isinstance(fit, RoadLoadFit), fit
    road_load = fit.to_record(_road_conditions())
    offset = estimate_pitch_offset(_cruise_pair(noise), squat=_Squat())
    assert isinstance(offset, PitchOffset), offset
    pulls = [_pull(h, noise=noise) for h in ("northbound", "southbound") for _ in range(2)]
    power = _ok(_power(pulls, road_load=road_load, pitch_offset=offset, driveline=driveline))
    error = _torque_error(power)
    assert np.max(np.abs(error)) < 0.02, np.max(np.abs(error))
    assert power.curve.rpm[-1] - power.curve.rpm[0] >= 2500.0
    assert power.imu_pitch_offset_deg == offset.deg


@pytest.mark.parametrize(("gear", "seed"), [(2, 41), (3, 42)])
def test_a_known_torque_curve_is_recovered_through_the_producer_at_its_rate(
    gear: int, seed: int
) -> None:
    # The whole chain at the rate it runs: instruments at their own rates and
    # resolutions, H07.3 at 20 Hz with its 0.5 s smoothing, then H07.7. A
    # 2nd-gear pull climbs ~565 rpm/s, under two 20 Hz samples per 50 rpm bin.
    rng = np.random.default_rng(seed)
    pulls = [
        _through_the_producer(_trajectory(h, gear=gear), rng) for h in ("northbound", "southbound")
    ]
    power = _ok(
        _power(pulls, pitch_offset=_offset(frame=CHAIN_FRAME), conditions=_conditions(gear=gear))
    )
    error = _torque_error(power)
    assert np.max(np.abs(error)) < 0.02, np.max(np.abs(error))
    assert power.curve.rpm[-1] - power.curve.rpm[0] >= 2000.0
    assert power.imu_reference_id == CHAIN_FRAME


def test_second_gear_at_the_producers_rate_is_computed_not_crashed() -> None:
    # 20 Hz, noise-free, 2nd gear: 1-2 samples per 50 rpm bin. The per-bin ask
    # follows the pull's own rate, so every bin counts.
    pair = [_pull(h, noise=SILENT, gear=2, hz=DEFAULT_HZ) for h in ("northbound", "southbound")]
    power = _ok(_power(pair, conditions=_conditions(gear=2)))
    assert np.max(np.abs(_torque_error(power))) < 2e-3
    # Too sparse to be a curve: 1st gear sampled at 5 Hz leaves most bins empty
    # (the 50-sample floor is lowered so the bin gate, not it, is what speaks).
    sparse = [
        _pull(h, noise=SILENT, gear=1, hz=5.0, rpm_end=5500.0) for h in ("northbound", "southbound")
    ]
    refusal = _refused(
        _power(sparse, conditions=_conditions(gear=1), min_samples=5), "too_few_bins"
    )
    assert "5.0 Hz" in refusal.reason


def test_each_physics_term_is_needed() -> None:
    # Noise-free, so each number is the term's own size (2,500-5,500 rpm, 3rd).
    def worst(**overrides: object) -> float:
        return float(np.max(np.abs(_torque_error(_ok(_power(PAIR, **overrides))))))

    assert worst() < 1e-3
    # Squat left in the specific force: 1.5 deg/g at ~0.17 g.
    assert worst(squat=None) > 0.015
    # The IMU pitch offset left in: 0.3 deg is ~119 N.
    assert worst(pitch_offset=_offset(0.0)) > 0.02
    # Rolling resistance and aero ignored.
    zero_load = _pair_road_load().model_copy(
        update={"a_n": 1e-9, "b_n_per_mps": 0.0, "c_n_per_mps2": 1e-9}
    )
    assert worst(road_load=zero_load) > 0.05
    # Engine inertia overstated (0.25 against 0.18): the spin-up share is wrong.
    assert worst(driveline=_driveline(0.25)) > 0.003
    # A driveline that puts η at the wheels (I_e reflected without it) reads
    # m_eff high by (1 − η)·I_e·N²/r².
    lossless_inertia = _driveline(ENGINE_INERTIA / ETA)
    assert worst(driveline=lossless_inertia) > 0.002


def test_engine_figures_are_wheel_over_eta_and_labelled_estimates() -> None:
    power = _ok(_power(PAIR))
    curve = power.curve
    np.testing.assert_allclose(curve.engine_power_kw_estimate, curve.wheel_power_kw / ETA)
    omega = curve.rpm * 2 * math.pi / 60
    np.testing.assert_allclose(curve.wheel_torque_nm, curve.wheel_power_kw * 1000.0 / omega)
    np.testing.assert_allclose(curve.engine_torque_nm_estimate, curve.wheel_torque_nm / ETA)
    assert power.engine_estimate == ENGINE_ESTIMATE_LABEL and "estimate" in power.engine_estimate
    assert power.driveline_efficiency == pytest.approx(ETA)
    assert power.inertia == "estimated" and power.squat_corrected
    assert power.correction == "none" and power.corrected is None


def test_sae_j1349_is_named_applied_only_to_na_and_never_to_the_comparable_curve() -> None:
    correction = J1349Correction(vapour_pressure_kpa=1.0, vapour_pressure_source="stated")
    power = _ok(_power(PAIR, correction=correction))
    # 80 kPa baro less 1 kPa vapour, 25 °C intake: 1.18·99/79 − 0.18.
    assert power.correction_factor == pytest.approx(1.18 * 99.0 / 79.0 - 0.18)
    assert power.corrected is not None
    np.testing.assert_allclose(
        power.corrected.wheel_power_kw, power.curve.wheel_power_kw * power.correction_factor
    )
    # What floors and comparisons read is always uncorrected.
    np.testing.assert_allclose(power.dyno_curve("wheel_power_kw").value, power.curve.wheel_power_kw)
    reference = _ok(
        _power(
            PAIR,
            conditions=_conditions(baro_kpa=100.0, intake_temp_c=25.0),
            correction=J1349Correction(vapour_pressure_kpa=1.0, vapour_pressure_source="stated"),
        )
    )
    assert reference.correction_factor == pytest.approx(1.0)
    refusal = _refused(
        _power(PAIR, correction=correction, conditions=_conditions(induction="boosted")),
        "correction_refused",
    )
    assert "naturally aspirated" in refusal.reason
    with pytest.raises(ValidationError):
        J1349Correction(vapour_pressure_kpa=1.0, vapour_pressure_source="")


def test_power_curves_feed_the_noise_floor() -> None:
    rng = np.random.default_rng(5)
    curves = []
    for _ in range(3):
        noise = _Noise(rng)
        pulls = [_pull(h, noise=noise) for h in ("northbound", "southbound")]
        curves.append(_ok(_power(pulls)).dyno_curve("wheel_power_kw"))
    floor = record_dyno_floor(curves, band_edges_rpm=[2500, 3500, 4500, 5500])
    assert not isinstance(floor, DynoRefusal), floor
    assert floor.conditions.gear == GEAR


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def _edit(pull: PowerPull, **changes: object) -> PowerPull:
    kept: dict[str, Any] = {k: v for k, v in changes.items() if k != "demand"}
    samples = replace(pull.samples, **kept)
    demand = changes.get("demand", pull.demand)
    return PowerPull(pull.heading, pull.gear, samples, demand)  # type: ignore[arg-type]


def test_argument_refusals_name_their_cause() -> None:
    north, south = PAIR
    _refused(_power([]), "inputs_missing")
    fourth = _pull("southbound", noise=SILENT, gear=4, rpm_end=4500.0)
    _refused(_power([north, fourth]), "invalid_input")
    refusal = _refused(_power(PAIR, conditions=_conditions(gear=2)), "conditions_mismatch")
    assert "gear 3" in refusal.reason and "gear 2" in refusal.reason
    single_load = _pair_road_load().model_copy(update={"directions": "single"})
    _refused(_power(PAIR, road_load=single_load), "single_direction")
    # A fit that was never recorded says nothing of the air its C belongs to.
    refusal = _refused(_power(PAIR, road_load=_pair_road_load_fit()), "inputs_missing")
    assert "baro_kpa" in refusal.reason and "M01.8" in refusal.reason
    # The pitch offset belongs to one IMU frame.
    framed = [
        PowerPull(p.heading, p.gear, replace(p.samples, imu_reference_id="b"), p.demand)
        for p in PAIR
    ]
    refusal = _refused(_power(framed, pitch_offset=_offset(frame="a")), "frame_mismatch")
    assert "'a'" in refusal.reason and "'b'" in refusal.reason
    assert _ok(_power(framed, pitch_offset=_offset(frame="b"))).imu_reference_id == "b"


def test_direction_refusals_and_the_single_label() -> None:
    north, south = PAIR
    refusal = _refused(_power([north]), "single_direction")
    assert "wind" in refusal.reason
    _refused(_power([north], allow_single_direction=True), "direction_mismatch")
    single = _ok(
        _power([north], allow_single_direction=True, conditions=_conditions(direction="single"))
    )
    assert single.direction == "single"
    _refused(_power(PAIR, conditions=_conditions(direction="single")), "direction_mismatch")
    east = PowerPull("eastbound", GEAR, south.samples, south.demand)
    _refused(_power([north, south, east]), "too_many_directions")
    low = _pull("northbound", noise=SILENT, rpm_end=3700.0)
    high = _pull("southbound", noise=SILENT, rpm_start=3900.0)
    _refused(_power([low, high]), "no_overlap")


def test_pull_refusals_name_their_cause() -> None:
    north, south = PAIR
    n = len(north.samples)
    part = np.zeros(n, dtype=bool)
    part[n // 2 : n // 2 + 60] = True
    ratio_4_over_3 = GEARBOX[3] / GEARBOX[2]
    north_rpm = north.samples.rpm
    assert north_rpm is not None

    _refused(_power([_edit(north, rpm=None), south]), "inputs_missing")
    refusal = _refused(_power([_edit(north, demand=None), south]), "inputs_missing")
    assert "operator-demand" in refusal.reason
    # A shift into 4th halfway: the engine speed drops onto 4th's line.
    shifted = np.where(part, north_rpm * ratio_4_over_3, north_rpm)
    refusal = _refused(_power([_edit(north, rpm=shifted), south]), "gear_change")
    assert "gear 4" in refusal.reason
    # The engine flares 8 % above the road speed's line: the clutch slipped.
    flared = np.where(part, north_rpm * 1.08, north_rpm)
    _refused(_power([_edit(north, rpm=flared), south]), "clutch_slip")
    # Speed 10 % high everywhere: rpm under the line, which slip cannot do.
    fast = north.samples.speed_mps * 1.10
    refusal = _refused(_power([_edit(north, speed_mps=fast), south]), "gear_ratio_mismatch")
    assert "speed scale" in refusal.reason
    lifted = np.where(part, 3.5, north.demand)  # type: ignore[arg-type]
    refusal = _refused(_power([_edit(north, demand=lifted), south]), "not_wot")
    assert "3.5" in refusal.reason
    rolling = _pull(
        "northbound",
        noise=SILENT,
        grade=lambda x: 0.03 * np.sin(2 * math.pi * np.asarray(x, dtype=float) / 150.0),
    )
    refusal = _refused(_power([rolling, south]), "grade_varies")
    assert "IMU" in refusal.reason
    short = _pull("northbound", noise=SILENT, rpm_end=3200.0)
    _refused(_power([short, south]), "rpm_span_too_small")
    below_floor = _pull("northbound", noise=SILENT, rpm_start=1500.0, rpm_end=2520.0)
    _refused(_power([below_floor, south]), "too_few_samples")


# ---------------------------------------------------------------------------
# M01.8: the road load's air, and the conditions it was coasted in
# ---------------------------------------------------------------------------

#: Dry-air gas constant, J/(kg K): written out here, not taken from the code.
R_DRY = 287.05
#: An Xterra-sized Cd*A (0.45 x 3.06 m^2), so C at 80 kPa and 25 C is the
#: 0.643 N/(m/s)^2 behind M01.8's "62 N at 100 km/h".
CD_A = 1.376


def _rho(baro_kpa: float, temp_c: float) -> float:
    return baro_kpa * 1000.0 / (R_DRY * (temp_c + 273.15))


def test_a_fit_at_80_kpa_applied_at_70_kpa_carries_no_extra_drag_m01_8() -> None:
    # M01.8's known answer: C = 1/2*rho*Cd*A fitted at 80 kPa, 25 C. Used
    # unscaled at 70 kPa it overstates aero drag by C*v^2*(1 - 70/80): 62 N at
    # 100 km/h (1.7 kW) and 121 N at 140 km/h (4.7 kW).
    c_fit = 0.5 * _rho(80.0, 25.0) * CD_A
    assert c_fit == pytest.approx(0.643, abs=1e-3)
    record = (
        _pair_road_load_fit()
        .to_record(_road_conditions(baro_kpa=80.0, ambient_temp_c=25.0))
        .model_copy(update={"c_n_per_mps2": c_fit})
    )
    v100, v140 = 100.0 / 3.6, 140.0 / 3.6
    speeds = np.array([v100, v140])
    true_at_70 = (
        record.a_n + record.b_n_per_mps * speeds + 0.5 * _rho(70.0, 25.0) * CD_A * speeds**2
    )
    unscaled = record.force(speeds) - true_at_70
    assert unscaled[0] == pytest.approx(62.0, abs=0.5)
    assert unscaled[1] == pytest.approx(121.6, abs=0.5)
    assert unscaled[0] * v100 / 1000.0 == pytest.approx(1.7, abs=0.05)
    assert unscaled[1] * v140 / 1000.0 == pytest.approx(4.7, abs=0.05)
    # Moved into the run's air, the overstatement is gone.
    air = record.in_air(70.0, 25.0)
    assert air.density_ratio == pytest.approx(70.0 / 80.0, rel=1e-12)
    np.testing.assert_allclose(air.force(speeds) - true_at_70, 0.0, atol=1e-9)
    assert air.c_n_per_mps2 == pytest.approx(0.5 * _rho(70.0, 25.0) * CD_A, rel=1e-12)
    assert air.c_fit_n_per_mps2 == c_fit and air.a_n == record.a_n
    # Temperature enters too, by the ambient reading on both sides: 80 kPa
    # at 5 C against a fit at 80 kPa and 25 C is 298.15/278.15 denser.
    assert record.in_air(80.0, 5.0).density_ratio == pytest.approx(298.15 / 278.15)
    assert "ambient_temp_c" in air.temperature_source
    assert "intake_temp_c" in air.temperature_source


def test_compute_power_moves_the_road_loads_c_into_the_runs_air() -> None:
    # The same pulls, run conditions at 80 kPa (the coast-downs' air) and at
    # 70 kPa: the wheel force differs by exactly the aero term the thinner air
    # removes, C*(1 - 70/80)*v^2 at each rpm's road speed.
    at_80 = _ok(_power(PAIR))
    at_70 = _ok(_power(PAIR, conditions=_conditions(baro_kpa=70.0)))
    assert at_80.road_load_air.density_ratio == 1.0
    assert at_70.road_load_air.density_ratio == pytest.approx(70.0 / 80.0)
    assert at_70.road_load_air.fit_baro_kpa == 80.0 and at_70.road_load_air.run_baro_kpa == 70.0
    v = at_70.curve.rpm * 2 * math.pi / 60 * RADIUS / (GEARBOX[GEAR - 1] * FINAL_DRIVE)
    expected = -C * (1.0 - 70.0 / 80.0) * v**2
    np.testing.assert_allclose(
        at_70.curve.wheel_force_n - at_80.curve.wheel_force_n, expected, rtol=1e-3, atol=0.05
    )
    # The unscaled answer was 80 kPa's, and the generator's road load is the
    # 80 kPa one, so it recovers the torque curve exactly there.
    assert np.max(np.abs(_torque_error(at_80))) < 1e-3


def test_a_stored_road_load_without_baro_is_refused_naming_the_field(tmp_path: Path) -> None:
    from src.analysis.dyno import load_dyno_profile
    from src.analysis.dyno_profile import DYNO_FILENAME

    old = _road_conditions().model_dump(mode="json")
    del old["baro_kpa"]
    with pytest.raises(ValidationError, match="baro_kpa"):
        RoadLoadConditions.model_validate(old)
    with pytest.raises(ValidationError, match="M01.8"):
        RoadLoadConditions.model_validate({**old, "baro_kpa": None})
    # A value in another unit is refused, not scaled 10x: 800 hPa, 11.6 psi.
    for wrong in (800.0, 11.6, 0.0):
        with pytest.raises(ValidationError, match="hPa"):
            RoadLoadConditions.model_validate({**old, "baro_kpa": wrong})
    for edge in (ROAD_LOAD_BARO_MIN_KPA, 63.0, ROAD_LOAD_BARO_MAX_KPA):
        assert RoadLoadConditions.model_validate({**old, "baro_kpa": edge}).baro_kpa == edge
    record = _pair_road_load().model_dump(mode="json")
    del record["conditions"]["baro_kpa"]
    path = tmp_path / DYNO_FILENAME
    path.write_text(json.dumps({"schema_version": 1, "road_load": record}))
    with pytest.raises(ValidationError, match="baro_kpa"):
        load_dyno_profile(path)


def test_a_runs_mass_payload_and_tires_against_the_borrowed_road_load_are_named() -> None:
    # Everything matching and stated: nothing to name.
    stated = _conditions(tire_pressure_front_kpa=240.0, tire_pressure_rear_kpa=240.0)
    assert _ok(_power(PAIR, conditions=stated)).road_load_mismatches == ()
    # Tires not stated: unchecked, not matched.
    unstated = _ok(_power(PAIR)).road_load_mismatches
    assert [(m.field, m.status) for m in unstated] == [
        ("tire_pressure_front_kpa", "unstated"),
        ("tire_pressure_rear_kpa", "unstated"),
    ]
    assert all("unchecked" in m.detail and m.run is None for m in unstated)
    # Heavier, a different payload, a soft front tire: each named, not refused.
    differ = _ok(
        _power(
            PAIR,
            test_mass_kg=MASS + 73.0,
            conditions=_conditions(
                payload_kg=200.0, tire_pressure_front_kpa=220.0, tire_pressure_rear_kpa=240.0
            ),
        )
    ).road_load_mismatches
    named = {m.field: m for m in differ}
    assert set(named) == {"test_mass_kg", "payload_kg", "tire_pressure_front_kpa"}
    assert all(m.status == "differs" for m in differ)
    assert named["test_mass_kg"].run == MASS + 73.0 and named["test_mass_kg"].road_load == MASS
    assert "+73 kg" in named["test_mass_kg"].detail
    assert named["payload_kg"].road_load == 127.0
    assert "220 kPa" in named["tire_pressure_front_kpa"].detail


# ---------------------------------------------------------------------------
# M01.9: J1349 outside its range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("baro_kpa", "factor"), [(82.0, 1.262), (70.0, 1.513), (63.0, 1.704)])
def test_j1349_at_this_trucks_altitudes_is_flagged_extrapolated(
    baro_kpa: float, factor: float
) -> None:
    # M01.9's figures: 25 C, 1 kPa vapour, the factor at 82 / 70 / 63 kPa.
    correction = J1349Correction(vapour_pressure_kpa=1.0, vapour_pressure_source="stated")
    power = _ok(_power(PAIR, conditions=_conditions(baro_kpa=baro_kpa), correction=correction))
    assert power.correction_factor == pytest.approx(factor, abs=5e-4)
    flagged = power.correction_range
    assert flagged is not None and not flagged.in_range
    assert flagged.flag == (
        f"extrapolated: {baro_kpa - 1.0:.1f} kPa dry is outside J1349's range (90-105 kPa)"
    )
    assert flagged.dry_pressure_kpa == baro_kpa - 1.0 and flagged.temperature_c == 25.0
    # The plain density ratio beside it: dry air against 99 kPa at 25 C.
    assert flagged.density_ratio == pytest.approx((baro_kpa - 1.0) / 99.0)
    assert flagged.temperature_source.startswith("intake_temp_c")
    assert "engine-bay" in flagged.temperature_source
    assert "0.85" in flagged.applied_to and "wheel" in flagged.applied_to
    assert "not verified" in flagged.range_source


def test_j1349_range_flag_inside_and_on_temperature() -> None:
    correction = J1349Correction(vapour_pressure_kpa=1.0, vapour_pressure_source="stated")
    inside = _ok(_power(PAIR, conditions=_conditions(baro_kpa=100.0), correction=correction))
    assert inside.correction_range is not None and inside.correction_range.in_range
    assert inside.correction_range.flag.startswith("within J1349's range")
    assert inside.correction_range.density_ratio == pytest.approx(1.0)
    hot = _ok(
        _power(
            PAIR,
            conditions=_conditions(baro_kpa=100.0, intake_temp_c=45.0),
            correction=correction,
        )
    )
    assert hot.correction_range is not None and not hot.correction_range.in_range
    assert hot.correction_range.flag == (
        "extrapolated: 45.0 C intake air is outside J1349's range (15-35 C)"
    )
    # No correction asked: no corrected curve, so no flag either.
    assert _ok(_power(PAIR)).correction_range is None


# ---------------------------------------------------------------------------
# M01.10: the speed scale, squared into power
# ---------------------------------------------------------------------------


def test_the_speed_scale_residual_and_its_power_band_are_carried_into_the_result() -> None:
    north, south = PAIR
    # Unchecked pulls (synthetic, no GPS check): named, never read as zero.
    silent = _ok(_power(PAIR)).speed_scale
    assert silent.speed_scale_residual is None and silent.power_band is None
    assert silent.n_unchecked == 2
    # 0x6F7's 0.9203 used for 0x2D1's 0.932: the scaled speed reads 1.27 %
    # fast (scaled / GPS - 1 > 0), passes the 3 % tolerance and is up to 2.6 %
    # of power. -0.03 is a speed 3 % slow: the band bounds it at 6.09 %.
    borrowed = 0.932 / 0.9203 - 1.0
    assert borrowed > 0
    pulls = [
        replace(north, speed_scale_residual=borrowed),
        replace(south, speed_scale_residual=-0.03),
    ]
    scale = _ok(_power(pulls)).speed_scale
    assert scale.residuals == (borrowed, -0.03) and scale.n_unchecked == 0
    assert scale.speed_scale_residual == -0.03
    assert scale.power_band == pytest.approx(0.0609)
    alone = _ok(_power([replace(north, speed_scale_residual=borrowed), south])).speed_scale
    assert alone.speed_scale_residual == borrowed and alone.n_unchecked == 1
    # Exact for a speed that reads fast: (0.932 / 0.9203)^2 - 1.
    assert alone.power_band == pytest.approx((0.932 / 0.9203) ** 2 - 1.0, rel=1e-12)
    assert alone.power_band == pytest.approx(0.02559, abs=1e-5)
    assert "squared" in alone.meaning and "2x" in alone.meaning


@pytest.mark.parametrize("ratio", [*np.linspace(0.90, 1.10, 41), 0.9203 / 0.932, 0.97, 1.03])
def test_the_power_band_bounds_the_squared_error_for_either_sign(ratio: float) -> None:
    # GpsSpeedCheck.ratio is GPS / scaled, so power reads 1/ratio^2 of the
    # truth when the force scales with the speed too. The band must never be
    # below that, for a speed reading fast (ratio < 1) or slow (ratio > 1);
    # it is exact for the first and over by 2r^2 for the second.
    check = GpsSpeedCheck("checked", ratio=float(ratio))
    residual = check.speed_scale_residual
    band = check.power_band
    assert residual is not None and band is not None
    assert residual == pytest.approx(1.0 / ratio - 1.0, rel=1e-12)
    true_error = abs(1.0 / ratio**2 - 1.0)
    assert band >= true_error - 1e-15
    if residual >= 0.0:
        assert band == pytest.approx(true_error, rel=1e-12, abs=1e-15)
    else:
        assert band - true_error == pytest.approx(2.0 * residual**2, rel=1e-9, abs=1e-15)


def test_the_power_band_at_the_speed_scale_tolerance() -> None:
    # The check passes |GPS / scaled - 1| <= 3 %. A 0.97 ratio is a speed
    # 3.09 % fast: 6.28 % of power, which the band states exactly; 1.03 is
    # 2.91 % slow: 5.74 %, bounded at 5.91 %.
    fast = GpsSpeedCheck("checked", ratio=1.0 - SPEED_SCALE_TOLERANCE)
    slow = GpsSpeedCheck("checked", ratio=1.0 + SPEED_SCALE_TOLERANCE)
    assert fast.power_band == pytest.approx(1.0 / 0.97**2 - 1.0)
    assert fast.power_band == pytest.approx(0.0628, abs=1e-4)
    assert slow.power_band == pytest.approx(0.0591, abs=1e-4)
    assert slow.power_band is not None and slow.power_band > 1.0 - 1.0 / 1.03**2


def test_the_producer_carries_its_gps_check_into_the_pull() -> None:
    rng = np.random.default_rng(42)
    pulls = [
        _through_the_producer(_trajectory(h, gear=3), rng) for h in ("northbound", "southbound")
    ]
    power = _ok(_power(pulls, pitch_offset=_offset(frame=CHAIN_FRAME)))
    # Each pull's own check, in order; a short pull may hold too few 1 Hz GPS
    # epochs to check, and is then counted unchecked rather than read as 0.
    scale = power.speed_scale
    assert scale.residuals == tuple(p.speed_scale_residual for p in pulls)
    assert scale.n_unchecked == sum(r is None for r in scale.residuals) < len(pulls)
    assert scale.speed_scale_residual is not None
    assert abs(scale.speed_scale_residual) < 0.01
