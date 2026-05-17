"""Trip, fuel, and performance derived channels (Task 6.6).

Pure functions over a *wide* telemetry frame (the ``to_wide`` /
``resample_wide`` output: a ``timestamp`` column plus ``obd:<PID>`` /
``decoded:<SIGNAL>`` columns). Two entry points:

* :func:`compute_derived_channels` — appends ``derived:<NAME>`` columns
  (instantaneous fuel rate, economy, boost/vacuum, estimated power/torque,
  and time-integrated trip distance / fuel used / idle time / CO2) for the
  dashboard, exports, and trip review. The instantaneous channels are
  stationary and could feed the feature pipeline; the cumulative ones are
  monotonic by construction and are deliberately *not* model features.
* :func:`summarize_trip` — a :class:`TripSummary` of scalar metrics
  (totals, average economy, idle fraction, 0-60 mph, 1/4-mile ET / trap).

Everything is an *estimate* from standard OBD channels; the docstrings name
the formula and its assumptions. Fuel/air math degrades gracefully through
three methods (fuel-rate PID → MAF → speed-density) so a vehicle that only
reports MAF still gets fuel numbers. No channel is fabricated: when the
inputs for a calculation are absent the column is all-NaN and the summary
field is ``None``, never a fake zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
import pandas as pd

from src.config import VehicleParameters

__all__ = [
    "FUEL_PROPERTIES",
    "FuelProperties",
    "TripSummary",
    "compute_derived_channels",
    "fuel_properties",
    "summarize_trip",
]

# Unit conversions kept explicit for readability over micro-DRY.
_KPH_PER_MPH = 1.609344
_SIXTY_MPH_KPH = 60.0 * _KPH_PER_MPH
_QUARTER_MILE_M = 402.336
_GAL_PER_L = 0.26417205
_MI_PER_KM = 0.62137119
#: Idle = engine turning but vehicle essentially stopped.
_IDLE_RPM_FLOOR = 200.0
_IDLE_SPEED_CEIL_KPH = 3.0
#: A launch starts at ≤ ~2 mph; a clean accel run never drops back to that.
_LAUNCH_SPEED_KPH = 2.0 * _KPH_PER_MPH
#: Cap per-row dt so a capture gap can't inflate integrated totals.
_MAX_DT_S = 5.0


@dataclass(frozen=True)
class FuelProperties:
    """Per-fuel constants used by the fuel/CO2 math."""

    afr_stoich: float  # stoichiometric air-fuel mass ratio
    density_kg_per_l: float
    co2_kg_per_l: float  # tailpipe CO2 per litre burned
    lhv_mj_per_kg: float  # lower heating value


#: Standard values; gasoline/diesel/E85 cover the configured fuel types.
FUEL_PROPERTIES: dict[str, FuelProperties] = {
    "gasoline": FuelProperties(14.7, 0.745, 2.31, 43.0),
    "diesel": FuelProperties(14.5, 0.832, 2.68, 42.6),
    "e85": FuelProperties(9.765, 0.781, 1.56, 28.5),
}


def fuel_properties(vehicle: VehicleParameters) -> FuelProperties:
    """Resolve fuel constants for the configured fuel type (gasoline default)."""
    return FUEL_PROPERTIES.get(vehicle.fuel_type, FUEL_PROPERTIES["gasoline"])


def _series(wide: pd.DataFrame, *names: str) -> pd.Series | None:
    """First present column among ``names`` as a float Series, else ``None``.

    Lets a calculation prefer an OBD PID but fall back to the decoded-CAN
    equivalent (``obd:RPM`` → ``decoded:ECM_RPM``) without each caller
    re-implementing the lookup.
    """
    for name in names:
        if name in wide.columns:
            col = pd.to_numeric(wide[name], errors="coerce")
            if col.notna().any():
                return col.astype(float)
    return None


def _dt_seconds(wide: pd.DataFrame) -> pd.Series:
    """Per-row elapsed seconds from the ``timestamp`` column, gap-capped.

    The first row gets dt=0 (nothing accrues before the trip starts); a long
    gap (USB drop, session seam) is clamped to ``_MAX_DT_S`` so integrated
    totals track wall-clock driving, not the gap.
    """
    ts = pd.to_datetime(wide["timestamp"], utc=True)
    dt = ts.diff().dt.total_seconds()
    dt = dt.fillna(0.0).clip(lower=0.0, upper=_MAX_DT_S)
    return dt.astype(float)


def _instant_fuel_g_s(
    wide: pd.DataFrame, vehicle: VehicleParameters, fuel: FuelProperties
) -> pd.Series | None:
    """Instantaneous fuel mass flow (g/s), best available method.

    1. ``ENGINE_FUEL_RATE`` PID (L/h) — most direct.
    2. MAF ÷ actual AFR, where actual AFR = stoich × commanded λ (PID 0x44);
       λ defaults to 1.0 when not reported.
    3. Speed-density: ideal-gas air mass from MAP/IAT/RPM/displacement/VE,
       then ÷ stoich AFR — the last resort when only MAP-style data exists.
    """
    rate_lph = _series(wide, "obd:ENGINE_FUEL_RATE", "decoded:ENGINE_FUEL_RATE")
    if rate_lph is not None:
        return rate_lph * fuel.density_kg_per_l * 1000.0 / 3600.0

    maf = _series(wide, "obd:MAF", "decoded:MAF")
    if maf is not None:
        lam = _series(wide, "obd:COMMANDED_EQUIV_RATIO")
        afr_actual = fuel.afr_stoich * (lam if lam is not None else 1.0)
        return maf / afr_actual

    rpm = _series(wide, "obd:RPM", "decoded:ECM_RPM")
    mapp = _series(wide, "obd:INTAKE_PRESSURE", "decoded:MAP")
    iat = _series(wide, "obd:INTAKE_TEMP", "decoded:IAT")
    if rpm is not None and mapp is not None:
        iat_k = (iat if iat is not None else pd.Series(25.0, index=wide.index)) + 273.15
        air_density = (mapp * 1000.0) / (287.05 * iat_k)  # kg/m^3
        disp_m3 = vehicle.displacement_l / 1000.0
        flow_m3_s = vehicle.volumetric_efficiency * disp_m3 * (rpm / 2.0 / 60.0)
        air_g_s = flow_m3_s * air_density * 1000.0
        return air_g_s / fuel.afr_stoich
    return None


def _estimated_power_kw(
    wide: pd.DataFrame,
    vehicle: VehicleParameters,
    fuel: FuelProperties,
    fuel_g_s: pd.Series | None,
) -> pd.Series | None:
    """Estimated wheel power (kW).

    Prefers the J1979 torque PIDs (actual torque % × reference torque → crank
    power via ω); otherwise a fuel-energy estimate: burned chemical power ×
    brake thermal efficiency × (1 − driveline loss).
    """
    rpm = _series(wide, "obd:RPM", "decoded:ECM_RPM")
    torque_pct = _series(wide, "obd:ACTUAL_ENGINE_TORQUE")
    ref_torque = _series(wide, "obd:ENGINE_REFERENCE_TORQUE")
    if rpm is not None and torque_pct is not None and ref_torque is not None:
        torque_nm = torque_pct / 100.0 * ref_torque
        crank_kw = torque_nm * rpm * (2.0 * np.pi / 60.0) / 1000.0
        return crank_kw * (1.0 - vehicle.drivetrain_loss_fraction)
    if fuel_g_s is not None:
        crank_kw = (fuel_g_s / 1000.0) * fuel.lhv_mj_per_kg * 1000.0 * vehicle.thermal_efficiency
        return crank_kw * (1.0 - vehicle.drivetrain_loss_fraction)
    return None


def compute_derived_channels(wide: pd.DataFrame, vehicle: VehicleParameters) -> pd.DataFrame:
    """Append ``derived:<NAME>`` columns. Returns a copy; input untouched.

    Added when their inputs exist (else skipped, not zero-filled):
    ``FUEL_RATE_LPH``, ``INSTANT_ECONOMY_L_PER_100KM``,
    ``INSTANT_ECONOMY_MPG``, ``BOOST_VACUUM_KPA``, ``ESTIMATED_POWER_HP``,
    ``ESTIMATED_TORQUE_NM``, and the time-integrated ``TRIP_DISTANCE_KM``,
    ``FUEL_USED_L``, ``CO2_KG``, ``IDLE_TIME_S``,
    ``AVG_ECONOMY_L_PER_100KM``.
    """
    if wide.empty or "timestamp" not in wide.columns:
        return wide.copy()
    out = wide.copy()
    fuel = fuel_properties(vehicle)
    dt = _dt_seconds(out)

    speed = _series(out, "obd:SPEED", "decoded:VEHICLE_SPEED")
    rpm = _series(out, "obd:RPM", "decoded:ECM_RPM")
    fuel_g_s = _instant_fuel_g_s(out, vehicle, fuel)

    if fuel_g_s is not None:
        fuel_lph = fuel_g_s / (fuel.density_kg_per_l * 1000.0) * 3600.0
        out["derived:FUEL_RATE_LPH"] = fuel_lph
        fuel_used_l = (fuel_lph / 3600.0 * dt).cumsum()
        out["derived:FUEL_USED_L"] = fuel_used_l
        out["derived:CO2_KG"] = fuel_used_l * fuel.co2_kg_per_l
        if speed is not None:
            moving = speed > 0
            l_per_100km = pd.Series(np.nan, index=out.index, dtype=float)
            l_per_100km[moving] = fuel_lph[moving] / speed[moving] * 100.0
            out["derived:INSTANT_ECONOMY_L_PER_100KM"] = l_per_100km
            mpg = pd.Series(np.nan, index=out.index, dtype=float)
            gal_h = fuel_lph * _GAL_PER_L
            mph = speed * _MI_PER_KM
            ok = moving & (gal_h > 0)
            mpg[ok] = mph[ok] / gal_h[ok]
            out["derived:INSTANT_ECONOMY_MPG"] = mpg

    if speed is not None:
        dist_km = (speed / 3600.0 * dt).cumsum()
        out["derived:TRIP_DISTANCE_KM"] = dist_km
        if "derived:FUEL_USED_L" in out.columns:
            avg = pd.Series(np.nan, index=out.index, dtype=float)
            nonzero = dist_km > 0
            avg[nonzero] = out["derived:FUEL_USED_L"][nonzero] / dist_km[nonzero] * 100.0
            out["derived:AVG_ECONOMY_L_PER_100KM"] = avg

    if rpm is not None and speed is not None:
        idling = (rpm > _IDLE_RPM_FLOOR) & (speed < _IDLE_SPEED_CEIL_KPH)
        out["derived:IDLE_TIME_S"] = (dt.where(idling, 0.0)).cumsum()

    mapp = _series(out, "obd:INTAKE_PRESSURE", "decoded:MAP")
    if mapp is not None:
        baro = _series(out, "obd:BAROMETRIC_PRESSURE")
        baro_v = baro if baro is not None else pd.Series(101.325, index=out.index)
        out["derived:BOOST_VACUUM_KPA"] = mapp - baro_v

    power_kw = _estimated_power_kw(out, vehicle, fuel, fuel_g_s)
    if power_kw is not None:
        out["derived:ESTIMATED_POWER_HP"] = power_kw * 1.34102209
        if rpm is not None:
            omega = rpm * (2.0 * np.pi / 60.0)
            torque = pd.Series(np.nan, index=out.index, dtype=float)
            spinning = omega > 0
            torque[spinning] = power_kw[spinning] * 1000.0 / omega[spinning]
            out["derived:ESTIMATED_TORQUE_NM"] = torque
    return out


@dataclass(frozen=True)
class TripSummary:
    """Scalar trip metrics. ``None`` = inputs absent / event never occurred."""

    duration_s: float
    distance_km: float | None
    fuel_used_l: float | None
    avg_economy_l_per_100km: float | None
    avg_economy_mpg: float | None
    co2_kg: float | None
    idle_time_s: float | None
    idle_fraction: float | None
    max_speed_kph: float | None
    zero_to_60_mph_s: float | None
    quarter_mile_s: float | None
    quarter_mile_trap_kph: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _accel_metrics(
    speed_kph: pd.Series, t_s: pd.Series
) -> tuple[float | None, float | None, float | None]:
    """0-60 mph time and 1/4-mile ET / trap speed from the speed trace.

    Scans every launch (a sample at ≤ ~2 mph followed by acceleration). For
    each launch, integrates distance forward and watches speed; a run that
    drops back to launch speed before 60 mph / 402 m is abandoned (not a
    clean pull). Reports the best (minimum) ET across launches.
    """
    speed = speed_kph.to_numpy(dtype=float)
    t = t_s.to_numpy(dtype=float)
    n = len(speed)
    best_060: float | None = None
    best_qm: float | None = None
    best_qm_trap: float | None = None
    i = 0
    while i < n - 1:
        if not (np.isfinite(speed[i]) and speed[i] <= _LAUNCH_SPEED_KPH):
            i += 1
            continue
        launch_t = t[i]
        dist_m = 0.0
        got_060: float | None = None
        j = i + 1
        while j < n:
            if not np.isfinite(speed[j]):
                break
            seg_dt = t[j] - t[j - 1]
            if seg_dt <= 0 or seg_dt > _MAX_DT_S:
                break
            # bogged back down to a stop — abandon this run
            if speed[j] <= _LAUNCH_SPEED_KPH and dist_m > 0:
                break
            dist_m += (speed[j] + speed[j - 1]) / 2.0 / 3.6 * seg_dt
            if got_060 is None and speed[j] >= _SIXTY_MPH_KPH:
                got_060 = t[j] - launch_t
            if dist_m >= _QUARTER_MILE_M:
                et = t[j] - launch_t
                if best_qm is None or et < best_qm:
                    best_qm = et
                    best_qm_trap = float(speed[j])
                break
            j += 1
        if got_060 is not None and (best_060 is None or got_060 < best_060):
            best_060 = got_060
        i = max(i + 1, j)
    return best_060, best_qm, best_qm_trap


def summarize_trip(wide: pd.DataFrame, vehicle: VehicleParameters) -> TripSummary:
    """Reduce a wide telemetry frame to a :class:`TripSummary`.

    Runs :func:`compute_derived_channels` internally, then reads the final
    cumulative values and computes the acceleration metrics from the speed
    trace. Safe on a frame missing any/all inputs — every optional metric
    becomes ``None``.
    """
    if wide.empty or "timestamp" not in wide.columns:
        return TripSummary(
            duration_s=0.0,
            distance_km=None,
            fuel_used_l=None,
            avg_economy_l_per_100km=None,
            avg_economy_mpg=None,
            co2_kg=None,
            idle_time_s=None,
            idle_fraction=None,
            max_speed_kph=None,
            zero_to_60_mph_s=None,
            quarter_mile_s=None,
            quarter_mile_trap_kph=None,
        )
    enriched = compute_derived_channels(wide, vehicle)
    ts = pd.to_datetime(enriched["timestamp"], utc=True)
    duration_s = float((ts.iloc[-1] - ts.iloc[0]).total_seconds()) if len(ts) > 1 else 0.0
    fuel = fuel_properties(vehicle)

    def _last(col: str) -> float | None:
        if col not in enriched.columns:
            return None
        s = enriched[col].dropna()
        return float(s.iloc[-1]) if not s.empty else None

    distance_km = _last("derived:TRIP_DISTANCE_KM")
    fuel_used_l = _last("derived:FUEL_USED_L")
    idle_time_s = _last("derived:IDLE_TIME_S")
    avg_l100: float | None = None
    avg_mpg: float | None = None
    if fuel_used_l is not None and fuel_used_l > 0 and distance_km is not None and distance_km > 0:
        avg_l100 = fuel_used_l / distance_km * 100.0
        avg_mpg = (distance_km * _MI_PER_KM) / (fuel_used_l * _GAL_PER_L)
    speed = _series(enriched, "obd:SPEED", "decoded:VEHICLE_SPEED")
    max_speed = float(speed.max()) if speed is not None and speed.notna().any() else None
    zero_60: float | None = None
    qm: float | None = None
    qm_trap: float | None = None
    if speed is not None:
        t_s = (ts - ts.iloc[0]).dt.total_seconds().astype(float)
        zero_60, qm, qm_trap = _accel_metrics(
            speed.reset_index(drop=True), t_s.reset_index(drop=True)
        )
    return TripSummary(
        duration_s=duration_s,
        distance_km=distance_km,
        fuel_used_l=fuel_used_l,
        avg_economy_l_per_100km=avg_l100,
        avg_economy_mpg=avg_mpg,
        co2_kg=(fuel_used_l * fuel.co2_kg_per_l if fuel_used_l is not None else None),
        idle_time_s=idle_time_s,
        idle_fraction=(
            idle_time_s / duration_s if idle_time_s is not None and duration_s > 0 else None
        ),
        max_speed_kph=max_speed,
        zero_to_60_mph_s=zero_60,
        quarter_mile_s=qm,
        quarter_mile_trap_kph=qm_trap,
    )
