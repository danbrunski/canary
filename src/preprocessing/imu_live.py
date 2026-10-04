"""Live IMU frame resolution and per-tick decimation (H02.4).

The streaming counterpart of :mod:`src.preprocessing.imu_session`'s linear
family, feeding :class:`~src.features.live.LiveFeatureSource`: raw
``imu:ACCEL_X/Y/Z`` samples in, and once per tick the same aggregates
``imu_decimation.decimate_imu_wide`` computes offline —
``imu:ACCEL_LON_MAX`` / ``imu:ACCEL_LON_RMS`` / ``imu:ACCEL_LAT_MAX`` /
``imu:JERK_MAX`` — so :class:`~src.daemon.autotag_hook.LiveAutoTagHook` and
the segmenter's accel modes have something to read.

The same three steps as :func:`~src.preprocessing.imu_frame.resolve_frame`,
incrementally:

1. **Gravity**: an exponential low-pass of the accel vector (the offline
   ``alpha``, per sample), advanced only by quiescent samples — steady speed
   over :attr:`LiveImuFrame.quiescent_seconds` and, when GPS course is fresh,
   a steady course. Speed is the best fresh source
   (``imu_frame.SPEED_SOURCES``: ``0x2D1`` broadcast, Mode 01, GPS), each
   judged on its own history, never mixed.
2. **Yaw**: the offline principal-axis fit as running sums over straight
   samples (steady course, ≥ 0.05 g horizontal).

Both updates are decided ``imu_frame.QUIESCENT_LOOKAHEAD_SECONDS`` late,
once the inputs have a reading from after the sample: the speed at an IMU
instant is the newest earlier reading, so a trailing judgement would count
the first second of every pull from rest as quiescent (the offline masks'
look-ahead, done causally). Outputs are not delayed: each sample is split
with the estimates as they stand.
3. **Sign** is not needed: every live output is a magnitude, and the tagger
   takes direction from the speed derivative.

Until gravity has a quiescent sample and the yaw a straight manoeuvre there
is nothing to split into LON/LAT, so no aggregate is emitted and
:attr:`LiveImuFrame.status` names which step is waiting. Live values are not
bit-identical to the offline frame (that one fits yaw over the whole
session). ``imu:*`` never trains or scores a detector (H01.1), but the live
``imu:ACCEL_*_MAX`` channels do reach the segmenter's accel-gated modes
(``launch``, ``hard_brake``, ``cornering`` where configured). Offline, the
``value_number`` chain rebuilds the same channels from the session's resolved
``imu_frame.json`` (``imu_stream``); a session never resolved has none there,
and says so. Where the two still differ is measured in
``tests/test_imu_stream.py`` (live emits nothing until its first straight
manoeuvre is decided).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from src.decode.bindings import Bindings
from src.decode.engineering import UnscaledDecodedChannelError, physical_value
from src.preprocessing.imu_decimation import (
    ACCEL_LAT_MAX_COLUMN,
    ACCEL_LON_MAX_COLUMN,
    ACCEL_LON_RMS_COLUMN,
    JERK_MAX_COLUMN,
)
from src.preprocessing.imu_frame import (
    GRAVITY_LOW_PASS_ALPHA,
    HEADING_COLUMN,
    QUIESCENT_LOOKAHEAD_SECONDS,
    SPEED_SOURCES,
)

#: Raw axis channel names as the collector records them (``imu:<name>``).
_AXES = {"ACCEL_X": 0, "ACCEL_Y": 1, "ACCEL_Z": 2}

#: Channels :class:`LiveImuFrame` steers by; ``LiveFeatureSource`` forwards
#: each accepted value of these.
IMU_FRAME_INPUTS: frozenset[str] = frozenset({*SPEED_SOURCES, HEADING_COLUMN})

#: Offline ``estimate_gravity``'s smoothing factor, per sample.
GRAVITY_ALPHA = GRAVITY_LOW_PASS_ALPHA
#: Extra history a steadiness judgement spans beyond ``quiescent_seconds +
#: lookahead``: a sample is decided at the first reading at least the
#: look-ahead after it, up to one reading period (GPS 1 s, Mode 01 1.43 s)
#: later still, and the window must still reach back over the sample's own.
_DECISION_SLACK_S = 1.5


@dataclass
class _Steadiness:
    """One input's recent history and whether it held steady over the window.

    Running sums, O(1) per reading: ``0x2D1`` speed arrives at ~100 Hz on the
    daemon's event loop. Linear spread is the sample standard deviation (the
    offline ``rolling().std()``); circular spread is Mardia's
    ``sqrt(−2 ln R)`` over the window (``imu_frame._circular_steady``).
    """

    window_s: float
    circular: bool
    threshold: float
    history: deque[tuple[float, float, float]] = field(default_factory=deque)
    steady: bool = False
    _s1: float = 0.0
    _s2: float = 0.0

    def _terms(self, value: float) -> tuple[float, float]:
        if self.circular:
            radians = math.radians(value)
            return math.cos(radians), math.sin(radians)
        return value, value * value

    def add(self, t: float, value: float) -> None:
        a, b = self._terms(value)
        self.history.append((t, a, b))
        self._s1 += a
        self._s2 += b
        while self.history and self.history[0][0] < t - self.window_s:
            _, old_a, old_b = self.history.popleft()
            self._s1 -= old_a
            self._s2 -= old_b
        n = len(self.history)
        if n == 1:  # re-anchor the sums so float drift cannot accumulate
            self._s1, self._s2 = a, b
        if n < 2 or t - self.history[0][0] < 0.5 * self.window_s:
            self.steady = False
            return
        if self.circular:
            resultant = min(1.0, math.hypot(self._s1 / n, self._s2 / n))
            spread = (
                math.degrees(math.sqrt(max(0.0, -2.0 * math.log(resultant))))
                if resultant > 0.0
                else math.inf
            )
        else:
            spread = math.sqrt(max(0.0, (self._s2 - self._s1 * self._s1 / n) / (n - 1)))
        self.steady = spread <= self.threshold

    def newest(self) -> float | None:
        return self.history[-1][0] if self.history else None


@dataclass
class LiveImuFrame:
    """Streaming gravity + yaw resolution and per-tick |LON|/|LAT| aggregates.

    Feed raw axes with :meth:`observe_imu`, speed/heading with
    :meth:`observe_input`, and read one tick's aggregates with
    :meth:`take_tick`. :meth:`reset` is a session boundary.
    """

    bindings: Bindings | None = None
    quiescent_seconds: float = 2.0
    speed_std_max_kph: float = 1.5
    heading_std_max_deg: float = 3.0
    #: An input older than this at an IMU sample is not steering it.
    input_max_age_s: float = 3.0
    min_yaw_magnitude_g: float = 0.05
    lookahead_s: float = QUIESCENT_LOOKAHEAD_SECONDS

    _speeds: dict[str, _Steadiness] = field(init=False, default_factory=dict)
    _heading: _Steadiness = field(init=False)
    _pending_t: float | None = field(init=False, default=None)
    _pending: list[float | None] = field(init=False, default_factory=lambda: [None] * 3)
    #: Samples awaiting their quiescent / straight decision.
    _undecided: deque[tuple[float, tuple[float, float, float]]] = field(
        init=False, default_factory=deque
    )
    _gravity: list[float] | None = field(init=False, default=None)
    _s_xy: float = field(init=False, default=0.0)
    _s_xx_yy: float = field(init=False, default=0.0)
    _samples: int = field(init=False, default=0)
    # Per-tick accumulators.
    _lon_max: float | None = field(init=False, default=None)
    _lon_sq: float = field(init=False, default=0.0)
    _lat_max: float | None = field(init=False, default=None)
    _jerk_max: float | None = field(init=False, default=None)
    _tick_n: int = field(init=False, default=0)
    _last_lon: tuple[float, float] | None = field(init=False, default=None)
    _last_resolved_at: datetime | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Forget everything: gravity, yaw, inputs, the open tick."""
        span = self.quiescent_seconds + self.lookahead_s + _DECISION_SLACK_S
        self._speeds = {
            name: _Steadiness(span, False, self.speed_std_max_kph) for name in SPEED_SOURCES
        }
        self._heading = _Steadiness(span, True, self.heading_std_max_deg)
        self._pending_t = None
        self._pending = [None, None, None]
        self._undecided = deque()
        self._gravity = None
        self._s_xy = 0.0
        self._s_xx_yy = 0.0
        self._samples = 0
        self._clear_tick()

    def _clear_tick(self) -> None:
        self._lon_max = None
        self._lon_sq = 0.0
        self._lat_max = None
        self._jerk_max = None
        self._tick_n = 0
        self._last_lon = None
        self._last_resolved_at = None

    # -- inputs --------------------------------------------------------------

    def observe_input(self, channel: str, value: float, observed_at: datetime) -> None:
        """A speed (``SPEED_SOURCES``) or ``gps:heading`` value. ``decoded:``
        speed arrives as raw counts and is scaled here; unscalable, it is
        ignored (that source then never steers)."""
        t = observed_at.timestamp()
        if channel == HEADING_COLUMN:
            self._heading.add(t, value)
            return
        steadiness = self._speeds.get(channel)
        if steadiness is None:
            return
        try:
            kph = physical_value(value, channel, self.bindings)
        except UnscaledDecodedChannelError:
            return
        if kph is not None and math.isfinite(kph):
            steadiness.add(t, kph)

    def _fresh(self, steadiness: _Steadiness, t: float) -> bool:
        newest = steadiness.newest()
        return newest is not None and t - newest <= self.input_max_age_s

    def _seen_past(self, steadiness: _Steadiness, t: float) -> bool:
        newest = steadiness.newest()
        return newest is not None and newest >= t + self.lookahead_s

    def _decide(self, now: float) -> None:
        """Apply gravity / yaw updates for every sample whose inputs now reach
        past it, oldest first. A sample whose inputs went silent is decided
        (as neither) once ``input_max_age_s`` past its look-ahead."""
        while self._undecided:
            t, a = self._undecided[0]
            timed_out = now >= t + self.lookahead_s + self.input_max_age_s
            speed = next((s for s in self._speeds.values() if self._fresh(s, t)), None)
            heading_fresh = self._fresh(self._heading, t)
            speed_ready = speed is not None and self._seen_past(speed, t)
            heading_ready = not heading_fresh or self._seen_past(self._heading, t)
            if not timed_out and not (speed_ready and heading_ready):
                return
            self._undecided.popleft()
            heading_steady = heading_fresh and heading_ready and self._heading.steady
            quiescent = (
                speed is not None
                and speed_ready
                and speed.steady
                and (heading_steady or not heading_fresh)
            )
            if quiescent:
                if self._gravity is None:
                    self._gravity = list(a)
                else:
                    g = self._gravity
                    for i in range(3):
                        g[i] = GRAVITY_ALPHA * a[i] + (1.0 - GRAVITY_ALPHA) * g[i]
            if heading_steady and self._gravity is not None:
                h1, h2 = _horizontal(a, self._gravity)
                if h1 * h1 + h2 * h2 >= self.min_yaw_magnitude_g**2:
                    self._s_xy += h1 * h2
                    self._s_xx_yy += h1 * h1 - h2 * h2

    # -- samples -------------------------------------------------------------

    def observe_imu(self, channel: str, value: float, observed_at: datetime) -> None:
        """One raw axis value; a sample is processed once X, Y and Z of the
        same instant have all arrived (the collector emits them together)."""
        axis = _AXES.get(channel)
        if axis is None:
            return
        t = observed_at.timestamp()
        if self._pending_t != t:
            self._pending_t = t
            self._pending = [None, None, None]
        self._pending[axis] = value
        x, y, z = self._pending
        if x is None or y is None or z is None:
            return
        self._pending = [None, None, None]
        self._sample(t, (x, y, z), observed_at)

    def _sample(self, t: float, a: tuple[float, float, float], observed_at: datetime) -> None:
        self._samples += 1
        self._undecided.append((t, a))
        self._decide(t)
        if self._gravity is None:
            return
        if self._s_xy == 0.0 and self._s_xx_yy == 0.0:
            return
        h1, h2 = _horizontal(a, self._gravity)
        yaw = 0.5 * math.atan2(2.0 * self._s_xy, self._s_xx_yy)
        lon = h1 * math.cos(yaw) + h2 * math.sin(yaw)
        lat = -h1 * math.sin(yaw) + h2 * math.cos(yaw)
        self._accumulate(t, lon, lat, observed_at)

    def _accumulate(self, t: float, lon: float, lat: float, observed_at: datetime) -> None:
        self._lon_max = abs(lon) if self._lon_max is None else max(self._lon_max, abs(lon))
        self._lat_max = abs(lat) if self._lat_max is None else max(self._lat_max, abs(lat))
        self._lon_sq += lon * lon
        self._tick_n += 1
        if self._last_lon is not None and t > self._last_lon[0]:
            jerk = abs((lon - self._last_lon[1]) / (t - self._last_lon[0]))
            self._jerk_max = jerk if self._jerk_max is None else max(self._jerk_max, jerk)
        self._last_lon = (t, lon)
        self._last_resolved_at = observed_at

    # -- outputs -------------------------------------------------------------

    def take_tick(self) -> tuple[dict[str, float], datetime | None]:
        """This tick's aggregates (empty when no sample resolved) and the
        instant of the newest sample in them; opens the next tick. Jerk is
        between consecutive samples inside the tick, as offline."""
        if self._tick_n == 0 or self._lon_max is None or self._lat_max is None:
            self._clear_tick()
            return {}, None
        values = {
            ACCEL_LON_MAX_COLUMN: self._lon_max,
            ACCEL_LON_RMS_COLUMN: math.sqrt(self._lon_sq / self._tick_n),
            ACCEL_LAT_MAX_COLUMN: self._lat_max,
        }
        if self._jerk_max is not None:
            values[JERK_MAX_COLUMN] = self._jerk_max
        observed_at = self._last_resolved_at
        self._clear_tick()
        return values, observed_at

    @property
    def status(self) -> str:
        """Which step the frame is at: ``no_imu_samples``,
        ``no_quiescent_period`` (no gravity reference yet), ``yaw_unresolved``
        (no straight manoeuvre yet) or ``resolving``."""
        if self._samples == 0:
            return "no_imu_samples"
        if self._gravity is None:
            return "no_quiescent_period"
        if self._s_xy == 0.0 and self._s_xx_yy == 0.0:
            return "yaw_unresolved"
        return "resolving"


def _horizontal(a: tuple[float, float, float], g: list[float]) -> tuple[float, float]:
    """``a − g`` projected onto the plane ⊥ ``g``, in the basis
    ``imu_frame._project_horizontal`` uses (body X projected; body Y when X is
    nearly vertical; ``e2 = ĝ × e1``)."""
    norm = math.sqrt(g[0] ** 2 + g[1] ** 2 + g[2] ** 2) or 1.0
    gh = (g[0] / norm, g[1] / norm, g[2] / norm)
    ref = (1.0, 0.0, 0.0) if abs(gh[0]) <= 0.9 else (0.0, 1.0, 0.0)
    dot = ref[0] * gh[0] + ref[1] * gh[1] + ref[2] * gh[2]
    e1 = [ref[i] - dot * gh[i] for i in range(3)]
    e1_norm = math.sqrt(sum(v * v for v in e1)) or 1.0
    e1 = [v / e1_norm for v in e1]
    e2 = (
        gh[1] * e1[2] - gh[2] * e1[1],
        gh[2] * e1[0] - gh[0] * e1[2],
        gh[0] * e1[1] - gh[1] * e1[0],
    )
    lin = (a[0] - g[0], a[1] - g[1], a[2] - g[2])
    h1 = lin[0] * e1[0] + lin[1] * e1[1] + lin[2] * e1[2]
    h2 = lin[0] * e2[0] + lin[1] * e2[1] + lin[2] * e2[2]
    return h1, h2


__all__ = ["GRAVITY_ALPHA", "IMU_FRAME_INPUTS", "LiveImuFrame"]
