"""Speed, acceleration and specific force for one dyno window, on one time base (H07.3).

The producer of :class:`src.analysis.dyno_contract.MotionSamples` from a session. For a
window ``[start, end]`` of the acquisition clock it returns, on one uniform
grid (:data:`DEFAULT_HZ`):

* **speed** — the best speed the vehicle profile lists
  (``dyno.yaml`` ``motion_sources.speed``, best first: on the Xterra the
  ~100 Hz ``0x2D1`` broadcast, decoded on read, then the ``0x6F7`` ABS copy,
  then the Mode 01 PID), divided by that channel's *measured* ratio to GPS,
  so it is absolute road speed at the source's resolution. GPS speed itself
  is the reference and the last resort. Nothing here names a channel or a
  ratio: both are profile data with their evidence.
* **acceleration** — the central difference of that speed, smoothed by a
  local quadratic fit (Savitzky-Golay) over :data:`DEFAULT_SMOOTHING_S`
  (:func:`matched_kernels`).
* **specific force** — ``imu:SF_LON`` through the session's recorded frame
  (``imu_frame.json``, H02.4), in m/s² (g × ``STANDARD_GRAVITY``), smoothed by
  the *same* kernel as the speed. ``f − a`` is the quantity every dyno fit
  leans on; two different filters would put their difference in it.
* **rpm** (``motion_sources.rpm``), **GPS altitude**, and optionally the
  operator-demand channel the WOT criterion reads.

**One time base, honestly.** Every channel is taken at its own acquisition
timestamps (CAN frames when received, IMU samples at the chip's sample
instant, GPS epochs when received) and brought to the grid without shifting:
channels faster than the grid are averaged into each grid step (a symmetric
boxcar, no phase), slower ones interpolated linearly. A hole in a selected
channel is refused (``inputs_gap``), never bridged. Nothing is compared by its
aggregate: the GPS scale check pairs GPS epochs with the speed at the same
instant (less a fitted lag, since GPS is received late).

**Refusals name their cause.** ``gps_quality`` (any epoch in the window
failing the H04.3 speed-committee gates, or the gate channels not recorded),
``inputs_missing``, ``inputs_gap``, ``speed_too_coarse`` (no listed source,
nor GPS, has enough samples per smoothing window to differentiate),
``speed_scale_mismatch`` (the scaled speed disagrees with GPS by more than
:data:`SPEED_SCALE_TOLERANCE`: a stale ratio, another tire, a wrong channel).

**A missing IMU frame degrades, by name.** :class:`DynoInputs` is still built
(speed, acceleration, rpm and altitude are useful alone) and records why
there is no specific force (:class:`ImuUnavailable`); :meth:`DynoInputs.motion`,
which every dyno fit needs, then refuses with that cause.

**The frame of record travels with the samples.** ``imu_reference_id`` is
copied onto every :class:`~src.analysis.dyno_contract.MotionSamples`, and the dyno's
estimators refuse to apply a pitch offset measured in any other frame.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from src.analysis.dyno_contract import (
    GPS_SPEED_CHANNEL,
    DynoRefusal,
    FloatArray,
    MotionSamples,
    MotionSources,
)
from src.decode.bindings import Bindings
from src.decode.engineering import is_decoded_column, physical_series, repo_bindings
from src.preprocessing.decode import decode_can_rows, keep_can_ids
from src.preprocessing.imu_session import (
    RAW_ACCEL_COLUMNS,
    SF_LON_COLUMN,
    channels_from_long,
    read_imu_frame_sidecar,
    specific_force_from_sidecar,
)
from src.preprocessing.loader import read_session
from src.signals.committees import SPEED_COMMITTEE, STANDARD_GRAVITY
from src.storage.broadcast_storage import held_series, session_can_storage
from src.telemetry import CAN_SOURCE, SOURCE_SEP

GPS_ALTITUDE_CHANNEL = "gps:altitude"

#: Output grid rate. Every dyno fit strides or bins internally; 20 Hz keeps a
#: 5 s pull at 100 samples (``dyno.MIN_SAMPLES`` is 50) and averages the
#: ~100 Hz broadcast speed and the IMU into each step.
DEFAULT_HZ = 20.0

#: Width of the local quadratic fit that smooths speed, specific force and
#: rpm and differentiates speed. 0.5 s passes a pull's acceleration envelope
#: and turns 0.1 km/h broadcast quantisation into ~0.02 m/s² of derivative
#: noise.
DEFAULT_SMOOTHING_S = 0.5
SMOOTHING_POLYORDER = 2

#: A speed source must put this many samples in one smoothing window to be
#: differentiated. The Mode 01 PID (~0.7 Hz) and 1 Hz GPS do not at 0.5 s;
#: they are named in the refusal rather than stretched.
MIN_SAMPLES_PER_SMOOTHING = 5

#: A hole in a smoothed channel (speed, specific force, rpm) longer than this
#: many of its median sample intervals, or than a quarter of the smoothing
#: window if that is longer, is a gap: the fit across it would be invented.
GAP_INTERVALS = 5.0
#: GPS channels are interpolated, not smoothed: at most one missing 1 Hz
#: epoch. Absolute, not a multiple of the median interval: the pre-HAT USB
#: receiver flushes each epoch twice ~0.15 s apart (median interval 0.34 s on
#: 2026-09-20T175050), so a median-based limit refused every real window.
GPS_MAX_GAP_S = 2.5

#: Scaled speed against GPS speed-over-ground, per epoch at the fitted lag
#: (least squares through the origin). The per-epoch ratio's steady-state
#: 10th-90th percentile spread is about +/-2.5 % on the pre-HAT receiver
#: (2026-09-20/21, gated like the committee), and GPS error is correlated
#: from epoch to epoch, so this is a gross-error check (a stale ratio after a
#: tire change, the wrong channel, a missing correction), not a calibration:
#: using 0x6F7's 0.9203 for 0x2D1 (0.932) is a 1.3 % error and passes it.
SPEED_SCALE_TOLERANCE = 0.03
#: GPS is received late: the committee evidence shows 3-5 s behind the wheels
#: on a launch. The check fits the lag over this range.
GPS_MAX_LAG_S = 6.0
GPS_MIN_LAG_S = -1.0
GPS_LAG_STEP_S = 0.05
#: Epochs slower than this carry Doppler jitter and the collector's 5 km/h
#: stationary snap; a check needs this many faster ones.
GPS_CHECK_MIN_KPH = 20.0
GPS_CHECK_MIN_EPOCHS = 5

ImuUnavailableKind = Literal["imu_frame_missing", "imu_frame_refused", "imu_rows_missing"]


@dataclass(frozen=True)
class ImuSpecificForce:
    """``imu:SF_LON`` in m/s² at the IMU's own instants, and its frame of record."""

    series: pd.Series
    reference_id: str


@dataclass(frozen=True)
class ImuUnavailable:
    """Why a window has no specific force."""

    kind: ImuUnavailableKind
    reason: str


@dataclass(frozen=True)
class GpsQuality:
    """The GPS epochs in the window, all of which passed the H04.3 gates."""

    epochs: int
    #: The gates applied, as written (``gps:num_satellites >= 7``).
    gates: tuple[str, ...]
    #: Per gate channel, the value in the window nearest its limit.
    worst: dict[str, float]


@dataclass(frozen=True)
class GpsSpeedCheck:
    """Scaled speed against GPS: ``ratio`` = GPS / scaled at the fitted lag."""

    status: Literal["checked", "reference", "not_checked"]
    ratio: float | None = None
    lag_s: float | None = None
    n_epochs: int = 0
    reason: str | None = None


@dataclass(frozen=True)
class DynoInputs:
    """One dyno window on one time base (H07.3). ``t_s`` is seconds since ``start``."""

    start: pd.Timestamp
    t_s: FloatArray
    #: Absolute road speed: the source over its ratio to GPS, smoothed.
    speed_mps: FloatArray
    accel_mps2: FloatArray
    #: GPS speed-over-ground interpolated to the grid, as received (it lags).
    gps_speed_mps: FloatArray
    speed_source: str
    speed_ratio_to_gps: float
    speed_evidence: str
    gps_quality: GpsQuality
    gps_check: GpsSpeedCheck
    hz: float
    smoothing_s: float
    sf_lon_mps2: FloatArray | None = None
    imu_reference_id: str | None = None
    imu_unavailable: ImuUnavailable | None = None
    altitude_m: FloatArray | None = None
    rpm: FloatArray | None = None
    rpm_source: str | None = None
    demand: FloatArray | None = None
    demand_source: str | None = None
    #: Optional inputs that are absent, and why (``rpm``, ``altitude``, ``demand``).
    unavailable: dict[str, str] = field(default_factory=dict)

    def motion(self) -> MotionSamples | DynoRefusal:
        """The samples every dyno fit takes, or why there are none.

        Refuses (``imu_unavailable``) with the recorded cause when the window
        has no specific force: no ``imu_frame.json``, a refused frame, or no
        raw accelerometer rows.
        """
        if self.sf_lon_mps2 is None:
            cause = self.imu_unavailable
            detail = f"{cause.kind}: {cause.reason}" if cause is not None else "not given"
            return DynoRefusal(
                "motion_inputs",
                "imu_unavailable",
                f"no longitudinal specific force for this window ({detail}); every dyno fit "
                "needs imu:SF_LON",
            )
        return MotionSamples(
            t_s=self.t_s,
            speed_mps=self.speed_mps,
            accel_mps2=self.accel_mps2,
            sf_lon_mps2=self.sf_lon_mps2,
            altitude_m=self.altitude_m,
            rpm=self.rpm,
            imu_reference_id=self.imu_reference_id,
        )


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------


def _seconds(index: pd.Index, origin: pd.Timestamp) -> FloatArray:
    stamps = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    seconds: FloatArray = ((stamps - origin) / pd.Timedelta(seconds=1)).to_numpy(dtype=np.float64)
    return seconds


def _clean(series: pd.Series) -> pd.Series:
    series = pd.to_numeric(series, errors="coerce").dropna()
    series.index = pd.DatetimeIndex(pd.to_datetime(series.index, utc=True))
    series = series[~series.index.duplicated(keep="last")]
    return series.sort_index(kind="stable").astype(float)


@dataclass(frozen=True)
class _Channel:
    """One channel's samples as seconds since the window start."""

    name: str
    t: FloatArray
    values: FloatArray

    @property
    def median_interval(self) -> float:
        return float(np.median(np.diff(self.t))) if len(self.t) > 1 else math.inf


def _channel(name: str, series: pd.Series, origin: pd.Timestamp) -> _Channel:
    clean = _clean(series)
    return _Channel(name, _seconds(clean.index, origin), clean.to_numpy(dtype=np.float64))


def _within(channel: _Channel, lo: float, hi: float) -> _Channel:
    inside = (channel.t >= lo) & (channel.t <= hi)
    return _Channel(channel.name, channel.t[inside], channel.values[inside])


def _gap(channel: _Channel, grid: FloatArray, limit_s: float) -> str | None:
    """Why ``channel`` does not cover ``grid`` without a hole longer than ``limit_s``."""
    near = _within(channel, grid[0] - limit_s, grid[-1] + limit_s)
    if len(near.t) < 2:
        return f"{channel.name} has {len(near.t)} sample(s) near the window"
    lead = near.t[0] - grid[0]
    tail = grid[-1] - near.t[-1]
    if lead > limit_s / 2.0:
        return f"{channel.name} starts {lead:.2f} s after the window (plus smoothing margin) does"
    if tail > limit_s / 2.0:
        return f"{channel.name} ends {tail:.2f} s before the window (plus smoothing margin) does"
    steps = np.diff(near.t)
    worst = int(np.argmax(steps))
    if steps[worst] > limit_s:
        return (
            f"{channel.name} has a {steps[worst]:.2f} s hole at t = {near.t[worst]:.2f} s "
            f"(limit {limit_s:.2f} s)"
        )
    return None


def _on_grid(channel: _Channel, grid: FloatArray, step: float) -> FloatArray:
    """``channel`` at ``grid``: the mean of its samples in each step when it is
    faster than the grid, linear interpolation when it is not."""
    near = _within(channel, grid[0] - step, grid[-1] + step)
    if near.median_interval < step:
        index = np.floor((near.t - (grid[0] - step / 2.0)) / step).astype(int)
        inside = (index >= 0) & (index < len(grid))
        counts = np.bincount(index[inside], minlength=len(grid)).astype(np.float64)
        sums = np.bincount(index[inside], weights=near.values[inside], minlength=len(grid))
        filled = counts > 0
        means = np.divide(sums, counts, out=np.zeros(len(grid)), where=filled)
        # Empty steps are short holes the gap check already allowed.
        binned: FloatArray = np.interp(grid, grid[filled], means[filled])
        return binned
    interpolated: FloatArray = np.interp(grid, near.t, near.values)
    return interpolated


def matched_kernels(half_width: int, step_s: float) -> tuple[FloatArray, FloatArray]:
    """A smoothing kernel and the derivative kernel matched to it.

    Smoothing is a local quadratic fit (Savitzky-Golay) over
    ``2·half_width + 1`` points. The derivative is *that same smoothing
    applied to the central difference*, not the fit's own slope: the slope of
    a quadratic fit is a differently weighted average of the derivative
    (on a 0.1 s launch the two disagreed by 0.25 m/s², all of it landing in
    ``f − a``). So ``smooth(f) − derivative(v)`` differs from
    ``smooth(f − dv/dt)`` only by a central difference against a true
    derivative, which the smoothing has already attenuated away.

    Both are returned ``2·half_width + 3`` long (the smoothing padded with a
    zero each end), so one ``valid`` correlation aligns them on the same
    samples; both are exact on a quadratic.
    """
    offsets = np.arange(-half_width, half_width + 1, dtype=np.float64)
    design = np.vander(offsets, SMOOTHING_POLYORDER + 1, increasing=True)
    fit = np.linalg.pinv(design)[0]
    smooth: FloatArray = np.concatenate([[0.0], fit, [0.0]])
    derivative: FloatArray = (
        np.concatenate([[0.0, 0.0], fit]) - np.concatenate([fit, [0.0, 0.0]])
    ) / (2.0 * step_s)
    return smooth, derivative


def _apply(values: FloatArray, coefficients: FloatArray) -> FloatArray:
    """The kernel centred on each interior point (``len − len(kernel) + 1`` values)."""
    out: FloatArray = np.correlate(values, coefficients, mode="valid")
    return out


def _gap_limit(channel: _Channel, smoothing_s: float) -> float:
    return max(GAP_INTERVALS * channel.median_interval, smoothing_s / 4.0)


# ---------------------------------------------------------------------------
# GPS
# ---------------------------------------------------------------------------


def _gps_quality(
    channels: Mapping[str, pd.Series], origin: pd.Timestamp, duration: float
) -> GpsQuality | DynoRefusal:
    """Every GPS epoch in the window against the speed committee's gates (H04.3)."""
    name = "motion_inputs"
    gates = SPEED_COMMITTEE.gates
    wanted = [GPS_SPEED_CHANNEL, *(gate.channel for gate in gates)]
    by_channel: dict[str, pd.Series] = {}
    for column in wanted:
        series = channels.get(column)
        if series is None or series.empty:
            continue
        clean = _clean(series)
        seconds = _seconds(clean.index, origin)
        by_channel[column] = clean[(seconds >= 0.0) & (seconds <= duration)]
    if GPS_SPEED_CHANNEL not in by_channel or by_channel[GPS_SPEED_CHANNEL].empty:
        return DynoRefusal(
            name,
            "inputs_missing",
            f"no {GPS_SPEED_CHANNEL} epoch in the window: absolute speed comes from GPS (H07 "
            "rules), and the collector withholds it on a marginal fix",
        )
    for gate in gates:
        if gate.channel not in by_channel or by_channel[gate.channel].empty:
            return DynoRefusal(
                name,
                "gps_quality",
                f"{gate.channel} was not recorded in the window, so the fix cannot be shown to "
                "meet the H04.3 speed gates (they fail closed)",
            )
    epochs = sorted(set().union(*(set(s.index) for s in by_channel.values())))
    failures: list[str] = []
    for epoch in epochs:
        point = {column: s.get(epoch) for column, s in by_channel.items()}
        for gate in gates:
            why = gate.failure(point)
            if why is not None:
                failures.append(f"{_seconds(pd.Index([epoch]), origin)[0]:.1f} s: {why}")
                break
    if failures:
        return DynoRefusal(
            name,
            "gps_quality",
            f"{len(failures)} of {len(epochs)} GPS epochs in the window fail the H04.3 speed "
            f"gates (first: {failures[0]}); absolute speed and its scale check need a "
            "trustworthy fix throughout",
        )
    worst: dict[str, float] = {}
    labels: list[str] = []
    for gate in gates:
        values = by_channel[gate.channel]
        if gate.min_value is not None:
            worst[gate.channel] = float(values.min())
            labels.append(f"{gate.channel} >= {gate.min_value:g}")
        if gate.max_value is not None:
            worst[gate.channel] = float(values.max())
            labels.append(f"{gate.channel} <= {gate.max_value:g}")
        if gate.max_abs is not None:
            worst[gate.channel] = float(values.abs().max())
            labels.append(f"|{gate.channel}| <= {gate.max_abs:g}")
    return GpsQuality(epochs=len(epochs), gates=tuple(labels), worst=worst)


def _gps_speed_check(
    gps: _Channel, speed: _Channel, ratio_to_gps: float, duration: float
) -> GpsSpeedCheck:
    """GPS / scaled speed at GPS epochs, at the lag that fits best.

    ``speed`` is the chosen source in km/h at its own instants; each GPS epoch
    ``t`` is paired with it at ``t − lag`` (GPS is received late), the same
    epochs at every lag. The ratio is least squares through the origin.
    """
    epochs = _within(gps, 0.0, duration)
    fast = epochs.values >= GPS_CHECK_MIN_KPH
    t = epochs.t[fast]
    g = epochs.values[fast]
    covered = (t - GPS_MAX_LAG_S >= speed.t[0]) & (t - GPS_MIN_LAG_S <= speed.t[-1])
    t, g = t[covered], g[covered]
    if len(t) < GPS_CHECK_MIN_EPOCHS:
        return GpsSpeedCheck(
            "not_checked",
            n_epochs=len(t),
            reason=f"{len(t)} GPS epochs at >= {GPS_CHECK_MIN_KPH:.0f} km/h with the speed "
            f"source recorded {GPS_MAX_LAG_S:.0f} s before them; the check needs "
            f"{GPS_CHECK_MIN_EPOCHS}",
        )
    best: tuple[float, float, float] | None = None
    for lag in np.arange(GPS_MIN_LAG_S, GPS_MAX_LAG_S + 1e-9, GPS_LAG_STEP_S):
        scaled = np.interp(t - lag, speed.t, speed.values) / ratio_to_gps
        denominator = float(scaled @ scaled)
        if denominator <= 0.0:
            continue
        k = float(g @ scaled) / denominator
        residual = float(np.sum((g - k * scaled) ** 2))
        if best is None or residual < best[0] - 1e-12:
            best = (residual, k, float(lag))
    if best is None:
        return GpsSpeedCheck("not_checked", n_epochs=len(t), reason="the scaled speed is zero")
    return GpsSpeedCheck("checked", ratio=best[1], lag_s=best[2], n_epochs=len(t))


# ---------------------------------------------------------------------------
# The producer
# ---------------------------------------------------------------------------


def build_motion_inputs(
    channels: Mapping[str, pd.Series],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    sources: MotionSources,
    imu: ImuSpecificForce | ImuUnavailable,
    demand_channel: str | None = None,
    hz: float = DEFAULT_HZ,
    smoothing_s: float = DEFAULT_SMOOTHING_S,
) -> DynoInputs | DynoRefusal:
    """Build one dyno window from channels already in engineering units (no I/O).

    ``channels`` maps ``source:channel`` to a numeric series indexed by UTC
    acquisition time: speeds in km/h, rpm in rpm, ``gps:altitude`` in m, the
    GPS gate channels (``gps:num_satellites``, ``gps:hdop``) and the demand
    channel as recorded. They may extend beyond the window; the smoothing
    margin and the GPS lag search read up to :data:`GPS_MAX_LAG_S` before it.

    Refusals, in order: ``invalid_input``, then GPS (``inputs_missing``,
    ``gps_quality``, ``inputs_gap``), then speed (``speed_too_coarse``,
    ``inputs_gap``, ``speed_scale_mismatch``), then ``inputs_gap`` for any
    other selected channel with a hole. A missing IMU frame, rpm, altitude or
    demand is recorded, not refused.
    """
    name = "motion_inputs"
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None or end.tzinfo is None:
        return DynoRefusal(name, "invalid_input", "start and end must be timezone-aware (UTC)")
    duration = (end - start) / pd.Timedelta(seconds=1)
    if duration <= 0.0 or hz <= 0.0:
        return DynoRefusal(
            name, "invalid_input", f"window of {duration:.2f} s at {hz:g} Hz holds no samples"
        )
    step = 1.0 / hz
    half = max(2, round(smoothing_s * hz / 2.0))
    width_s = 2 * half * step
    margin = half + 1  # the matched kernels' half length
    grid = np.arange(-margin, round(duration * hz) + margin + 1, dtype=np.float64) * step
    output = grid[margin:-margin]

    quality = _gps_quality(channels, start, duration)
    if isinstance(quality, DynoRefusal):
        return quality
    gps = _channel(GPS_SPEED_CHANNEL, channels[GPS_SPEED_CHANNEL], start)
    gps_gap = _gap(gps, output, GPS_MAX_GAP_S)
    if gps_gap is not None:
        return DynoRefusal(name, "inputs_gap", gps_gap)

    # Speed: the first listed source present and fine enough to differentiate.
    candidates = [(s.channel, s.ratio_to_gps, s.evidence) for s in sources.speed]
    candidates.append((GPS_SPEED_CHANNEL, 1.0, "the absolute reference (ratio 1 by definition)"))
    coarse: list[str] = []
    chosen: tuple[_Channel, float, str] | None = None
    for column, ratio, evidence in candidates:
        series = channels.get(column)
        if series is None or series.empty:
            continue
        candidate = _channel(column, series, start)
        near = _within(candidate, grid[0] - width_s, grid[-1] + width_s)
        if len(near.t) < 2:
            continue
        per_window = width_s / near.median_interval
        if per_window < MIN_SAMPLES_PER_SMOOTHING:
            coarse.append(f"{column} ({1.0 / near.median_interval:.2f} Hz)")
            continue
        chosen = (candidate, ratio, evidence)
        break
    if chosen is None:
        present = ", ".join(coarse) if coarse else "none present in the window"
        return DynoRefusal(
            name,
            "speed_too_coarse",
            f"no speed source puts {MIN_SAMPLES_PER_SMOOTHING} samples in the {width_s:.2f} s "
            f"smoothing window ({present}); differentiating a coarser speed invents the "
            "acceleration between its steps",
        )
    speed_channel, ratio, evidence = chosen
    speed_gap = _gap(speed_channel, grid, _gap_limit(speed_channel, smoothing_s))
    if speed_gap is not None:
        return DynoRefusal(name, "inputs_gap", speed_gap)
    if speed_channel.name == GPS_SPEED_CHANNEL:
        check = GpsSpeedCheck("reference", ratio=1.0, lag_s=0.0, reason="speed is GPS itself")
    else:
        check = _gps_speed_check(gps, speed_channel, ratio, duration)
        if check.ratio is not None and abs(check.ratio - 1.0) > SPEED_SCALE_TOLERANCE:
            return DynoRefusal(
                name,
                "speed_scale_mismatch",
                f"{speed_channel.name} over its profile ratio {ratio:g} reads "
                f"{100 * (1.0 / check.ratio - 1.0):+.1f} % against GPS ({check.n_epochs} epochs, "
                f"GPS {check.lag_s:+.2f} s late); this window implies a ratio of "
                f"{ratio / check.ratio:.4f}. Re-measure the ratio (a tire change moves it) "
                f"before trusting power (tolerance {100 * SPEED_SCALE_TOLERANCE:.0f} %)",
            )

    smooth, derivative = matched_kernels(half, step)
    kmh = _on_grid(speed_channel, grid, step)
    mps = kmh / 3.6 / ratio
    speed = _apply(mps, smooth)
    accel = _apply(mps, derivative)

    sf: FloatArray | None = None
    reference_id: str | None = None
    unavailable_imu: ImuUnavailable | None = None
    if isinstance(imu, ImuSpecificForce):
        imu_channel = _channel(SF_LON_COLUMN, imu.series, start)
        imu_gap = _gap(imu_channel, grid, _gap_limit(imu_channel, smoothing_s))
        if imu_gap is not None:
            return DynoRefusal(name, "inputs_gap", imu_gap)
        sf = _apply(_on_grid(imu_channel, grid, step), smooth)
        reference_id = imu.reference_id
    else:
        unavailable_imu = imu

    unavailable: dict[str, str] = {}
    rpm: FloatArray | None = None
    rpm_source: str | None = None
    for column in sources.rpm:
        series = channels.get(column)
        if series is None or series.empty:
            continue
        rpm_channel = _channel(column, series, start)
        if len(_within(rpm_channel, grid[0], grid[-1]).t) < 2:
            continue
        rpm_gap = _gap(rpm_channel, grid, _gap_limit(rpm_channel, smoothing_s))
        if rpm_gap is not None:
            return DynoRefusal(name, "inputs_gap", rpm_gap)
        rpm = _apply(_on_grid(rpm_channel, grid, step), smooth)
        rpm_source = column
        break
    if rpm is None:
        unavailable["rpm"] = (
            f"none of {sources.rpm} has samples in the window"
            if sources.rpm
            else "the profile lists no rpm source (dyno.yaml motion_sources.rpm)"
        )

    altitude: FloatArray | None = None
    altitude_series = channels.get(GPS_ALTITUDE_CHANNEL)
    if altitude_series is None or altitude_series.empty:
        unavailable["altitude"] = f"{GPS_ALTITUDE_CHANNEL} was not recorded"
    else:
        altitude_channel = _channel(GPS_ALTITUDE_CHANNEL, altitude_series, start)
        altitude_gap = _gap(altitude_channel, output, GPS_MAX_GAP_S)
        if altitude_gap is not None:
            return DynoRefusal(name, "inputs_gap", altitude_gap)
        altitude = _on_grid(altitude_channel, output, step)

    demand: FloatArray | None = None
    if demand_channel is not None:
        demand_series = channels.get(demand_channel)
        if demand_series is None or demand_series.empty:
            unavailable["demand"] = f"{demand_channel} was not recorded"
        else:
            demand_ch = _channel(demand_channel, demand_series, start)
            demand_gap = _gap(demand_ch, output, _gap_limit(demand_ch, smoothing_s))
            if demand_gap is not None:
                return DynoRefusal(name, "inputs_gap", demand_gap)
            demand = _on_grid(demand_ch, output, step)
    else:
        unavailable["demand"] = "no operator-demand channel was asked for"

    return DynoInputs(
        start=start,
        t_s=output,
        speed_mps=speed,
        accel_mps2=accel,
        gps_speed_mps=_on_grid(gps, output, step) / 3.6,
        speed_source=speed_channel.name,
        speed_ratio_to_gps=ratio,
        speed_evidence=evidence,
        gps_quality=quality,
        gps_check=check,
        hz=hz,
        smoothing_s=width_s,
        sf_lon_mps2=sf,
        imu_reference_id=reference_id,
        imu_unavailable=unavailable_imu,
        altitude_m=altitude,
        rpm=rpm,
        rpm_source=rpm_source,
        demand=demand,
        demand_source=demand_channel if demand is not None else None,
        unavailable=unavailable,
    )


# ---------------------------------------------------------------------------
# Session I/O
# ---------------------------------------------------------------------------


def _filters(
    columns: Iterable[str], can_ids: set[int], lo: pd.Timestamp, hi: pd.Timestamp
) -> list[list[tuple[str, str, Any]]]:
    from src.decode.on_read import can_channel_spellings

    window: list[tuple[str, str, Any]] = [("timestamp", ">=", lo), ("timestamp", "<=", hi)]
    by_source: dict[str, list[str]] = {}
    for column in columns:
        source, channel = column.split(SOURCE_SEP, 1)
        by_source.setdefault(source, []).append(channel)
    filters = [
        [("source", "==", source), ("channel", "in", sorted(set(names))), *window]
        for source, names in by_source.items()
    ]
    if can_ids:
        filters.append(
            [
                ("source", "==", CAN_SOURCE),
                ("channel", "in", can_channel_spellings(can_ids)),
                *window,
            ]
        )
    return filters


def read_imu_specific_force(
    session_dir: Path, channels: Mapping[str, pd.Series]
) -> ImuSpecificForce | ImuUnavailable:
    """``imu:SF_LON`` (m/s²) through the session's frame of record, or why not."""
    sidecar = read_imu_frame_sidecar(session_dir)
    if sidecar is None:
        return ImuUnavailable(
            "imu_frame_missing",
            f"{session_dir.name} has no imu_frame.json: resolve its frame first "
            "(scripts/resolve_imu_frame.py, H02.4)",
        )
    if sidecar.specific_force is None:
        refusal = sidecar.refusal
        cause = f"{refusal.kind}: {refusal.reason}" if refusal is not None else "unresolved"
        return ImuUnavailable("imu_frame_refused", f"the session's imu frame was refused ({cause})")
    missing = [c for c in RAW_ACCEL_COLUMNS if c not in channels or channels[c].empty]
    if missing:
        return ImuUnavailable(
            "imu_rows_missing", f"no {', '.join(missing)} rows in the window (raw accelerometer)"
        )
    accel = pd.concat({c: _clean(channels[c]) for c in RAW_ACCEL_COLUMNS}, axis=1, join="inner")
    sf = specific_force_from_sidecar(accel, sidecar)[SF_LON_COLUMN] * STANDARD_GRAVITY
    return ImuSpecificForce(series=sf, reference_id=sidecar.specific_force.reference_id)


def read_motion_inputs(
    session_dir: Path,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    sources: MotionSources,
    bindings: Bindings | None = None,
    demand_channel: str | None = None,
    hz: float = DEFAULT_HZ,
    smoothing_s: float = DEFAULT_SMOOTHING_S,
) -> DynoInputs | DynoRefusal:
    """:func:`build_motion_inputs` for a window of a stored session.

    One bounded read (the window plus the smoothing margin and the GPS lag
    search) of exactly the channels it needs; ``decoded:`` sources are decoded
    here from their raw frames, whatever their binding confidence (the loader
    decodes only ``confirmed`` ones on read), and scaled through the catalog.
    Specific force comes through ``imu_frame.json`` (:func:`read_imu_specific_force`).
    """

    catalog = bindings if bindings is not None else repo_bindings()
    wanted = {s.channel for s in sources.speed} | set(sources.rpm)
    wanted |= {GPS_SPEED_CHANNEL, GPS_ALTITUDE_CHANNEL, *(g.channel for g in SPEED_COMMITTEE.gates)}
    if demand_channel is not None:
        wanted.add(demand_channel)
    decoded = {c.split(SOURCE_SEP, 1)[1] for c in wanted if is_decoded_column(c)}
    signals = [s for s in catalog.signals if s.name in decoded] if catalog is not None else []
    subset = catalog.model_copy(update={"signals": signals}) if catalog and signals else None
    can_ids = {s.can_id for s in signals}
    stored = sorted(c for c in wanted if not is_decoded_column(c)) + list(RAW_ACCEL_COLUMNS)
    margin = pd.Timedelta(seconds=GPS_MAX_LAG_S + 2.0 * smoothing_s + 5.0)
    # P02.10: in a change-only session the value at the window's start may
    # have been stored up to one stale timeout earlier; read back that far.
    timeout = session_can_storage(Path(session_dir)).stale_timeout_seconds
    lo = pd.Timestamp(start) - margin - pd.Timedelta(seconds=timeout or 0.0)
    hi = pd.Timestamp(end) + margin
    long = read_session(
        Path(session_dir),
        filters=_filters(stored, can_ids, lo, hi),
        decoded="as_recorded",
        time_range=(lo, hi),
    )
    long = keep_can_ids(long, can_ids)
    if subset is not None:
        long = decode_can_rows(long, subset)
    raw = channels_from_long(long)
    channels = {
        column: physical_series(series, column, catalog) if is_decoded_column(column) else series
        for column, series in raw.items()
    }
    # P02.10: a session storing CAN frames change-only kept a broadcast frame
    # only when its payload changed. Interpolating between those rows would
    # invent ramps across every hold and read a steady leg as holes, so each
    # decoded channel gets its unchanged frames back: the last stored value,
    # repeated at the channel's frame period (at most the grid step), for up
    # to the stale timeout the recorder re-writes an unchanged id within.
    if timeout is not None:
        channels = {
            column: held_series(series, validity_s=timeout, period_s=1.0 / hz)
            if is_decoded_column(column)
            else series
            for column, series in channels.items()
        }
    imu = read_imu_specific_force(Path(session_dir), channels)
    return build_motion_inputs(
        channels,
        start=start,
        end=end,
        sources=sources,
        imu=imu,
        demand_channel=demand_channel,
        hz=hz,
        smoothing_s=smoothing_s,
    )


__all__ = [
    "DEFAULT_HZ",
    "DEFAULT_SMOOTHING_S",
    "GPS_ALTITUDE_CHANNEL",
    "SPEED_SCALE_TOLERANCE",
    "DynoInputs",
    "GpsQuality",
    "GpsSpeedCheck",
    "ImuSpecificForce",
    "ImuUnavailable",
    "build_motion_inputs",
    "read_imu_specific_force",
    "read_motion_inputs",
    "matched_kernels",
]
