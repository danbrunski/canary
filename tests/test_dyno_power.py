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
import math
from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from src.analysis.dyno import (
    ENGINE_ESTIMATE_LABEL,
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
    DynoInputs,
    ImuSpecificForce,
    build_motion_inputs,
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
TYRE_CIRCUMFERENCE = 2.609
RADIUS = TYRE_CIRCUMFERENCE / (2 * math.pi)
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
    tyre_loaded_circumference_m=TYRE_CIRCUMFERENCE,
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
    return accel * np.cos(phi) + G * np.sin(theta + phi)


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
    return PowerPull(path.heading, path.gear, motion, inputs.demand)


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
                    t_s=np.arange(n) * (600.0 / speed / n),
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


def _pair_road_load() -> RoadLoadFit:
    """What a coast-down pair measures in this wind: C·w² lands in A."""
    return RoadLoadFit(
        a_n=A + C * WIND**2,
        b_n_per_mps=B,
        c_n_per_mps2=C,
        a_se_n=0.0,
        b_se_n_per_mps=0.0,
        c_se_n_per_mps2=0.0,
        directions="pair",
        per_direction={},
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
    return power.curve.engine_torque_nm_estimate / torque(power.curve.rpm) - 1.0


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
    road_load = fit_road_load(coasts, driveline=driveline, test_mass_kg=MASS, squat=_Squat())
    assert isinstance(road_load, RoadLoadFit), road_load
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
    zero_load = replace(_pair_road_load(), a_n=1e-9, b_n_per_mps=0.0, c_n_per_mps2=1e-9)
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
    samples = replace(pull.samples, **{k: v for k, v in changes.items() if k != "demand"})
    demand = changes.get("demand", pull.demand)
    return PowerPull(pull.heading, pull.gear, samples, demand)  # type: ignore[arg-type]


def test_argument_refusals_name_their_cause() -> None:
    north, south = PAIR
    _refused(_power([]), "inputs_missing")
    fourth = _pull("southbound", noise=SILENT, gear=4, rpm_end=4500.0)
    _refused(_power([north, fourth]), "invalid_input")
    refusal = _refused(_power(PAIR, conditions=_conditions(gear=2)), "conditions_mismatch")
    assert "gear 3" in refusal.reason and "gear 2" in refusal.reason
    single_load = replace(_pair_road_load(), directions="single")
    _refused(_power(PAIR, road_load=single_load), "single_direction")
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

    _refused(_power([_edit(north, rpm=None), south]), "inputs_missing")
    refusal = _refused(_power([_edit(north, demand=None), south]), "inputs_missing")
    assert "operator-demand" in refusal.reason
    # A shift into 4th halfway: the engine speed drops onto 4th's line.
    shifted = np.where(part, north.samples.rpm * ratio_4_over_3, north.samples.rpm)  # type: ignore[operator]
    refusal = _refused(_power([_edit(north, rpm=shifted), south]), "gear_change")
    assert "gear 4" in refusal.reason
    # The engine flares 8 % above the road speed's line: the clutch slipped.
    flared = np.where(part, north.samples.rpm * 1.08, north.samples.rpm)  # type: ignore[operator]
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
