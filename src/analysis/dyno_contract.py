"""The dyno's shared vocabulary: refusals, motion samples, stored motion sources.

The leaf of the dyno modules. Everything here is needed by input preparation
(:mod:`src.analysis.dyno_inputs`), by every estimator and by the stored
profile, and imports none of them: a refusal, the one window of motion an
estimator reads (:class:`MotionSamples`), the channels a window is built from
(:class:`MotionSources`, a section of ``dyno.yaml``), the array validation,
and the least-squares helper the three calibration fits share.

Moved verbatim out of ``dyno.py`` (R16); :mod:`src.analysis.dyno` re-exports
every public name.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, model_validator

FloatArray = npt.NDArray[np.float64]


#: Fewest samples any fit accepts. At the 10 Hz GNSS / smoothed-derivative
#: rate H07.3 delivers, five seconds.
MIN_SAMPLES = 50


class StrictModel(BaseModel):
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
    # estimate_pitch_offset (M01.7): a heading weighted by distance that covered none
    "no_distance",
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
    # dyno_pulls (H07.7): a found window that is no single-gear pull
    "gear_unresolved",
    "lift_or_shift",
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


def as_float_array(name: str, values: Any) -> FloatArray:
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
            "t_s": as_float_array("t_s", self.t_s),
            "speed_mps": as_float_array("speed_mps", self.speed_mps),
            "accel_mps2": as_float_array("accel_mps2", self.accel_mps2),
            "sf_lon_mps2": as_float_array("sf_lon_mps2", self.sf_lon_mps2),
        }
        if self.altitude_m is not None:
            columns["altitude_m"] = as_float_array("altitude_m", self.altitude_m)
        if self.rpm is not None:
            columns["rpm"] = as_float_array("rpm", self.rpm)
        lengths = {name: len(values) for name, values in columns.items()}
        if len(set(lengths.values())) != 1:
            raise ValueError(f"MotionSamples columns differ in length: {lengths}")
        if len(columns["t_s"]) > 1 and np.any(np.diff(columns["t_s"]) <= 0):
            raise ValueError("t_s must be strictly increasing (one acquisition clock)")
        for name, values in columns.items():
            object.__setattr__(self, name, values)

    def __len__(self) -> int:
        return len(self.t_s)


class SquatLike(Protocol):
    @property
    def deg_per_g(self) -> float: ...


class RoadLoadLike(Protocol):
    def force(self, speed_mps: FloatArray) -> FloatArray: ...


def lstsq(design: FloatArray, target: FloatArray) -> tuple[FloatArray, FloatArray, float]:
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


#: The absolute speed reference (H07 rules: absolute speed comes from GPS).
GPS_SPEED_CHANNEL = "gps:speed"


class SpeedSource(StrictModel):
    """A speed channel the dyno may differentiate, with its measured scale (H07.3).

    ECU and broadcast speeds read under true road speed on most vehicles (tire
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


class MotionSources(StrictModel):
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
