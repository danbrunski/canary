"""Specific force, pitch and the IMU's frame: what a motion window is corrected by.

Squat (``δ(a)``, degrees of body pitch per g) and the IMU's residual pitch
offset (``μ``) both tilt the longitudinal accelerometer; this module removes
them (:func:`correct_specific_force`) and measures the offset from a direction
pair (:func:`estimate_pitch_offset`, each heading weighted by distance,
:func:`distance_weights`). A pitch offset belongs to one IMU frame,
and :func:`frame_mismatch` is the refusal every estimator that applies one
shares.

Depends only on :mod:`src.analysis.dyno_contract`. Moved verbatim out of
``dyno.py`` (R16); ``_correct_specific_force``, ``_grouped`` and
``_distance_m`` are now the public ``remove_sensor_tilt``, ``group_legs`` and
``distance_m`` because the calibration modules call them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

from src.analysis.dyno_contract import (
    MIN_SAMPLES,
    DynoRefusal,
    FloatArray,
    MotionSamples,
    SquatLike,
)
from src.signals.committees import STANDARD_GRAVITY


def pitch_deg(accel_mps2: FloatArray, deg_per_g: float) -> FloatArray:
    """Body pitch (nose up positive) at each acceleration, in degrees."""
    return deg_per_g * (np.asarray(accel_mps2, dtype=np.float64) / STANDARD_GRAVITY)


def remove_sensor_tilt(
    sf_lon_mps2: FloatArray,
    accel_mps2: FloatArray,
    *,
    squat: SquatLike | None,
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
    #: Distance-weighted mean static pitch per heading (``μ ± grade``); half
    #: their difference is the mean grade of the stretch, a check against the
    #: road's GPS profile.
    heading_pitch_deg: dict[str, float]
    n_samples: int
    #: The IMU frame of record the offset was measured in
    #: (:attr:`MotionSamples.imu_reference_id`). Required, and checked by every
    #: estimator that applies the offset: a pitch offset is a property of one
    #: rotation, and meaningless in another.
    reference_id: str | None
    #: Road distance each heading's mean was weighted over (m). Two headings
    #: of very different length did not cover the same stretch, and the
    #: difference of their mean grades leaks into ``deg``. Empty for an offset
    #: that was stated rather than measured.
    heading_distance_m: dict[str, float] = field(default_factory=dict)


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


def group_legs(legs: Sequence[Leg], estimator: str) -> dict[str, list[MotionSamples]] | DynoRefusal:
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
    squat: SquatLike | None,
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
    return remove_sensor_tilt(
        samples.sf_lon_mps2, samples.accel_mps2, squat=squat, pitch_offset_deg=pitch_offset.deg
    )


def estimate_pitch_offset(
    legs: Sequence[Leg],
    *,
    squat: SquatLike | None,
    min_samples: int = MIN_SAMPLES,
) -> PitchOffset | DynoRefusal:
    """The session's IMU pitch offset, from both directions of one road.

    Per sample ``f − a − g·sin δ(a) = g·sin(θ + μ)``: road grade ``θ`` flips
    sign with the heading and ``μ`` does not, so averaging the two headings'
    mean static pitch leaves ``μ``. One heading cannot separate the two and is
    refused (``single_direction``).

    Each heading's mean is weighted by **distance**, ``v·dt`` per sample
    (:func:`distance_weights`), never by time. Grade cancels between headings
    only if every metre of road carries the same weight in both. In a
    coast-down or a pull speed changes monotonically, so a time mean
    over-weights the end each heading crosses slowly, and those are opposite
    ends of the road (M01.7, ``tests/test_dyno.py``: a true 0.300 deg read
    0.2625 deg on a ±0.5 % crest, and road-load ``A`` 165.2 N for 180 N,
    8.2 % low). A stopped sample weighs nothing; a heading that covered no
    distance is refused (``no_distance``). A negative speed is refused
    (``invalid_input``): ``speed_mps`` is absolute, and the motion reader
    clamps its smoothed speed at zero, so only a caller's own bad input
    reaches that refusal.

    What remains is the road itself: the cancellation is exact only when both
    headings cover the *same stretch*. A coast-down uphill ends sooner than
    the one downhill, so the two stretches differ, and half the difference of
    their distance-weighted mean grades leaks into the result;
    :attr:`PitchOffset.heading_distance_m` shows how far apart they were.
    Within one heading, runs are pooled by distance, so a longer run counts
    for more.
    """
    name = "pitch_offset"
    grouped = group_legs(legs, name)
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
    heading_distance: dict[str, float] = {}
    total = 0
    for heading, runs in grouped.items():
        n = sum(len(s) for s in runs)
        reversing = sum(int(np.count_nonzero(s.speed_mps < 0.0)) for s in runs)
        if reversing:
            return DynoRefusal(
                name,
                "invalid_input",
                f"{heading!r} has {reversing} samples of negative speed; speed_mps is absolute "
                "road speed, and a distance weight cannot be negative",
            )
        weight = np.concatenate([distance_weights(s) for s in runs])
        distance = float(weight.sum())
        if distance <= 0.0 and any(np.any(s.speed_mps > 0.0) for s in runs):
            # Moving, but every run is one sample: no interval, so no distance.
            return DynoRefusal(
                name,
                "too_few_samples",
                f"{heading!r} has {n} samples and no run spans an interval; the offset needs "
                f"{min_samples} moving samples",
            )
        if distance <= 0.0:
            return DynoRefusal(
                name,
                "no_distance",
                f"{heading!r} covered no distance ({n} samples, speed zero throughout): a "
                "heading's pitch is averaged over the road it covered, and a stopped vehicle "
                "covers none",
            )
        moving = int(np.count_nonzero(weight > 0.0))
        if moving < min_samples:
            return DynoRefusal(
                name,
                "too_few_samples",
                f"{heading!r} has {moving} moving samples (of {n}); the offset needs {min_samples}",
            )
        static = np.concatenate(
            [
                remove_sensor_tilt(s.sf_lon_mps2, s.accel_mps2, squat=squat, pitch_offset_deg=0.0)
                - s.accel_mps2
                for s in runs
            ]
        )
        pitch = np.degrees(np.arcsin(np.clip(static / STANDARD_GRAVITY, -1.0, 1.0)))
        heading_pitch[heading] = float(np.dot(weight, pitch) / distance)
        heading_distance[heading] = distance
        total += n
    return PitchOffset(
        deg=float(np.mean(list(heading_pitch.values()))),
        heading_pitch_deg=heading_pitch,
        n_samples=total,
        reference_id=legs[0].samples.imu_reference_id,
        heading_distance_m=heading_distance,
    )


def distance_weights(samples: MotionSamples) -> FloatArray:
    """Each sample's share of the road covered, ``v·dt`` (m).

    ``dt`` is half the interval to each neighbour (half of one interval at
    either end), so the weights are the trapezoid rule for ``∫v dt`` and sum
    to ``distance_m(samples)[-1]``. A stopped sample weighs nothing, and an
    irregular grid weighs each sample by the time it actually stands for. A
    hole in the grid is bridged by the samples either side of it; the motion
    reader refuses holes (``inputs_gap``) before a window gets here.
    """
    t = samples.t_s
    if len(t) < 2:
        return np.zeros(len(t), dtype=np.float64)
    step = np.diff(t)
    dt_s = np.empty(len(t), dtype=np.float64)
    dt_s[0] = step[0] / 2.0
    dt_s[-1] = step[-1] / 2.0
    dt_s[1:-1] = (step[:-1] + step[1:]) / 2.0
    weights: FloatArray = samples.speed_mps * dt_s
    return weights


def distance_m(samples: MotionSamples) -> FloatArray:
    steps = np.diff(samples.t_s) * 0.5 * (samples.speed_mps[1:] + samples.speed_mps[:-1])
    return np.concatenate([[0.0], np.cumsum(steps)])
