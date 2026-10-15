"""Wheel and estimated engine power from wide-open-throttle pulls (H07.7).

``compute_power`` turns WOT pulls in one gear, both directions, into wheel
power and torque against engine rpm, with the engine figures only ever
``wheel / η`` and labelled estimates. Its samples come from
:mod:`src.analysis.dyno_inputs`; the pulls themselves are found by
:mod:`src.analysis.dyno_pulls`.

Depends on the contract, motion, driveline, road-load and comparison modules
(never on the squat or inertia fits: it takes the squat as a protocol).
Moved verbatim out of ``dyno.py`` (R16).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from pydantic import Field

from src.analysis.dyno_comparison import (
    CURVE_GRID_STEP_RPM,
    DynoCurve,
    Quantity,
    RunConditions,
    rpm_grid,
)
from src.analysis.dyno_contract import (
    MIN_SAMPLES,
    DynoRefusal,
    FloatArray,
    MotionSamples,
    RoadLoadLike,
    SquatLike,
    StrictModel,
    as_float_array,
)
from src.analysis.dyno_driveline import Driveline, InertiaFlag
from src.analysis.dyno_motion import PitchOffset, frame_mismatch, remove_sensor_tilt
from src.analysis.dyno_road_load import RoadLoadCoefficients, RoadLoadFit
from src.signals.committees import STANDARD_GRAVITY

#: A pull's engine speed must stay within this fraction of what the stated
#: gear and the road speed give (``rpm = v·N/r``). Adjacent gears are 25-30 %
#: apart, so a shift cannot hide inside it; H07.11 found 4th reading 2.0 %
#: high against the published ratios, so a tighter band would refuse sound
#: pulls. Slip below it goes unseen, and costs the rpm axis that fraction (the
#: wheel force comes from the motion, not from the engine speed).
POWER_GEAR_TOLERANCE = 0.04

#: A pull must span this much engine speed at or above the WOT criterion's
#: rpm floor.
POWER_MIN_RPM_SPAN = 1000.0

#: Grade range a pull may cover: 2nd to 98th percentile of the road grade the
#: IMU itself sees (``tan asin((f_corrected − a)/g)``, moving mean over
#: :data:`POWER_GRADE_SMOOTH_S`). The IMU carries grade in ``f``, so a steady
#: grade costs nothing. A changing one is where what the force leans on stops
#: holding: the chassis pitches against the road over crests and dips (the
#: squat model knows only acceleration), the vertical load and so rolling
#: resistance change, and the two headings stop crossing the same grade at the
#: same rpm. 2 % is H07.7's stated amount, not a measured one; H07.12's flat
#: straight road sits far inside it.
POWER_MAX_GRADE_RANGE = 0.02
POWER_GRADE_SMOOTH_S = 1.0

#: Most samples an rpm bin is asked for. The ask is sized from the pull's
#: own rate (samples per bin on average, ``n·bin/span``), capped here and
#: never below 1: a 2nd-gear pull at H07.3's 20 Hz climbs ~565 rpm/s, 1.8
#: samples per 50 rpm bin, and a fixed 3 dropped every bin. Those samples are
#: already averaged over H07.3's smoothing window, so one per bin is not one
#: raw reading.
POWER_MIN_PER_BIN = 3

#: A pull must fill at least this fraction of the 50 rpm bins it spans;
#: below it the curve between its points would be interpolation, not data
#: (``too_few_bins``, naming the sample rate).
POWER_MIN_BIN_FILL = 0.5

#: How the engine-side figures are made, stated on every result.
ENGINE_ESTIMATE_LABEL = (
    "estimate: wheel / (1 - drivetrain_loss_fraction), torque-proportional losses "
    "assumed; not a measurement"
)

#: What ``wheel_torque_nm`` means: wheel power over *engine* angular speed, the
#: chassis-dyno convention (torque at the crank after driveline losses), so a
#: power and a torque curve share the rpm axis.
WHEEL_TORQUE_CONVENTION = "wheel power / engine angular speed (chassis-dyno convention)"


class _WotLike(Protocol):
    """The two fields of :class:`src.analysis.tuning_frame.WotCriterion` used here."""

    @property
    def threshold_value(self) -> float: ...

    @property
    def rpm_floor(self) -> float: ...


@dataclass(frozen=True)
class PowerPull:
    """One wide-open-throttle pull in one gear, one direction (H07.7).

    ``demand`` is the operator-demand channel (``WotCriterion.role``, in its
    unit) on the samples' time base. It is required: without it nothing shows
    the throttle was open.
    """

    heading: str
    gear: int
    samples: MotionSamples
    demand: FloatArray | None

    def __post_init__(self) -> None:
        if self.demand is not None:
            demand = as_float_array("demand", self.demand)
            if len(demand) != len(self.samples):
                raise ValueError(
                    f"demand has {len(demand)} samples, the motion {len(self.samples)}"
                )
            object.__setattr__(self, "demand", demand)


class J1349Correction(StrictModel):
    """SAE J1349 inputs the run conditions do not carry.

    ``CF = 1.18·(99/p_d)·sqrt((T + 273)/298) − 0.18`` with ``p_d`` the dry-air
    pressure (the run's ``baro_kpa`` less the water-vapour pressure) and ``T``
    the run's ``intake_temp_c``. Nothing on the truck measures humidity, so the
    vapour pressure is stated, with where it came from. Defined for NA engines:
    a boosted run is refused.
    """

    vapour_pressure_kpa: float = Field(ge=0.0)
    vapour_pressure_source: str = Field(min_length=1)


@dataclass(frozen=True)
class PowerCurve:
    """Wheel and estimated engine figures against engine rpm, on one grid."""

    rpm: FloatArray
    wheel_force_n: FloatArray
    wheel_power_kw: FloatArray
    #: :data:`WHEEL_TORQUE_CONVENTION`.
    wheel_torque_nm: FloatArray
    #: :data:`ENGINE_ESTIMATE_LABEL`.
    engine_power_kw_estimate: FloatArray
    engine_torque_nm_estimate: FloatArray

    def scaled(self, factor: float) -> PowerCurve:
        return PowerCurve(
            rpm=self.rpm,
            wheel_force_n=self.wheel_force_n * factor,
            wheel_power_kw=self.wheel_power_kw * factor,
            wheel_torque_nm=self.wheel_torque_nm * factor,
            engine_power_kw_estimate=self.engine_power_kw_estimate * factor,
            engine_torque_nm_estimate=self.engine_torque_nm_estimate * factor,
        )


@dataclass(frozen=True)
class DynoPower:
    """Power and torque against rpm for one run: a direction pair, or labelled single."""

    #: The direction pair's mean at each rpm (or the single heading's curve).
    curve: PowerCurve
    per_heading: dict[str, PowerCurve]
    direction: Literal["pair", "single"]
    conditions: RunConditions
    gear: int
    n_pulls: int
    test_mass_kg: float
    effective_mass_kg: float
    driveline_efficiency: float
    engine_estimate: str
    wheel_torque_convention: str
    inertia: InertiaFlag
    squat_corrected: bool
    imu_pitch_offset_deg: float
    imu_reference_id: str | None
    #: ``sae_j1349`` only when asked for; :attr:`corrected` holds the curve
    #: times ``correction_factor``. :meth:`dyno_curve` never returns it.
    correction: Literal["none", "sae_j1349"]
    correction_factor: float
    corrected: PowerCurve | None
    correction_inputs: J1349Correction | None

    def dyno_curve(self, quantity: Quantity) -> DynoCurve:
        """The uncorrected curve, for :func:`record_dyno_floor` / :func:`compare_runs`.

        Always uncorrected: a floor and a comparison are same-day and same-road
        by construction, and a corrected run compared with an uncorrected one
        would read the correction as a difference.
        """
        values = self.curve.wheel_power_kw if quantity == "wheel_power_kw" else None
        if quantity == "wheel_torque_nm":
            values = self.curve.wheel_torque_nm
        if values is None:
            raise ValueError(f"no curve for {quantity!r}")
        return DynoCurve(
            quantity=quantity, rpm=self.curve.rpm, value=values, conditions=self.conditions
        )


def _moving_mean(values: FloatArray, t_s: FloatArray, seconds: float) -> FloatArray:
    step = float(np.median(np.diff(t_s))) if len(t_s) > 1 else seconds
    width = max(1, round(seconds / step))
    if width <= 1 or width >= len(values):
        return values
    kernel = np.ones(width) / width
    smoothed: FloatArray = np.convolve(values, kernel, mode="valid")
    return smoothed


def _j1349_factor(correction: J1349Correction, conditions: RunConditions) -> float | DynoRefusal:
    if conditions.induction != "na":
        return DynoRefusal(
            "power",
            "correction_refused",
            "SAE J1349 is defined for naturally aspirated engines; this run is "
            f"{conditions.induction}, so the correction is refused, not applied",
        )
    dry = conditions.baro_kpa - correction.vapour_pressure_kpa
    if dry <= 0.0:
        return DynoRefusal(
            "power",
            "invalid_input",
            f"vapour pressure {correction.vapour_pressure_kpa} kPa leaves no dry air at "
            f"{conditions.baro_kpa} kPa",
        )
    return 1.18 * (99.0 / dry) * math.sqrt((conditions.intake_temp_c + 273.0) / 298.0) - 0.18


@dataclass(frozen=True)
class _PullPoints:
    rpm: FloatArray
    force: FloatArray
    power_w: FloatArray


def _pull_points(
    index: int,
    pull: PowerPull,
    *,
    driveline: Driveline,
    test_mass_kg: float,
    road_load: RoadLoadLike,
    squat: SquatLike | None,
    pitch_offset: PitchOffset,
    wot: _WotLike,
    min_samples: int,
    gear_tolerance: float,
    max_grade_range: float,
    min_rpm_span: float,
    rpm_bin: float,
) -> _PullPoints | DynoRefusal:
    name = "power"
    label = f"pull {index} ({pull.heading}, gear {pull.gear})"
    s = pull.samples
    if s.rpm is None:
        return DynoRefusal(name, "inputs_missing", f"{label} has no engine rpm")
    if pull.demand is None:
        return DynoRefusal(
            name,
            "inputs_missing",
            f"{label} has no operator-demand channel, so nothing shows the throttle was wide open",
        )
    keep = s.rpm >= wot.rpm_floor
    if int(keep.sum()) < min_samples:
        return DynoRefusal(
            name,
            "too_few_samples",
            f"{label} has {int(keep.sum())} samples at or above the WOT criterion's rpm floor "
            f"({wot.rpm_floor:.0f}); the computation needs {min_samples}",
        )
    t = s.t_s[keep]
    v = s.speed_mps[keep]
    a = s.accel_mps2[keep]
    rpm = s.rpm[keep]
    r = driveline.tire_radius_m
    to_rpm = 60.0 / (2.0 * math.pi)
    ratio = driveline.overall_ratio(pull.gear)
    implied = v * ratio / r * to_rpm
    deviation = rpm / np.maximum(implied, 1e-9) - 1.0
    worst = int(np.argmax(np.abs(deviation)))
    if abs(float(deviation[worst])) > gear_tolerance:
        at = f"at t = {t[worst]:.2f} s ({rpm[worst]:.0f} rpm, {v[worst] * 3.6:.1f} km/h)"
        for other in range(1, len(driveline.gearbox_ratios) + 1):
            if other == pull.gear:
                continue
            other_rpm = v[worst] * driveline.overall_ratio(other) / r * to_rpm
            if abs(rpm[worst] / other_rpm - 1.0) <= gear_tolerance:
                return DynoRefusal(
                    name,
                    "gear_change",
                    f"{label}: engine speed matches gear {other}, not {pull.gear}, {at}; "
                    "a pull is one gear",
                )
        if deviation[worst] > 0:
            return DynoRefusal(
                name,
                "clutch_slip",
                f"{label}: engine speed runs {100 * deviation[worst]:+.1f} % above what gear "
                f"{pull.gear} and the road speed give, {at} (band +/-"
                f"{100 * gear_tolerance:.0f} %); the clutch slipped",
            )
        return DynoRefusal(
            name,
            "gear_ratio_mismatch",
            f"{label}: engine speed runs {100 * deviation[worst]:+.1f} % below what gear "
            f"{pull.gear} and the road speed give, {at} (band +/-{100 * gear_tolerance:.0f} %). "
            "Slip cannot lower it: the speed scale, the tire or the stated gear is wrong",
        )
    demand = pull.demand[keep]
    low = int(np.argmin(demand))
    if demand[low] < wot.threshold_value:
        return DynoRefusal(
            name,
            "not_wot",
            f"{label}: operator demand falls to {demand[low]:.3g} at t = {t[low]:.2f} s "
            f"({rpm[low]:.0f} rpm), below the WOT criterion's {wot.threshold_value:g}",
        )
    f = remove_sensor_tilt(s.sf_lon_mps2[keep], a, squat=squat, pitch_offset_deg=pitch_offset.deg)
    static = np.clip((f - a) / STANDARD_GRAVITY, -1.0, 1.0)
    grade = _moving_mean(np.tan(np.arcsin(static)), t, POWER_GRADE_SMOOTH_S)
    lo_grade, hi_grade = np.percentile(grade, [2.0, 98.0])
    if hi_grade - lo_grade > max_grade_range:
        return DynoRefusal(
            name,
            "grade_varies",
            f"{label}: the road's grade (from the IMU) spans {100 * lo_grade:+.2f} to "
            f"{100 * hi_grade:+.2f} % over the pull; a pull may cover "
            f"{100 * max_grade_range:.1f} %",
        )
    span = float(rpm.max() - rpm.min())
    if span < min_rpm_span:
        return DynoRefusal(
            name,
            "rpm_span_too_small",
            f"{label} spans {span:.0f} rpm at wide-open throttle; a curve needs {min_rpm_span:.0f}",
        )
    m_eff = driveline.effective_mass(test_mass_kg, pull.gear)
    force = test_mass_kg * f + (m_eff - test_mass_kg) * a + road_load.force(v)
    power = force * v
    edges = np.arange(math.floor(rpm.min() / rpm_bin) * rpm_bin, rpm.max() + rpm_bin, rpm_bin)
    index_of = np.digitize(rpm, edges) - 1
    spanned = len(edges) - 1
    per_bin = len(rpm) * rpm_bin / max(span, rpm_bin)
    ask = int(min(POWER_MIN_PER_BIN, max(1, math.floor(per_bin))))
    rows: list[tuple[float, float, float]] = []
    for b in range(spanned):
        members = index_of == b
        if int(members.sum()) >= ask:
            rows.append(
                (
                    float(rpm[members].mean()),
                    float(force[members].mean()),
                    float(power[members].mean()),
                )
            )
    if len(rows) < max(2, math.ceil(POWER_MIN_BIN_FILL * spanned)):
        rate = 1.0 / float(np.median(np.diff(t))) if len(t) > 1 else 0.0
        return DynoRefusal(
            name,
            "too_few_bins",
            f"{label}: {len(rows)} of the {spanned} {rpm_bin:.0f} rpm bins it spans hold "
            f">= {ask} sample(s) ({len(rpm)} samples at {rate:.1f} Hz over {span:.0f} rpm); "
            f"a curve needs {100 * POWER_MIN_BIN_FILL:.0f} % of them. Sample faster, or pull "
            "in a taller gear",
        )
    points = np.array(rows)
    return _PullPoints(rpm=points[:, 0], force=points[:, 1], power_w=points[:, 2])


def compute_power(
    pulls: Sequence[PowerPull],
    *,
    driveline: Driveline,
    test_mass_kg: float,
    road_load: RoadLoadFit | RoadLoadCoefficients,
    squat: SquatLike | None,
    pitch_offset: PitchOffset,
    wot: _WotLike,
    conditions: RunConditions,
    correction: J1349Correction | None = None,
    allow_single_direction: bool = False,
    min_samples: int = MIN_SAMPLES,
    gear_tolerance: float = POWER_GEAR_TOLERANCE,
    max_grade_range: float = POWER_MAX_GRADE_RANGE,
    min_rpm_span: float = POWER_MIN_RPM_SPAN,
    grid_step_rpm: float = CURVE_GRID_STEP_RPM,
) -> DynoPower | DynoRefusal:
    """Wheel power and torque against engine rpm from WOT pulls in one gear (H07.7).

    Per sample, with ``f`` the specific force corrected for the IMU's tilt
    (squat ``δ(a)`` and the session's pitch offset ``μ``)::

        F_wheel = m·f + (m_eff − m)·a + F_rl(v)        P_wheel = F_wheel·v

    ``m_eff`` is :meth:`Driveline.effective_mass` for the gear: the wheel side
    plus ``η·I_e·N²/r²`` (:data:`INERTIA_CONVENTION`), so ``F_wheel`` is
    ``η·N/r·T``, the engine's torque through the driveline with none of it
    spent spinning the engine up. Wheel torque is ``P_wheel / ω_engine``
    (:data:`WHEEL_TORQUE_CONVENTION`); engine power and torque are only ever
    ``wheel / η`` and say so (:data:`ENGINE_ESTIMATE_LABEL`).

    Each pull is binned by engine rpm (:data:`CURVE_GRID_STEP_RPM`), each
    heading's pulls averaged at the same rpm, and the pair is the mean of the
    two headings at the same rpm: a steady wind adds ``±2·C·w·v`` to the road
    load, which the pair-averaged coefficients leave in the headings and the
    pair cancels. One heading is refused unless ``allow_single_direction``,
    and is then labelled ``single``.

    Required, so none is forgotten silently: ``pitch_offset`` (from
    :func:`estimate_pitch_offset` on a direction pair in the same IMU frame;
    pulls from another frame are refused), ``squat`` (``None`` is recorded as
    ``squat_corrected: False``), a road load from a coast-down *pair*, the WOT
    criterion and the run's conditions.

    Refusals, in order: arguments (``inputs_missing``, ``invalid_input``,
    ``conditions_mismatch``, ``single_direction`` for the road load,
    ``correction_refused``), ``frame_mismatch``, then per pull
    ``inputs_missing``, ``too_few_samples``, ``gear_change`` / ``clutch_slip``
    / ``gear_ratio_mismatch``, ``not_wot``, ``grade_varies``,
    ``rpm_span_too_small``, ``too_few_bins``; then ``too_many_directions``,
    ``single_direction``, ``direction_mismatch`` and ``no_overlap``. GPS quality is
    refused where the samples are made (:mod:`src.analysis.dyno_inputs`).
    """
    name = "power"
    if not pulls:
        return DynoRefusal(name, "inputs_missing", "no pulls were given")
    gears = sorted({pull.gear for pull in pulls})
    if len(gears) > 1:
        return DynoRefusal(
            name, "invalid_input", f"pulls in gears {gears}; a power curve is one gear"
        )
    gear = gears[0]
    if not 1 <= gear <= len(driveline.gearbox_ratios):
        return DynoRefusal(
            name, "invalid_input", f"gear {gear} is not one of 1..{len(driveline.gearbox_ratios)}"
        )
    if conditions.gear != gear:
        return DynoRefusal(
            name,
            "conditions_mismatch",
            f"the pulls are in gear {gear}; the run conditions say gear {conditions.gear}",
        )
    if road_load.directions != "pair":
        return DynoRefusal(
            name,
            "single_direction",
            "the road load came from one coast-down direction: grade, wind and the IMU's "
            "pitch offset are still in its A and B",
        )
    factor = 1.0
    if correction is not None:
        computed = _j1349_factor(correction, conditions)
        if isinstance(computed, DynoRefusal):
            return computed
        factor = computed
    mismatch = frame_mismatch(name, pitch_offset, [pull.samples for pull in pulls])
    if mismatch is not None:
        return mismatch
    by_heading: dict[str, list[_PullPoints]] = {}
    for index, pull in enumerate(pulls):
        if not pull.heading:
            return DynoRefusal(name, "invalid_input", f"pull {index} has no heading label")
        points = _pull_points(
            index,
            pull,
            driveline=driveline,
            test_mass_kg=test_mass_kg,
            road_load=road_load,
            squat=squat,
            pitch_offset=pitch_offset,
            wot=wot,
            min_samples=min_samples,
            gear_tolerance=gear_tolerance,
            max_grade_range=max_grade_range,
            min_rpm_span=min_rpm_span,
            rpm_bin=grid_step_rpm,
        )
        if isinstance(points, DynoRefusal):
            return points
        by_heading.setdefault(pull.heading, []).append(points)
    if len(by_heading) > 2:
        return DynoRefusal(
            name,
            "too_many_directions",
            f"headings {sorted(by_heading)}: a pair is one road driven both ways",
        )
    direction: Literal["pair", "single"] = "pair" if len(by_heading) == 2 else "single"
    if direction == "single" and not allow_single_direction:
        (heading,) = by_heading
        return DynoRefusal(
            name,
            "single_direction",
            f"only {heading!r} was pulled; one direction leaves wind (+/-2*C*w*v) and what "
            "the IMU misses of the grade in the curve",
        )
    if conditions.direction != direction:
        return DynoRefusal(
            name,
            "direction_mismatch",
            f"the pulls make a {direction} run; the run conditions say {conditions.direction}",
        )
    every = [points for runs in by_heading.values() for points in runs]
    lo = max(float(points.rpm[0]) for points in every)
    hi = min(float(points.rpm[-1]) for points in every)
    grid = rpm_grid(lo, hi, grid_step_rpm) if hi > lo else np.empty(0)
    if len(grid) < 2:
        return DynoRefusal(
            name,
            "no_overlap",
            f"the pulls share {lo:.0f}-{hi:.0f} rpm; a curve needs two {grid_step_rpm:.0f} rpm "
            "points that every pull covers",
        )
    omega = grid * 2.0 * math.pi / 60.0
    eta = driveline.efficiency

    def _curve(force: FloatArray, power_w: FloatArray) -> PowerCurve:
        wheel_torque = power_w / omega
        return PowerCurve(
            rpm=grid,
            wheel_force_n=force,
            wheel_power_kw=power_w / 1000.0,
            wheel_torque_nm=wheel_torque,
            engine_power_kw_estimate=power_w / 1000.0 / eta,
            engine_torque_nm_estimate=wheel_torque / eta,
        )

    per_heading: dict[str, PowerCurve] = {}
    for heading, runs in by_heading.items():
        force = np.mean([np.interp(grid, p.rpm, p.force) for p in runs], axis=0)
        power = np.mean([np.interp(grid, p.rpm, p.power_w) for p in runs], axis=0)
        per_heading[heading] = _curve(force, power)
    curves = list(per_heading.values())
    curve = _curve(
        np.mean([c.wheel_force_n for c in curves], axis=0),
        np.mean([c.wheel_power_kw for c in curves], axis=0) * 1000.0,
    )
    return DynoPower(
        curve=curve,
        per_heading=per_heading,
        direction=direction,
        conditions=conditions,
        gear=gear,
        n_pulls=len(pulls),
        test_mass_kg=test_mass_kg,
        effective_mass_kg=driveline.effective_mass(test_mass_kg, gear),
        driveline_efficiency=eta,
        engine_estimate=ENGINE_ESTIMATE_LABEL,
        wheel_torque_convention=WHEEL_TORQUE_CONVENTION,
        inertia=driveline.inertia,
        squat_corrected=squat is not None,
        imu_pitch_offset_deg=pitch_offset.deg,
        imu_reference_id=pitch_offset.reference_id,
        correction="none" if correction is None else "sae_j1349",
        correction_factor=factor,
        corrected=None if correction is None else curve.scaled(factor),
        correction_inputs=correction,
    )
