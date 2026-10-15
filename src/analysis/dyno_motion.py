"""Specific force, pitch and the IMU's frame: what a motion window is corrected by.

Squat (``δ(a)``, degrees of body pitch per g) and the IMU's residual pitch
offset (``μ``) both tilt the longitudinal accelerometer; this module removes
them (:func:`correct_specific_force`) and measures the offset from a direction
pair (:func:`estimate_pitch_offset`). A pitch offset belongs to one IMU frame,
and :func:`frame_mismatch` is the refusal every estimator that applies one
shares.

Depends only on :mod:`src.analysis.dyno_contract`. Moved verbatim out of
``dyno.py`` (R16); ``_correct_specific_force``, ``_grouped`` and
``_distance_m`` are now the public ``remove_sensor_tilt``, ``group_legs`` and
``distance_m`` because the calibration modules call them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

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
    sign with the heading and ``μ`` does not, so the mean static pitch of the
    two headings is ``μ``. Exact when both legs cover the same stretch; if
    they cover different sections, half the difference of those sections'
    mean grades leaks in. One heading cannot separate the two and is refused.
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
                remove_sensor_tilt(s.sf_lon_mps2, s.accel_mps2, squat=squat, pitch_offset_deg=0.0)
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


def distance_m(samples: MotionSamples) -> FloatArray:
    steps = np.diff(samples.t_s) * 0.5 * (samples.speed_mps[1:] + samples.speed_mps[:-1])
    return np.concatenate([[0.0], np.cumsum(steps)])
