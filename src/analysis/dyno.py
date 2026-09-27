"""A driving dyno's building blocks: squat, road load, rotating inertia, noise floor (H07).

At the wheels, during a pull in a fixed gear (``docs/roadmap/tasks/H07-driving-dyno.md``)::

    F_wheel = m·f + (m_eff − m)·a + F_rl(v)        F_rl(v) = A + B·v + C·v²

``f`` is the IMU's longitudinal **specific force** (``a + g·sin θ``, so it
carries the grade), ``a`` the acceleration along the road, ``m`` the test mass
and ``m_eff`` the mass plus the rotating inertia reflected through the gear.
This module holds the four pieces the power computation (H07.7) stands on:

* :func:`fit_squat` — the body pitches under acceleration and tilts the IMU;
  on flat ground ``f − a`` is that tilt alone, fitted as degrees per g (H07.4).
* :func:`fit_road_load` — ``A + B·v + C·v²`` from neutral coast-downs, where
  wheel force is zero; one fit per direction, averaged over the pair (H07.5).
* :class:`Driveline` / :func:`fit_engine_inertia` — ``m_eff`` per gear from
  stated estimates, then the engine-side inertia measured from pulls in two
  gears over the same rpm band (H07.6).
* :class:`DynoNoiseFloor` / :func:`compare_runs` — the same-day, same-ROM
  spread per rpm band, and the refusal to call two runs different inside it
  (H07.8).
* :func:`compute_power` — wheel power and torque against rpm from WOT pulls,
  with the engine figures only ever ``wheel / η`` and labelled estimates
  (H07.7). Its samples come from :mod:`src.analysis.dyno_inputs` (H07.3).

Rules carried from H07 and ``AGENTS.md``:

* **Vehicle facts are data.** Mass, tyre and gears come from the profile's
  :class:`~src.config.VehicleParameters`; inertia estimates, the squat
  coefficient, the road load and the noise floors live in the vehicle's
  ``dyno.yaml`` beside ``noise_floor.yaml``. Nothing here names a vehicle.
* **A stored result cannot exist without its conditions.** Every record
  (:class:`SquatCoefficient`, :class:`RoadLoadCoefficients`,
  :class:`EngineInertiaRecord`, :class:`DynoNoiseFloor`) has a required
  ``conditions`` field; a fit becomes a record only through ``to_record``.
* **Refusals name their cause.** Every estimator returns its result or a
  :class:`DynoRefusal` whose ``kind`` says which gate failed, never an empty
  or invented number.
* **Estimated is labelled.** Until the two-gear measurement exists, the
  engine-side inertia is an estimate and every result says ``inertia:
  estimated``.
* **A pitch offset belongs to one IMU frame.** :class:`PitchOffset` carries
  the ``reference_id`` of the frame it was measured in, and every estimator
  that applies one refuses samples from another (``frame_mismatch``).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from statistics import NormalDist
from typing import TYPE_CHECKING, Any, Literal, Protocol

import numpy as np
import numpy.typing as npt
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.analysis.committees import STANDARD_GRAVITY
from src.analysis.noise_floor import SIGMA_THRESHOLD
from src.atomic_write import atomic_write_text

if TYPE_CHECKING:
    from src.config import CanaryConfig, VehicleParameters

FloatArray = npt.NDArray[np.float64]

#: Per-vehicle dyno facts, in the profile data directory.
DYNO_FILENAME = "dyno.yaml"

#: Fewest samples any fit accepts. At the 10 Hz GNSS / smoothed-derivative
#: rate H07.3 delivers, five seconds.
MIN_SAMPLES = 50

#: Flat-ground gate for the squat fit, as a fraction (0.005 = 0.5 % grade,
#: 0.29 deg). A *constant* grade is absorbed by the fit's offset; what the fit
#: cannot separate from pitch is grade that changes along the stretch, and a
#: road already steeper than this is the one likely to. H07.12 picks a flat
#: road, which sits well inside it.
SQUAT_MAX_GRADE = 0.005

#: The squat fit's flat gate is applied to the grade of every stretch this
#: long (sliding by :data:`SQUAT_GRADE_STEP_M`), not only to the window's
#: mean: a crest (+1.2 % then −1.2 %) has a mean grade of zero and, with the
#: pull on the way up and the lift on the way down, reads as 6.9 deg/g of
#: "squat" against a true 1.2. Whether GPS altitude resolves 0.5 % over 200 m
#: depends entirely on its noise (:class:`AltitudeNoise`): white 1 m at 10 Hz
#: does (2 se ≈ 0.4 %); the wander this truck's receiver shows parked does not.
SQUAT_GRADE_SEGMENT_M = 200.0
SQUAT_GRADE_STEP_M = 50.0
#: A segment needs this many altitude samples to have a grade at all.
SQUAT_MIN_SEGMENT_SAMPLES = 10

#: Family-wise false-``not_flat`` rate across a window's segments. A 240 s
#: window holds ~27 independent 200 m segments; testing each at 2 se refused
#: 3 of 50 flat roads under AR(1) noise. The per-segment threshold is
#: ``max_grade + z·se`` with ``z = Φ⁻¹(1 − α/2m)`` for ``m`` independent
#: segments (Bonferroni): 2.3 for a 460 m window, 2.8 for 90 s, 3.1 for 240 s.
SQUAT_FLAT_FAMILY_ALPHA = 0.05

#: Grade that moves *in step with the acceleration* is what corrupts the squat
#: slope, even below the flat gate: a ±0.4 % rolling road in phase with ±0.25 g
#: reads as 2.1 deg/g against a true 1.2 and passes every grade gate. So the
#: fit also regresses the GPS-altitude grade (centred 200 m fit at each sample)
#: on ``a/g`` — the slope it would leak into ``k`` — and refuses when that
#: leak is this many of its standard errors from zero. 3, not 2.5: on real
#: parked altitude noise, bracketed, 2.5 refused 3 of 90 flat 90 s windows as
#: grade_correlated and 3 refused 0 of 90 in one sweep, but an independent
#: sweep of 2026-09-20T152758 still found 2 of 82 (the noise is nonstationary
#: and its z-tails heavy: expect a few per cent). ±0.4 % in phase is still
#: caught 50/50 and ±0.3 % 41/50 (90 s windows, white 1 m altitude noise).
SQUAT_GRADE_LEAK_SIGMA = 3.0

#: The squat fit needs acceleration that moves. Below 0.1 g of spread the
#: slope is set by noise; the H07.12 protocol spans ~0.3 g of pull to braking.
SQUAT_MIN_ACCEL_SPAN_G = 0.10

#: A coast-down direction must span this much speed. ``B·v`` and ``C·v²`` are
#: collinear over a narrow band, so a short coast yields confident nonsense;
#: SAE J2263 coasts from 115 to 15 km/h.
COAST_MIN_SPEED_SPAN_KPH = 30.0

#: Two-gear inertia: the pulls must share this much rpm.
INERTIA_MIN_RPM_OVERLAP = 1000.0

#: Two-gear inertia: the engine's angular acceleration must differ between the
#: gears by at least this much (rad/s²) on average. The inertia term is
#: ``I·Δα``; with Δα near zero the two gears carry the same information.
INERTIA_MIN_ALPHA_DIFFERENCE = 5.0

#: Rpm bin width for pairing two gears' pulls at the same engine speed.
INERTIA_RPM_BIN = 100.0

#: Fewest samples per gear in an rpm bin for that bin to count.
INERTIA_MIN_PER_BIN = 3

#: A noise floor needs at least this many pull pairs (H07.8).
MIN_PULL_PAIRS = 3

#: Rpm grid step for resampling dyno curves before comparing them.
CURVE_GRID_STEP_RPM = 50.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


RefusalKind = Literal[
    "invalid_input",
    "not_configured",
    "inputs_missing",
    "too_few_samples",
    "accel_span_too_small",
    "not_flat",
    "grade_unresolvable",
    "grade_correlated",
    "not_stationary",
    "speed_span_too_small",
    "single_direction",
    "too_many_directions",
    "non_physical",
    "same_gear",
    "rpm_overlap_too_small",
    "alpha_difference_too_small",
    "too_few_pull_pairs",
    "not_same_day_same_rom",
    "no_spread",
    "no_floor",
    "conditions_mismatch",
    "direction_mismatch",
    "no_overlap",
    "frame_mismatch",
    "gps_quality",
    "inputs_gap",
    "speed_too_coarse",
    "speed_scale_mismatch",
    "imu_unavailable",
    "gear_change",
    "clutch_slip",
    "gear_ratio_mismatch",
    "not_wot",
    "grade_varies",
    "rpm_span_too_small",
    "too_few_bins",
    "correction_refused",
]


@dataclass(frozen=True)
class DynoRefusal:
    """No result, and which kind of "no" it is."""

    estimator: str
    kind: RefusalKind
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": False,
            "estimator": self.estimator,
            "kind": self.kind,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def _as_float_array(name: str, values: Any) -> FloatArray:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    bad = int(np.count_nonzero(~np.isfinite(array)))
    if bad:
        raise ValueError(f"{name} has {bad} non-finite values; drop them upstream (H07.3)")
    return array


@dataclass(frozen=True)
class MotionSamples:
    """One window of motion on a single time base (what H07.3 produces).

    ``speed_mps`` is absolute road speed (GPS, or broadcast speed scaled by the
    measured ECU/GPS ratio); ``accel_mps2`` its smoothed derivative;
    ``sf_lon_mps2`` the IMU's longitudinal specific force, forward positive,
    gravity kept (``imu:SF_LON``, H02.4). ``altitude_m`` (GPS) is needed only
    by the squat fit's flat gate, ``rpm`` by the two-gear inertia fit and the
    power computation.

    ``imu_reference_id`` is the frame of record ``sf_lon_mps2`` was resolved
    in (``imu_frame.json``'s ``specific_force.reference_id``;
    :func:`src.analysis.dyno_inputs.read_motion_inputs` sets it). A pitch
    offset carries the id it was measured in, and every estimator that applies
    one refuses (``frame_mismatch``) samples from another frame. ``None`` is a
    frame of its own: synthetic data, which matches only an offset that also
    says ``None``.
    """

    t_s: FloatArray
    speed_mps: FloatArray
    accel_mps2: FloatArray
    sf_lon_mps2: FloatArray
    altitude_m: FloatArray | None = None
    rpm: FloatArray | None = None
    imu_reference_id: str | None = None

    def __post_init__(self) -> None:
        columns: dict[str, FloatArray] = {
            "t_s": _as_float_array("t_s", self.t_s),
            "speed_mps": _as_float_array("speed_mps", self.speed_mps),
            "accel_mps2": _as_float_array("accel_mps2", self.accel_mps2),
            "sf_lon_mps2": _as_float_array("sf_lon_mps2", self.sf_lon_mps2),
        }
        if self.altitude_m is not None:
            columns["altitude_m"] = _as_float_array("altitude_m", self.altitude_m)
        if self.rpm is not None:
            columns["rpm"] = _as_float_array("rpm", self.rpm)
        lengths = {name: len(values) for name, values in columns.items()}
        if len(set(lengths.values())) != 1:
            raise ValueError(f"MotionSamples columns differ in length: {lengths}")
        if len(columns["t_s"]) > 1 and np.any(np.diff(columns["t_s"]) <= 0):
            raise ValueError("t_s must be strictly increasing (one acquisition clock)")
        for name, values in columns.items():
            object.__setattr__(self, name, values)

    def __len__(self) -> int:
        return len(self.t_s)


class _SquatLike(Protocol):
    @property
    def deg_per_g(self) -> float: ...


class _RoadLoadLike(Protocol):
    def force(self, speed_mps: FloatArray) -> FloatArray: ...


def pitch_deg(accel_mps2: FloatArray, deg_per_g: float) -> FloatArray:
    """Body pitch (nose up positive) at each acceleration, in degrees."""
    return deg_per_g * (np.asarray(accel_mps2, dtype=np.float64) / STANDARD_GRAVITY)


def _correct_specific_force(
    sf_lon_mps2: FloatArray,
    accel_mps2: FloatArray,
    *,
    squat: _SquatLike | None,
    pitch_offset_deg: float,
) -> FloatArray:
    """``f − g·sin(δ(a) + μ)`` for a bare offset in degrees.

    Private: only callers that have already checked the offset's frame (or
    measure it themselves, with 0) may pass a float. The public form is
    :func:`correct_specific_force`, which takes the :class:`PitchOffset` and
    the samples and refuses a mismatched frame.
    """
    sf = np.asarray(sf_lon_mps2, dtype=np.float64)
    tilt: FloatArray = np.full(len(sf), float(pitch_offset_deg), dtype=np.float64)
    if squat is not None:
        tilt = tilt + pitch_deg(accel_mps2, squat.deg_per_g)
    corrected: FloatArray = sf - STANDARD_GRAVITY * np.sin(np.radians(tilt))
    return corrected


@dataclass(frozen=True)
class Leg:
    """One run in one direction on a road, labelled by heading (``northbound``).

    A coast-down (H07.5), a pull, or a steady stretch: what matters is that the
    two headings of a pair covered the same road.
    """

    heading: str
    samples: MotionSamples


@dataclass(frozen=True)
class PitchOffset:
    """The IMU's residual pitch offset for one session, from a direction pair.

    The offset is whatever the frame resolution left along the longitudinal
    axis: mounting residual, accelerometer bias, the grade of the spot the
    gravity vector was estimated on. It adds ``g·sin μ`` to every specific
    force sample and, unlike grade, does not change sign when the vehicle
    turns round: left in, 0.3 deg is ~119 N of phantom rolling resistance on a
    2.3 t truck and moves a two-gear inertia by ~44 %.
    """

    deg: float
    #: Mean static pitch per heading (``μ ± grade``); half their difference is
    #: the mean grade of the stretch, a check against the road's GPS profile.
    heading_pitch_deg: dict[str, float]
    n_samples: int
    #: The IMU frame of record the offset was measured in
    #: (:attr:`MotionSamples.imu_reference_id`). Required, and checked by every
    #: estimator that applies the offset: a pitch offset is a property of one
    #: rotation, and meaningless in another.
    reference_id: str | None


def frame_mismatch(
    estimator: str, pitch_offset: PitchOffset, samples: Sequence[MotionSamples]
) -> DynoRefusal | None:
    """``frame_mismatch`` when any samples were resolved in another IMU frame
    than the one ``pitch_offset`` was measured in, else ``None``."""
    found = {s.imu_reference_id for s in samples}
    if found == {pitch_offset.reference_id}:
        return None
    frames = sorted(found, key=str)
    return DynoRefusal(
        estimator,
        "frame_mismatch",
        f"the pitch offset was measured in IMU frame {pitch_offset.reference_id!r}; the "
        f"samples are in {frames}. An offset belongs to the rotation it was measured "
        "against: measure it again from a direction pair in this frame",
    )


def _grouped(legs: Sequence[Leg], estimator: str) -> dict[str, list[MotionSamples]] | DynoRefusal:
    if not legs:
        return DynoRefusal(estimator, "inputs_missing", "no legs were given")
    by_heading: dict[str, list[MotionSamples]] = {}
    for leg in legs:
        if not leg.heading:
            return DynoRefusal(estimator, "invalid_input", "a leg has no heading label")
        by_heading.setdefault(leg.heading, []).append(leg.samples)
    if len(by_heading) > 2:
        return DynoRefusal(
            estimator,
            "too_many_directions",
            f"headings {sorted(by_heading)}: a pair is one road driven both ways",
        )
    return by_heading


def correct_specific_force(
    samples: MotionSamples,
    *,
    squat: _SquatLike | None,
    pitch_offset: PitchOffset,
) -> FloatArray | DynoRefusal:
    """Specific force with the sensor's tilt removed: ``f − g·sin(δ(a) + μ)``.

    ``δ(a)`` is body squat (``None`` leaves it in; callers record that as
    ``squat_corrected: False``; half a degree is ~3 % of a 0.3 g pull). ``μ``
    is the IMU's residual pitch offset, measured by
    :func:`estimate_pitch_offset` in one IMU frame: ``samples`` from any other
    frame (:attr:`MotionSamples.imu_reference_id` against
    :attr:`PitchOffset.reference_id`) are refused (``frame_mismatch``), never
    corrected with an offset that belongs to another rotation. Both keywords
    are required so neither can be forgotten silently.
    """
    mismatch = frame_mismatch("correct_specific_force", pitch_offset, [samples])
    if mismatch is not None:
        return mismatch
    return _correct_specific_force(
        samples.sf_lon_mps2, samples.accel_mps2, squat=squat, pitch_offset_deg=pitch_offset.deg
    )


def estimate_pitch_offset(
    legs: Sequence[Leg],
    *,
    squat: _SquatLike | None,
    min_samples: int = MIN_SAMPLES,
) -> PitchOffset | DynoRefusal:
    """The session's IMU pitch offset, from both directions of one road.

    Per sample ``f − a − g·sin δ(a) = g·sin(θ + μ)``: road grade ``θ`` flips
    sign with the heading and ``μ`` does not, so the mean static pitch of the
    two headings is ``μ``. Exact when both legs cover the same stretch; if
    they cover different sections, half the difference of those sections'
    mean grades leaks in. One heading cannot separate the two and is refused.
    """
    name = "pitch_offset"
    grouped = _grouped(legs, name)
    if isinstance(grouped, DynoRefusal):
        return grouped
    frames = sorted({leg.samples.imu_reference_id for leg in legs}, key=str)
    if len(frames) > 1:
        return DynoRefusal(
            name,
            "frame_mismatch",
            f"the legs were resolved in different IMU frames {frames}; an offset from their "
            "mean would belong to neither",
        )
    if len(grouped) == 1:
        (heading,) = grouped
        return DynoRefusal(
            name,
            "single_direction",
            f"only {heading!r} was driven; one direction cannot separate the IMU's pitch "
            "offset from the road's grade",
        )
    heading_pitch: dict[str, float] = {}
    total = 0
    for heading, runs in grouped.items():
        n = sum(len(s) for s in runs)
        if n < min_samples:
            return DynoRefusal(
                name,
                "too_few_samples",
                f"{heading!r} has {n} samples; the offset needs {min_samples}",
            )
        static = np.concatenate(
            [
                _correct_specific_force(
                    s.sf_lon_mps2, s.accel_mps2, squat=squat, pitch_offset_deg=0.0
                )
                - s.accel_mps2
                for s in runs
            ]
        )
        pitch = np.degrees(np.arcsin(np.clip(static / STANDARD_GRAVITY, -1.0, 1.0)))
        heading_pitch[heading] = float(pitch.mean())
        total += n
    return PitchOffset(
        deg=float(np.mean(list(heading_pitch.values()))),
        heading_pitch_deg=heading_pitch,
        n_samples=total,
        reference_id=legs[0].samples.imu_reference_id,
    )


def _distance_m(samples: MotionSamples) -> FloatArray:
    steps = np.diff(samples.t_s) * 0.5 * (samples.speed_mps[1:] + samples.speed_mps[:-1])
    return np.concatenate([[0.0], np.cumsum(steps)])


def _lstsq(design: FloatArray, target: FloatArray) -> tuple[FloatArray, FloatArray, float]:
    """Coefficients, their standard errors, and the residual sd."""
    coef, *_ = np.linalg.lstsq(design, target, rcond=None)
    residual = target - design @ coef
    dof = max(len(target) - design.shape[1], 1)
    sigma2 = float(residual @ residual) / dof
    cov = sigma2 * np.linalg.pinv(design.T @ design)
    se = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    return (
        np.asarray(coef, dtype=np.float64),
        np.asarray(se, dtype=np.float64),
        math.sqrt(sigma2),
    )


# ---------------------------------------------------------------------------
# H07.4 — squat
# ---------------------------------------------------------------------------


class AltitudeNoise(_Strict):
    """GPS altitude noise, measured where the road contributes nothing: parked.

    GNSS altitude is not white. On this truck, parked, it wanders by
    2.5-5 m (sd) with an autocorrelation still 0.6-0.97 after 20 s
    (``~/canary-data/sessions-0920``; ``tests/fixtures/gnss_altitude``), so a
    grade standard error that assumes independent samples is 3-10x too small,
    and a flat road reads as a crest. From altitude alone, road shape and
    receiver wander cannot be told apart at the scale of a segment; a
    stationary stretch of the same session measures the wander by itself.

    Modelled as white noise plus a powered-exponential wander, with variogram
    ``γ(h) = white·[h > 0] + correlated·(1 − exp(−(h/τ)^α))``, 0 < α < 2 (a
    valid variogram throughout). α = 1 is an AR(1)/Ornstein-Uhlenbeck wander;
    real parked altitude here is smoother, γ growing as ~h^1.5 from 1 to 30 s,
    which an exponential (linear at short lags) misdescribes at the lags a
    segment spans. Every
    standard error in :func:`fit_squat` is ``−wᵀ·Γ·w`` for weights ``w`` that
    sum to zero (a slope), which needs only the variogram, never the sill, so
    a receiver that has not stopped wandering within the reference is still
    described at the lags a segment spans.
    """

    white_var_m2: float = Field(ge=0.0)
    correlated_var_m2: float = Field(ge=0.0)
    tau_s: float = Field(gt=0.0)
    #: Shape of the wander's variogram at lags below ``tau_s``.
    alpha: float = Field(default=1.0, gt=0.0, lt=2.0)
    #: Length of the stationary reference (s) and its sample count.
    reference_s: float = Field(gt=0.0)
    n_reference: int = Field(ge=2)

    @model_validator(mode="after")
    def _some_noise(self) -> AltitudeNoise:
        if self.white_var_m2 + self.correlated_var_m2 <= 0.0:
            raise ValueError("an altitude noise model with zero variance resolves nothing")
        return self

    def variogram(self, lag_s: FloatArray) -> FloatArray:
        h = np.abs(np.asarray(lag_s, dtype=np.float64))
        gamma: FloatArray = self.white_var_m2 * (h > 0) + self.correlated_var_m2 * (
            1.0 - np.exp(-((h / self.tau_s) ** self.alpha))
        )
        return gamma


#: A stationary reference for :func:`measure_altitude_noise`: at least this
#: long, and never faster than this (GNSS speed at rest jitters ~0.1 m/s).
#: The segment se it yields is unbiased (median model/true 0.96-1.03 for white,
#: τ = 2, 10 and 40 s noise); its spread shrinks with length: 10th-90th
#: percentile 0.84-1.10 at 120 s, 0.93-1.09 at 300 s. Park for 3 minutes.
ALTITUDE_NOISE_MIN_REFERENCE_S = 180.0
ALTITUDE_NOISE_MAX_SPEED_MPS = 0.5
_ALTITUDE_NOISE_ALPHAS = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 1.9)


def measure_altitude_noise(
    samples: MotionSamples,
    *,
    min_duration_s: float = ALTITUDE_NOISE_MIN_REFERENCE_S,
    max_speed_mps: float = ALTITUDE_NOISE_MAX_SPEED_MPS,
) -> AltitudeNoise | DynoRefusal:
    """Fit :class:`AltitudeNoise` to a stationary stretch of the same session.

    The empirical variogram is taken on a uniform grid at the reference's
    median sample interval, at ~40 log-spaced lags up to half its length, and
    fitted (relative least squares, non-negative variances) over a grid of
    ``τ`` from half a sample to 20x the reference and of ``α`` from 0.5 to 1.9.
    """
    name = "altitude_noise"
    if samples.altitude_m is None:
        return DynoRefusal(name, "inputs_missing", "GPS altitude was not captured")
    moving = float(np.max(np.abs(samples.speed_mps)))
    if moving > max_speed_mps:
        return DynoRefusal(
            name,
            "not_stationary",
            f"speed reaches {moving:.2f} m/s; the noise reference must be parked "
            f"(<= {max_speed_mps:.1f} m/s) so the road contributes nothing",
        )
    duration = float(samples.t_s[-1] - samples.t_s[0])
    if duration < min_duration_s:
        return DynoRefusal(
            name,
            "too_few_samples",
            f"{duration:.1f} s parked; the noise reference needs {min_duration_s:.0f} s",
        )
    step = float(np.median(np.diff(samples.t_s)))
    grid = np.arange(samples.t_s[0], samples.t_s[-1] + 1e-9, step)
    z = np.interp(grid, samples.t_s, samples.altitude_m)
    lags = np.unique(np.round(np.logspace(0.0, math.log10(len(z) / 2), 40)).astype(int))
    lags = lags[(lags >= 1) & (lags < len(z) - 1)]
    gamma = np.array([0.5 * float(np.mean((z[k:] - z[:-k]) ** 2)) for k in lags])
    if not np.any(gamma > 0.0):
        return DynoRefusal(
            name,
            "no_spread",
            "GPS altitude did not change at all over the reference (frozen or quantised); "
            "it cannot say how precisely altitude resolves grade",
        )
    h = lags * step
    weight = 1.0 / np.maximum(gamma, float(gamma[gamma > 0].min()))
    best: tuple[float, float, float, float, float] | None = None
    taus = np.logspace(math.log10(step / 2.0), math.log10(20.0 * duration), 100)
    for tau, alpha in ((t, a) for t in taus for a in _ALTITUDE_NOISE_ALPHAS):
        shape = 1.0 - np.exp(-((h / tau) ** alpha))
        candidates: list[tuple[float, float]] = []
        design = np.column_stack([np.ones_like(h), shape]) * weight[:, None]
        coef, *_ = np.linalg.lstsq(design, gamma * weight, rcond=None)
        if coef[0] >= 0.0 and coef[1] >= 0.0:
            candidates.append((float(coef[0]), float(coef[1])))
        for column in (0, 1):
            x = design[:, column]
            value = max(float(x @ (gamma * weight)) / float(x @ x), 0.0)
            candidates.append((value, 0.0) if column == 0 else (0.0, value))
        for white, corr in candidates:
            model = white + corr * shape
            sse = float(np.sum(((model - gamma) * weight) ** 2))
            if best is None or sse < best[0]:
                best = (sse, white, corr, float(tau), float(alpha))
    assert best is not None
    _, white, corr, tau, alpha = best
    return AltitudeNoise(
        white_var_m2=white,
        correlated_var_m2=corr,
        tau_s=tau,
        alpha=alpha,
        reference_s=duration,
        n_reference=len(samples),
    )


class SquatConditions(_Strict):
    """What the squat coefficient depends on: load and tyres (H07.4)."""

    payload_kg: float = Field(ge=0.0)
    tyre_pressure_front_kpa: float = Field(gt=0.0)
    tyre_pressure_rear_kpa: float = Field(gt=0.0)
    road: str = Field(min_length=1)
    date: dt.date
    sessions: list[str] = Field(min_length=1)


class SquatCoefficient(_Strict):
    """Body pitch per g of acceleration, as stored in ``dyno.yaml``."""

    #: Nose-up pitch, degrees per g of acceleration.
    deg_per_g: float
    #: Pitch at zero acceleration on the fitted stretch: that session's IMU
    #: pitch offset plus the stretch's grade. Provenance only — never applied
    #: to another session (:func:`estimate_pitch_offset` measures each one).
    offset_deg: float
    residual_sd_deg: float = Field(ge=0.0)
    n_samples: int = Field(ge=MIN_SAMPLES)
    accel_span_g: float = Field(gt=0.0)
    #: GPS-altitude grade of the stretch it was fitted on (fraction).
    grade: float
    #: Largest |grade| of any 200 m segment of that stretch (fraction).
    max_segment_grade: float = Field(ge=0.0)
    #: How much the stretch's GPS grade, moving with the acceleration, would
    #: add to ``deg_per_g`` (deg/g) and its standard error: the fit's residual
    #: exposure to grade, below the resolution the refusal can act on.
    grade_leak_deg_per_g: float
    grade_leak_se_deg_per_g: float = Field(ge=0.0)
    #: The parked altitude-noise references every grade se above was computed
    #: from (each se is the larger across them).
    altitude_noise: list[AltitudeNoise] = Field(min_length=1)
    conditions: SquatConditions


@dataclass(frozen=True)
class SquatFit:
    deg_per_g: float
    deg_per_g_se: float
    offset_deg: float
    residual_sd_deg: float
    n_samples: int
    accel_span_g: float
    grade: float
    grade_se: float
    max_segment_grade: float
    grade_leak_deg_per_g: float
    grade_leak_se_deg_per_g: float
    altitude_noise: list[AltitudeNoise]

    def to_record(self, conditions: SquatConditions) -> SquatCoefficient:
        return SquatCoefficient(
            deg_per_g=self.deg_per_g,
            offset_deg=self.offset_deg,
            residual_sd_deg=self.residual_sd_deg,
            n_samples=self.n_samples,
            accel_span_g=self.accel_span_g,
            grade=self.grade,
            max_segment_grade=self.max_segment_grade,
            grade_leak_deg_per_g=self.grade_leak_deg_per_g,
            grade_leak_se_deg_per_g=self.grade_leak_se_deg_per_g,
            altitude_noise=list(self.altitude_noise),
            conditions=conditions,
        )


def _accel_span_g(accel: FloatArray) -> float:
    lo, hi = np.percentile(accel, [2.0, 98.0])
    return float(hi - lo) / STANDARD_GRAVITY


#: Standard errors are exact double sums over sample pairs; above this many
#: samples a series is strided down first (the slope and its se both come
#: from the strided samples, so they stay consistent).
_MAX_SE_SAMPLES = 400
_MAX_LEAK_SAMPLES = 1200


def _stride(n: int, limit: int) -> slice:
    return slice(None, None, max(1, math.ceil(n / limit)))


def _weighted_var(weights: FloatArray, t_s: FloatArray, noise: AltitudeNoise) -> float:
    """``Var(Σ w·z)`` for weights summing to zero, from the noise variogram.

    ``−wᵀΓw`` with ``Γ_ij = γ(|t_i − t_j|)``. On a uniform time grid (what
    H07.3 delivers) ``Γ`` is Toeplitz, so the same sum is taken over lags from
    the autocorrelation of ``w``; otherwise the dense double sum.
    """
    steps = np.diff(t_s)
    if len(steps) and np.allclose(steps, steps[0], rtol=1e-6, atol=1e-9):
        lagged = np.correlate(weights, weights, mode="full")[len(weights) - 1 :]
        gamma_k = noise.variogram(np.arange(len(weights), dtype=np.float64) * float(steps[0]))
        total = float(gamma_k[0] * lagged[0] + 2.0 * np.dot(gamma_k[1:], lagged[1:]))
    else:
        total = float(weights @ noise.variogram(t_s[:, None] - t_s[None, :]) @ weights)
    return max(-total, 0.0)


NoiseReferences = AltitudeNoise | Sequence[AltitudeNoise]


def _references(noise: NoiseReferences) -> list[AltitudeNoise]:
    return [noise] if isinstance(noise, AltitudeNoise) else list(noise)


def _worst_sd(weights: FloatArray, t_s: FloatArray, noise: NoiseReferences) -> float:
    """``sd(Σ w·z)`` under the noisiest of the references, for these weights.

    Each se is maximised on its own: the reference that is noisier at a
    segment's lags need not be the one that is noisier over a whole window.
    """
    return max(math.sqrt(_weighted_var(weights, t_s, ref)) for ref in _references(noise))


def _grade_with_se(
    t_s: FloatArray, distance: FloatArray, altitude: FloatArray, noise: NoiseReferences
) -> tuple[float, float]:
    """OLS grade of altitude on distance, and its se under the noise model(s)."""
    pick = _stride(len(t_s), _MAX_SE_SAMPLES)
    t, s, z = t_s[pick], distance[pick], altitude[pick]
    centred = s - s.mean()
    sxx = float(centred @ centred)
    if sxx <= 0.0:
        return 0.0, math.inf
    weights = centred / sxx
    return float(weights @ z), _worst_sd(weights, t, noise)


@dataclass(frozen=True)
class _SegmentGrade:
    start_m: float
    end_m: float
    grade: float
    se: float


def _segment_grades(
    t_s: FloatArray,
    distance: FloatArray,
    altitude: FloatArray,
    noise: NoiseReferences,
    *,
    length_m: float,
    step_m: float,
) -> list[_SegmentGrade]:
    """Grade of every ``length_m`` stretch, sliding by ``step_m``, with its se."""
    total = float(distance[-1])
    starts = np.arange(0.0, max(total - length_m, 0.0) + 1e-9, step_m)
    segments: list[_SegmentGrade] = []
    for start in starts:
        end = min(start + length_m, total)
        inside = (distance >= start) & (distance <= end)
        if int(inside.sum()) < SQUAT_MIN_SEGMENT_SAMPLES:
            continue
        grade, se = _grade_with_se(t_s[inside], distance[inside], altitude[inside], noise)
        segments.append(_SegmentGrade(float(start), float(end), grade, se))
    return segments


def _local_grade_operator(distance: FloatArray, half_window_m: float) -> FloatArray:
    """Row ``i`` holds the OLS slope weights over ``±half_window_m`` around sample ``i``."""
    n = len(distance)
    operator = np.zeros((n, n))
    lo = np.searchsorted(distance, distance - half_window_m, side="left")
    hi = np.searchsorted(distance, distance + half_window_m, side="right")
    for i in range(n):
        s = distance[lo[i] : hi[i]]
        centred = s - s.mean()
        sxx = float(centred @ centred)
        if len(s) >= 3 and sxx > 0.0:
            operator[i, lo[i] : hi[i]] = centred / sxx
    return operator


def _grade_leak(
    t_s: FloatArray,
    distance: FloatArray,
    altitude: FloatArray,
    accel_g: FloatArray,
    noise: NoiseReferences,
) -> tuple[float, float]:
    """Slope of local GPS grade (deg) on ``a/g``, and its se under the noise model.

    The leak is linear in altitude, ``(180/π)·uᵀ·L·z`` for the local-grade
    operator ``L`` and the regression weights ``u``, so its variance is the
    exact ``−cᵀΓc`` with ``c = (180/π)·Lᵀu`` (``Σc = 0``).
    """
    pick = _stride(len(t_s), _MAX_LEAK_SAMPLES)
    t, s, z, x = t_s[pick], distance[pick], altitude[pick], accel_g[pick]
    operator = _local_grade_operator(s, SQUAT_GRADE_SEGMENT_M / 2.0)
    ok = np.abs(operator).sum(axis=1) > 0.0
    x_ok = x[ok]
    centred = x_ok - x_ok.mean()
    sxx = float(centred @ centred)
    if sxx <= 0.0:
        return 0.0, math.inf
    u = centred / sxx
    grade_deg = np.degrees(np.arctan(operator[ok] @ z))
    leak = float(u @ grade_deg)
    combined = np.degrees(operator[ok].T @ u)
    return leak, _worst_sd(combined, t, noise)


def fit_squat(
    samples: MotionSamples,
    *,
    altitude_noise: NoiseReferences,
    max_grade: float = SQUAT_MAX_GRADE,
    min_samples: int = MIN_SAMPLES,
    min_accel_span_g: float = SQUAT_MIN_ACCEL_SPAN_G,
) -> SquatFit | DynoRefusal:
    """Fit body pitch against acceleration on flat ground (H07.4).

    On flat ground ``f − a = g·sin(θ + δ)`` with θ constant, so
    ``asin((f − a)/g)`` in degrees is fitted as ``offset + k·(a/g)``: ``k`` is
    the squat coefficient, the offset absorbs the IMU's pitch offset and any
    constant grade (so it belongs to that session and stretch, and no
    correction applies it — :func:`estimate_pitch_offset` separates the two).

    Flatness is checked, not assumed: GPS altitude is fitted against distance
    travelled, over the whole window and over every 200 m segment of it
    (:data:`SQUAT_GRADE_SEGMENT_M`). ``altitude_noise`` is required — measured
    by :func:`measure_altitude_noise` on a parked stretch of the same session
    — because every standard error comes from it: GNSS altitude wanders, and
    a se that assumes independent samples refused 200 of 200 flat roads
    under AR(1) noise (τ = 10 s, 1 m). The wander is not stationary either
    (its 10 s variogram varied ~20x across one parked afternoon), so bracket
    the runs with a reference before and after and pass both: every se (the
    window's, each segment's, the leak's) is computed under each reference
    and the larger kept. Then, in this order:

    * ``not_flat`` for any grade shown to exceed the gate, however noisy the
      altitude: the window's mean beyond ``max_grade`` + 2 se, or any
      segment's beyond ``max_grade`` + z se (z family-wise over the window's
      segments, :data:`SQUAT_FLAT_FAMILY_ALPHA`). A 4 % grade read as
      4.05 ± 0.30 % is not flat, whatever else the noise hides. A crest or a
      rolling road with a flat mean is caught here.
    * ``grade_unresolvable`` only then, when the altitude noise leaves the
      window's or a segment's 2 se above ``max_grade``, or the altitude is too
      sparse to form 200 m segments: nothing was shown to exceed the gate, but
      GPS altitude cannot show the road is inside it either.
    * ``grade_correlated`` when the local GPS grade, regressed on ``a/g``,
      would leak more than :data:`SQUAT_GRADE_LEAK_SIGMA` se into ``k``: grade
      below the gate that moves in step with the acceleration (a swing of
      ``Δθ`` degrees in phase with ``Δa`` g adds ``Δθ/Δa``).

    **What none of them can see.** Grade that changes over less than a
    200 m segment, or by less than the altitude noise resolves, is invisible;
    its possible effect on ``k`` is bounded by ~3x the recorded leak se
    (0.12 deg/g for 90 s, 0.10 for 240 s under white 1 m noise: ±0.1 % in
    phase, +0.2 deg/g, passes 47 of 50), and accelerating at different places,
    in both directions, keeps it out of phase.

    **How often a real window can be judged, and how often it is wrongly
    refused.** On this truck's real parked GNSS altitude (2026-09-20/21, the
    pre-HAT USB receiver), with bracketed references, about half of flat
    windows score and half are ``grade_unresolvable``; the rest are false
    refusals from nonstationary noise worse than both references, whose
    z-tails are heavy. Measured: 42 scored / 47 unresolvable / 1
    ``not_flat`` of 90 (one sweep, 90 s windows); 2 ``grade_correlated`` and
    1 ``not_flat`` of 82 (an independent sweep of 2026-09-20T152758). Expect a
    few per cent. With one reference taken at the start of a parked run,
    every window (96/96) was unresolvable.

    Noise in ``a`` enters both sides with opposite signs, which biases ``k``
    low by about ``var(noise)/var(a)`` in the fit's own units; H07.3's
    smoothed derivative over a 0.3 g span keeps that to about 1 %.
    """
    name = "squat"
    n = len(samples)
    if n < min_samples:
        return DynoRefusal(name, "too_few_samples", f"{n} samples; the fit needs {min_samples}")
    if samples.altitude_m is None:
        return DynoRefusal(
            name,
            "inputs_missing",
            "GPS altitude was not captured, so the stretch cannot be shown to be flat",
        )
    distance = _distance_m(samples)
    if distance[-1] <= 0.0:
        return DynoRefusal(name, "grade_unresolvable", "the vehicle did not move in the window")
    references = _references(altitude_noise)
    if not references:
        return DynoRefusal(name, "inputs_missing", "no parked altitude-noise reference was given")
    # What the noise does over the time a 200 m segment takes at this window's
    # speed: identifiable from any reference, unlike its sill or τ when the
    # wander has not levelled off within the reference.
    segment_s = SQUAT_GRADE_SEGMENT_M / max(float(np.median(samples.speed_mps)), 1.0)
    wander_m = max(math.sqrt(float(ref.variogram(np.array([segment_s]))[0])) for ref in references)
    noise_text = (
        f"parked altitude noise changes by {wander_m:.2f} m (sd) over the {segment_s:.1f} s "
        f"a {SQUAT_GRADE_SEGMENT_M:.0f} m segment takes"
    )
    grade, grade_se = _grade_with_se(samples.t_s, distance, samples.altitude_m, references)
    if abs(grade) > max_grade + 2.0 * grade_se:
        return DynoRefusal(
            name,
            "not_flat",
            f"GPS altitude gives a {100 * grade:+.2f} % grade (se {100 * grade_se:.2f} %) "
            f"over {distance[-1]:.0f} m; the squat fit needs flat ground "
            f"(|grade| <= {100 * max_grade:.2f} % + 2 se)",
        )
    segments = _segment_grades(
        samples.t_s,
        distance,
        samples.altitude_m,
        references,
        length_m=SQUAT_GRADE_SEGMENT_M,
        step_m=SQUAT_GRADE_STEP_M,
    )
    independent = max(float(distance[-1]) / SQUAT_GRADE_SEGMENT_M, 1.0)
    z = NormalDist().inv_cdf(1.0 - SQUAT_FLAT_FAMILY_ALPHA / (2.0 * independent))
    for segment in segments:
        if abs(segment.grade) > max_grade + z * segment.se:
            return DynoRefusal(
                name,
                "not_flat",
                f"GPS altitude gives a {100 * segment.grade:+.2f} % grade "
                f"(se {100 * segment.se:.2f} %) over {segment.start_m:.0f}-"
                f"{segment.end_m:.0f} m, though the window's mean is {100 * grade:+.2f} %; "
                "grade that changes along the stretch reads as pitch "
                f"(|grade| <= {100 * max_grade:.2f} % + {z:.1f} se per "
                f"{SQUAT_GRADE_SEGMENT_M:.0f} m, {independent:.0f} independent segments)",
            )
    if 2.0 * grade_se > max_grade:
        return DynoRefusal(
            name,
            "grade_unresolvable",
            f"GPS altitude resolves the window's grade only to +/-{200 * grade_se:.2f} % "
            f"(2 se) over {distance[-1]:.0f} m ({noise_text}); the flat gate is "
            f"{100 * max_grade:.2f} %. Nothing was shown to exceed it; nothing shows the "
            "road is inside it",
        )
    if not segments and distance[-1] >= SQUAT_GRADE_SEGMENT_M:
        return DynoRefusal(
            name,
            "grade_unresolvable",
            f"GPS altitude is too sparse to judge any {SQUAT_GRADE_SEGMENT_M:.0f} m segment "
            f"(each needs {SQUAT_MIN_SEGMENT_SAMPLES} samples), so a crest could pass unseen",
        )
    resolved = [seg for seg in segments if 2.0 * seg.se <= max_grade]
    if len(resolved) < len(segments):
        worst = max(seg.se for seg in segments)
        return DynoRefusal(
            name,
            "grade_unresolvable",
            f"{len(segments) - len(resolved)} of {len(segments)} "
            f"{SQUAT_GRADE_SEGMENT_M:.0f} m segments resolve grade only to "
            f"+/-{200 * worst:.2f} % (2 se, {noise_text}); the flat gate is "
            f"{100 * max_grade:.2f} %. The road may be flat; GPS altitude cannot show it",
        )
    max_segment_grade = max((abs(seg.grade) for seg in segments), default=abs(grade))
    leak, leak_se = _grade_leak(
        samples.t_s,
        distance,
        samples.altitude_m,
        samples.accel_mps2 / STANDARD_GRAVITY,
        references,
    )
    if abs(leak) > SQUAT_GRADE_LEAK_SIGMA * leak_se:
        return DynoRefusal(
            name,
            "grade_correlated",
            f"the road's GPS grade moves with the acceleration: on its own it would add "
            f"{leak:+.2f} deg/g (se {leak_se:.2f}) to the squat coefficient. Accelerate at "
            "different places, or in both directions over the same stretch",
        )
    span = _accel_span_g(samples.accel_mps2)
    if span < min_accel_span_g:
        return DynoRefusal(
            name,
            "accel_span_too_small",
            f"acceleration spans {span:.3f} g (2nd-98th percentile); the slope needs "
            f">= {min_accel_span_g:.2f} g",
        )
    ratio = np.clip((samples.sf_lon_mps2 - samples.accel_mps2) / STANDARD_GRAVITY, -1.0, 1.0)
    pitch = np.degrees(np.arcsin(ratio))
    design = np.column_stack([np.ones(n), samples.accel_mps2 / STANDARD_GRAVITY])
    coef, se, residual_sd = _lstsq(design, pitch)
    return SquatFit(
        deg_per_g=float(coef[1]),
        deg_per_g_se=float(se[1]),
        offset_deg=float(coef[0]),
        residual_sd_deg=residual_sd,
        n_samples=n,
        accel_span_g=span,
        grade=grade,
        grade_se=grade_se,
        max_segment_grade=max_segment_grade,
        grade_leak_deg_per_g=leak,
        grade_leak_se_deg_per_g=leak_se,
        altitude_noise=references,
    )


# ---------------------------------------------------------------------------
# H07.6 — rotating inertia (the driveline model the other fits need)
# ---------------------------------------------------------------------------

InertiaFlag = Literal["estimated", "measured_engine"]


class InertiaConditions(_Strict):
    road: str = Field(min_length=1)
    date: dt.date
    sessions: list[str] = Field(min_length=1)
    payload_kg: float = Field(ge=0.0)
    tune_revision: str = Field(min_length=1)


#: The one inertia convention (H07.6). The crank, flywheel and clutch sit
#: upstream of the gearbox, so the torque that accelerates them never reaches
#: the lossy gears: the wheel sees ``η·N/r·(T − I_e·α)``. Two gears measure the
#: product ``η·I_e``; the physical ``I_e`` is that divided by the assumed ``η``.
INERTIA_CONVENTION = (
    "engine inertia upstream of torque-proportional driveline losses: wheel force = "
    "eta*N/r*(T - I_e*alpha); the two-gear fit measures eta*I_e (reflected_kg_m2) and "
    "engine_kg_m2 = reflected_kg_m2 / driveline_efficiency; eta equal in both gears"
)


class EngineInertiaRecord(_Strict):
    """A two-gear measurement of the engine-side inertia (H07.6)."""

    #: What the fit measures: ``η·I_e``, the engine inertia as the wheels see it.
    reflected_kg_m2: float = Field(gt=0.0)
    reflected_kg_m2_se: float = Field(ge=0.0)
    #: The ``η`` assumed to turn it into a crank inertia (1 − the profile's
    #: ``drivetrain_loss_fraction`` when measured).
    driveline_efficiency: float = Field(gt=0.0, le=1.0)
    #: Physical crank-side inertia, ``reflected_kg_m2 / driveline_efficiency``.
    engine_kg_m2: float = Field(gt=0.0)
    engine_kg_m2_se: float = Field(ge=0.0)
    gears: tuple[int, int]
    rpm_min: float
    rpm_max: float
    n_bins: int = Field(ge=2)
    imu_pitch_offset_deg: float
    #: The IMU frame of record the pulls and the offset were resolved in.
    imu_reference_id: str | None = None
    squat_corrected: bool
    #: A different η in each gear breaks the fit: a 1 % difference moves the
    #: result by roughly 1 % of torque over ``I·Δα``.
    #: Must equal :data:`INERTIA_CONVENTION`; a record in another convention is
    #: refused, not reinterpreted.
    assumption: str = INERTIA_CONVENTION
    conditions: InertiaConditions

    @model_validator(mode="after")
    def _one_convention(self) -> EngineInertiaRecord:
        if self.assumption != INERTIA_CONVENTION:
            raise ValueError(
                f"inertia record in another convention ({self.assumption!r}); this module "
                f"uses: {INERTIA_CONVENTION}"
            )
        expected = self.reflected_kg_m2 / self.driveline_efficiency
        if not math.isclose(self.engine_kg_m2, expected, rel_tol=1e-6):
            raise ValueError(
                f"engine_kg_m2 {self.engine_kg_m2} is not reflected_kg_m2 / "
                f"driveline_efficiency = {expected}: {INERTIA_CONVENTION}"
            )
        return self


class InertiaData(_Strict):
    """Rotating-inertia facts for one vehicle, as stored in ``dyno.yaml``.

    Wheel-side inertia is built from the profile's measured
    ``wheel_tire_assembly_kg`` and the tyre radius, through a stated shape
    factor; the engine side (crank, flywheel, clutch, gearbox input) is an
    estimate until :attr:`measured_engine` holds a two-gear measurement.
    """

    #: Road wheels that turn with the vehicle (the spare does not).
    road_wheels: int = Field(ge=2)
    #: One wheel and tyre's inertia as a fraction of ``m·r²`` (a tyre's mass
    #: sits near the rim, so 0.6-0.75; a solid disc is 0.5).
    wheel_shape_factor: float = Field(gt=0.0, le=1.0)
    #: Propshaft, differentials, axle shafts, hubs and rotors, referred to the
    #: road wheels (kg·m² at wheel speed).
    driveline_output_kg_m2: float = Field(ge=0.0)
    #: Crank, flywheel, clutch and gearbox input shaft at engine speed (kg·m²),
    #: an estimate of the *physical* inertia, upstream of driveline losses
    #: (:data:`INERTIA_CONVENTION`); the wheels see ``η`` times it.
    engine_side_kg_m2: float = Field(gt=0.0)
    evidence: str = Field(min_length=1)
    measured_engine: EngineInertiaRecord | None = None


@dataclass(frozen=True)
class Driveline:
    """Everything the dyno needs to turn engine speed into wheel speed and back."""

    tyre_radius_m: float
    final_drive: float
    gearbox_ratios: tuple[float, ...]
    #: All road wheels plus output driveline, at wheel speed (kg·m²).
    wheel_side_kg_m2: float
    #: Engine side at engine speed (kg·m²), physical: upstream of the losses.
    engine_side_kg_m2: float
    #: ``η`` = 1 − the profile's ``drivetrain_loss_fraction``: torque-proportional
    #: losses between the crank and the wheels (:data:`INERTIA_CONVENTION`).
    efficiency: float
    inertia: InertiaFlag

    @classmethod
    def from_vehicle(
        cls, params: VehicleParameters, inertia: InertiaData | None
    ) -> Driveline | DynoRefusal:
        """Build from profile facts, or refuse naming every fact that is missing."""
        missing = [
            label
            for label, value in (
                ("final_drive", params.final_drive),
                ("gearbox_ratios", params.gearbox_ratios),
                ("tyre_loaded_circumference_m", params.tyre_loaded_circumference_m),
                ("wheel_tire_assembly_kg", params.wheel_tire_assembly_kg),
                ("dyno.yaml inertia", inertia),
            )
            if not value
        ]
        if missing:
            return DynoRefusal(
                "driveline",
                "not_configured",
                "the vehicle profile lacks " + ", ".join(missing),
            )
        assert params.final_drive is not None
        assert params.tyre_loaded_circumference_m is not None
        assert params.wheel_tire_assembly_kg is not None
        assert inertia is not None
        radius = params.tyre_loaded_circumference_m / (2.0 * math.pi)
        wheel = inertia.wheel_shape_factor * params.wheel_tire_assembly_kg * radius**2
        efficiency = 1.0 - params.drivetrain_loss_fraction
        if not 0.0 < efficiency <= 1.0:
            return DynoRefusal(
                "driveline",
                "invalid_input",
                f"drivetrain_loss_fraction {params.drivetrain_loss_fraction} leaves no "
                "driveline efficiency in (0, 1]",
            )
        measured = inertia.measured_engine
        if measured is not None and not math.isclose(
            measured.driveline_efficiency, efficiency, rel_tol=1e-9
        ):
            return DynoRefusal(
                "driveline",
                "conditions_mismatch",
                f"the measured engine inertia assumed driveline efficiency "
                f"{measured.driveline_efficiency}; the profile now gives {efficiency:g} "
                "(1 - drivetrain_loss_fraction). The measurement is reflected_kg_m2 = "
                f"{measured.reflected_kg_m2:.4f}; re-derive engine_kg_m2 with the new value.",
            )
        return cls(
            tyre_radius_m=radius,
            final_drive=params.final_drive,
            gearbox_ratios=tuple(params.gearbox_ratios),
            wheel_side_kg_m2=inertia.road_wheels * wheel + inertia.driveline_output_kg_m2,
            engine_side_kg_m2=(
                inertia.engine_side_kg_m2 if measured is None else measured.engine_kg_m2
            ),
            efficiency=efficiency,
            inertia="estimated" if measured is None else "measured_engine",
        )

    def overall_ratio(self, gear: int) -> float:
        """Gearbox × final drive for a 1-based gear number."""
        if not 1 <= gear <= len(self.gearbox_ratios):
            raise ValueError(f"gear {gear} is not one of 1..{len(self.gearbox_ratios)}")
        return self.gearbox_ratios[gear - 1] * self.final_drive

    def neutral_effective_mass(self, mass_kg: float) -> float:
        """``m_eff`` with the clutch in: only the wheel side turns with the road."""
        return mass_kg + self.wheel_side_kg_m2 / self.tyre_radius_m**2

    def effective_mass(self, mass_kg: float, gear: int) -> float:
        """``m_eff`` in a gear, as the wheel force sees it.

        Wheel side plus ``η·I_e·N²/r²``: the engine's inertia torque is taken
        off at the crank, upstream of the losses (:data:`INERTIA_CONVENTION`).
        """
        ratio = self.overall_ratio(gear)
        return (
            self.neutral_effective_mass(mass_kg)
            + self.efficiency * self.engine_side_kg_m2 * ratio**2 / self.tyre_radius_m**2
        )


@dataclass(frozen=True)
class GearPull:
    """One pull in one gear (clutch engaged, no slip)."""

    gear: int
    samples: MotionSamples


@dataclass(frozen=True)
class EngineInertiaFit:
    reflected_kg_m2: float
    reflected_kg_m2_se: float
    driveline_efficiency: float
    engine_kg_m2: float
    engine_kg_m2_se: float
    gears: tuple[int, int]
    rpm_min: float
    rpm_max: float
    n_bins: int
    mean_alpha_difference: float
    imu_pitch_offset_deg: float
    imu_reference_id: str | None
    squat_corrected: bool

    def to_record(self, conditions: InertiaConditions) -> EngineInertiaRecord:
        return EngineInertiaRecord(
            reflected_kg_m2=self.reflected_kg_m2,
            reflected_kg_m2_se=self.reflected_kg_m2_se,
            driveline_efficiency=self.driveline_efficiency,
            engine_kg_m2=self.engine_kg_m2,
            engine_kg_m2_se=self.engine_kg_m2_se,
            gears=self.gears,
            rpm_min=self.rpm_min,
            rpm_max=self.rpm_max,
            n_bins=self.n_bins,
            imu_pitch_offset_deg=self.imu_pitch_offset_deg,
            imu_reference_id=self.imu_reference_id,
            squat_corrected=self.squat_corrected,
            conditions=conditions,
        )


def _binned(
    values: FloatArray, rpm: FloatArray, edges: FloatArray
) -> tuple[FloatArray, FloatArray]:
    index = np.digitize(rpm, edges) - 1
    means = np.full(len(edges) - 1, np.nan)
    counts = np.zeros(len(edges) - 1)
    for i in range(len(edges) - 1):
        members = values[index == i]
        counts[i] = len(members)
        if len(members):
            means[i] = float(members.mean())
    return means, counts


def fit_engine_inertia(
    pulls: Sequence[GearPull],
    *,
    driveline: Driveline,
    test_mass_kg: float,
    road_load: _RoadLoadLike,
    squat: _SquatLike | None,
    pitch_offset: PitchOffset,
    rpm_bin: float = INERTIA_RPM_BIN,
    min_rpm_overlap: float = INERTIA_MIN_RPM_OVERLAP,
    min_alpha_difference: float = INERTIA_MIN_ALPHA_DIFFERENCE,
    min_samples: int = MIN_SAMPLES,
) -> EngineInertiaFit | DynoRefusal:
    """Measure engine-side inertia from pulls in two gears over one rpm band (H07.6).

    ``pulls`` holds one or more pulls in each of exactly two gears; each
    gear's pulls are pooled (the H07.12 protocol drives three per gear).

    The crank inertia sits upstream of the gearbox, so the torque that
    accelerates it is taken off before the losses (:data:`INERTIA_CONVENTION`).
    In gear ``i`` at engine speed ω, with overall ratio ``N_i`` and
    torque-proportional driveline efficiency ``η``::

        τ_i = η·(T(ω) − I_e·α_i)        τ_i = r/N_i · (m·f + (J_w/r²)·a + F_rl(v))

    where ``α_i = N_i·a/r`` is the engine's angular acceleration and ``J_w``
    the wheel-side inertia. At the same ω both gears share ``η·T``, so per rpm
    bin ``τ_b − τ_a = η·I_e·(α_a − α_b)``; the bins are fitted through the
    origin. That measures ``η·I_e`` (``reflected_kg_m2``); the crank inertia
    is it divided by the driveline's assumed ``η``, so an ``η`` 5 % off moves
    ``engine_kg_m2`` 5 % but leaves ``m_eff`` (which uses ``η·I_e``) exact.
    ``η`` must be the same in both gears (stated on the record). ``J_w`` stays
    the stated estimate: it multiplies ``a`` in both gears in near-fixed
    proportion, so two gears cannot separate it.

    ``pitch_offset`` is required: an IMU pitch offset adds ``m·g·sin μ``
    to both gears' force but divides by different ratios, so 0.3 deg left in
    moves the result by ~44 %. Take it from :func:`estimate_pitch_offset` on a
    direction pair in the same IMU frame (the road-load pair records one);
    pulls from another frame are refused (``frame_mismatch``).
    """
    name = "engine_inertia"
    gears = sorted({pull.gear for pull in pulls})
    if not pulls:
        return DynoRefusal(name, "inputs_missing", "no pulls were given")
    mismatch = frame_mismatch(name, pitch_offset, [pull.samples for pull in pulls])
    if mismatch is not None:
        return mismatch
    if len(gears) == 1:
        return DynoRefusal(name, "same_gear", f"every pull is in gear {gears[0]}")
    if len(gears) > 2:
        return DynoRefusal(
            name, "invalid_input", f"pulls in gears {gears}; the fit takes exactly two gears"
        )
    per_gear: dict[int, list[tuple[FloatArray, FloatArray, FloatArray]]] = {g: [] for g in gears}
    for pull in pulls:
        s = pull.samples
        if len(s) < min_samples:
            return DynoRefusal(
                name,
                "too_few_samples",
                f"gear {pull.gear} pull has {len(s)} samples; the fit needs {min_samples}",
            )
        if s.rpm is None:
            return DynoRefusal(name, "inputs_missing", f"gear {pull.gear} pull has no engine rpm")
        try:
            ratio = driveline.overall_ratio(pull.gear)
        except ValueError as exc:
            return DynoRefusal(name, "invalid_input", str(exc))
        r = driveline.tyre_radius_m
        sf = _correct_specific_force(
            s.sf_lon_mps2, s.accel_mps2, squat=squat, pitch_offset_deg=pitch_offset.deg
        )
        force = (
            test_mass_kg * sf
            + driveline.wheel_side_kg_m2 / r**2 * s.accel_mps2
            + road_load.force(s.speed_mps)
        )
        per_gear[pull.gear].append((r / ratio * force, ratio * s.accel_mps2 / r, s.rpm))
    torques = [np.concatenate([t for t, _, _ in per_gear[g]]) for g in gears]
    alphas = [np.concatenate([a for _, a, _ in per_gear[g]]) for g in gears]
    rpms = [np.concatenate([w for _, _, w in per_gear[g]]) for g in gears]
    lo = max(float(rpms[0].min()), float(rpms[1].min()))
    hi = min(float(rpms[0].max()), float(rpms[1].max()))
    if hi - lo < min_rpm_overlap:
        return DynoRefusal(
            name,
            "rpm_overlap_too_small",
            f"the gear {gears[0]} and gear {gears[1]} pulls share "
            f"{max(hi - lo, 0.0):.0f} rpm; the fit needs {min_rpm_overlap:.0f}",
        )
    edges = np.arange(lo, hi + 1e-9, rpm_bin)
    tau_a, n_a = _binned(torques[0], rpms[0], edges)
    tau_b, n_b = _binned(torques[1], rpms[1], edges)
    alpha_a, _ = _binned(alphas[0], rpms[0], edges)
    alpha_b, _ = _binned(alphas[1], rpms[1], edges)
    keep = (n_a >= INERTIA_MIN_PER_BIN) & (n_b >= INERTIA_MIN_PER_BIN)
    if int(keep.sum()) < 2:
        return DynoRefusal(
            name,
            "rpm_overlap_too_small",
            f"only {int(keep.sum())} rpm bins hold >= {INERTIA_MIN_PER_BIN} samples in both gears",
        )
    d_tau = tau_b[keep] - tau_a[keep]
    d_alpha = alpha_a[keep] - alpha_b[keep]
    mean_d_alpha = float(np.mean(np.abs(d_alpha)))
    if mean_d_alpha < min_alpha_difference:
        return DynoRefusal(
            name,
            "alpha_difference_too_small",
            f"engine angular acceleration differs by {mean_d_alpha:.1f} rad/s^2 between the "
            f"gears; the inertia term needs >= {min_alpha_difference:.1f}",
        )
    coef, se, _ = _lstsq(d_alpha[:, None], d_tau)
    reflected = float(coef[0])
    if reflected <= 0.0:
        return DynoRefusal(
            name,
            "non_physical",
            f"fitted engine inertia {reflected:.3f} kg m^2 (as the wheels see it) is not "
            "positive; check the road load, the mass and that neither pull slipped the clutch",
        )
    eta = driveline.efficiency
    return EngineInertiaFit(
        reflected_kg_m2=reflected,
        reflected_kg_m2_se=float(se[0]),
        driveline_efficiency=eta,
        engine_kg_m2=reflected / eta,
        engine_kg_m2_se=float(se[0]) / eta,
        gears=(gears[0], gears[1]),
        rpm_min=lo,
        rpm_max=hi,
        n_bins=int(keep.sum()),
        mean_alpha_difference=mean_d_alpha,
        imu_pitch_offset_deg=pitch_offset.deg,
        imu_reference_id=pitch_offset.reference_id,
        squat_corrected=squat is not None,
    )


# ---------------------------------------------------------------------------
# H07.5 — road load from coast-downs
# ---------------------------------------------------------------------------


class RoadLoadConditions(_Strict):
    """What road load depends on: the road, load, tyres and air (H07.5)."""

    road: str = Field(min_length=1)
    date: dt.date
    sessions: list[str] = Field(min_length=1)
    payload_kg: float = Field(ge=0.0)
    tyre_pressure_front_kpa: float = Field(gt=0.0)
    tyre_pressure_rear_kpa: float = Field(gt=0.0)
    ambient_temp_c: float


class DirectionFit(_Strict):
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


class RoadLoadCoefficients(_Strict):
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
        v = np.asarray(speed_mps, dtype=np.float64)
        return self.a_n + self.b_n_per_mps * v + self.c_n_per_mps2 * v**2


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
    squat: _SquatLike | None,
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
    grouped = _grouped(coastdowns, name)
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
                _correct_specific_force(
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
        coef, se, residual_sd = _lstsq(design, force)
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


# ---------------------------------------------------------------------------
# H07.8 — repeatability before claims
# ---------------------------------------------------------------------------

Quantity = Literal["wheel_power_kw", "wheel_torque_nm"]
QUANTITY_UNITS: dict[str, str] = {"wheel_power_kw": "kW", "wheel_torque_nm": "N m"}

#: The fields a floor must share with a comparison for it to apply: the same
#: gear and road, and the same induction (an NA floor says nothing about a
#: boosted engine's spread).
FLOOR_MATCH_FIELDS: tuple[str, ...] = ("gear", "road", "induction")

#: Additionally shared by every pull pair a floor is recorded from.
FLOOR_SAME_DAY_FIELDS: tuple[str, ...] = (*FLOOR_MATCH_FIELDS, "tune_revision", "date")


class RunConditions(_Strict):
    """The conditions every dyno result carries (H07 rules). Required, all of them."""

    gear: int = Field(ge=1)
    #: ``pair`` is the average of both directions on one road; ``single`` is not.
    direction: Literal["pair", "single"]
    tune_revision: str = Field(min_length=1)
    induction: Literal["na", "boosted"]
    baro_kpa: float = Field(gt=0.0)
    intake_temp_c: float
    ambient_temp_c: float
    road: str = Field(min_length=1)
    date: dt.date
    payload_kg: float = Field(ge=0.0)
    sessions: list[str] = Field(min_length=1)


@dataclass(frozen=True)
class DynoCurve:
    """One quantity against engine rpm for one run (a pull pair, normally)."""

    quantity: Quantity
    rpm: FloatArray
    value: FloatArray
    conditions: RunConditions

    def __post_init__(self) -> None:
        rpm = _as_float_array("rpm", self.rpm)
        value = _as_float_array("value", self.value)
        if len(rpm) != len(value):
            raise ValueError(f"rpm has {len(rpm)} points, value {len(value)}")
        if len(rpm) < 2 or np.any(np.diff(rpm) <= 0):
            raise ValueError("a dyno curve needs >= 2 points in strictly increasing rpm")
        object.__setattr__(self, "rpm", rpm)
        object.__setattr__(self, "value", value)


class FloorConditions(_Strict):
    """The regime a floor applies to, and what it was measured under."""

    gear: int = Field(ge=1)
    road: str = Field(min_length=1)
    induction: Literal["na", "boosted"]
    tune_revision: str = Field(min_length=1)
    date: dt.date
    baro_kpa_range: tuple[float, float]
    intake_temp_c_range: tuple[float, float]
    ambient_temp_c_range: tuple[float, float]


class FloorBand(_Strict):
    rpm_min: float
    rpm_max: float
    #: Spread of one pull pair about the same-day mean, per rpm point, pooled
    #: over the band (RMS of the per-point sample sd).
    sd: float = Field(gt=0.0)
    n_points: int = Field(ge=2)

    @model_validator(mode="after")
    def _ordered(self) -> FloorBand:
        if self.rpm_max <= self.rpm_min:
            raise ValueError(f"band {self.rpm_min}-{self.rpm_max} rpm is empty")
        return self

    @property
    def sd_of_difference(self) -> float:
        """Spread of the difference between two runs, each a single pair."""
        return math.sqrt(2.0) * self.sd


class DynoNoiseFloor(_Strict):
    """Same-day, same-ROM run-to-run spread per rpm band (H07.8)."""

    quantity: Quantity
    unit: str = Field(min_length=1)
    conditions: FloorConditions
    n_pull_pairs: int = Field(ge=MIN_PULL_PAIRS)
    bands: list[FloorBand] = Field(min_length=1)
    note: str = ""

    def matches(self, quantity: str, conditions: RunConditions) -> bool:
        return self.quantity == quantity and all(
            getattr(self.conditions, key) == getattr(conditions, key) for key in FLOOR_MATCH_FIELDS
        )


def _grid(lo: float, hi: float, step: float) -> FloatArray:
    start = math.ceil(lo / step) * step
    return np.arange(start, hi + 1e-9, step, dtype=np.float64)


def _span(values: Sequence[float]) -> tuple[float, float]:
    return (float(min(values)), float(max(values)))


def record_dyno_floor(
    pairs: Sequence[DynoCurve],
    *,
    band_edges_rpm: Sequence[float],
    grid_step_rpm: float = CURVE_GRID_STEP_RPM,
    note: str = "",
) -> DynoNoiseFloor | DynoRefusal:
    """Measure the noise floor from same-day, same-ROM pull pairs (H07.8).

    Each curve is resampled onto one rpm grid over the band all of them
    cover; the per-point sample sd across pairs is pooled per band. The
    stored ``sd`` is one pair's spread; :attr:`FloorBand.sd_of_difference`
    is what a comparison of two pairs is judged against.
    """
    name = "noise_floor"
    if len(pairs) < MIN_PULL_PAIRS:
        return DynoRefusal(
            name,
            "too_few_pull_pairs",
            f"{len(pairs)} pull pairs; a floor needs at least {MIN_PULL_PAIRS}",
        )
    singles = [i for i, p in enumerate(pairs) if p.conditions.direction != "pair"]
    if singles:
        return DynoRefusal(
            name,
            "direction_mismatch",
            f"runs {singles} are single-direction; a floor is measured on pull pairs",
        )
    quantities = {p.quantity for p in pairs}
    if len(quantities) != 1:
        return DynoRefusal(name, "invalid_input", f"mixed quantities {sorted(quantities)}")
    first = pairs[0].conditions
    for key in FLOOR_SAME_DAY_FIELDS:
        values = {str(getattr(p.conditions, key)) for p in pairs}
        if len(values) != 1:
            return DynoRefusal(
                name,
                "not_same_day_same_rom",
                f"the pull pairs differ in {key}: {sorted(values)}; a noise floor is one day, "
                "one ROM, one gear, one road",
            )
    edges = sorted(float(e) for e in band_edges_rpm)
    if len(edges) < 2:
        return DynoRefusal(name, "invalid_input", "band_edges_rpm needs at least two edges")
    lo = max(float(p.rpm[0]) for p in pairs)
    hi = min(float(p.rpm[-1]) for p in pairs)
    grid = _grid(lo, hi, grid_step_rpm) if hi > lo else np.empty(0)
    matrix = np.vstack([np.interp(grid, p.rpm, p.value) for p in pairs])
    point_var = matrix.var(axis=0, ddof=1) if len(grid) else np.empty(0)
    bands: list[FloorBand] = []
    for band_lo, band_hi in zip(edges[:-1], edges[1:], strict=True):
        inside = (grid >= band_lo) & (grid < band_hi)
        if int(inside.sum()) < 2:
            continue
        sd = math.sqrt(float(point_var[inside].mean()))
        scale = float(np.abs(matrix[:, inside]).max())
        if sd <= 1e-9 * max(scale, 1.0):
            return DynoRefusal(
                name,
                "no_spread",
                f"the pull pairs are identical over {band_lo:.0f}-{band_hi:.0f} rpm; "
                "the same pull recorded twice is not a null run",
            )
        bands.append(FloorBand(rpm_min=band_lo, rpm_max=band_hi, sd=sd, n_points=int(inside.sum())))
    if not bands:
        return DynoRefusal(
            name,
            "no_overlap",
            f"the pull pairs share {lo:.0f}-{hi:.0f} rpm, which fills no band of "
            f"{edges} with two grid points",
        )
    quantity = pairs[0].quantity
    return DynoNoiseFloor(
        quantity=quantity,
        unit=QUANTITY_UNITS[quantity],
        conditions=FloorConditions(
            gear=first.gear,
            road=first.road,
            induction=first.induction,
            tune_revision=first.tune_revision,
            date=first.date,
            baro_kpa_range=_span([p.conditions.baro_kpa for p in pairs]),
            intake_temp_c_range=_span([p.conditions.intake_temp_c for p in pairs]),
            ambient_temp_c_range=_span([p.conditions.ambient_temp_c for p in pairs]),
        ),
        n_pull_pairs=len(pairs),
        bands=bands,
        note=note,
    )


BandVerdictKind = Literal["differs", "within_floor", "not_covered"]


@dataclass(frozen=True)
class BandVerdict:
    rpm_min: float
    rpm_max: float
    verdict: BandVerdictKind
    #: Mean of the per-rpm-point differences (b − a) in the band.
    delta: float | None = None
    sd_of_difference: float | None = None
    sigma: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rpm_min": self.rpm_min,
            "rpm_max": self.rpm_max,
            "verdict": self.verdict,
            "delta": self.delta,
            "sd_of_difference": self.sd_of_difference,
            "sigma": self.sigma,
        }


@dataclass(frozen=True)
class RunComparison:
    quantity: Quantity
    unit: str
    floor_conditions: FloorConditions
    sigma_threshold: float
    bands: tuple[BandVerdict, ...] = field(default_factory=tuple)

    @property
    def differs(self) -> bool:
        """True only when at least one band clears the floor."""
        return any(band.verdict == "differs" for band in self.bands)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": True,
            "quantity": self.quantity,
            "unit": self.unit,
            "floor_conditions": self.floor_conditions.model_dump(mode="json"),
            "sigma_threshold": self.sigma_threshold,
            "differs": self.differs,
            "bands": [band.as_dict() for band in self.bands],
        }


def compare_runs(
    run_a: DynoCurve,
    run_b: DynoCurve,
    floor: DynoNoiseFloor | None,
    *,
    sigma_threshold: float = SIGMA_THRESHOLD,
    grid_step_rpm: float = CURVE_GRID_STEP_RPM,
) -> RunComparison | DynoRefusal:
    """Say, band by band, whether ``run_b`` differs from ``run_a`` beyond the floor.

    The two curves are paired at the same rpm on one grid; a band's delta is
    the mean of those per-point differences, judged against the floor's
    per-point spread of a difference (conservative: averaging correlated
    points does not shrink the noise much). A band ``differs`` only at
    ``|delta| >= sigma_threshold × sd_of_difference``; below it the verdict is
    ``within_floor``, never "smaller effect". Refuses without a floor, when
    the floor was measured in another gear, road, induction or quantity, and
    when either run is single-direction.
    """
    name = "compare_runs"
    if floor is None:
        return DynoRefusal(
            name,
            "no_floor",
            "no dyno noise floor has been measured for this regime; record three same-day "
            "pull pairs first (H07.8)",
        )
    for label, run in (("run_a", run_a), ("run_b", run_b)):
        if run.quantity != floor.quantity:
            return DynoRefusal(
                name,
                "conditions_mismatch",
                f"{label} is {run.quantity}; the floor is {floor.quantity}",
            )
        for key in FLOOR_MATCH_FIELDS:
            ours, theirs = getattr(run.conditions, key), getattr(floor.conditions, key)
            if ours != theirs:
                return DynoRefusal(
                    name,
                    "conditions_mismatch",
                    f"{label} has {key} {ours!r}; the floor was measured with {key} "
                    f"{theirs!r}. A floor is only valid for its own regime.",
                )
        if run.conditions.direction != "pair":
            return DynoRefusal(
                name,
                "direction_mismatch",
                f"{label} is single-direction; the floor is the spread of pull pairs, and "
                "one direction carries grade and wind error it does not include",
            )
    lo = max(float(run_a.rpm[0]), float(run_b.rpm[0]))
    hi = min(float(run_a.rpm[-1]), float(run_b.rpm[-1]))
    grid = _grid(lo, hi, grid_step_rpm) if hi > lo else np.empty(0)
    delta = np.interp(grid, run_b.rpm, run_b.value) - np.interp(grid, run_a.rpm, run_a.value)
    verdicts: list[BandVerdict] = []
    for band in floor.bands:
        inside = (grid >= band.rpm_min) & (grid < band.rpm_max)
        if not inside.any():
            verdicts.append(BandVerdict(band.rpm_min, band.rpm_max, "not_covered"))
            continue
        band_delta = float(delta[inside].mean())
        sigma = band_delta / band.sd_of_difference
        verdicts.append(
            BandVerdict(
                band.rpm_min,
                band.rpm_max,
                "differs" if abs(sigma) >= sigma_threshold else "within_floor",
                delta=band_delta,
                sd_of_difference=band.sd_of_difference,
                sigma=sigma,
            )
        )
    if all(v.verdict == "not_covered" for v in verdicts):
        return DynoRefusal(
            name,
            "no_overlap",
            f"the runs share {lo:.0f}-{hi:.0f} rpm, which touches no band of the floor",
        )
    return RunComparison(
        quantity=floor.quantity,
        unit=floor.unit,
        floor_conditions=floor.conditions,
        sigma_threshold=sigma_threshold,
        bands=tuple(verdicts),
    )


# ---------------------------------------------------------------------------
# H07.7 — power
# ---------------------------------------------------------------------------

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
            demand = _as_float_array("demand", self.demand)
            if len(demand) != len(self.samples):
                raise ValueError(
                    f"demand has {len(demand)} samples, the motion {len(self.samples)}"
                )
            object.__setattr__(self, "demand", demand)


class J1349Correction(_Strict):
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
    road_load: _RoadLoadLike,
    squat: _SquatLike | None,
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
    r = driveline.tyre_radius_m
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
            "Slip cannot lower it: the speed scale, the tyre or the stated gear is wrong",
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
    f = _correct_specific_force(
        s.sf_lon_mps2[keep], a, squat=squat, pitch_offset_deg=pitch_offset.deg
    )
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
    squat: _SquatLike | None,
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
    grid = _grid(lo, hi, grid_step_rpm) if hi > lo else np.empty(0)
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


# ---------------------------------------------------------------------------
# The profile file
# ---------------------------------------------------------------------------


#: The absolute speed reference (H07 rules: absolute speed comes from GPS).
GPS_SPEED_CHANNEL = "gps:speed"


class SpeedSource(_Strict):
    """A speed channel the dyno may differentiate, with its measured scale (H07.3).

    ECU and broadcast speeds read under true road speed on most vehicles (tyre
    growth, a conservative speedometer) by an amount that differs between
    copies of the same quantity, so each channel carries its own ratio, as
    measured, never a platform default.
    """

    #: ``source:channel`` as the session reader names it (``decoded:`` counts
    #: are scaled through the bindings catalog first).
    channel: str = Field(min_length=1)
    #: ``channel / gps:speed`` on gated, same-instant pairs at steady speed.
    #: Absolute speed is ``channel / ratio_to_gps``.
    ratio_to_gps: float = Field(gt=0.5, lt=1.5)
    evidence: str = Field(min_length=1)


class MotionSources(_Strict):
    """Which channels a dyno window is built from, best first (H07.3).

    Channel names are vehicle facts (a broadcast id on one vehicle is nothing
    on another), so they live here and not in code. GPS speed is the
    reference and the last resort, with ratio 1 by definition; it is never
    listed.
    """

    speed: list[SpeedSource] = Field(default_factory=list)
    rpm: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _distinct(self) -> MotionSources:
        channels = [source.channel for source in self.speed]
        if GPS_SPEED_CHANNEL in channels:
            raise ValueError(
                f"{GPS_SPEED_CHANNEL} is the reference every ratio is measured against; "
                "it is used when no listed source has data, and is not listed"
            )
        for label, names in (("speed", channels), ("rpm", self.rpm)):
            if len(set(names)) != len(names):
                raise ValueError(f"motion_sources.{label} lists a channel twice: {names}")
        return self


class DynoProfile(_Strict):
    """A vehicle's ``dyno.yaml``: inertia estimates and measured dyno facts."""

    schema_version: Literal[1] = 1
    motion_sources: MotionSources | None = None
    inertia: InertiaData | None = None
    squat: SquatCoefficient | None = None
    road_load: RoadLoadCoefficients | None = None
    noise_floors: list[DynoNoiseFloor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _stored_facts_are_sound(self) -> DynoProfile:
        if self.road_load is not None and self.road_load.directions != "pair":
            raise ValueError(
                "road_load must come from a coast-down pair (both directions of one road); "
                "a single direction leaves grade and wind in A and B"
            )
        seen: set[tuple[Any, ...]] = set()
        for floor in self.noise_floors:
            key = (floor.quantity, *(getattr(floor.conditions, k) for k in FLOOR_MATCH_FIELDS))
            if key in seen:
                raise ValueError(f"two noise floors for the same regime {key}")
            seen.add(key)
        return self

    def noise_floor_for(
        self, quantity: Quantity, conditions: RunConditions
    ) -> DynoNoiseFloor | DynoRefusal:
        """The floor measured for this quantity, gear, road and induction."""
        for floor in self.noise_floors:
            if floor.matches(quantity, conditions):
                return floor
        if not self.noise_floors:
            return DynoRefusal(
                "noise_floor", "no_floor", "no dyno noise floor has been recorded for this vehicle"
            )
        regimes = sorted(
            f"{f.quantity} gear {f.conditions.gear} on {f.conditions.road!r} "
            f"({f.conditions.induction})"
            for f in self.noise_floors
        )
        return DynoRefusal(
            "noise_floor",
            "conditions_mismatch",
            f"no floor for {quantity} gear {conditions.gear} on {conditions.road!r} "
            f"({conditions.induction}); recorded: {regimes}",
        )


def load_dyno_profile(path: Path) -> DynoProfile | None:
    """Load a vehicle's dyno facts, or ``None`` when it has no ``dyno.yaml``.

    A file that exists but is empty, is not a mapping, or does not validate
    raises, naming the file: a truncated ``dyno.yaml`` must not read as "no
    floor measured", which is a different answer.
    """
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        kind = "empty" if raw is None else f"a {type(raw).__name__}, not a mapping"
        raise ValueError(f"{path} is {kind}; a dyno profile needs at least schema_version")
    return DynoProfile.model_validate(raw)


def save_dyno_profile(path: Path, profile: DynoProfile) -> None:
    atomic_write_text(
        path, yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False), mkdir=True
    )


def dyno_profile_path(config: CanaryConfig) -> Path:
    """The active vehicle's ``dyno.yaml``, beside its other profile data."""
    from src.profiles import profile_dir

    return profile_dir(config) / DYNO_FILENAME


__all__ = [
    "DYNO_FILENAME",
    "ENGINE_ESTIMATE_LABEL",
    "GPS_SPEED_CHANNEL",
    "WHEEL_TORQUE_CONVENTION",
    "AltitudeNoise",
    "MIN_PULL_PAIRS",
    "DynoPower",
    "J1349Correction",
    "MotionSources",
    "PowerCurve",
    "PowerPull",
    "SpeedSource",
    "compute_power",
    "frame_mismatch",
    "BandVerdict",
    "DirectionFit",
    "Driveline",
    "DynoCurve",
    "DynoNoiseFloor",
    "DynoProfile",
    "DynoRefusal",
    "EngineInertiaFit",
    "EngineInertiaRecord",
    "FloorBand",
    "FloorConditions",
    "GearPull",
    "InertiaConditions",
    "InertiaData",
    "Leg",
    "MotionSamples",
    "PitchOffset",
    "RoadLoadCoefficients",
    "RoadLoadConditions",
    "RoadLoadFit",
    "RunComparison",
    "RunConditions",
    "SquatCoefficient",
    "SquatConditions",
    "SquatFit",
    "compare_runs",
    "correct_specific_force",
    "estimate_pitch_offset",
    "dyno_profile_path",
    "fit_engine_inertia",
    "fit_road_load",
    "fit_squat",
    "load_dyno_profile",
    "measure_altitude_noise",
    "pitch_deg",
    "record_dyno_floor",
    "save_dyno_profile",
]
