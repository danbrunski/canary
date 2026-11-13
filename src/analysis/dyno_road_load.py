"""Road load ``A + B·v + C·v²`` from neutral coast-downs, both directions (H07.5).

``fit_road_load`` fits each heading on its own after measuring the IMU's
pitch offset from the pair, then averages them (a steady wind cancels in
``B``). :class:`RoadLoadCoefficients` is the stored record and refuses to
exist without its :class:`RoadLoadConditions`.

Depends on the contract, motion and driveline modules. Moved verbatim out of
``dyno.py`` (R16).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import Field, field_validator, model_validator

from src.analysis.dyno_contract import (
    MIN_SAMPLES,
    DynoRefusal,
    FloatArray,
    SquatLike,
    StrictModel,
    lstsq,
)
from src.analysis.dyno_driveline import Driveline, InertiaFlag
from src.analysis.dyno_motion import Leg, estimate_pitch_offset, group_legs, remove_sensor_tilt
from src.signals.committees import STANDARD_GRAVITY

#: A coast-down direction must span this much speed. ``B·v`` and ``C·v²`` are
#: collinear over a narrow band, so a short coast yields confident nonsense;
#: SAE J2263 coasts from 115 to 15 km/h.
COAST_MIN_SPEED_SPAN_KPH = 30.0

#: Celsius to kelvin, for the ideal-gas density ratio.
KELVIN_OFFSET = 273.15

#: Plausible barometric pressure for a road-load record, kPa. Above: the
#: sea-level record high is ~108.4 kPa. Below: ~48 kPa is the standard
#: atmosphere at 5,800 m, above the highest paved roads. Outside it the
#: value is in another unit (hPa or mbar read ~10x; psi and inHg read low),
#: and a C scaled by it would be off by that factor.
ROAD_LOAD_BARO_MIN_KPA = 45.0
ROAD_LOAD_BARO_MAX_KPA = 110.0

#: Which temperature the air-density ratio uses, stated on every result that
#: applies it (M01.8). Ambient, not the ECM's intake air temperature: aero
#: drag acts on the air the body moves through, and on this truck the IAT
#: sensor reads engine-bay air at low flow (M01.9), which a coast-down in
#: neutral is.
ROAD_LOAD_TEMPERATURE_SOURCE = (
    "ambient_temp_c (the road load's own record and the run's conditions); not intake_temp_c, "
    "which reads engine-bay air at low flow on this truck"
)


def air_density_ratio(
    baro_kpa: float, ambient_temp_c: float, *, ref_baro_kpa: float, ref_ambient_temp_c: float
) -> float:
    """``ρ / ρ_ref`` for dry air by the ideal-gas law: ``(p / p_ref)·(T_ref / T)``.

    Humidity is in neither side: nothing on the truck measures it. Moist air
    is lighter than dry air at the same pressure and temperature by
    ``0.378·e/p``: about 1.5 % at 70 kPa, 35 °C and 50 % RH, less when cooler
    or drier. Both sides carry the same omission, so the ratio errs only by
    the humidity *difference* between the coast-down and the run.
    """
    return (baro_kpa / ref_baro_kpa) * (
        (ref_ambient_temp_c + KELVIN_OFFSET) / (ambient_temp_c + KELVIN_OFFSET)
    )


class RoadLoadConditions(StrictModel):
    """What road load depends on: the road, load, tires and air (H07.5).

    ``baro_kpa`` is required (M01.8): ``C`` is ``½·ρ·Cd·A``, so it belongs to
    the air it was fitted in, and a power run at another barometer scales it
    by ``ρ_run / ρ_fit`` (:meth:`RoadLoadCoefficients.in_air`). This truck's
    roads span 63-82 kPa; a fit at 80 kPa used unscaled at 70 kPa overstates
    aero drag by 62 N at 100 km/h. The coast-down sessions' measured
    barometer (``obd:BAROMETRIC_PRESSURE`` median over the coasts) is what
    goes here; a record without one is refused, never given a default.
    """

    road: str = Field(min_length=1)
    date: dt.date
    sessions: list[str] = Field(min_length=1)
    payload_kg: float = Field(ge=0.0)
    tire_pressure_front_kpa: float = Field(gt=0.0)
    tire_pressure_rear_kpa: float = Field(gt=0.0)
    ambient_temp_c: float
    #: Bounds checked by :meth:`_baro_is_plausible` (so the refusal can name the
    #: likely unit) and stated in the schema.
    baro_kpa: float = Field(
        json_schema_extra={"minimum": ROAD_LOAD_BARO_MIN_KPA, "maximum": ROAD_LOAD_BARO_MAX_KPA}
    )

    @field_validator("baro_kpa")
    @classmethod
    def _baro_is_plausible(cls, value: float) -> float:
        if not ROAD_LOAD_BARO_MIN_KPA <= value <= ROAD_LOAD_BARO_MAX_KPA:
            raise ValueError(
                f"baro_kpa {value:g} is outside {ROAD_LOAD_BARO_MIN_KPA:g}-"
                f"{ROAD_LOAD_BARO_MAX_KPA:g} kPa (sea level to the highest roads): a value in "
                "hPa, mbar, psi or inHg? Record kPa"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _baro_is_stated(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("baro_kpa") is None:
            raise ValueError(
                "road_load conditions carry no baro_kpa: C is 1/2*rho*Cd*A and means "
                "nothing without the air density it was fitted in (M01.8). Record the "
                "coast-down sessions' measured barometric pressure; none is assumed"
            )
        return data


@dataclass(frozen=True)
class AirScaledRoadLoad:
    """A stored road load moved into a run's air: ``C`` times ``ρ_run / ρ_fit`` (M01.8).

    ``A`` (rolling resistance) and ``B`` do not depend on the air and are
    applied as stored.
    """

    a_n: float
    b_n_per_mps: float
    #: The stored ``C`` times :attr:`density_ratio`.
    c_n_per_mps2: float
    c_fit_n_per_mps2: float
    density_ratio: float
    fit_baro_kpa: float
    fit_ambient_temp_c: float
    run_baro_kpa: float
    run_ambient_temp_c: float
    temperature_source: str

    def force(self, speed_mps: FloatArray) -> FloatArray:
        v = np.asarray(speed_mps, dtype=np.float64)
        return self.a_n + self.b_n_per_mps * v + self.c_n_per_mps2 * v**2


class DirectionFit(StrictModel):
    """One direction's coast-down fit; the pair's difference is a wind/grade check."""

    a_n: float
    b_n_per_mps: float
    c_n_per_mps2: float
    #: Standard errors from the fit's residuals (white-noise assumption: a
    #: real drive's correlated vibration makes these optimistic).
    a_se_n: float = Field(ge=0.0)
    b_se_n_per_mps: float = Field(ge=0.0)
    c_se_n_per_mps2: float = Field(ge=0.0)
    residual_sd_n: float = Field(ge=0.0)
    n_samples: int = Field(ge=1)
    speed_min_mps: float
    speed_max_mps: float
    #: Mean static pitch of this heading (IMU offset ± the road's grade).
    static_pitch_deg: float


class RoadLoadCoefficients(StrictModel):
    """``F_rl(v) = A + B·v + C·v²`` (N, v in m/s), as stored in ``dyno.yaml``."""

    a_n: float = Field(gt=0.0)
    b_n_per_mps: float
    c_n_per_mps2: float = Field(gt=0.0)
    #: Standard errors of the pair means. ``B`` is the weakly identified one:
    #: over one coast its ``v`` column is nearly a mix of ``1`` and ``v²``.
    a_se_n: float = Field(ge=0.0)
    b_se_n_per_mps: float = Field(ge=0.0)
    c_se_n_per_mps2: float = Field(ge=0.0)
    #: ``pair`` averages both directions of one road; ``single`` carries grade
    #: and wind error and is refused by :class:`DynoProfile`.
    directions: Literal["pair", "single"]
    per_direction: dict[str, DirectionFit] = Field(min_length=1)
    residual_sd_n: float = Field(ge=0.0)
    test_mass_kg: float = Field(gt=0.0)
    neutral_effective_mass_kg: float = Field(gt=0.0)
    inertia: InertiaFlag
    squat_corrected: bool
    #: The session's IMU pitch offset, removed before fitting; ``None`` on a
    #: single direction, which cannot measure it (and is not storable).
    imu_pitch_offset_deg: float | None
    #: The IMU frame of record the coasts were resolved in (provenance: with
    #: the offset removed, A, B and C belong to no frame).
    imu_reference_id: str | None = None
    conditions: RoadLoadConditions

    def force(self, speed_mps: FloatArray) -> FloatArray:
        """Road load in the air it was *fitted* in; a run elsewhere uses :meth:`in_air`."""
        v = np.asarray(speed_mps, dtype=np.float64)
        return self.a_n + self.b_n_per_mps * v + self.c_n_per_mps2 * v**2

    def in_air(self, baro_kpa: float, ambient_temp_c: float) -> AirScaledRoadLoad:
        """This road load at a run's barometer and ambient temperature (M01.8)."""
        ratio = air_density_ratio(
            baro_kpa,
            ambient_temp_c,
            ref_baro_kpa=self.conditions.baro_kpa,
            ref_ambient_temp_c=self.conditions.ambient_temp_c,
        )
        return AirScaledRoadLoad(
            a_n=self.a_n,
            b_n_per_mps=self.b_n_per_mps,
            c_n_per_mps2=self.c_n_per_mps2 * ratio,
            c_fit_n_per_mps2=self.c_n_per_mps2,
            density_ratio=ratio,
            fit_baro_kpa=self.conditions.baro_kpa,
            fit_ambient_temp_c=self.conditions.ambient_temp_c,
            run_baro_kpa=baro_kpa,
            run_ambient_temp_c=ambient_temp_c,
            temperature_source=ROAD_LOAD_TEMPERATURE_SOURCE,
        )


@dataclass(frozen=True)
class RoadLoadFit:
    a_n: float
    b_n_per_mps: float
    c_n_per_mps2: float
    a_se_n: float
    b_se_n_per_mps: float
    c_se_n_per_mps2: float
    directions: Literal["pair", "single"]
    per_direction: dict[str, DirectionFit]
    residual_sd_n: float
    test_mass_kg: float
    neutral_effective_mass_kg: float
    inertia: InertiaFlag
    squat_corrected: bool
    imu_pitch_offset_deg: float | None
    imu_reference_id: str | None = None

    def force(self, speed_mps: FloatArray) -> FloatArray:
        v = np.asarray(speed_mps, dtype=np.float64)
        return self.a_n + self.b_n_per_mps * v + self.c_n_per_mps2 * v**2

    def to_record(self, conditions: RoadLoadConditions) -> RoadLoadCoefficients:
        return RoadLoadCoefficients(
            a_n=self.a_n,
            b_n_per_mps=self.b_n_per_mps,
            c_n_per_mps2=self.c_n_per_mps2,
            a_se_n=self.a_se_n,
            b_se_n_per_mps=self.b_se_n_per_mps,
            c_se_n_per_mps2=self.c_se_n_per_mps2,
            directions=self.directions,
            per_direction=dict(self.per_direction),
            residual_sd_n=self.residual_sd_n,
            test_mass_kg=self.test_mass_kg,
            neutral_effective_mass_kg=self.neutral_effective_mass_kg,
            inertia=self.inertia,
            squat_corrected=self.squat_corrected,
            imu_pitch_offset_deg=self.imu_pitch_offset_deg,
            imu_reference_id=self.imu_reference_id,
            conditions=conditions,
        )


def fit_road_load(
    coastdowns: Sequence[Leg],
    *,
    driveline: Driveline,
    test_mass_kg: float,
    squat: SquatLike | None,
    allow_single_direction: bool = False,
    min_samples: int = MIN_SAMPLES,
    min_speed_span_kph: float = COAST_MIN_SPEED_SPAN_KPH,
) -> RoadLoadFit | DynoRefusal:
    """Fit road load from neutral coast-downs, both directions (H07.5, SAE J2263 style).

    With the clutch in, wheel force is zero, so per sample
    ``F_rl = −m·f − (m_eff,neutral − m)·a``. Each heading is fitted to
    ``A + B·v + C·v²`` on its own, then the two are averaged: a steady wind
    ``w`` adds ``±2·C·w`` to ``B`` in opposite directions and cancels, leaving
    ``C·w²`` in ``A`` (with C = 0.55 N/(m/s)² and A = 180 N: 1.2 % at 2 m/s,
    2.75 % at 3 m/s). The
    IMU carries grade in ``f``; the pair cancels what it misses.

    The IMU's pitch offset does *not* cancel between directions: it is the
    same force in both, indistinguishable from rolling resistance (0.3 deg is
    ~119 N on a 2.3 t truck, most of a typical ``A``). The pair measures it
    first (:func:`estimate_pitch_offset`, from kinematics alone) and removes
    it; a single direction cannot, and records ``imu_pitch_offset_deg: None``.

    ``squat`` is required so the caller states whether the dive under braking
    was corrected; ``None`` is recorded as ``squat_corrected: False``.
    """
    name = "road_load"
    grouped = group_legs(coastdowns, name)
    if isinstance(grouped, DynoRefusal):
        return grouped
    frames = sorted({leg.samples.imu_reference_id for leg in coastdowns}, key=str)
    if len(frames) > 1:
        return DynoRefusal(
            name,
            "frame_mismatch",
            f"the coasts were resolved in different IMU frames {frames}; one pitch offset "
            "cannot be removed from both",
        )
    by_heading = grouped
    offset_deg: float | None = None
    if len(by_heading) == 1:
        if not allow_single_direction:
            (heading,) = by_heading
            return DynoRefusal(
                name,
                "single_direction",
                f"only {heading!r} was coasted; one direction leaves grade, wind and the "
                "IMU's pitch offset in A and B",
            )
    else:
        offset = estimate_pitch_offset(coastdowns, squat=squat, min_samples=min_samples)
        if isinstance(offset, DynoRefusal):
            return DynoRefusal(name, offset.kind, offset.reason)
        offset_deg = offset.deg
    m_eff = driveline.neutral_effective_mass(test_mass_kg)
    fits: dict[str, DirectionFit] = {}
    residual_sq: list[float] = []
    for heading, runs in by_heading.items():
        v = np.concatenate([s.speed_mps for s in runs])
        a = np.concatenate([s.accel_mps2 for s in runs])
        f = np.concatenate(
            [
                remove_sensor_tilt(
                    s.sf_lon_mps2,
                    s.accel_mps2,
                    squat=squat,
                    pitch_offset_deg=offset_deg or 0.0,
                )
                for s in runs
            ]
        )
        static = np.degrees(np.arcsin(np.clip((f - a) / STANDARD_GRAVITY, -1.0, 1.0)))
        if len(v) < min_samples:
            return DynoRefusal(
                name,
                "too_few_samples",
                f"{heading!r} has {len(v)} samples; the fit needs {min_samples}",
            )
        span_kph = float(v.max() - v.min()) * 3.6
        if span_kph < min_speed_span_kph:
            return DynoRefusal(
                name,
                "speed_span_too_small",
                f"{heading!r} spans {span_kph:.1f} km/h; separating B from C needs "
                f">= {min_speed_span_kph:.0f} km/h",
            )
        force = -test_mass_kg * f - (m_eff - test_mass_kg) * a
        design = np.column_stack([np.ones_like(v), v, v**2])
        coef, se, residual_sd = lstsq(design, force)
        fits[heading] = DirectionFit(
            a_n=float(coef[0]),
            b_n_per_mps=float(coef[1]),
            c_n_per_mps2=float(coef[2]),
            a_se_n=float(se[0]),
            b_se_n_per_mps=float(se[1]),
            c_se_n_per_mps2=float(se[2]),
            residual_sd_n=residual_sd,
            n_samples=len(v),
            speed_min_mps=float(v.min()),
            speed_max_mps=float(v.max()),
            static_pitch_deg=float(static.mean()) + (offset_deg or 0.0),
        )
        residual_sq.append(residual_sd**2)
    a_n = float(np.mean([fit.a_n for fit in fits.values()]))
    b_n = float(np.mean([fit.b_n_per_mps for fit in fits.values()]))
    c_n = float(np.mean([fit.c_n_per_mps2 for fit in fits.values()]))

    def _mean_se(values: list[float]) -> float:
        return math.sqrt(sum(v**2 for v in values)) / len(values)

    if a_n <= 0.0 or c_n <= 0.0:
        return DynoRefusal(
            name,
            "non_physical",
            f"A = {a_n:.1f} N, C = {c_n:.3f} N/(m/s)^2: rolling resistance and aero drag "
            "cannot be negative; check that the clutch was in and the speed is absolute",
        )
    return RoadLoadFit(
        a_n=a_n,
        b_n_per_mps=b_n,
        c_n_per_mps2=c_n,
        a_se_n=_mean_se([fit.a_se_n for fit in fits.values()]),
        b_se_n_per_mps=_mean_se([fit.b_se_n_per_mps for fit in fits.values()]),
        c_se_n_per_mps2=_mean_se([fit.c_se_n_per_mps2 for fit in fits.values()]),
        directions="pair" if len(fits) == 2 else "single",
        per_direction=fits,
        residual_sd_n=math.sqrt(float(np.mean(residual_sq))),
        test_mass_kg=test_mass_kg,
        neutral_effective_mass_kg=m_eff,
        inertia=driveline.inertia,
        squat_corrected=squat is not None,
        imu_pitch_offset_deg=offset_deg,
        imu_reference_id=coastdowns[0].samples.imu_reference_id,
    )
