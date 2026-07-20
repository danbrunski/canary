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
from src.preprocessing.resampler import SESSION_COLUMN, TIMESTAMP_COLUMN

__all__ = [
    "FUEL_PROPERTIES",
    "IDLE_RPM_FLOOR",
    "IDLE_SPEED_CEIL_KPH",
    "MAX_INTEGRATION_DT_S",
    "FuelProperties",
    "TripSummary",
    "compute_anomaly_signals",
    "compute_derived_channels",
    "compute_instantaneous_channels",
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
#: Public aliases of the tuning constants above (Task 1.3) — the live
#: session-scoped trip-integral accumulator (``LiveFeatureSource``) needs the
#: exact same cap/idle thresholds as this module's batch ``cumsum`` so the
#: two integration paths share one definition instead of two numbers that
#: could silently drift apart.
MAX_INTEGRATION_DT_S = _MAX_DT_S
IDLE_RPM_FLOOR = _IDLE_RPM_FLOOR
IDLE_SPEED_CEIL_KPH = _IDLE_SPEED_CEIL_KPH
#: STFT is the *fast* closed-loop fuel-trim channel: ``STFT_B1`` and
#: ``STFT_B2`` are polled at different points in the OBD round-robin, so during
#: a transient (tip-in/out, shift) the per-bank samples catch different phases
#: of the same swing and their instantaneous difference is dominated by sampling
#: skew, not real bank asymmetry. We therefore difference a *time-smoothed* STFT
#: so the bank-balance residual only trips on **sustained** imbalance (a real
#: fault, which LTFT — the slow channel — would corroborate), not on routine
#: driving transients. A module constant, not config: train and serve MUST use
#: the identical window or the fitted surface and live scoring would diverge, and
#: a config field could drift between the training box and the Pi. Kept well
#: under the live ring buffer's span (``idle_transient_lookback_seconds``, 180 s
#: by default) so the serve-side smoothing window is always fully populated.
STFT_BANK_BALANCE_SMOOTHING_SECONDS = 30.0


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


def _session_time_smooth(wide: pd.DataFrame, values: pd.Series, seconds: float) -> pd.Series:
    """Session-aware, time-based trailing rolling mean of ``values``.

    Mirrors :func:`compute_features`' windowing exactly — group by
    ``session``, order by ``timestamp``, and take a ``{seconds}s`` trailing
    mean on a datetime index — so a window for one session never bleeds rows
    from another even when they are wall-clock adjacent. ``min_periods=1`` so
    the first samples of a session (and the live buffer right after startup)
    still get a value (the mean of what's available so far) rather than NaN,
    which a downstream behavior recipe would otherwise skip.

    Falls back to the raw series when ``session``/``timestamp`` are absent
    (e.g. a bare-frame unit test) — the only real callers, the offline and
    live feature pipelines, always carry both columns.
    """
    if SESSION_COLUMN not in wide.columns or TIMESTAMP_COLUMN not in wide.columns:
        return values
    frame = pd.DataFrame(
        {
            SESSION_COLUMN: wide[SESSION_COLUMN].to_numpy(),
            "_ts": pd.to_datetime(wide[TIMESTAMP_COLUMN], utc=True).to_numpy(),
            "_v": values.to_numpy(dtype=float),
        },
        index=wide.index,
    )
    out = pd.Series(np.nan, index=wide.index, dtype=float)
    for _, group in frame.groupby(SESSION_COLUMN, sort=False, dropna=False):
        ordered = group.sort_values("_ts", kind="stable")
        smoothed = ordered.set_index("_ts")["_v"].rolling(f"{int(seconds)}s", min_periods=1).mean()
        out.loc[ordered.index] = smoothed.to_numpy(dtype=float)
    return out


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


def compute_instantaneous_channels(wide: pd.DataFrame, vehicle: VehicleParameters) -> pd.DataFrame:
    """Append the row-local (non-integrated) ``derived:<NAME>`` channels.

    Unlike the cumulative trip integrals in :func:`compute_derived_channels`,
    every column here is a pure function of its *own row* — no ``dt``, no
    ``cumsum``, no dependence on how many rows precede it in ``wide``. That
    makes it safe to call on a live ring-buffer window (Task 1.1): the last
    row's value is identical whether the buffer holds one row or a thousand,
    unlike the trip integrals which need a session-scoped accumulator (Task
    1.3) rather than a windowed cumsum.

    Added when their inputs exist (else skipped, not zero-filled):
    ``FUEL_RATE_LPH``, ``INSTANT_ECONOMY_L_PER_100KM``,
    ``INSTANT_ECONOMY_MPG``, ``BOOST_VACUUM_KPA``, ``ESTIMATED_POWER_HP``,
    ``ESTIMATED_TORQUE_NM``.
    """
    if wide.empty or "timestamp" not in wide.columns:
        return wide.copy()
    out = wide.copy()
    fuel = fuel_properties(vehicle)

    speed = _series(out, "obd:SPEED", "decoded:VEHICLE_SPEED")
    rpm = _series(out, "obd:RPM", "decoded:ECM_RPM")
    fuel_g_s = _instant_fuel_g_s(out, vehicle, fuel)

    if fuel_g_s is not None:
        fuel_lph = fuel_g_s / (fuel.density_kg_per_l * 1000.0) * 3600.0
        out["derived:FUEL_RATE_LPH"] = fuel_lph
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


def compute_derived_channels(wide: pd.DataFrame, vehicle: VehicleParameters) -> pd.DataFrame:
    """Append ``derived:<NAME>`` columns. Returns a copy; input untouched.

    Runs :func:`compute_instantaneous_channels` (``FUEL_RATE_LPH``,
    ``INSTANT_ECONOMY_L_PER_100KM``, ``INSTANT_ECONOMY_MPG``,
    ``BOOST_VACUUM_KPA``, ``ESTIMATED_POWER_HP``, ``ESTIMATED_TORQUE_NM``)
    then appends the time-integrated ``TRIP_DISTANCE_KM``, ``FUEL_USED_L``,
    ``CO2_KG``, ``IDLE_TIME_S``, ``AVG_ECONOMY_L_PER_100KM`` — a ``cumsum``
    over every row of ``wide``, which is only correct when ``wide`` is a
    *complete* session frame (the offline batch path). A live ring-buffer
    window must not cumsum through this function; see
    ``LiveFeatureSource``'s session-scoped accumulators (Task 1.3) instead.
    """
    if wide.empty or "timestamp" not in wide.columns:
        return wide.copy()
    out = compute_instantaneous_channels(wide, vehicle)
    dt = _dt_seconds(out)

    speed = _series(out, "obd:SPEED", "decoded:VEHICLE_SPEED")
    rpm = _series(out, "obd:RPM", "decoded:ECM_RPM")

    if "derived:FUEL_RATE_LPH" in out.columns:
        fuel_lph = out["derived:FUEL_RATE_LPH"]
        fuel_used_l = (fuel_lph / 3600.0 * dt).cumsum()
        out["derived:FUEL_USED_L"] = fuel_used_l
        fuel = fuel_properties(vehicle)
        out["derived:CO2_KG"] = fuel_used_l * fuel.co2_kg_per_l

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

    return out


def compute_anomaly_signals(wide: pd.DataFrame) -> pd.DataFrame:
    """Append ``derived:<NAME>`` columns useful for anomaly detection.

    Sister to :func:`compute_derived_channels` (which computes trip-meter
    style outputs like FUEL_USED_L for the dashboard). This function adds
    the *physical-residual* signals the behavior / IF / LSTM detectors
    consume: bank-balance asymmetries, air mass per cycle, AFR errors,
    TPS sensor cross-check. Each output is gated on its inputs being
    present so a session without OBD or without DID polling skips
    cleanly rather than NaN-filling.

    Outputs (added when inputs are present):

    * ``derived:AIR_MASS_PER_CYCLE`` — MAF translated to grams of air
      per engine cycle (4-stroke: one cycle = 2 revs of crank, all
      cylinders combined). Stable in a healthy engine for a given
      RPM/load operating point. Inputs: ``obd:MAF``, ``obd:RPM``.

    * ``derived:STFT_BANK_BALANCE`` / ``derived:LTFT_BANK_BALANCE`` —
      short / long fuel trim difference between banks. Expected ~0
      everywhere; sustained nonzero indicates a bank-specific fuel or air
      issue. The STFT version is **time-smoothed**
      (``STFT_BANK_BALANCE_SMOOTHING_SECONDS``) because the fast STFT channel's
      instantaneous bank delta is dominated by round-robin sampling skew during
      transients; the slow LTFT version is left raw (it is already the drift
      channel). Inputs: ``obd:SHORT_FUEL_TRIM_1/2``, ``obd:LONG_FUEL_TRIM_1/2``.

    * ``derived:AFR_BANK_BALANCE`` — wideband AFR sensor voltage
      difference between banks. Once the AF-sensor DIDs are polling
      (see ``obd_dids`` in ``canary.example.yaml``), this becomes the
      sharpest bank-asymmetry detector available. Inputs:
      ``did:ECM_AF_SENSOR_V_B1``, ``did:ECM_AF_SENSOR_V_B2``.

    * ``derived:TPS_VOLTAGE_PER_PCT`` — raw TPS voltage divided by the
      ECM-calculated throttle percent. Expected to be linear (constant
      ratio) within the operating range. Drift means the calibration
      curve is shifting before any DTC. Inputs:
      ``did:ECM_THROTTLE_POS_SENSOR_1``, ``obd:THROTTLE_POS``.

    The function is pure; caller is responsible for ordering it
    correctly in the feature pipeline (it expects mode segmentation
    and any other prior steps to have run first if they care about
    `operating_mode`).
    """
    if wide.empty:
        return wide.copy()
    out = wide.copy()

    # Air mass per cycle (g/cycle). Two-rev-per-cycle 4-stroke:
    # mass_per_cycle = MAF[g/s] * 120 / RPM. The factor 120 = 60[s/min] * 2[revs/cycle].
    maf = _series(out, "obd:MAF")
    rpm = _series(out, "obd:RPM", "decoded:ECM_RPM")
    if maf is not None and rpm is not None:
        rpm_safe = rpm.where(rpm > 0)
        out["derived:AIR_MASS_PER_CYCLE"] = (maf * 120.0 / rpm_safe).astype(float)

    # Per-bank fuel trim asymmetry. Both STFT and LTFT versions, since
    # they characterize fast (closed-loop) and slow (drift) bias paths.
    stft_b1 = _series(out, "obd:SHORT_FUEL_TRIM_1")
    stft_b2 = _series(out, "obd:SHORT_FUEL_TRIM_2")
    if stft_b1 is not None and stft_b2 is not None:
        # Time-smooth the difference (linear, so == difference of smoothed
        # banks) to reject sampling-skew transients; see
        # STFT_BANK_BALANCE_SMOOTHING_SECONDS. The raw per-bank STFT columns
        # are left instantaneous — only the *asymmetry* signal is corrupted by
        # asynchronous polling, not the per-bank trims themselves.
        out["derived:STFT_BANK_BALANCE"] = _session_time_smooth(
            out, stft_b1 - stft_b2, STFT_BANK_BALANCE_SMOOTHING_SECONDS
        ).astype(float)
    ltft_b1 = _series(out, "obd:LONG_FUEL_TRIM_1")
    ltft_b2 = _series(out, "obd:LONG_FUEL_TRIM_2")
    if ltft_b1 is not None and ltft_b2 is not None:
        out["derived:LTFT_BANK_BALANCE"] = (ltft_b1 - ltft_b2).astype(float)

    # Wideband AFR voltage asymmetry (DID-dependent).
    afr_b1 = _series(out, "did:ECM_AF_SENSOR_V_B1")
    afr_b2 = _series(out, "did:ECM_AF_SENSOR_V_B2")
    if afr_b1 is not None and afr_b2 is not None:
        out["derived:AFR_BANK_BALANCE"] = (afr_b1 - afr_b2).astype(float)

    # TPS voltage vs calculated throttle %. Inputs span different units
    # (V vs %) so the ratio is the useful invariant — a TPS that's drifted
    # to a different voltage range produces a different ratio.
    tps_v = _series(out, "did:ECM_THROTTLE_POS_SENSOR_1")
    tps_pct = _series(out, "obd:THROTTLE_POS")
    if tps_v is not None and tps_pct is not None:
        # Guard against div-by-zero at closed throttle (pct = 0); the metric
        # is meaningful only when the pedal is being pressed.
        pct_safe = tps_pct.where(tps_pct > 1.0)
        out["derived:TPS_VOLTAGE_PER_PCT"] = (tps_v / pct_safe).astype(float)

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
    #: Trip average speed (distance / elapsed time), not average of the speed
    #: trace — cheap from the same totals ``summarize_trip`` already has, and
    #: the more standard "trip computer" definition. ``None`` whenever
    #: ``distance_km`` is ``None`` (no speed channel) or the trip has zero
    #: duration. Noted absent in review (Task 1.6).
    avg_speed_kph: float | None
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
            avg_speed_kph=None,
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
    avg_speed = (
        distance_km / (duration_s / 3600.0) if distance_km is not None and duration_s > 0 else None
    )
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
        avg_speed_kph=avg_speed,
        zero_to_60_mph_s=zero_60,
        quarter_mile_s=qm,
        quarter_mile_trap_kph=qm_trap,
    )
