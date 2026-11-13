"""Engine-side inertia measured from pulls in two gears over one rpm band (H07.6).

``fit_engine_inertia`` measures ``η·I_e`` from the difference in wheel torque
between two gears at the same engine speed, against the difference in the
engine's angular acceleration. The stored record and the driveline it feeds
are in :mod:`src.analysis.dyno_driveline`.

Depends on the contract, motion, driveline and road-load modules. The road load
is a stored record (:class:`RoadLoadCoefficients`), never a bare force
protocol, because only a record says which air its ``C`` was fitted in and the
fit moves it into the pulls' air (M01.8). Squat is still taken as a protocol.
Moved out of ``dyno.py`` (R16).
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
    lstsq,
)
from src.analysis.dyno_driveline import Driveline, EngineInertiaRecord, InertiaConditions
from src.analysis.dyno_motion import PitchOffset, frame_mismatch, remove_sensor_tilt
from src.analysis.dyno_road_load import (
    ROAD_LOAD_BARO_MAX_KPA,
    ROAD_LOAD_BARO_MIN_KPA,
    AirScaledRoadLoad,
    RoadLoadCoefficients,
)

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
    #: The road load as applied: the record's ``C`` moved into the pulls' air
    #: (M01.8), with the density ratio and both barometers.
    road_load_air: AirScaledRoadLoad

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
    road_load: RoadLoadCoefficients,
    baro_kpa: float,
    ambient_temp_c: float,
    squat: SquatLike | None,
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

    ``road_load`` must be a *record*, and ``baro_kpa`` / ``ambient_temp_c``
    (the pulls' measured barometer and ambient temperature) are required: the
    record's ``C`` (``½·ρ·Cd·A``) is moved into the pulls' air by
    ``ρ_run / ρ_fit`` (:meth:`RoadLoadCoefficients.in_air`, ambient on both
    sides, as :func:`compute_power` does; M01.8). Unscaled, it does not cancel
    between the gears: at one engine speed the higher gear is faster, so it
    carries more of the excess drag. A fit at 80 kPa applied to pulls at
    70 kPa read the crank inertia 33 % high in the synthetic two-gear harness
    (``test_inertia_fit_moves_the_road_load_into_the_pulls_air_m01_8``). An
    unrecorded :class:`RoadLoadFit` is refused ``inputs_missing``; a
    barometer outside the record range, ``invalid_input``. The applied road
    load is carried as ``road_load_air``.
    """
    name = "engine_inertia"
    gears = sorted({pull.gear for pull in pulls})
    if not pulls:
        return DynoRefusal(name, "inputs_missing", "no pulls were given")
    # Typed out, and refused at run time for an untyped caller handing over a
    # RoadLoadFit: a fit carries no conditions, so no air to scale from.
    if not isinstance(road_load, RoadLoadCoefficients):
        return DynoRefusal(
            name,
            "inputs_missing",
            "the road load is an unrecorded fit, so nothing says which air its C "
            "(1/2*rho*Cd*A) was fitted in; record it with RoadLoadFit.to_record and the "
            "coast-downs' measured baro_kpa before using it at another barometer (M01.8)",
        )
    if not ROAD_LOAD_BARO_MIN_KPA <= baro_kpa <= ROAD_LOAD_BARO_MAX_KPA:
        return DynoRefusal(
            name,
            "invalid_input",
            f"the pulls' baro_kpa {baro_kpa:g} is outside {ROAD_LOAD_BARO_MIN_KPA:g}-"
            f"{ROAD_LOAD_BARO_MAX_KPA:g} kPa (sea level to the highest roads): a value in "
            "hPa, mbar, psi or inHg? Give kPa",
        )
    air = road_load.in_air(baro_kpa, ambient_temp_c)
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
        r = driveline.tire_radius_m
        sf = remove_sensor_tilt(
            s.sf_lon_mps2, s.accel_mps2, squat=squat, pitch_offset_deg=pitch_offset.deg
        )
        force = (
            test_mass_kg * sf
            + driveline.wheel_side_kg_m2 / r**2 * s.accel_mps2
            + air.force(s.speed_mps)
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
    coef, se, _ = lstsq(d_alpha[:, None], d_tau)
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
        road_load_air=air,
    )
