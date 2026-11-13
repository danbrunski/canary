"""Body squat: pitch per g of acceleration, fitted on flat ground (H07.4).

``fit_squat`` regresses the IMU's apparent pitch on acceleration over a
stretch it first shows to be flat (window, 200 m segments, and grade that
moves in step with the acceleration), with every grade standard error taken
from the parked altitude-noise model of :mod:`src.analysis.dyno_altitude_noise`.
:class:`SquatCoefficient` is the stored record; it cannot exist without its
:class:`SquatConditions`.

Depends on the contract, motion and altitude-noise modules only. Moved
verbatim out of ``dyno.py`` (R16).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np
from pydantic import Field

from src.analysis.dyno_altitude_noise import AltitudeNoise
from src.analysis.dyno_contract import (
    MIN_SAMPLES,
    DynoRefusal,
    FloatArray,
    MotionSamples,
    StrictModel,
    lstsq,
)
from src.analysis.dyno_motion import distance_m
from src.signals.committees import STANDARD_GRAVITY

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

#: What ``deg_per_g`` is (M01.10): an *apparent* slope. The fit regresses
#: ``asin((f − a)/g)`` on ``a/g``, so besides body pitch it absorbs any scale
#: error between the speed (``a`` is its derivative) and the IMU: a 1.3 %
#: speed-scale or IMU-scale error is 0.74 deg/g. Removing it with the stored
#: slope removes that error with the squat, which is why the speed scale
#: reaches power squared. The stored key stays ``deg_per_g`` so existing
#: ``dyno.yaml`` records read; this label goes beside it wherever it is shown.
SQUAT_SLOPE_MEANING = (
    "apparent squat: body pitch per g plus whatever speed-scale and IMU-scale error the "
    "regression of asin((f - a)/g) on a/g absorbs (1.3 % of scale is 0.74 deg/g)"
)


class SquatConditions(StrictModel):
    """What the squat coefficient depends on: load and tires (H07.4)."""

    payload_kg: float = Field(ge=0.0)
    tire_pressure_front_kpa: float = Field(gt=0.0)
    tire_pressure_rear_kpa: float = Field(gt=0.0)
    road: str = Field(min_length=1)
    date: dt.date
    sessions: list[str] = Field(min_length=1)


class SquatCoefficient(StrictModel):
    """Body pitch per g of acceleration, as stored in ``dyno.yaml``."""

    #: Nose-up pitch, degrees per g of acceleration: *apparent*, not purely
    #: squat (:data:`SQUAT_SLOPE_MEANING`, M01.10).
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
    #: Apparent, not purely squat (:data:`SQUAT_SLOPE_MEANING`).
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
    distance = distance_m(samples)
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
    coef, se, residual_sd = lstsq(design, pitch)
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
