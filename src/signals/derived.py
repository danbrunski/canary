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

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from src.columns import SESSION_COLUMN, TIMESTAMP_COLUMN, arrived_column, gap_column
from src.config import VehicleParameters
from src.decode.engineering import UnscaledDecodedChannelError, physical_series
from src.signals import vehicle_signals
from src.signals.committees import COMMITTEES, MEMBER_TRANSFORMS, committee_channel_reason

if TYPE_CHECKING:
    # `src.signals.afr` imports `fuel_properties` from this module, so the
    # runtime import would be circular. Only the annotation is needed; the
    # code below reads one attribute and never constructs one.
    from src.signals.afr import AfrCalibration

log = logging.getLogger(__name__)

__all__ = [
    "DERIVED_CHANNEL_INPUTS",
    "DERIVED_CHANNEL_OPTIONAL_INPUTS",
    "FUEL_PROPERTIES",
    "IDLE_RPM_FLOOR",
    "IDLE_SPEED_CEIL_KPH",
    "LEGACY_VE_CHANNEL",
    "MAX_INTEGRATION_DT_S",
    "VE_AMBIENT_CHANNEL",
    "VE_MANIFOLD_CHANNEL",
    "DerivedDeclarationError",
    "FuelProperties",
    "TripSummary",
    "compute_anomaly_signals",
    "compute_derived_channels",
    "compute_instantaneous_channels",
    "fuel_properties",
    "resolved_dependencies",
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
#: Cap per-sample dt so a capture *outage* can't inflate integrated totals.
#: Generous on purpose: integration runs over each channel's own valid
#: samples, and on a contended OBD bus a fast channel's real poll spacing
#: routinely stretches to tens of seconds (2026-07-26 drive: SPEED p99
#: spacing 32 s) — a tight cap silently discards genuinely-driven time and
#: halved the trip odometer. 60 s comfortably exceeds worst-case healthy
#: poll spacing while still bounding a true dropout (USB drop, daemon
#: restart) to one capped increment.
_MAX_DT_S = 60.0
#: How close PID 0x44 must read to 1.0 before φ and λ can be used
#: interchangeably. Inside this band the two conventions disagree by at most
#: ~2 % of AFR; outside it, an unverified convention makes the reading
#: uninterpretable rather than merely imprecise.
_EQUIV_RATIO_STOICH_TOLERANCE = 0.01
#: An accel run (0-60, 1/4-mile) is only trustworthy on a *dense* speed
#: trace; a gap longer than this breaks the run rather than interpolating
#: through it. Deliberately much tighter than the trip-integral cap above —
#: mis-timing a launch matters, mis-crediting 30 s of cruise does not.
_ACCEL_MAX_SEGMENT_DT_S = 5.0
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


def _append_dependency_gap(
    frame: pd.DataFrame,
    output: str,
    *dependency_groups: tuple[str, ...],
) -> None:
    """Attach ``output__gap`` as the OR of the selected inputs' freshness.

    Each dependency group is an ordered fallback list matching ``_series``.
    A derived value must never become a fresh GRU-D observation when any
    physical input used to compute it is stale or absent.
    """
    stale = pd.Series(False, index=frame.index, dtype=bool)
    # A03.1 — the output's value is new on a row where any input it reads
    # arrived (and none is stale). Only attached when every selected input
    # carries an arrival flag; otherwise the sequence mask falls back to
    # freshness for this output and the manifest names it.
    arrived: pd.Series | None = pd.Series(False, index=frame.index, dtype=bool)
    for candidates in dependency_groups:
        selected = _selected_input(frame, candidates)
        if selected is None:
            stale |= True
            arrived = None
            continue
        stale |= pd.to_numeric(frame[selected], errors="coerce").isna()
        source_gap = gap_column(selected)
        if source_gap in frame.columns:
            stale |= frame[source_gap].fillna(True).astype(bool)
        source_arrived = arrived_column(selected)
        if arrived is not None and source_arrived in frame.columns:
            arrived |= frame[source_arrived].fillna(False).astype(bool)
        else:
            arrived = None
    frame[gap_column(output)] = stale
    if arrived is not None:
        frame[arrived_column(output)] = arrived & ~stale


class DerivedDeclarationError(ValueError):
    """A derived channel's literal input declaration disagrees with the inputs
    its computation is built from (B08.1)."""


def _require_literal(
    what: str, built: Mapping[str, object], literal: Mapping[str, object], where: str
) -> None:
    """Refuse to import when ``literal`` (parsed by the lineage audit) is not
    exactly ``built`` (what the computation uses), naming every difference."""
    if dict(built) == dict(literal):
        return
    differences = [
        f"{name}: built {built.get(name)!r}, declared {literal.get(name)!r}"
        for name in sorted(set(built) | set(literal))
        if built.get(name) != literal.get(name)
    ]
    raise DerivedDeclarationError(
        f"{what} disagree with the literal declarations in {where} (B08.1: the "
        "artifact-lineage audit parses the literal ones, so they must be exact): "
        + "; ".join(differences)
    )


#: The optional inputs built from vehicle declarations: none. Measured AFR's
#: baro was declared here until D03.41 (2026-10-13), but the voltage route
#: withholds every row without a fresh one (B06.7), so baro is a *required*
#: input of that route and sits in its conjunctive groups in
#: :data:`_VEHICLE_INPUTS`. Kept, empty, so a vehicle module's literal
#: ``DERIVED_CHANNEL_OPTIONAL_INPUTS`` refuses at import unless code here
#: builds it too.
_VEHICLE_OPTIONAL_INPUTS: dict[str, tuple[str, ...]] = {}
_require_literal(
    "the vehicle-built optional derived inputs",
    _VEHICLE_OPTIONAL_INPUTS,
    vehicle_signals.literal_derived_optional_inputs(),
    "src.signals.vehicle_signals DERIVED_CHANNEL_OPTIONAL_INPUTS",
)

#: Columns a derived channel reads *when present* but does not need: the
#: fuel-rate lambda / commanded-equivalence-ratio refinement of the MAF method
#: and the speed-density IAT (25 degC when absent). Not part of the dependency
#: groups (their absence never makes the channel unproducible), but they are
#: inputs all the same, so :mod:`src.trainability` checks them too.
#:
#: B08.1 — every entry is a literal, here or in a vehicle module, so the
#: artifact-lineage audit can parse it at a build's revision;
#: ``tests/test_derived_inputs_declared.py`` proves each one is read.
DERIVED_CHANNEL_OPTIONAL_INPUTS: dict[str, tuple[str, ...]] = {
    "derived:FUEL_RATE_LPH": (
        "obd:O2_WR_LAMBDA_S1",
        "obd:O2_WR_LAMBDA_S2",
        "obd:COMMANDED_EQUIV_RATIO",
        "obd:INTAKE_TEMP",
        "decoded:IAT",
    ),
    # P02.15: the target map has no value while the ECM commands fuel cut
    # (commanded equivalence ratio exactly 0, src.signals.afr.fuel_cut_rows).
    "derived:TARGET_AFR": ("obd:COMMANDED_EQUIV_RATIO",),
    **_VEHICLE_OPTIONAL_INPUTS,
}

#: B04.5 — manifold-referenced VE: measured air mass over the ideal-gas fill
#: at *measured manifold pressure* and measured intake temperature. Near
#: 80-95 % on a healthy NA engine at any throttle, because the throttle loss
#: is already in the reference. Needs a MAP sensor.
VE_MANIFOLD_CHANNEL = "derived:VE_MANIFOLD_PERCENT"
#: B04.5 — ambient-referenced VE (the textbook inlet-referenced definition):
#: the same measured mass over the fill at *measured barometric pressure* and
#: measured intake temperature. Includes throttling, so it reads ~20-30 % at
#: idle and approaches manifold VE only at WOT. Available on any vehicle that
#: answers PID 0x33; the two are different quantities and never substitute.
VE_AMBIENT_CHANNEL = "derived:VE_AMBIENT_PERCENT"
#: The pre-B04.5 channel: manifold-referenced when the session carried MAP,
#: ambient-referenced otherwise, and computed at 25 degC when no IAT column
#: existed (those rows are flagged stale through its gap). Produced unchanged
#: for detectors whose manifests already consume it; use
#: :func:`resolved_dependencies` to say which definition a frame produced.
LEGACY_VE_CHANNEL = "derived:VE_PERCENT"


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
    """First present column among ``names`` as a float Series in engineering
    units, else ``None``.

    Lets a calculation prefer an OBD PID but fall back to the decoded-CAN
    equivalent (``obd:RPM`` → ``decoded:ECM_RPM``) without each caller
    re-implementing the lookup. A ``decoded:`` column holds raw binding counts
    (8 per rpm for ECM_RPM), so it goes through
    :func:`src.decode.engineering.physical_series`; one whose binding has no
    pinned scale is skipped as if absent — a calculation never sees counts.
    """
    for name in names:
        if name in wide.columns:
            try:
                col = physical_series(wide[name], name)
            except UnscaledDecodedChannelError:
                log.warning(
                    "skipping unscaled decoded channel as a physical input",
                    extra={"channel": name},
                )
                continue
            if col.notna().any():
                return col
    return None


def _selected_input(wide: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    """The column :func:`_series` would read from ``candidates`` (or ``None``).

    Freshness gaps and provenance must name the column the *value* came from:
    a ``decoded:`` candidate with no pinned scale is skipped by ``_series``,
    so it is skipped here too.
    """
    for name in candidates:
        if name not in wide.columns:
            continue
        try:
            col = physical_series(wide[name], name)
        except UnscaledDecodedChannelError:
            continue
        if col.notna().any():
            return name
    return None


def resolved_dependencies(wide: pd.DataFrame, output: str) -> list[str | None]:
    """Which input each of ``output``'s dependency groups resolved to in ``wide``.

    ``None`` for a group with no usable column — for the legacy VE channel's
    temperature group that means its value used the 25 degC default (and its
    gap marks every row stale). Provenance for reports that must say which VE
    definition a frame actually produced (B04.5).
    """
    return [_selected_input(wide, group) for group in DERIVED_CHANNEL_INPUTS[output]]


def _session_time_rolling(
    wide: pd.DataFrame, values: pd.Series, seconds: float, how: str = "mean"
) -> pd.Series:
    """Session-aware, time-based trailing rolling aggregate of ``values``.

    Mirrors :func:`compute_features`' windowing exactly — group by
    ``session``, order by ``timestamp``, and take a ``{seconds}s`` trailing
    ``how`` (``"mean"`` or ``"sum"``) on a datetime index — so a window for
    one session never bleeds rows from another even when they are
    wall-clock adjacent. ``min_periods=1`` so the first samples of a session
    (and the live buffer right after startup) still get a value rather than
    NaN, which a downstream behavior recipe would otherwise skip.

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
        rolling = ordered.set_index("_ts")["_v"].rolling(f"{int(seconds)}s", min_periods=1)
        rolled = getattr(rolling, how)()
        out.loc[ordered.index] = rolled.to_numpy(dtype=float)
    return out


def _session_time_smooth(wide: pd.DataFrame, values: pd.Series, seconds: float) -> pd.Series:
    """Trailing time-*mean* of ``values`` — see :func:`_session_time_rolling`."""
    return _session_time_rolling(wide, values, seconds, how="mean")


def _cumulative_time_integral(wide: pd.DataFrame, values: pd.Series) -> pd.Series:
    """Session-aware cumulative time integral of ``values`` (per-second units).

    Integrates over the channel's **own valid (non-NaN) samples**, not the
    frame's row grid. The resampler NaN-masks a fast channel's cells once
    they age past its ``max_gap`` (2 s at the 1 Hz grid), but on a contended
    OBD bus the channel's healthy poll spacing is routinely wider than that
    — a per-*row* dt × value cumsum then silently accrues nothing for most
    of the drive (the 2026-07-26 trip summary reported half the GPS-truth
    distance and a quarter of the MAF-integrated fuel this way). Trapezoid
    between consecutive valid samples of the same session, dt capped at
    ``_MAX_DT_S`` so a true capture outage still can't inflate the total.
    Rows between/after valid samples carry the running total forward; rows
    before the first valid sample are 0.
    """
    ts = pd.to_datetime(wide[TIMESTAMP_COLUMN], utc=True)
    # `isfinite`, not `notna`: ±inf passes `notna`, and one infinite sample in
    # a *cumulative* channel is not one bad row — the running total never
    # comes back. A single `inf` fuel rate took out 80 % of a 26-minute
    # drive's FUEL_USED_L and CO2_KG on 2026-08-07. Callers should not have to
    # remember to sanitise; the integrator is the layer that can guarantee it.
    valid = np.isfinite(values) & ts.notna()
    out = pd.Series(np.nan, index=wide.index, dtype=float)
    if not valid.any():
        return out.fillna(0.0)
    v = values[valid].astype(float)
    dt = ts[valid].diff().dt.total_seconds().fillna(0.0).clip(lower=0.0, upper=_MAX_DT_S)
    if SESSION_COLUMN in wide.columns:
        # Never accrue across a session seam — dt=0 restarts the trapezoid
        # at each session's first valid sample (the cumsum itself stays
        # continuous, matching the frame-wide cumulative contract).
        session = wide[SESSION_COLUMN][valid]
        dt = dt.where(session.eq(session.shift()), 0.0)
    increment = ((v + v.shift()) / 2.0 * dt).where(dt > 0, 0.0)
    out[valid] = increment.cumsum()
    return out.ffill().fillna(0.0)


def _commanded_afr_from_pid_44(
    commanded: pd.Series,
    calibration: AfrCalibration | None,
    afr_stoich: float,
) -> pd.Series:
    """Actual AFR from PID 0x44, or NaN where the reading cannot be read.

    φ and λ are reciprocals that **both read 1.0 at stoich**, so treating one
    as the other is silent everywhere most sanity-checking happens and grows
    with how rich the target is. This ECM is φ; reading it as λ turns a
    commanded 12.6 AFR into 18.3 and understates fuel by ~1.5x at peak
    enrichment. :meth:`AfrCalibration.commanded_afr` already refuses to guess —
    this is the vectorised equivalent for the wide-frame path, which used to
    bypass it entirely and hard-code the λ reading.

    Without a verified convention the only rows that can be interpreted are
    those reading ≈1.0, where both conventions agree. The rest go NaN: we know
    fuelling is *not* stoich there and cannot say in which direction, and a
    stoich-shaped guess would be a fabricated measurement.
    """
    # 0 means "not available" on this ECM, never a real ratio — see the
    # `inf` incident recorded in the caller.
    ratio = commanded.where(commanded > 0)
    convention = calibration.commanded_equiv_ratio_convention if calibration else "unverified"
    if convention == "phi":
        return afr_stoich / ratio
    if convention == "lambda":
        return afr_stoich * ratio
    return pd.Series(afr_stoich, index=commanded.index, dtype=float).where(
        (ratio - 1.0).abs() <= _EQUIV_RATIO_STOICH_TOLERANCE
    )


def _instant_fuel_g_s(
    wide: pd.DataFrame,
    vehicle: VehicleParameters,
    fuel: FuelProperties,
    calibration: AfrCalibration | None = None,
) -> pd.Series | None:
    """Instantaneous fuel mass flow (g/s), best available method.

    1. ``ENGINE_FUEL_RATE`` PID (L/h) — most direct.
    2. MAF ÷ actual AFR. Prefers the *measured* wideband O2 PIDs (0x24/0x25 —
       Task 2.1), which are λ. Falls back to *commanded* PID 0x44, which is
       φ or λ depending on the ECM and therefore needs ``calibration``; see
       :func:`_commanded_afr_from_pid_44`. With neither reported, AFR is
       stoich.
    3. Speed-density: ideal-gas air mass from MAP/IAT/RPM/displacement/VE,
       then ÷ stoich AFR — the last resort when only MAP-style data exists.
    """
    rate_lph = _series(wide, "obd:ENGINE_FUEL_RATE", "decoded:ENGINE_FUEL_RATE")
    if rate_lph is not None:
        return rate_lph * fuel.density_kg_per_l * 1000.0 / 3600.0

    maf = _series(wide, "obd:MAF", "decoded:MAF")
    if maf is not None:
        lam = _series(wide, "obd:O2_WR_LAMBDA_S1", "obd:O2_WR_LAMBDA_S2")
        if lam is None:
            commanded = _series(wide, "obd:COMMANDED_EQUIV_RATIO")
            if commanded is None:
                return maf / fuel.afr_stoich
            return maf / _commanded_afr_from_pid_44(commanded, calibration, fuel.afr_stoich)
        # A reported λ of 0 is the ECM saying "not available", not a
        # measurement. Dividing by it yielded `inf` fuel rate, and because
        # `inf` is not NaN the cumulative integrators carried it to the end of
        # the session — 80 % of the 2026-08-07 drive's FUEL_USED_L and CO2_KG.
        #
        # NaN, not a fallback to stoich: λ is missing precisely during
        # open-loop enrichment, which is the one condition where assuming
        # λ=1.0 is guaranteed wrong. An absent sample is honest; a
        # stoich-shaped guess is a fabricated measurement.
        return maf / (fuel.afr_stoich * lam.where(lam > 0))

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


#: Below this road speed an rpm/speed ratio is unreliable (Task 4.6): speed
#: PID quantization dominates the ratio, and neutral/clutch-in makes RPM
#: unrelated to road speed anyway (idle in neutral would otherwise cluster
#: to whichever gear ratio happens to be nearest).
_GEAR_ESTIMATE_MIN_SPEED_KPH = 8.0


def _gear_estimate(
    wide: pd.DataFrame,
    vehicle: VehicleParameters,
    speed: pd.Series | None,
    rpm: pd.Series | None,
) -> pd.Series | None:
    """Gear number (1-indexed) from the rpm/speed ratio, nearest-clustered to
    ``vehicle.rpm_per_kph()`` (Task 4.6, H07.1).

    The table is computed from the profile's gearbox ratios, final drive and
    loaded tire circumference, never hardcoded here (this platform is meant to
    generalize to any vehicle). Until all three are known the vehicle simply
    never gets this channel — same absent-input contract as every other
    channel here, not a special case. ``speed`` is the ECU's, which may read
    under true speed (the Xterra's reads ~8 % low); adjacent gears differ by
    far more, so the nearest match holds.

    There is no TCM on this (manual-transmission) vehicle's bus, so this
    ratio clustering is the *only* gear source available, not a fallback for
    a missing TCM signal.
    """
    table_values = vehicle.rpm_per_kph()
    if not table_values or speed is None or rpm is None:
        return None
    speed_safe = speed.where(speed > _GEAR_ESTIMATE_MIN_SPEED_KPH)
    ratio = rpm / speed_safe
    gear = pd.Series(np.nan, index=wide.index, dtype=float)
    valid = ratio.notna()
    if not valid.any():
        return gear
    table = np.asarray(table_values, dtype=float)
    vals = ratio[valid].to_numpy()
    nearest = np.argmin(np.abs(vals[:, np.newaxis] - table[np.newaxis, :]), axis=1)
    gear.loc[ratio[valid].index] = (nearest + 1).astype(float)
    return gear


def compute_instantaneous_channels(
    wide: pd.DataFrame,
    vehicle: VehicleParameters,
    *,
    calibration: AfrCalibration | None = None,
) -> pd.DataFrame:
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
    ``ESTIMATED_TORQUE_NM``, ``GEAR_ESTIMATE`` (Task 4.6 — only when the
    profile's gearbox ratios, final drive and tire are all known, H07.1).
    """
    if wide.empty or "timestamp" not in wide.columns:
        return wide.copy()
    out = wide.copy()
    fuel = fuel_properties(vehicle)

    # No broadcast fallback for speed: the `decoded:VEHICLE_SPEED` that used to
    # sit here names no binding, so it never resolved (removed 2026-09-18).
    speed = _series(out, "obd:SPEED")
    rpm = _series(out, "obd:RPM", "decoded:ECM_RPM")
    fuel_g_s = _instant_fuel_g_s(out, vehicle, fuel, calibration)

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
    baro = _series(out, "obd:BAROMETRIC_PRESSURE")
    if mapp is not None and baro is not None:
        # Boost/vacuum is MAP *relative to ambient*, so a missing baro makes it
        # uncomputable, not approximable. This used to substitute sea-level
        # 101.325 kPa, which is ~21 kPa wrong at the 2000 m where this vehicle
        # actually runs — enough to report a healthy NA engine as sitting in
        # permanent deep vacuum, and wrong at *any* non-sea-level altitude.
        # Skip the channel instead, per this function's stated contract.
        out["derived:BOOST_VACUUM_KPA"] = mapp - baro

    power_kw = _estimated_power_kw(out, vehicle, fuel, fuel_g_s)
    if power_kw is not None:
        out["derived:ESTIMATED_POWER_HP"] = power_kw * 1.34102209
        if rpm is not None:
            omega = rpm * (2.0 * np.pi / 60.0)
            torque = pd.Series(np.nan, index=out.index, dtype=float)
            spinning = omega > 0
            torque[spinning] = power_kw[spinning] * 1000.0 / omega[spinning]
            out["derived:ESTIMATED_TORQUE_NM"] = torque

    gear = _gear_estimate(out, vehicle, speed, rpm)
    if gear is not None:
        out["derived:GEAR_ESTIMATE"] = gear
    return out


def compute_derived_channels(
    wide: pd.DataFrame,
    vehicle: VehicleParameters,
    *,
    calibration: AfrCalibration | None = None,
) -> pd.DataFrame:
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
    out = compute_instantaneous_channels(wide, vehicle, calibration=calibration)

    speed = _series(out, "obd:SPEED")
    rpm = _series(out, "obd:RPM", "decoded:ECM_RPM")

    if "derived:FUEL_RATE_LPH" in out.columns:
        fuel_used_l = _cumulative_time_integral(out, out["derived:FUEL_RATE_LPH"] / 3600.0)
        out["derived:FUEL_USED_L"] = fuel_used_l
        fuel = fuel_properties(vehicle)
        out["derived:CO2_KG"] = fuel_used_l * fuel.co2_kg_per_l

    if speed is not None:
        dist_km = _cumulative_time_integral(out, speed / 3600.0)
        out["derived:TRIP_DISTANCE_KM"] = dist_km
        if "derived:FUEL_USED_L" in out.columns:
            avg = pd.Series(np.nan, index=out.index, dtype=float)
            nonzero = dist_km > 0
            avg[nonzero] = out["derived:FUEL_USED_L"][nonzero] / dist_km[nonzero] * 100.0
            out["derived:AVG_ECONOMY_L_PER_100KM"] = avg

    if rpm is not None and speed is not None:
        # Idle is an *indicator* integral: 1.0 where idling, 0.0 where
        # driving, NaN where either input is unsampled — so unsampled
        # stretches bridge (capped) instead of silently dropping the time.
        both = rpm.notna() & speed.notna()
        idling = pd.Series(np.nan, index=out.index, dtype=float)
        idling[both] = (
            (rpm[both] > _IDLE_RPM_FLOOR) & (speed[both] < _IDLE_SPEED_CEIL_KPH)
        ).astype(float)
        out["derived:IDLE_TIME_S"] = _cumulative_time_integral(out, idling)

    return out


#: Narrowband (zirconia) O2 sensor rich/lean switch threshold — standard
#: ~0.45 V crossover for every J1979 0-1 V narrowband sensor, not
#: vehicle-specific. Used only for switch-counting (Task 4.4); the wideband
#: A/F sensors report λ directly and never go through this threshold.
_O2_SWITCH_THRESHOLD_V = 0.45

#: Trailing window for the catalyst-efficiency switch-rate ratio (Task 4.4).
#: Long enough to span several upstream closed-loop switch cycles (a few Hz)
#: so the ratio isn't noise-dominated by a handful of samples, short enough
#: to still track a degrading cat within one drive. Module constant, not
#: config, for the same train/serve-parity reason as
#: ``STFT_BANK_BALANCE_SMOOTHING_SECONDS`` above.
CAT_EFFICIENCY_WINDOW_SECONDS = 60.0


def _air_mass_per_cycle_g(wide: pd.DataFrame) -> pd.Series | None:
    """Measured air mass per engine cycle (g/cycle) from MAF/RPM.

    4-stroke: one cycle = 2 revs of crank, all cylinders combined.
    ``mass_per_cycle = MAF[g/s] * 120 / RPM`` — the factor 120 = 60[s/min] *
    2[revs/cycle]. RPM <= 0 (engine off / KOEO) is masked to NaN via
    ``.where``, never a fake zero or a division error. Shared by
    ``derived:AIR_MASS_PER_CYCLE`` (below) and ``derived:VE_PERCENT``
    (Task 4.1), which normalizes this same measured mass against the
    theoretical 100%-VE fill.
    """
    maf = _series(wide, "obd:MAF")
    rpm = _series(wide, "obd:RPM", "decoded:ECM_RPM")
    if maf is None or rpm is None:
        return None
    rpm_safe = rpm.where(rpm > 0)
    return (maf * 120.0 / rpm_safe).astype(float)


def _theoretical_air_mass_per_cycle_g(
    wide: pd.DataFrame, vehicle: VehicleParameters
) -> pd.Series | None:
    """100%-VE reference fill (g/cycle): ideal-gas air mass that would fill
    the configured displacement at the given manifold pressure/temperature.

    Same ideal-gas form as the speed-density fallback in
    ``_instant_fuel_g_s``: ``density = P[Pa] / (287.05 * T[K])``, mass =
    density * displaced volume. Prefers MAP (``obd:INTAKE_PRESSURE``);
    falls back to barometric pressure when MAP isn't polled, and IAT defaults
    to 25 C when absent (same convention as ``_instant_fuel_g_s``). That
    fallback is why this feeds only the *legacy* ``derived:VE_PERCENT``: at
    part throttle MAP is far below baro, so the two references give
    different quantities (~85 % vs ~25 % at idle) under one name. New
    consumers read :data:`VE_MANIFOLD_CHANNEL` / :data:`VE_AMBIENT_CHANNEL`
    (B04.5), computed by :func:`_referenced_ve_percent`.
    """
    pressure_kpa = _series(wide, "obd:INTAKE_PRESSURE", "decoded:MAP")
    if pressure_kpa is None:
        pressure_kpa = _series(wide, "obd:BAROMETRIC_PRESSURE")
    if pressure_kpa is None:
        return None
    iat = _series(wide, "obd:INTAKE_TEMP", "decoded:IAT")
    iat_c = iat if iat is not None else pd.Series(25.0, index=wide.index)
    return _ideal_gas_fill_g(pressure_kpa, iat_c, vehicle)


def _ideal_gas_fill_g(
    pressure_kpa: pd.Series, iat_c: pd.Series, vehicle: VehicleParameters
) -> pd.Series:
    """Ideal-gas air mass (g/cycle) filling the configured displacement at
    ``pressure_kpa`` and ``iat_c`` — the 100 %-VE reference."""
    iat_k = iat_c + 273.15
    air_density = (pressure_kpa * 1000.0) / (287.05 * iat_k)  # kg/m^3
    disp_m3 = vehicle.displacement_l / 1000.0
    return (air_density * disp_m3 * 1000.0).astype(float)  # g/cycle at 100% VE


def _referenced_ve_percent(
    wide: pd.DataFrame,
    air_mass_measured: pd.Series,
    output: str,
    vehicle: VehicleParameters,
) -> pd.Series | None:
    """B04.5 — VE against exactly one measured pressure reference and a
    measured intake temperature; ``None`` when either is absent (never a
    fallback reference, never a default temperature)."""
    _air, pressure_group, iat_group = DERIVED_CHANNEL_INPUTS[output]
    pressure_kpa = _series(wide, *pressure_group)
    iat_c = _series(wide, *iat_group)
    if pressure_kpa is None or iat_c is None:
        return None
    fill = _ideal_gas_fill_g(pressure_kpa, iat_c, vehicle)
    return (air_mass_measured / fill.where(fill > 0) * 100.0).astype(float)


def _switch_count_rolling(wide: pd.DataFrame, voltage: pd.Series, seconds: float) -> pd.Series:
    """Trailing count of rich/lean threshold crossings, time-windowed per session.

    A "crossing" is any sample where the above/below-``_O2_SWITCH_THRESHOLD_V``
    state differs from the previous sample. Used by the catalyst-efficiency
    switch-rate ratio (Task 4.4); a narrowband O2 trace's crossing count over
    a trailing window approximates its switching frequency without needing a
    uniform sample rate.
    """
    crossed = (voltage > _O2_SWITCH_THRESHOLD_V).astype(float)
    transitions = crossed.diff().abs().fillna(0.0)
    return _session_time_rolling(wide, transitions, seconds, how="sum")


@dataclass(frozen=True)
class SignalInputs:
    """What one :class:`DerivedSignal` computes from.

    ``series`` holds one entry per input group, resolved exactly as
    :func:`_series` resolves it (``None`` when the group has no usable
    column). ``computed`` holds the signals already produced earlier in the
    table this call, for signals built on another signal's *value* rather
    than on its column (VE reads the air mass even when it is all-NaN).
    """

    frame: pd.DataFrame
    vehicle: VehicleParameters | None
    series: tuple[pd.Series | None, ...]
    computed: Mapping[str, pd.Series]

    @property
    def complete(self) -> bool:
        return all(value is not None for value in self.series)


@dataclass(frozen=True)
class DerivedSignal:
    """One ``derived:`` anomaly signal: its name, its inputs, its formula.

    ``input_groups`` is the only declaration of what the signal reads — the
    freshness gap, :data:`DERIVED_CHANNEL_INPUTS` (capture-profile warnings,
    trainability) and the computation all use it, so they cannot disagree.
    ``compute`` returns ``None`` to skip the signal on this frame.
    """

    name: str
    input_groups: tuple[tuple[str, ...], ...]
    compute: Callable[[SignalInputs], pd.Series | None]


def _float(values: pd.Series) -> pd.Series:
    return values.astype(float)


def _air_mass_signal(inputs: SignalInputs) -> pd.Series | None:
    return _air_mass_per_cycle_g(inputs.frame)


def _legacy_ve_signal(inputs: SignalInputs) -> pd.Series | None:
    air = inputs.computed.get("derived:AIR_MASS_PER_CYCLE")
    if air is None or inputs.vehicle is None:
        return None
    theoretical = _theoretical_air_mass_per_cycle_g(inputs.frame, inputs.vehicle)
    if theoretical is None:
        return None
    return _float(air / theoretical.where(theoretical > 0) * 100.0)


def _referenced_ve_signal(output: str) -> Callable[[SignalInputs], pd.Series | None]:
    def compute(inputs: SignalInputs) -> pd.Series | None:
        air = inputs.computed.get("derived:AIR_MASS_PER_CYCLE")
        if air is None or inputs.vehicle is None:
            return None
        return _referenced_ve_percent(inputs.frame, air, output, inputs.vehicle)

    return compute


def _both(
    formula: Callable[[pd.DataFrame, pd.Series, pd.Series], pd.Series],
) -> Callable[[SignalInputs], pd.Series | None]:
    """The common shape: both input groups resolve, then ``formula(frame, a, b)``."""

    def compute(inputs: SignalInputs) -> pd.Series | None:
        if not inputs.complete:
            return None
        a, b = inputs.series
        assert a is not None and b is not None
        return _float(formula(inputs.frame, a, b))

    return compute


def _smoothed_difference(frame: pd.DataFrame, a: pd.Series, b: pd.Series) -> pd.Series:
    # Time-smooth the difference (linear, so == difference of smoothed banks)
    # to reject sampling-skew transients; see
    # STFT_BANK_BALANCE_SMOOTHING_SECONDS. The raw per-bank STFT columns are
    # left instantaneous — only the *asymmetry* signal is corrupted by
    # asynchronous polling, not the per-bank trims themselves.
    return _session_time_smooth(frame, a - b, STFT_BANK_BALANCE_SMOOTHING_SECONDS)


def _difference(_frame: pd.DataFrame, a: pd.Series, b: pd.Series) -> pd.Series:
    return a - b


def _total(_frame: pd.DataFrame, a: pd.Series, b: pd.Series) -> pd.Series:
    return a + b


def _cat_efficiency(frame: pd.DataFrame, upstream: pd.Series, downstream: pd.Series) -> pd.Series:
    up_switches = _switch_count_rolling(frame, upstream, CAT_EFFICIENCY_WINDOW_SECONDS)
    down_switches = _switch_count_rolling(frame, downstream, CAT_EFFICIENCY_WINDOW_SECONDS)
    ratio = (down_switches / up_switches.where(up_switches > 0)).clip(upper=1.0)
    return 1.0 - ratio


def _voltage_per_pct(_frame: pd.DataFrame, volts: pd.Series, pct: pd.Series) -> pd.Series:
    # Guard against div-by-zero at closed throttle (pct = 0); the metric is
    # meaningful only when the pedal is being pressed.
    return volts / pct.where(pct > 1.0)


#: The physical-residual signals whose inputs every vehicle names the same way
#: (standard Mode 01 PIDs, decoded broadcast channels, other derived channels).
SHARED_ANOMALY_SIGNALS: tuple[DerivedSignal, ...] = (
    DerivedSignal(
        "derived:AIR_MASS_PER_CYCLE",
        (("obd:MAF",), ("obd:RPM", "decoded:ECM_RPM")),
        _air_mass_signal,
    ),
    # Legacy: the reference pressure depends on what the session polled
    # (MAP when present, else baro) — two physical quantities under one name.
    # Kept bit-identical only because tracked IF/LSTM manifests consume it;
    # no behavior recipe targets it (B04.5). See LEGACY_VE_CHANNEL.
    DerivedSignal(
        LEGACY_VE_CHANNEL,
        (
            ("derived:AIR_MASS_PER_CYCLE",),
            ("obd:INTAKE_PRESSURE", "decoded:MAP", "obd:BAROMETRIC_PRESSURE"),
            ("obd:INTAKE_TEMP", "decoded:IAT"),
        ),
        _legacy_ve_signal,
    ),
    # B04.5 — one physical reference per channel, no fallback between them
    # and no default temperature: absent MAP / baro / IAT means no value.
    DerivedSignal(
        VE_MANIFOLD_CHANNEL,
        (
            ("derived:AIR_MASS_PER_CYCLE",),
            ("obd:INTAKE_PRESSURE", "decoded:MAP"),
            ("obd:INTAKE_TEMP", "decoded:IAT"),
        ),
        _referenced_ve_signal(VE_MANIFOLD_CHANNEL),
    ),
    DerivedSignal(
        VE_AMBIENT_CHANNEL,
        (
            ("derived:AIR_MASS_PER_CYCLE",),
            ("obd:BAROMETRIC_PRESSURE",),
            ("obd:INTAKE_TEMP", "decoded:IAT"),
        ),
        _referenced_ve_signal(VE_AMBIENT_CHANNEL),
    ),
    DerivedSignal(
        "derived:STFT_BANK_BALANCE",
        (("obd:SHORT_FUEL_TRIM_1",), ("obd:SHORT_FUEL_TRIM_2",)),
        _both(_smoothed_difference),
    ),
    DerivedSignal(
        "derived:LTFT_BANK_BALANCE",
        (("obd:LONG_FUEL_TRIM_1",), ("obd:LONG_FUEL_TRIM_2",)),
        _both(_difference),
    ),
    DerivedSignal(
        "derived:TOTAL_TRIM_B1",
        (("obd:SHORT_FUEL_TRIM_1",), ("obd:LONG_FUEL_TRIM_1",)),
        _both(_total),
    ),
    DerivedSignal(
        "derived:TOTAL_TRIM_B2",
        (("obd:SHORT_FUEL_TRIM_2",), ("obd:LONG_FUEL_TRIM_2",)),
        _both(_total),
    ),
    DerivedSignal(
        "derived:CAT_EFFICIENCY_B1",
        (("obd:O2_B1S1",), ("obd:O2_B1S2",)),
        _both(_cat_efficiency),
    ),
    DerivedSignal(
        "derived:CAT_EFFICIENCY_B2",
        (("obd:O2_B2S1",), ("obd:O2_B2S2",)),
        _both(_cat_efficiency),
    ),
)

#: Shared formulas over vehicle-specific inputs (R05.4): each vehicle names
#: its channels in :mod:`src.signals.vehicle_signals`, so no manufacturer
#: identifier sits in this module. A signal no vehicle declares is absent.
VEHICLE_INPUT_SIGNALS: tuple[tuple[str, Callable[[SignalInputs], pd.Series | None]], ...] = (
    ("derived:AFR_BANK_BALANCE", _both(_difference)),
    # Inputs span different units (V vs %) so the ratio is the useful
    # invariant — a TPS that's drifted to a different voltage range produces
    # a different ratio.
    ("derived:TPS_VOLTAGE_PER_PCT", _both(_voltage_per_pct)),
)


def _vehicle_input_signals() -> tuple[DerivedSignal, ...]:
    formulas = dict(VEHICLE_INPUT_SIGNALS)
    declared = vehicle_signals.signal_inputs(tuple(formulas))
    return tuple(DerivedSignal(name, groups, formulas[name]) for name, groups in declared.items())


#: Every physical-residual signal :func:`compute_anomaly_signals` appends, in
#: output order (IF/LSTM manifests consume these names and their ``__gap``
#: columns, so order and formulae are pinned bit-identical — R05.4): the
#: shared signals, then the vehicle-input ones.
ANOMALY_SIGNALS: tuple[DerivedSignal, ...] = (
    *SHARED_ANOMALY_SIGNALS,
    *_vehicle_input_signals(),
)


#: The registry entries whose inputs a vehicle names, built from its
#: declarations in :mod:`src.signals.vehicle_signals` (R05.4: no manufacturer
#: identifier sits in this module). Each vehicle module also writes them out
#: literally, and the import refuses unless the two are equal (B08.1).
_VEHICLE_INPUTS: dict[str, tuple[tuple[str, ...], ...]] = {
    # --- src.signals.afr.append_afr_channels ------------------------------
    # Measured AFR: the bank's wideband lambda PID, else the AF-sensor voltage
    # DID through the profile's transfer (only when a calibration is loaded).
    # D03.41: the voltage route converts each row at the ECM's barometric gain
    # and withholds rows with no fresh baro (B06.7), so it needs voltage *and*
    # baro. Written in the conjunctive form of "lambda, or (voltage and baro)"
    # like FUEL_RATE_LPH: each group necessary, together sufficient. The baro
    # group is present where any vehicle declares an A/F baro channel.
    **{
        f"derived:AFR_{kind}B{bank}": (
            (
                *vehicle_signals.wideband_lambda_columns(bank),
                *vehicle_signals.af_sensor_voltage_columns(bank),
            ),
            *(
                (
                    (
                        *vehicle_signals.wideband_lambda_columns(bank),
                        *vehicle_signals.af_sensor_baro_columns(),
                    ),
                )
                if vehicle_signals.af_sensor_baro_columns()
                else ()
            ),
        )
        for bank in (1, 2)
        for kind in ("", "CENSORED_")
    },
    # B05.8 — per-instrument-pair λ bank balances (afr.LAMBDA_BANK_BALANCE_*):
    # the A/F voltage DIDs through the transfer, and the wide-range lambda
    # PIDs (produced only once afr_calibration.yaml verifies the bank-2 PID).
    # B06.7: both banks are converted at the ECM's barometric gain, so the
    # vehicle's baro channel is a required input of the voltage pair.
    # B06.7 renamed it from derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE (the gain-1
    # values; a deliberate contract change). The baro group is required where
    # any vehicle declares an A/F baro channel, and absent otherwise.
    "derived:LAMBDA_BANK_BALANCE_AF_VOLTAGE_BARO": (
        vehicle_signals.af_sensor_voltage_columns(1),
        vehicle_signals.af_sensor_voltage_columns(2),
        *(
            (vehicle_signals.af_sensor_baro_columns(),)
            if vehicle_signals.af_sensor_baro_columns()
            else ()
        ),
    ),
    "derived:LAMBDA_BANK_BALANCE_WIDEBAND": (
        vehicle_signals.wideband_lambda_columns(1),
        vehicle_signals.wideband_lambda_columns(2),
    ),
    # P02.14 — a vehicle's DID read in an engineering unit its catalog entry
    # does not carry (src.preprocessing.channel_units.append_did_views): the
    # one DID, scaled. The Xterra's is base fuel schedule in ms (DID 0x1208).
    **{view.channel: ((view.source,),) for view in vehicle_signals.did_views()},
    # P02.13 — the load input is the vehicle's verified channel for the target
    # map's axis (``afr.DEFAULT_TARGET_LOAD_AXIS``). With no declaration the
    # group is empty: unsatisfiable, and the channel is never produced. It read
    # obd:ENGINE_LOAD (%) on a ms axis until 2026-09-28. The Xterra declares
    # derived:BASE_FUEL_SCHEDULE since P02.14 (2026-10-02).
    "derived:TARGET_AFR": (
        ("obd:RPM",),
        vehicle_signals.load_axis_columns("base_fuel_schedule"),
    ),
}
_require_literal(
    "the vehicle-built derived inputs",
    _VEHICLE_INPUTS,
    vehicle_signals.literal_derived_inputs(),
    "src.signals.vehicle_signals DERIVED_CHANNEL_INPUTS",
)

#: Inputs each derived channel needs, as the ordered fallback groups
#: :func:`_append_dependency_gap` consumes (one group = "any one of these").
#: Declared here so callers that must reason about a derived channel without
#: computing it — capture-profile validation resolving a `derived:` model
#: input down to pollable channels — share one source of truth with the
#: computation itself.
#:
#: H01.1 — it covers **every** ``derived:`` channel the feature pipeline and
#: the live tick can emit (``tests/test_trainability.py`` featurizes a frame
#: carrying every input and fails on an undeclared output), because it is
#: also what makes a derived channel trainable: :mod:`src.trainability`
#: admits one only when every input named here (and in
#: :data:`DERIVED_CHANNEL_OPTIONAL_INPUTS`) is itself trainable, and treats an
#: undeclared derived channel as not trainable. Declaring inputs is how a new
#: derived channel becomes a feature.
#:
#: B08.1 — every entry is **literal**, so the artifact-lineage audit can parse
#: it at a build's git revision without importing that revision: a ``"..."``
#: entry below, a ``DerivedSignal("...", ((...),), ...)`` in
#: :data:`SHARED_ANOMALY_SIGNALS`, or a vehicle module's ``SIGNAL_INPUTS`` /
#: ``DERIVED_CHANNEL_INPUTS``. Entries that restate a structure the code
#: computes from (committees, vehicle declarations) are checked against it at
#: import. ``tests/test_derived_inputs_declared.py`` runs every derivation with
#: inputs removed and proves each declared input is read and nothing else is.
DERIVED_CHANNEL_INPUTS: dict[str, tuple[tuple[str, ...], ...]] = {
    # --- compute_anomaly_signals: declared once, in ANOMALY_SIGNALS (R05.4) --
    **{signal.name: signal.input_groups for signal in ANOMALY_SIGNALS},
    # --- vehicle-named inputs (afr.append_afr_channels, DID views) ----------
    **_VEHICLE_INPUTS,
    # AFR error is measured minus target, withheld where the measurement is
    # censored (src.signals.afr.append_afr_channels reads the censor flag).
    "derived:AFR_ERROR_B1": (
        ("derived:AFR_B1",),
        ("derived:AFR_CENSORED_B1",),
        ("derived:TARGET_AFR",),
    ),
    "derived:AFR_ERROR_B2": (
        ("derived:AFR_B2",),
        ("derived:AFR_CENSORED_B2",),
        ("derived:TARGET_AFR",),
    ),
    # --- compute_instantaneous_channels: performance channels ----------------
    # Fuel rate has three alternative methods (fuel-rate PID; MAF; speed-density
    # from RPM and MAP), so its groups are the conjunctive form of "any one
    # method": each group is necessary, and together they are sufficient.
    # Refinements read only when present are in DERIVED_CHANNEL_OPTIONAL_INPUTS.
    "derived:FUEL_RATE_LPH": (
        (
            "obd:ENGINE_FUEL_RATE",
            "decoded:ENGINE_FUEL_RATE",
            "obd:MAF",
            "decoded:MAF",
            "obd:RPM",
            "decoded:ECM_RPM",
        ),
        (
            "obd:ENGINE_FUEL_RATE",
            "decoded:ENGINE_FUEL_RATE",
            "obd:MAF",
            "decoded:MAF",
            "obd:INTAKE_PRESSURE",
            "decoded:MAP",
        ),
    ),
    "derived:INSTANT_ECONOMY_L_PER_100KM": (("derived:FUEL_RATE_LPH",), ("obd:SPEED",)),
    "derived:INSTANT_ECONOMY_MPG": (("derived:FUEL_RATE_LPH",), ("obd:SPEED",)),
    "derived:BOOST_VACUUM_KPA": (
        ("obd:INTAKE_PRESSURE", "decoded:MAP"),
        ("obd:BAROMETRIC_PRESSURE",),
    ),
    # Torque PIDs (actual % x reference, at rpm), else the fuel-energy estimate.
    "derived:ESTIMATED_POWER_HP": (
        ("obd:RPM", "decoded:ECM_RPM", "derived:FUEL_RATE_LPH"),
        ("obd:ACTUAL_ENGINE_TORQUE", "derived:FUEL_RATE_LPH"),
        ("obd:ENGINE_REFERENCE_TORQUE", "derived:FUEL_RATE_LPH"),
    ),
    "derived:ESTIMATED_TORQUE_NM": (
        ("derived:ESTIMATED_POWER_HP",),
        ("obd:RPM", "decoded:ECM_RPM"),
    ),
    "derived:GEAR_ESTIMATE": (("obd:SPEED",), ("obd:RPM", "decoded:ECM_RPM")),
    # --- compute_derived_channels / the live session accumulators ------------
    "derived:FUEL_USED_L": (("derived:FUEL_RATE_LPH",),),
    "derived:CO2_KG": (("derived:FUEL_USED_L",),),
    "derived:TRIP_DISTANCE_KM": (("obd:SPEED",),),
    "derived:AVG_ECONOMY_L_PER_100KM": (("derived:FUEL_USED_L",), ("derived:TRIP_DISTANCE_KM",)),
    "derived:IDLE_TIME_S": (("obd:RPM", "decoded:ECM_RPM"), ("obd:SPEED",)),
    # --- src.signals.committees: divergence residuals -----------------------
    # Any ``min_members`` of the members, on rows where every gate holds
    # (checked against COMMITTEES below).
    "derived:SPEED_SOURCE_DIVERGENCE": (
        ("obd:SPEED", "decoded:WHEEL_SPEED_6F7_A", "gps:speed"),
        ("gps:num_satellites",),
        ("gps:hdop",),
    ),
    "derived:LON_ACCEL_SOURCE_DIVERGENCE": (
        ("imu:ACCEL_LON_MEAN", "derived:OBD_SPEED_ACCEL_G", "derived:GPS_SPEED_ACCEL_G"),
    ),
    "derived:HEADING_RATE_DIVERGENCE": (("derived:GPS_COURSE_RATE", "imu:GYRO_Z"),),
    "derived:STATIONARY_CONFIRM_DIVERGENCE": (
        ("obd:SPEED", "gps:speed", "derived:IMU_STILLNESS_SPEED_PROXY"),
    ),
    "derived:ALTITUDE_SOURCE_DIVERGENCE": (("gps:altitude", "baro:altitude"),),
    "derived:BARO_PRESSURE_DIVERGENCE": (
        ("baro:pressure", "obd:BAROMETRIC_PRESSURE", "derived:MAP_AT_IDLE_KPA"),
    ),
    "derived:VERTICAL_SPEED_DIVERGENCE": (
        (
            "derived:BARO_ALT_RATE",
            "derived:GPS_VERTICAL_SPEED",
            "derived:IMU_VERTICAL_ACCEL_INTEGRAL",
        ),
    ),
    # Committee member transforms (append_committee_member_transforms): the
    # first speed source with a value, differentiated (checked below).
    "derived:OBD_SPEED_ACCEL_G": (
        ("decoded:VEHICLE_SPEED_2D1", "decoded:WHEEL_SPEED_6F7_A", "obd:SPEED"),
    ),
    "derived:GPS_SPEED_ACCEL_G": (("gps:speed",),),
}
_require_literal(
    "the sensor committees' members and gates",
    {
        **{
            committee.output: (
                committee.members,
                *((gate.channel,) for gate in committee.gates),
            )
            for committee in COMMITTEES
        },
        **{output: (sources,) for output, sources in MEMBER_TRANSFORMS.items()},
    },
    {
        name: groups
        for name, groups in DERIVED_CHANNEL_INPUTS.items()
        if committee_channel_reason(name) is not None
    },
    "src.signals.derived DERIVED_CHANNEL_INPUTS",
)


def compute_anomaly_signals(
    wide: pd.DataFrame, vehicle: VehicleParameters | None = None
) -> pd.DataFrame:
    """Append ``derived:<NAME>`` columns useful for anomaly detection.

    Sister to :func:`compute_derived_channels` (which computes trip-meter
    style outputs like FUEL_USED_L for the dashboard). This function adds
    the *physical-residual* signals the behavior / IF / LSTM detectors
    consume: bank-balance asymmetries, air mass per cycle, AFR errors,
    TPS sensor cross-check. Each output is gated on its inputs being
    present so a session without OBD or without DID polling skips
    cleanly rather than NaN-filling.

    ``vehicle`` is optional (default ``None``) purely so existing bare-frame
    callers/tests don't have to thread a config through — it only gates
    ``derived:VE_PERCENT`` (Task 4.1), which needs the configured engine
    displacement and is skipped like any other absent-input case when
    ``vehicle`` isn't supplied.

    Outputs (added when inputs are present; each is model-trainable unless
    noted otherwise — see ``NON_TRAINABLE_COUNTER_CHANNELS`` in
    ``feature_pipeline.py`` for the actual exclusion mechanism):

    * ``derived:AIR_MASS_PER_CYCLE`` — MAF translated to grams of air
      per engine cycle (4-stroke: one cycle = 2 revs of crank, all
      cylinders combined). Stable in a healthy engine for a given
      RPM/load operating point. Inputs: ``obd:MAF``, ``obd:RPM``.

    * ``derived:VE_PERCENT`` (Task 4.1) — the same measured air mass above,
      expressed as a percentage of the *theoretical* 100%-VE fill (ideal
      gas law over MAP-or-baro, IAT, and the configured displacement —
      see ``_theoretical_air_mass_per_cycle_g``). Where AIR_MASS_PER_CYCLE
      is a stable-but-uncalibrated proxy, VE_PERCENT is physics-normalized
      and directly interpretable (~85-95% naturally-aspirated at part
      throttle; collapsing VE can mean unmetered air or MAF under-reporting,
      while spiking VE can mean MAF over-reporting or a pressure/IAT bias).
      A restriction changes both measured flow and manifold pressure, so this
      ratio alone does not identify it. Inputs: ``obd:MAF``, ``obd:RPM``, MAP-or-baro,
      ``vehicle.displacement_l``. There is no DID pressure cross-check:
      the dev vehicle's engine has no MAP sensor, and its DIDs 0x1206/0x1207
      (once catalogued as intake pressure) are the injected pulse.
      **Legacy** (B04.5): its reference is MAP-or-baro depending on what the
      session polled, so it names two quantities; it is kept unchanged for
      the detectors that consume it and no behavior recipe targets it.

    * ``derived:VE_MANIFOLD_PERCENT`` / ``derived:VE_AMBIENT_PERCENT``
      (B04.5) — the same ratio against exactly one measured reference:
      manifold pressure (needs MAP) or barometric pressure (PID 0x33), each
      with measured IAT. No fallback between them and no default
      temperature — a missing reference or IAT means no value. Neither
      separates MAF, pressure, IAT or mechanical causes by itself: all three
      VE channels share the MAF/RPM measurement of AIR_MASS_PER_CYCLE.

    * ``derived:STFT_BANK_BALANCE`` / ``derived:LTFT_BANK_BALANCE`` —
      short / long fuel trim difference between banks. Expected ~0
      everywhere; sustained nonzero indicates a bank-specific fuel or air
      issue. The STFT version is **time-smoothed**
      (``STFT_BANK_BALANCE_SMOOTHING_SECONDS``) because the fast STFT channel's
      instantaneous bank delta is dominated by round-robin sampling skew during
      transients; the slow LTFT version is left raw (it is already the drift
      channel). Inputs: ``obd:SHORT_FUEL_TRIM_1/2``, ``obd:LONG_FUEL_TRIM_1/2``.

    * ``derived:TOTAL_TRIM_B1`` / ``derived:TOTAL_TRIM_B2`` (Task 4.3) —
      LTFT + STFT per bank, the vacuum-leak-vs-MAF-skew discriminator (a
      human reads the fitted ``total_trim_vs_rpm_load`` map: elevated only
      at idle/low-load points → unmetered air; elevated everywhere → MAF
      or fuel-pressure bias). No detection logic here, just the channel.
      Inputs: same as the bank-balance pair above, per bank.

    * ``derived:AFR_BANK_BALANCE`` — wideband AFR sensor voltage
      difference between banks. Once the AF-sensor DIDs are polling
      (see ``obd_dids`` in ``canary.example.yaml``), this becomes the
      sharpest bank-asymmetry detector available. Inputs: the vehicle's
      per-bank A/F sensor voltage channels (:mod:`src.signals.vehicle_signals`).

    * ``derived:CAT_EFFICIENCY_B1`` / ``derived:CAT_EFFICIENCY_B2``
      (Task 4.4) — a bounded **0-1 heuristic index**, explicitly NOT a
      claimed literal catalyst efficiency percentage: ``1 -
      downstream_switch_count / upstream_switch_count`` over a trailing
      ``CAT_EFFICIENCY_WINDOW_SECONDS`` window (the classic secondary-O2
      switch-count cat test — a healthy cat damps the fast upstream
      rich/lean switching before it reaches the downstream sensor; 1 =
      downstream never switches relative to upstream, 0 = downstream
      switches exactly as fast, i.e. no damping). Read only as a trend
      (fingerprinted, Task 4.4), not a pass/fail number. Inputs:
      ``obd:O2_B{1,2}S1`` (upstream, narrowband), ``obd:O2_B{1,2}S2``
      (downstream, narrowband). Mode 06 catalyst-monitor margin
      (``src/control/monitor_state.py``) cross-check deliberately **not**
      wired here — that margin is a pass/fail threshold test result
      (test value vs min/max), not a continuous trend series, so it
      doesn't align cleanly with this rolling-window index; noted as a
      follow-up rather than forced.

    * ``derived:TPS_VOLTAGE_PER_PCT`` — raw TPS voltage divided by the
      ECM-calculated throttle percent. Expected to be linear (constant
      ratio) within the operating range. Drift means the calibration
      curve is shifting before any DTC. Inputs: the vehicle's raw TPS
      voltage channel (:mod:`src.signals.vehicle_signals`), ``obd:THROTTLE_POS``.

    The function is pure; caller is responsible for ordering it
    correctly in the feature pipeline (it expects mode segmentation
    and any other prior steps to have run first if they care about
    `operating_mode`).
    """
    if wide.empty:
        return wide.copy()
    out = wide.copy()
    computed: dict[str, pd.Series] = {}
    for signal in ANOMALY_SIGNALS:
        inputs = SignalInputs(
            frame=out,
            vehicle=vehicle,
            series=tuple(_series(out, *group) for group in signal.input_groups),
            computed=computed,
        )
        values = signal.compute(inputs)
        if values is None:
            continue
        computed[signal.name] = values
        out[signal.name] = values
        _append_dependency_gap(out, signal.name, *signal.input_groups)
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
            if seg_dt <= 0 or seg_dt > _ACCEL_MAX_SEGMENT_DT_S:
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


def summarize_trip(
    wide: pd.DataFrame,
    vehicle: VehicleParameters,
    *,
    calibration: AfrCalibration | None = None,
) -> TripSummary:
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
    enriched = compute_derived_channels(wide, vehicle, calibration=calibration)
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
    speed = _series(enriched, "obd:SPEED")
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
