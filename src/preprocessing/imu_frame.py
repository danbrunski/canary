"""Gravity/frame resolution for raw IMU channels (Phase 2a.4).

Turns raw body-frame ``imu:ACCEL_X/Y/Z`` into frame-resolved
``imu:ACCEL_LON``/``imu:ACCEL_LAT`` (vehicle-longitudinal / vehicle-lateral,
in g), per ``docs/roadmap/imu-gps-integration.md`` ("Gravity / frame
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
   OBD speed derivative — ground truth we already have with no IMU
   involved — rather than trusting the IMU's own arbitrary axis labeling.

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

import numpy as np
import pandas as pd

from src.atomic_write import atomic_write_text

YAW_OFFSET_FILENAME = "imu_yaw_offset.json"

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


def quiescent_mask(
    speed_kph: pd.Series,
    heading_deg: pd.Series | None,
    *,
    window: int,
    speed_std_max_kph: float = 1.5,
    heading_std_max_deg: float = 3.0,
) -> pd.Series:
    """Windows where the vehicle is neither accelerating nor turning.

    Gates gravity re-estimation (step 1): a low-pass computed over rows
    where the vehicle is actively maneuvering would blend real motion
    acceleration into the "gravity" estimate. Speed steadiness alone covers
    a session with no GPS heading (heading arg ``None``); heading steadiness
    additionally excludes a constant-speed turn (lateral accel, no speed
    change) once GPS course is available.
    """
    steady_speed = _rolling_std(speed_kph, window) <= speed_std_max_kph
    steady_speed = steady_speed.fillna(False)
    if heading_deg is None or pd.to_numeric(heading_deg, errors="coerce").isna().all():
        return steady_speed
    return steady_speed & _circular_steady(heading_deg, window, heading_std_max_deg)


def straight_mask(
    heading_deg: pd.Series,
    *,
    window: int,
    heading_std_max_deg: float = 3.0,
) -> pd.Series:
    """Windows where GPS course is steady — a straight pull, at any speed.

    Deliberately does not gate on speed steadiness (unlike
    :func:`quiescent_mask`): a straight-line accel/brake event is exactly
    the signal :func:`resolve_yaw_offset` needs, and it necessarily changes
    speed.
    """
    return _circular_steady(heading_deg, window, heading_std_max_deg)


def estimate_gravity(accel: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    """Running low-pass of the 3-axis accel vector, held during non-quiescent rows.

    ``accel`` has columns ``x``/``y``/``z`` (g units, body frame). The
    estimate only advances on quiescent rows — computed here as an EWM over
    the quiescent subsequence in time order, then reindexed back and
    forward/backward-filled so every row (quiescent or not) has a current
    gravity estimate to subtract. Rows before the first quiescent sample
    fall back to the nearest (first) available estimate (``bfill``) rather
    than an assumed-level mount, so a session that starts mid-maneuver still
    gets a real estimate as soon as one exists.
    """
    quiescent_accel = accel.loc[mask.reindex(accel.index, fill_value=False)]
    if quiescent_accel.empty:
        # No quiescent window at all (e.g. a session shorter than one
        # rolling-std window) — assume a level mount so callers still get a
        # (uncorrected) linear-accel estimate instead of raising.
        return pd.DataFrame({"x": 0.0, "y": 0.0, "z": 1.0}, index=accel.index)
    smoothed = quiescent_accel.ewm(alpha=0.02, adjust=False).mean()
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

    Returns ``(possibly-flipped lon, sign_flipped)``.
    """
    speed = pd.to_numeric(obd_speed_kph, errors="coerce")
    dt = pd.to_datetime(timestamps, utc=True).diff().dt.total_seconds()
    speed_accel = speed.diff() / dt.replace(0.0, np.nan)
    both = pd.concat([lon, speed_accel], axis=1).dropna()
    if both.empty:
        return lon, False
    correlation = float((np.sign(both.iloc[:, 0]) * np.sign(both.iloc[:, 1])).sum())
    if correlation < 0:
        return -lon, True
    return lon, False


def resolve_frame(
    frame: pd.DataFrame,
    *,
    accel_x_column: str = "imu:ACCEL_X",
    accel_y_column: str = "imu:ACCEL_Y",
    accel_z_column: str = "imu:ACCEL_Z",
    speed_column: str = "obd:SPEED",
    heading_column: str = "gps:heading",
    timestamp_column: str = "timestamp",
    quiescent_window: int = 20,
    gravity_removed: bool = False,
    yaw_offset_rad: float | None = None,
) -> FrameResolution:
    """Run the full gravity → heading → sign pipeline over one session's frame.

    ``gravity_removed=True`` is the BNO055 path: skips straight to step 2,
    reading ``accel_x_column``/``accel_y_column`` directly as the sensor's
    already-gravity-removed horizontal components (its ``z`` axis / gravity
    magnitude is irrelevant once fusion has run). ``yaw_offset_rad``
    overrides step 2 entirely (the persisted-per-session value, once
    computed — see :func:`read_yaw_offset`) so a session doesn't need a
    fresh straight segment before its later rows can be frame-resolved.
    """
    timestamps = frame[timestamp_column]
    speed = (
        frame[speed_column]
        if speed_column in frame.columns
        else pd.Series(np.nan, index=frame.index)
    )
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
        quiescent = quiescent_mask(speed, heading, window=quiescent_window)
        gravity = estimate_gravity(accel, quiescent)
        linear_accel = accel.to_numpy() - gravity.to_numpy()
        h1_arr, h2_arr = _project_horizontal(linear_accel, gravity.to_numpy())
        h1 = pd.Series(h1_arr, index=frame.index)
        h2 = pd.Series(h2_arr, index=frame.index)

    offset = yaw_offset_rad
    if offset is None and heading is not None:
        straight = straight_mask(heading, window=quiescent_window)
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
    path = Path(session_dir) / YAW_OFFSET_FILENAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("yaw_offset_rad")
    return float(value) if isinstance(value, (int, float)) else None
