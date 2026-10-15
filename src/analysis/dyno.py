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

* **Vehicle facts are data.** Mass, tire and gears come from the profile's
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

Where the code lives (R16; this module only re-exports, so every name below is
importable from :mod:`src.analysis.dyno` as the owner's own object):

* :mod:`src.analysis.dyno_contract` -- ``DynoRefusal``, ``MotionSamples``,
  ``MotionSources``, array validation (the leaf; imports no estimator);
* :mod:`src.analysis.dyno_motion` -- specific force, pitch offset, IMU frame;
* :mod:`src.analysis.dyno_altitude_noise`, :mod:`src.analysis.dyno_squat` --
  parked GPS-altitude noise and the squat fit (H07.4);
* :mod:`src.analysis.dyno_driveline`, :mod:`src.analysis.dyno_inertia` --
  stored inertia facts and ``Driveline``; the two-gear inertia fit (H07.6);
* :mod:`src.analysis.dyno_road_load` -- coast-down road load (H07.5);
* :mod:`src.analysis.dyno_comparison` -- run conditions, noise floor and run
  comparison (H07.8);
* :mod:`src.analysis.dyno_power` -- ``compute_power`` (H07.7);
* :mod:`src.analysis.dyno_profile` -- ``DynoProfile`` and its load/save/path.

Input preparation (:mod:`src.analysis.dyno_inputs`), pull detection
(:mod:`src.analysis.dyno_pulls`) and the workbench
(:mod:`src.analysis.dyno_workbench`) keep their own responsibilities. New code
imports the owner; this facade is for callers outside ``analysis``, tests and
tools. ``tests/test_dyno_boundaries.py`` enforces that no owner imports it.
"""

from __future__ import annotations

from src.analysis.dyno_altitude_noise import (
    ALTITUDE_NOISE_MAX_SPEED_MPS,
    ALTITUDE_NOISE_MIN_REFERENCE_S,
    AltitudeNoise,
    measure_altitude_noise,
)
from src.analysis.dyno_comparison import (
    CURVE_GRID_STEP_RPM,
    FLOOR_MATCH_FIELDS,
    FLOOR_SAME_DAY_FIELDS,
    MIN_PULL_PAIRS,
    QUANTITY_UNITS,
    BandVerdict,
    BandVerdictKind,
    DynoCurve,
    DynoNoiseFloor,
    FloorBand,
    FloorConditions,
    Quantity,
    RunComparison,
    RunConditions,
    compare_runs,
    record_dyno_floor,
)
from src.analysis.dyno_contract import (
    GPS_SPEED_CHANNEL,
    MIN_SAMPLES,
    DynoRefusal,
    FloatArray,
    MotionSamples,
    MotionSources,
    RefusalKind,
    SpeedSource,
)
from src.analysis.dyno_driveline import (
    INERTIA_CONVENTION,
    Driveline,
    EngineInertiaRecord,
    InertiaConditions,
    InertiaData,
    InertiaFlag,
)
from src.analysis.dyno_inertia import (
    INERTIA_MIN_ALPHA_DIFFERENCE,
    INERTIA_MIN_PER_BIN,
    INERTIA_MIN_RPM_OVERLAP,
    INERTIA_RPM_BIN,
    EngineInertiaFit,
    GearPull,
    fit_engine_inertia,
)
from src.analysis.dyno_motion import (
    Leg,
    PitchOffset,
    correct_specific_force,
    estimate_pitch_offset,
    frame_mismatch,
    pitch_deg,
)
from src.analysis.dyno_power import (
    ENGINE_ESTIMATE_LABEL,
    POWER_GEAR_TOLERANCE,
    POWER_GRADE_SMOOTH_S,
    POWER_MAX_GRADE_RANGE,
    POWER_MIN_BIN_FILL,
    POWER_MIN_PER_BIN,
    POWER_MIN_RPM_SPAN,
    WHEEL_TORQUE_CONVENTION,
    DynoPower,
    J1349Correction,
    PowerCurve,
    PowerPull,
    compute_power,
)
from src.analysis.dyno_profile import (
    DYNO_FILENAME,
    DynoProfile,
    dyno_profile_path,
    load_dyno_profile,
    save_dyno_profile,
)
from src.analysis.dyno_road_load import (
    COAST_MIN_SPEED_SPAN_KPH,
    DirectionFit,
    RoadLoadCoefficients,
    RoadLoadConditions,
    RoadLoadFit,
    fit_road_load,
)
from src.analysis.dyno_squat import (
    SQUAT_FLAT_FAMILY_ALPHA,
    SQUAT_GRADE_LEAK_SIGMA,
    SQUAT_GRADE_SEGMENT_M,
    SQUAT_GRADE_STEP_M,
    SQUAT_MAX_GRADE,
    SQUAT_MIN_ACCEL_SPAN_G,
    SQUAT_MIN_SEGMENT_SAMPLES,
    NoiseReferences,
    SquatCoefficient,
    SquatConditions,
    SquatFit,
    _grade_with_se,  # noqa: F401  (private; tests/test_dyno.py imports it from here)
    fit_squat,
)

__all__ = [
    "ALTITUDE_NOISE_MAX_SPEED_MPS",
    "ALTITUDE_NOISE_MIN_REFERENCE_S",
    "AltitudeNoise",
    "BandVerdict",
    "BandVerdictKind",
    "COAST_MIN_SPEED_SPAN_KPH",
    "CURVE_GRID_STEP_RPM",
    "DYNO_FILENAME",
    "DirectionFit",
    "Driveline",
    "DynoCurve",
    "DynoNoiseFloor",
    "DynoPower",
    "DynoProfile",
    "DynoRefusal",
    "ENGINE_ESTIMATE_LABEL",
    "EngineInertiaFit",
    "EngineInertiaRecord",
    "FLOOR_MATCH_FIELDS",
    "FLOOR_SAME_DAY_FIELDS",
    "FloatArray",
    "FloorBand",
    "FloorConditions",
    "GPS_SPEED_CHANNEL",
    "GearPull",
    "INERTIA_CONVENTION",
    "INERTIA_MIN_ALPHA_DIFFERENCE",
    "INERTIA_MIN_PER_BIN",
    "INERTIA_MIN_RPM_OVERLAP",
    "INERTIA_RPM_BIN",
    "InertiaConditions",
    "InertiaData",
    "InertiaFlag",
    "J1349Correction",
    "Leg",
    "MIN_PULL_PAIRS",
    "MIN_SAMPLES",
    "MotionSamples",
    "MotionSources",
    "NoiseReferences",
    "POWER_GEAR_TOLERANCE",
    "POWER_GRADE_SMOOTH_S",
    "POWER_MAX_GRADE_RANGE",
    "POWER_MIN_BIN_FILL",
    "POWER_MIN_PER_BIN",
    "POWER_MIN_RPM_SPAN",
    "PitchOffset",
    "PowerCurve",
    "PowerPull",
    "QUANTITY_UNITS",
    "Quantity",
    "RefusalKind",
    "RoadLoadCoefficients",
    "RoadLoadConditions",
    "RoadLoadFit",
    "RunComparison",
    "RunConditions",
    "SQUAT_FLAT_FAMILY_ALPHA",
    "SQUAT_GRADE_LEAK_SIGMA",
    "SQUAT_GRADE_SEGMENT_M",
    "SQUAT_GRADE_STEP_M",
    "SQUAT_MAX_GRADE",
    "SQUAT_MIN_ACCEL_SPAN_G",
    "SQUAT_MIN_SEGMENT_SAMPLES",
    "SpeedSource",
    "SquatCoefficient",
    "SquatConditions",
    "SquatFit",
    "WHEEL_TORQUE_CONVENTION",
    "compare_runs",
    "compute_power",
    "correct_specific_force",
    "dyno_profile_path",
    "estimate_pitch_offset",
    "fit_engine_inertia",
    "fit_road_load",
    "fit_squat",
    "frame_mismatch",
    "load_dyno_profile",
    "measure_altitude_noise",
    "pitch_deg",
    "record_dyno_floor",
    "save_dyno_profile",
]
