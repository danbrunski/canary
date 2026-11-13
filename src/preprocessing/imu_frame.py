"""Gravity/frame resolution for raw IMU channels (Phase 2a.4).

Turns raw body-frame ``imu:ACCEL_X/Y/Z`` into frame-resolved
``imu:ACCEL_LON``/``imu:ACCEL_LAT`` (vehicle-longitudinal / vehicle-lateral,
in g), per ``docs/roadmap/archive/imu-gps-integration.md`` ("Gravity / frame
resolution"). Three steps, run in this order:

1. **Gravity estimate** (:func:`estimate_gravity`) — a low-pass of the
   3-axis accel vector, updated only during *quiescent* windows
   (:func:`quiescent_mask`: steady OBD speed **and** steady GPS heading —
   the vehicle isn't accelerating or turning). Vehicle acceleration is
   transient (high-frequency); gravity is DC — a running low-pass over
   quiescent samples tracks "down" in body coordinates *and* absorbs
   mount-tilt/temperature drift for free, which is why this re-estimates
   continuously rather than calibrating once (a one-shot calibration on a
   sloped driveway would be permanently biased by the driveway).
2. **Heading resolution** (:func:`resolve_yaw_offset`) — gravity is
   rotationally symmetric about vertical, so it carries zero information
   about which horizontal direction is "forward". During *straight*
   segments (:func:`straight_mask`: steady GPS course, independent of
   speed — this includes accel/brake events that happen to occur on a
   straight pull) the horizontal linear-accel vector traces out the
   vehicle's fore-aft axis; its direction in body coordinates is the
   mount's yaw offset about vertical, and is otherwise unobservable
   (persisted once per session — see :func:`write_yaw_offset` — since it's
   a property of the *mount*, not of any one instant).
3. **Sign** (:func:`resolve_longitudinal_sign`) — longitudinal is a line,
   not a direction (steps 1-2 fix the axis but not which end is
   "forward"), so the final polarity comes from correlating against the
   speed derivative — ground truth we already have with no IMU
   involved — rather than trusting the IMU's own arbitrary axis labeling.

**Speed.** Every speed this module steers by (quiescence, sign) is the best
one the session has, :data:`SPEED_SOURCES` in order: the ~100 Hz ``0x2D1``
broadcast, then the Mode 01 PID, then GPS (H02.4). The ``tuning`` capture
profile does not poll ``obd:SPEED``, so a mask tied to it never found a
quiescent window there. ``decoded:`` speed holds raw counts and is scaled
through :func:`src.decode.engineering.physical_series`.

**Axes.** Resolved axes follow ISO 8855: ``LON`` forward, ``LAT`` to the left,
vertical up, so ``LAT = up × LON``. A sign flip of ``LON`` flips ``LAT`` with
it; otherwise the lateral sign would depend on which way the yaw fit happened
to land.

**Two families** (H02.4): :func:`resolve_frame` gives *linear* acceleration
(gravity removed by the quiescent low-pass — tags and segmenting), and
:func:`resolve_specific_force` gives *specific force* (``a + g·sin θ``, gravity
kept — the driving dyno, H07). The low-pass also removes the slowly varying
grade, which the dyno needs, so the specific-force frame uses **one constant
rotation per session** from a recorded reference (:class:`SpecificForceFrame`):
the residual pitch it leaves is a single constant the dyno measures
(``src.analysis.dyno.estimate_pitch_offset``), never a drifting one.

**BNO055 bypass.** The BNO055 (the doc's recommended chip) does steps 1-2
on-chip: it outputs gravity-removed linear acceleration and an absolute
orientation quaternion directly, so its collector should populate
``imu:ACCEL_LON``/``imu:ACCEL_LAT`` pre-tilt-compensated already (still in
the *sensor's* arbitrary horizontal frame, not yet vehicle-aligned).
:func:`resolve_frame`'s ``gravity_removed=True`` skips straight to step 2
so the BNO055 path and the raw-chip (MPU-6050/ICM-20948) path share the
same heading + sign code and only differ in whether the gravity low-pass
runs — the bypass is a parameter, not a separate code path.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd

from src.atomic_write import atomic_write_text
from src.config import validate_broadcast_stale_timeout
from src.decode.bindings import Bindings
from src.decode.engineering import UnscaledDecodedChannelError, physical_series
from src.storage.broadcast_storage import is_broadcast_channel
from src.storage.sidecar import read_json_object

YAW_OFFSET_FILENAME = "imu_yaw_offset.json"

#: Speeds the quiescent mask and the sign steer by, best first (H02.4): the
#: ~100 Hz ``0x2D1`` broadcast (0.1 km/h per count, ``bindings.yaml``
#: ``VEHICLE_SPEED_2D1``), the Mode 01 PID (~0.7 Hz, 1 km/h steps; absent under
#: the ``tuning`` capture profile), then GPS speed-over-ground (1 Hz). The
#: first with any value is used for the whole session, never mixed per row:
#: the sources differ in lag and the ECU ones read ~8 % under GPS. Only
#: steadiness and the derivative's sign are read, so that scale needs no
#: correction here.
SPEED_SOURCES: tuple[str, ...] = ("decoded:VEHICLE_SPEED_2D1", "obd:SPEED", "gps:speed")
#: GPS course over ground, degrees: the only yaw reference (the recorded
#: decision in ``imu-gps-integration.md``).
HEADING_COLUMN = "gps:heading"

#: A quiescent reference whose mean specific force is not ~1 g is not gravity:
#: gravity-removed (fused) output, the wrong unit, or a failing part. The chip
#: read 1.0055 g at rest on the HAT (H02.2).
GRAVITY_MAGNITUDE_RANGE_G = (0.8, 1.2)

#: How far past a sample its quiescence (and straightness) is judged, s. The
#: speed and course at an IMU instant are the newest *earlier* reading, so for
#: up to one reading period after a manoeuvre starts they still look steady: a
#: trailing window alone marks the first second of every pull from rest
#: "quiescent" and pours 0.3 g into the gravity estimate. Judging each sample
#: over ``[t − window, t + lookahead]`` needs the lookahead to exceed the
#: slowest speed source's period: GPS 1 Hz, Mode 01 speed ~0.7 Hz (1.43 s).
QUIESCENT_LOOKAHEAD_SECONDS = 1.5

#: A speed or heading older than this at an IMU sample is missing there, not
#: carried: GPS is 1 Hz and Mode 01 speed ~0.7 Hz. A broadcast speed stored
#: change-only is held longer (:func:`input_max_age_seconds`).
INPUT_MAX_AGE_SECONDS = 3.0


def input_max_age_seconds(
    column: str,
    change_only_timeout_seconds: float | None,
    *,
    base_seconds: float = INPUT_MAX_AGE_SECONDS,
) -> float:
    """How old a reading of ``column`` may be at an IMU sample and still steer
    it: the one rule the offline resolver (``imu_session``), its part-by-part
    rebuild (``imu_stream``) and the live frame (``imu_live``) share.

    ``base_seconds`` (:data:`INPUT_MAX_AGE_SECONDS`) for every input of a
    stream that carries every frame. A broadcast channel (``can:``/``decoded:``
    /``dbc:``) of a stream stored change-only (P02.10: a frame is kept only when
    its payload changes, an unchanged id re-written before
    ``change_only_timeout_seconds``) has no row while its value holds, so its
    newest row stands for "still this value" until the timeout, never longer:
    ``max(base_seconds, timeout)``, the resampler's broadcast ceiling rule.
    Without it a speed frame with no rolling counter, stored once per
    re-write on a steady leg, ages out three seconds after each row and the leg
    loses its quiescent windows. ``None``: every frame stored (or a stream that
    is not change-only), and nothing changes.
    """
    if change_only_timeout_seconds is None or not is_broadcast_channel(column):
        return base_seconds
    return max(base_seconds, validate_broadcast_stale_timeout(change_only_timeout_seconds))


#: The gravity low-pass's time constant, seconds of *quiescent* samples
#: (M01.13). Each quiescent sample moves the estimate by
#: ``α = 1 − exp(−Δt/τ)`` of the way to it (:func:`gravity_low_pass_alpha`),
#: ``Δt`` the interval the sample covers, so the filter means the same thing at
#: every IMU rate. It was a per-sample ``α = 0.02`` until 2026-11-13: τ 3.96 s
#: at 12.5 Hz, 0.48 s at 104 Hz, 0.24 s at 208 Hz.
#:
#: The quiescence gate (speed std ≤ 1.5 km/h over the 2 s window plus the
#: 1.5 s look-ahead) passes a steady ramp up to ~0.042 g, so a gentle sustained
#: acceleration *is* fed to this filter, and only τ decides how much of it the
#: estimate swallows: ``1 − exp(−T/τ)`` after ``T`` seconds. The same τ sets
#: how fast a new road grade leaves ``ACCEL_LON`` (the linear family takes
#: grade out by design; ``SF_*`` keep it). The two cannot be told apart by an
#: accelerometer, so τ is a trade between them. Over two real drives' speed,
#: course and GPS-altitude grade with a synthesised IMU (M01.13, no stored
#: drive has IMU rows yet), the per-sample error against d(road speed)/dt is
#: flat from 5 to 15 s and 10 s is the longest τ within 5 % of the minimum on
#: both: it keeps 61 % of a 5 s gate-passing ramp (5 s: 37 %) and leaves about
#: 24-39 % of a grade's term in ``ACCEL_LON`` (5 s: 14-28 %). Numbers in
#: ``docs/roadmap/tasks/M01-physics-model-accuracy.md`` (M01.13).
GRAVITY_TIME_CONSTANT_SECONDS = 10.0

#: The longest interval one IMU sample stands for in the low-pass, seconds.
#: A sample after a gap (FIFO overrun, an IMU dropout) observed only its own
#: sample period, not the gap: without this it would take ``α → 1`` and
#: replace the estimate with one vibrating sample. The slowest LSM6DSOX rate
#: in use is 12.5 Hz (0.08 s), so every regular interval is below it.
GRAVITY_MAX_SAMPLE_SECONDS = 0.1


def gravity_low_pass_alpha(
    sample_seconds: float,
    *,
    time_constant_seconds: float = GRAVITY_TIME_CONSTANT_SECONDS,
    max_sample_seconds: float = GRAVITY_MAX_SAMPLE_SECONDS,
) -> float:
    """``α = 1 − exp(−Δt/τ)`` for a quiescent sample covering ``sample_seconds``.

    ``Δt`` is clamped to ``[0, max_sample_seconds]`` (see
    :data:`GRAVITY_MAX_SAMPLE_SECONDS`); a missing one (the session's first
    IMU sample has no interval) counts as ``max_sample_seconds``. Pure Python
    floats, so the offline loop (:func:`low_pass_gravity`) and the live frame
    (``imu_live``) compute the same bits from the same interval.
    """
    dt = (
        min(max(sample_seconds, 0.0), max_sample_seconds)
        if math.isfinite(sample_seconds)
        else max_sample_seconds
    )
    return -math.expm1(-dt / time_constant_seconds)


class GravityState(NamedTuple):
    """The gravity low-pass's whole state: the estimate (body frame, g) and
    the weight its samples carry, ``1 − exp(−T/τ)`` after ``T`` quiescent
    seconds (``0`` before any). Carried across parts by ``imu_stream``."""

    x: float
    y: float
    z: float
    weight: float

    @property
    def estimate(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


#: Before the first quiescent sample: no estimate, no weight.
GRAVITY_START = GravityState(0.0, 0.0, 0.0, 0.0)


def gravity_step(
    state: GravityState, sample: tuple[float, float, float], alpha: float
) -> GravityState:
    """One low-pass update, the one expression every path evaluates (offline
    :func:`low_pass_gravity`, live ``imu_live``).

    A bias-corrected exponential mean: ``w ← (1 − α)·w + α`` and
    ``g ← g + (α / w)·(a − g)``, which is ``Σ αₖ·∏(1 − α)·aₖ / w`` over the
    samples so far. Once ``w`` nears 1 (a few τ of quiescent samples) it is
    the plain ``g ← α·a + (1 − α)·g``; before that it is the time-weighted mean
    of the samples seen, not one seed sample held for τ. The first sample is
    the estimate exactly (``α / α = 1``). A zero-length sample with no weight
    yet stands in until a weighted one arrives.

    A sample with a non-finite axis is skipped: ``state`` itself is returned
    (callers test ``is``), so one NaN cannot poison an axis for the rest of
    the session (the ``ewm`` this replaced skipped NaNs too).
    """
    if not (math.isfinite(sample[0]) and math.isfinite(sample[1]) and math.isfinite(sample[2])):
        return state
    weight = (1.0 - alpha) * state.weight + alpha
    if weight == 0.0:
        return GravityState(sample[0], sample[1], sample[2], 0.0)
    gain = alpha / weight
    return GravityState(
        state.x + gain * (sample[0] - state.x),
        state.y + gain * (sample[1] - state.y),
        state.z + gain * (sample[2] - state.z),
        weight,
    )


def sample_intervals_seconds(timestamps: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    """Seconds from each IMU sample to the one before it (``nan`` for the
    first): the ``Δt`` each sample covers in the gravity low-pass.

    Integer nanoseconds divided once, so it equals the live frame's
    ``timedelta.total_seconds()`` of the same two instants bit for bit.
    """
    index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True)).as_unit("ns")
    ns = index.tz_localize(None).to_numpy().view(np.int64)
    out = np.full(len(ns), np.nan)
    if len(ns) > 1:
        out[1:] = np.diff(ns) / 1e9
    return out


def low_pass_gravity(
    samples: np.ndarray,
    sample_seconds: np.ndarray,
    state: GravityState = GRAVITY_START,
) -> tuple[np.ndarray, GravityState]:
    """The low-pass over quiescent ``samples`` (n × 3, g), in time order.

    ``sample_seconds`` is each sample's interval (:func:`sample_intervals_seconds`
    over *every* IMU sample, quiescent or not, then the quiescent rows taken).
    ``state`` is the filter before the first sample (:data:`GRAVITY_START` for
    a session's first). Returns the estimate after each sample and the state
    after the last. A scalar loop on purpose: a caller that carries the state
    (``imu_stream``) reproduces the whole-session result bit for bit however
    the rows are cut.
    """
    values = np.asarray(samples, dtype=np.float64).tolist()
    intervals = np.asarray(sample_seconds, dtype=np.float64).tolist()
    out = np.empty((len(values), 3), dtype=np.float64)
    # Rows before the first usable sample have no estimate (NaN, which
    # ``estimate_gravity`` back-fills), never GRAVITY_START's zeros.
    started = state is not GRAVITY_START
    for i, (x, y, z) in enumerate(values):
        stepped = gravity_step(state, (x, y, z), gravity_low_pass_alpha(intervals[i]))
        started = started or stepped is not state
        state = stepped
        out[i] = state.estimate if started else (math.nan, math.nan, math.nan)
    return out, state


#: Body-frame accel axis used as the initial horizontal reference when
#: building the plane perpendicular to gravity (see ``_project_horizontal``).
_REFERENCE_AXIS = np.array([1.0, 0.0, 0.0])
_FALLBACK_AXIS = np.array([0.0, 1.0, 0.0])


@dataclass(frozen=True)
class FrameResolution:
    """Frame-resolved series plus the per-session parameters that produced them."""

    linear_accel_lon: pd.Series
    linear_accel_lat: pd.Series
    #: Radians; ``None`` when no straight-segment sample was ever available
    #: to resolve it (heading stays unresolved — see the doc's "Risks":
    #: cornering may bleed into hard_accel until a straight pull occurs).
    yaw_offset_rad: float | None
    #: ``True`` if the OBD-speed-derivative correlation found the axis
    #: pointing backward and flipped it. Surfaced for diagnostics/tests, not
    #: consumed downstream.
    sign_flipped: bool


def _rolling_std(series: pd.Series, window: int) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").rolling(window, min_periods=window).std()


def _circular_steady(heading_deg: pd.Series, window: int, std_max_deg: float) -> pd.Series:
    """Mardia circular standard deviation <= threshold, rolling over ``window``.

    A plain rolling std on raw degrees breaks at the 0/360 wrap; the
    resultant-length formula (``R = sqrt(mean(cos)^2 + mean(sin)^2)``,
    ``circular_std = sqrt(-2 ln R)``) doesn't need unwrapping and is the
    standard circular-statistics analog of variance.
    """
    radians = np.deg2rad(pd.to_numeric(heading_deg, errors="coerce"))
    cos_roll = np.cos(radians).rolling(window, min_periods=window).mean()
    sin_roll = np.sin(radians).rolling(window, min_periods=window).mean()
    resultant = np.sqrt(cos_roll**2 + sin_roll**2).clip(upper=1.0)
    with np.errstate(divide="ignore"):
        circ_std_rad = np.sqrt(np.clip(-2.0 * np.log(resultant.replace(0.0, np.nan)), 0.0, None))
    return pd.Series(circ_std_rad <= math.radians(std_max_deg), index=heading_deg.index)


def select_speed(
    frame: pd.DataFrame, *, bindings: Bindings | None = None
) -> tuple[pd.Series, str] | None:
    """The best speed column in ``frame`` (km/h) and its name, or ``None``.

    :data:`SPEED_SOURCES` in order; the first present with any finite value
    wins. A ``decoded:`` source is scaled to km/h (its rows are raw counts); one
    the catalog cannot scale is skipped, never read as counts.
    """
    for column in SPEED_SOURCES:
        if column not in frame.columns:
            continue
        try:
            speed = physical_series(frame[column], column, bindings)
        except UnscaledDecodedChannelError:
            continue
        if speed.notna().any():
            return speed, column
    return None


def _looking_ahead(steady: pd.Series, lookahead: int) -> pd.Series:
    """``steady`` computed over ``window + lookahead`` rows, re-indexed so row
    ``i`` covers ``[i − window + 1, i + lookahead]``."""
    if lookahead <= 0:
        return steady.fillna(False).astype(bool)
    return steady.shift(-lookahead).fillna(False).astype(bool)


def quiescent_mask(
    speed_kph: pd.Series,
    heading_deg: pd.Series | None,
    *,
    window: int,
    speed_std_max_kph: float = 1.5,
    heading_std_max_deg: float = 3.0,
    lookahead: int = 0,
) -> pd.Series:
    """Windows where the vehicle is neither accelerating nor turning.

    Gates gravity re-estimation (step 1): a low-pass computed over rows
    where the vehicle is actively maneuvering would blend real motion
    acceleration into the "gravity" estimate. Speed steadiness alone covers
    a session with no GPS heading (heading arg ``None``); heading steadiness
    additionally excludes a constant-speed turn (lateral accel, no speed
    change) once GPS course is available.

    ``lookahead`` rows extend each window past the row (see
    :data:`QUIESCENT_LOOKAHEAD_SECONDS`); ``0`` is a trailing window.
    """
    steady_speed = steady_speed_mask(
        speed_kph, window=window, speed_std_max_kph=speed_std_max_kph, lookahead=lookahead
    )
    if heading_deg is None or pd.to_numeric(heading_deg, errors="coerce").isna().all():
        return steady_speed
    return steady_speed & straight_mask(
        heading_deg, window=window, heading_std_max_deg=heading_std_max_deg, lookahead=lookahead
    )


def steady_speed_mask(
    speed_kph: pd.Series,
    *,
    window: int,
    speed_std_max_kph: float = 1.5,
    lookahead: int = 0,
) -> pd.Series:
    """:func:`quiescent_mask`'s speed half: rolling speed std over
    ``[i − window + 1, i + lookahead]`` at or under ``speed_std_max_kph``.

    Split out so the offline stream (``imu_stream``) evaluates the same
    windows part by part; row ``i`` depends only on the rows in its window.
    """
    span = window + max(lookahead, 0)
    return _looking_ahead(_rolling_std(speed_kph, span) <= speed_std_max_kph, lookahead)


def straight_mask(
    heading_deg: pd.Series,
    *,
    window: int,
    heading_std_max_deg: float = 3.0,
    lookahead: int = 0,
) -> pd.Series:
    """Windows where GPS course is steady — a straight pull, at any speed.

    Deliberately does not gate on speed steadiness (unlike
    :func:`quiescent_mask`): a straight-line accel/brake event is exactly
    the signal :func:`resolve_yaw_offset` needs, and it necessarily changes
    speed. ``lookahead`` as in :func:`quiescent_mask`.
    """
    span = window + max(lookahead, 0)
    return _looking_ahead(_circular_steady(heading_deg, span, heading_std_max_deg), lookahead)


def estimate_gravity(
    accel: pd.DataFrame, mask: pd.Series, timestamps: pd.Series | pd.DatetimeIndex
) -> pd.DataFrame:
    """Running low-pass of the 3-axis accel vector, held during non-quiescent rows.

    ``accel`` has columns ``x``/``y``/``z`` (g units, body frame), one row per
    IMU sample at ``timestamps`` (positionally). The estimate only advances on
    quiescent rows, each by :func:`gravity_low_pass_alpha` of its own sample
    interval (:data:`GRAVITY_TIME_CONSTANT_SECONDS`), then is reindexed back and
    forward/backward-filled so every row (quiescent or not) has a current
    gravity estimate to subtract. Rows before the first quiescent sample
    fall back to the nearest (first) available estimate (``bfill``) rather
    than an assumed-level mount, so a session that starts mid-maneuver still
    gets a real estimate as soon as one exists.

    ``timestamps`` is required: the filter's time constant is in seconds, so
    a row count alone cannot say how far a sample moves it (M01.13).
    """
    if len(timestamps) != len(accel):
        raise ValueError(f"{len(timestamps)} timestamps for {len(accel)} IMU rows")
    quiescent = mask.reindex(accel.index, fill_value=False).to_numpy(dtype=bool)
    if not quiescent.any():
        # No quiescent window at all (e.g. a session shorter than one
        # rolling-std window) — assume a level mount so callers still get a
        # (uncorrected) linear-accel estimate instead of raising.
        return pd.DataFrame({"x": 0.0, "y": 0.0, "z": 1.0}, index=accel.index)
    intervals = sample_intervals_seconds(timestamps)
    values = accel[["x", "y", "z"]].to_numpy(dtype=np.float64)
    smoothed = pd.DataFrame(
        low_pass_gravity(values[quiescent], intervals[quiescent])[0],
        index=accel.index[quiescent],
        columns=["x", "y", "z"],
    )
    return smoothed.reindex(accel.index).ffill().bfill()


def _project_horizontal(
    linear_accel: np.ndarray, gravity: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Project body-frame linear acceleration onto the plane perpendicular to gravity.

    Removes any residual out-of-horizontal-plane component left over after
    subtracting the (possibly tilted) gravity vector, so the two returned
    components (``h1``, ``h2``) are a genuinely horizontal, tilt-compensated
    basis — not just body X/Y, which would still carry a tilt-proportional
    error whenever the mount has pitch/roll. ``h1``/``h2`` are in an
    arbitrary body-fixed horizontal frame; :func:`resolve_yaw_offset` is
    what picks out which combination is longitudinal/lateral.
    """
    g_norm = np.linalg.norm(gravity, axis=1, keepdims=True)
    g_norm = np.where(g_norm == 0, 1.0, g_norm)
    g_hat = gravity / g_norm

    ref = np.tile(_REFERENCE_AXIS, (len(gravity), 1))
    # If gravity is nearly parallel to the reference axis (mount effectively
    # on its side), e1 would degenerate — fall back to the other axis for
    # those rows.
    alignment = np.abs(np.sum(ref * g_hat, axis=1))
    degenerate = alignment > 0.9
    if degenerate.any():
        ref = ref.copy()
        ref[degenerate] = _FALLBACK_AXIS

    e1 = ref - np.sum(ref * g_hat, axis=1, keepdims=True) * g_hat
    e1_norm = np.linalg.norm(e1, axis=1, keepdims=True)
    e1_norm = np.where(e1_norm == 0, 1.0, e1_norm)
    e1 = e1 / e1_norm
    e2 = np.cross(g_hat, e1)

    h1 = np.sum(linear_accel * e1, axis=1)
    h2 = np.sum(linear_accel * e2, axis=1)
    return h1, h2


def resolve_yaw_offset(
    h1: pd.Series,
    h2: pd.Series,
    mask: pd.Series,
    *,
    min_magnitude: float = 0.05,
) -> float | None:
    """Return the mount's yaw offset (radians) about vertical, or ``None`` if unresolved.

    Longitudinal/lateral is a *line* through the origin, not a signed
    direction (accelerating and braking point opposite ways along the same
    axis) — so this resolves the axis mod pi via the standard principal-axis
    (second-moment) formula, the same construction as a 2D inertia
    tensor/PCA eigenvector: ``theta = 0.5 * atan2(2 * sum(h1*h2), sum(h1^2 -
    h2^2))``. Only rows in ``mask`` (straight segments) with a large enough
    horizontal accel magnitude contribute, so near-zero noise doesn't
    dominate the fit.
    """
    h1 = pd.to_numeric(h1, errors="coerce")
    h2 = pd.to_numeric(h2, errors="coerce")
    magnitude_sq = h1**2 + h2**2
    usable = mask.reindex(h1.index, fill_value=False) & (magnitude_sq >= min_magnitude**2)
    usable &= h1.notna() & h2.notna()
    if not usable.any():
        return None
    x = h1[usable]
    y = h2[usable]
    s_xy = float((x * y).sum())
    s_xx_yy = float((x**2 - y**2).sum())
    if s_xy == 0.0 and s_xx_yy == 0.0:
        return None
    return 0.5 * math.atan2(2.0 * s_xy, s_xx_yy)


def _rotate(h1: pd.Series, h2: pd.Series, yaw_offset_rad: float) -> tuple[pd.Series, pd.Series]:
    cos_t = math.cos(yaw_offset_rad)
    sin_t = math.sin(yaw_offset_rad)
    lon = h1 * cos_t + h2 * sin_t
    lat = -h1 * sin_t + h2 * cos_t
    return lon, lat


def resolve_longitudinal_sign(
    lon: pd.Series,
    obd_speed_kph: pd.Series,
    timestamps: pd.Series,
) -> tuple[pd.Series, bool]:
    """Flip ``lon``'s global polarity to agree with the OBD speed derivative.

    :func:`resolve_yaw_offset` fixes the fore-aft *axis* but not which end is
    "forward" (mod pi). ``d(obd:SPEED)/dt`` is unambiguous ground truth —
    positive means speeding up, negative means slowing down — with no IMU
    involved, so correlating the two picks the correct global sign. A
    single per-session flip (not a per-row correction): the axis itself is
    fixed by the mount, so once resolved the polarity is fixed for the whole
    session too.

    Returns ``(possibly-flipped lon, sign_flipped)``. No evidence at all (see
    :func:`longitudinal_sign_votes`) keeps the sign; a caller that must know
    reads the votes itself.
    """
    agree, disagree = longitudinal_sign_votes(lon, obd_speed_kph, timestamps)
    if disagree > agree:
        return -lon, True
    return lon, False


def longitudinal_sign_votes(
    lon: pd.Series, speed_kph: pd.Series, timestamps: pd.Series
) -> tuple[int, int]:
    """``(agree, disagree)``: rows where ``lon`` and d(speed)/dt share / oppose a sign.

    Rows where either is zero or missing vote neither way, so ``(0, 0)`` means
    the session holds no evidence for the sign (no speed change, or no speed).
    """
    speed = pd.to_numeric(speed_kph, errors="coerce")
    dt = pd.to_datetime(timestamps, utc=True).diff().dt.total_seconds()
    speed_accel = speed.diff() / dt.replace(0.0, np.nan)
    both = pd.concat([pd.to_numeric(lon, errors="coerce"), speed_accel], axis=1).dropna()
    if both.empty:
        return 0, 0
    product = np.sign(both.iloc[:, 0].to_numpy()) * np.sign(both.iloc[:, 1].to_numpy())
    return int(np.count_nonzero(product > 0)), int(np.count_nonzero(product < 0))


# ---------------------------------------------------------------------------
# Specific force: gravity kept, one constant rotation per session (H02.4)
# ---------------------------------------------------------------------------


def horizontal_basis(up: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors spanning the plane perpendicular to ``up`` (body frame).

    The constant-reference counterpart of :func:`_project_horizontal`'s per-row
    basis, built the same way (body X projected, body Y where X is nearly
    vertical), so a yaw fitted in one basis means the same thing in the other.
    ``e2 = up × e1``.
    """
    up_hat = np.asarray(up, dtype=np.float64)
    up_hat = up_hat / np.linalg.norm(up_hat)
    ref = _REFERENCE_AXIS if abs(float(up_hat @ _REFERENCE_AXIS)) <= 0.9 else _FALLBACK_AXIS
    e1 = ref - float(ref @ up_hat) * up_hat
    e1 = e1 / np.linalg.norm(e1)
    e2 = np.cross(up_hat, e1)
    return e1, e2


def specific_force_rotation(
    gravity_body_g: np.ndarray, yaw_offset_rad: float, *, sign_flipped: bool
) -> np.ndarray:
    """Body → vehicle rotation, rows ``(LON, LAT, UP)`` in body coordinates.

    ``UP`` is the reference specific force's direction (an accelerometer at
    rest reads +1 g *up*); ``LON`` is the yaw-rotated horizontal axis, negated
    when the sign resolved backwards; ``LAT = UP × LON`` (ISO 8855: left). The
    rows are orthonormal and right-handed whatever the chip's axes.
    """
    up = np.asarray(gravity_body_g, dtype=np.float64)
    up = up / np.linalg.norm(up)
    e1, e2 = horizontal_basis(up)
    lon = math.cos(yaw_offset_rad) * e1 + math.sin(yaw_offset_rad) * e2
    if sign_flipped:
        lon = -lon
    lat = np.cross(up, lon)
    return np.vstack([lon, lat, up])


def apply_rotation(accel_body_g: np.ndarray, rotation: np.ndarray) -> np.ndarray:
    """``accel_body_g`` (n × 3, body frame) in the vehicle frame (n × 3: LON, LAT, UP)."""
    rotated: np.ndarray = np.asarray(accel_body_g, dtype=np.float64) @ np.asarray(rotation).T
    return rotated


@dataclass(frozen=True)
class SpecificForceResolution:
    """The specific-force frame of one session, or why a step did not resolve.

    ``rotation`` is ``None`` when the yaw or the sign did not resolve
    (``yaw_offset_rad is None`` / ``sign_votes == (0, 0)``); nothing is then
    rotated, because a guessed axis would put braking into ``SF_LAT``.
    """

    gravity_body_g: np.ndarray
    yaw_offset_rad: float | None
    sign_flipped: bool
    sign_votes: tuple[int, int]
    rotation: np.ndarray | None


def resolve_specific_force(
    accel_body_g: pd.DataFrame,
    quiescent: pd.Series,
    straight: pd.Series,
    speed_kph: pd.Series,
    timestamps: pd.Series,
    *,
    min_magnitude: float = 0.05,
) -> SpecificForceResolution:
    """One constant body → vehicle rotation for a session, gravity kept.

    ``accel_body_g`` has columns ``x``/``y``/``z`` (g). The reference "up" is
    the **mean** specific force over every ``quiescent`` row: a single
    orientation for the whole session, never the running low-pass
    :func:`estimate_gravity` uses, because re-estimating during a steady climb
    would take the grade out of ``SF_LON`` and make the residual pitch vary
    along the drive (the dyno assumes one constant offset per session). Yaw
    comes from straight rows' linear acceleration about that reference
    (:func:`resolve_yaw_offset`), the sign from the speed derivative.

    Whatever grade the vehicle sat on over the quiescent rows stays in the
    reference as a constant pitch offset: ``SF_LON = a·cos θr + sin(θ − θr)``
    for a mean reference pitch ``θr``. It is recorded, not removed — see
    ``src.preprocessing.imu_session``.
    """
    mask = quiescent.reindex(accel_body_g.index, fill_value=False).to_numpy(dtype=bool)
    values = accel_body_g[["x", "y", "z"]].to_numpy(dtype=np.float64)
    gravity = values[mask].mean(axis=0)
    e1, e2 = horizontal_basis(gravity)
    linear = values - gravity
    h1 = pd.Series(linear @ e1, index=accel_body_g.index)
    h2 = pd.Series(linear @ e2, index=accel_body_g.index)
    yaw = resolve_yaw_offset(h1, h2, straight, min_magnitude=min_magnitude)
    if yaw is None:
        return SpecificForceResolution(gravity, None, False, (0, 0), None)
    lon, _ = _rotate(h1, h2, yaw)
    agree, disagree = longitudinal_sign_votes(lon, speed_kph, timestamps)
    if agree == 0 and disagree == 0:
        return SpecificForceResolution(gravity, yaw, False, (0, 0), None)
    flipped = disagree > agree
    rotation = specific_force_rotation(gravity, yaw, sign_flipped=flipped)
    return SpecificForceResolution(gravity, yaw, flipped, (agree, disagree), rotation)


def resolve_frame(
    frame: pd.DataFrame,
    *,
    accel_x_column: str = "imu:ACCEL_X",
    accel_y_column: str = "imu:ACCEL_Y",
    accel_z_column: str = "imu:ACCEL_Z",
    speed_column: str | None = None,
    heading_column: str = HEADING_COLUMN,
    timestamp_column: str = "timestamp",
    quiescent_window: int = 20,
    quiescent_lookahead: int = 0,
    gravity_removed: bool = False,
    yaw_offset_rad: float | None = None,
    bindings: Bindings | None = None,
) -> FrameResolution:
    """Run the full gravity → heading → sign pipeline over one session's frame.

    ``gravity_removed=True`` is the BNO055 path: skips straight to step 2,
    reading ``accel_x_column``/``accel_y_column`` directly as the sensor's
    already-gravity-removed horizontal components (its ``z`` axis / gravity
    magnitude is irrelevant once fusion has run). ``yaw_offset_rad``
    overrides step 2 entirely (the persisted-per-session value, once
    computed — see :func:`read_yaw_offset`) so a session doesn't need a
    fresh straight segment before its later rows can be frame-resolved.

    ``speed_column`` ``None`` (the default) takes the best speed the frame has
    (:func:`select_speed`); a named column is read through the same scaling.
    ``quiescent_lookahead`` (rows) is the masks' look-ahead; a caller at IMU
    rate passes :data:`QUIESCENT_LOOKAHEAD_SECONDS` in rows.
    """
    timestamps = frame[timestamp_column]
    speed: pd.Series
    if speed_column is None:
        selected = select_speed(frame, bindings=bindings)
        speed = selected[0] if selected is not None else pd.Series(np.nan, index=frame.index)
    elif speed_column in frame.columns:
        speed = physical_series(frame[speed_column], speed_column, bindings)
    else:
        speed = pd.Series(np.nan, index=frame.index)
    heading = frame[heading_column] if heading_column in frame.columns else None

    if gravity_removed:
        h1 = pd.to_numeric(frame[accel_x_column], errors="coerce")
        h2 = pd.to_numeric(frame[accel_y_column], errors="coerce")
    else:
        accel = pd.DataFrame(
            {
                "x": pd.to_numeric(frame[accel_x_column], errors="coerce"),
                "y": pd.to_numeric(frame[accel_y_column], errors="coerce"),
                "z": pd.to_numeric(frame[accel_z_column], errors="coerce"),
            },
            index=frame.index,
        )
        quiescent = quiescent_mask(
            speed, heading, window=quiescent_window, lookahead=quiescent_lookahead
        )
        gravity = estimate_gravity(accel, quiescent, timestamps)
        linear_accel = accel.to_numpy() - gravity.to_numpy()
        h1_arr, h2_arr = _project_horizontal(linear_accel, gravity.to_numpy())
        h1 = pd.Series(h1_arr, index=frame.index)
        h2 = pd.Series(h2_arr, index=frame.index)

    offset = yaw_offset_rad
    if offset is None and heading is not None:
        straight = straight_mask(heading, window=quiescent_window, lookahead=quiescent_lookahead)
        offset = resolve_yaw_offset(h1, h2, straight)

    if offset is None:
        # Heading hasn't converged yet (no GPS, or no straight segment seen
        # so far) — the doc's documented risk. Report raw horizontal
        # components unresolved rather than guessing a rotation; callers
        # can retry once a straight segment/offset becomes available.
        return FrameResolution(
            linear_accel_lon=h1,
            linear_accel_lat=h2,
            yaw_offset_rad=None,
            sign_flipped=False,
        )

    lon, lat = _rotate(h1, h2, offset)
    lon, flipped = resolve_longitudinal_sign(lon, speed, timestamps)
    if flipped:
        # Keep LAT = up × LON (ISO 8855, left positive) through the flip.
        lat = -lat
    return FrameResolution(
        linear_accel_lon=lon,
        linear_accel_lat=lat,
        yaw_offset_rad=offset,
        sign_flipped=flipped,
    )


# ---------------------------------------------------------------------------
# Per-session yaw offset persistence
# ---------------------------------------------------------------------------


def write_yaw_offset(session_dir: Path, yaw_offset_rad: float) -> None:
    """Persist the resolved yaw offset so the rest of the session (and any
    later re-run) reuses it instead of re-resolving from scratch — the mount
    rotation is a property of the session's physical setup, not of any one
    tick. Atomic tmp+rename write, same convention as every other sidecar."""
    path = Path(session_dir) / YAW_OFFSET_FILENAME
    atomic_write_text(path, json.dumps({"yaw_offset_rad": yaw_offset_rad}, indent=2) + "\n")


def read_yaw_offset(session_dir: Path) -> float | None:
    """Read back a previously persisted yaw offset, or ``None`` if absent/unreadable."""
    data = read_json_object(Path(session_dir) / YAW_OFFSET_FILENAME)
    if data is None:
        return None
    value = data.get("yaw_offset_rad")
    return float(value) if isinstance(value, (int, float)) else None
