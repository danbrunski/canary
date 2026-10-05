"""Live IMU frame resolution and per-tick decimation (H02.4).

The streaming counterpart of :mod:`src.preprocessing.imu_session`'s linear
family, feeding :class:`~src.features.live.LiveFeatureSource`: raw
``imu:ACCEL_X/Y/Z`` samples in, and once per tick the same aggregates
``imu_decimation.decimate_imu_wide`` computes offline
(``imu_decimation.IMU_TICK_COLUMNS``) — ``imu:ACCEL_LON_MAX`` /
``imu:ACCEL_LON_RMS`` / ``imu:ACCEL_LAT_MAX`` / ``imu:JERK_MAX`` — so
:class:`~src.daemon.autotag_hook.LiveAutoTagHook` and the segmenter's accel
modes have something to read, and the one signed aggregate,
``imu:ACCEL_LON_MEAN``, the longitudinal-acceleration committee's IMU member
(H04.4).

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
3. **Sign** (H04.4), for ``imu:ACCEL_LON_MEAN`` only — the magnitudes need
   none, and the tagger takes direction from the speed derivative. The
   offline rule (``imu_frame.longitudinal_sign_votes``) votes at the first
   IMU sample after each new speed reading, by the sign of the change. Live
   keeps those votes as a *vector* in the horizontal plane,
   ``F = Σ sign(Δv)·h̄``, with ``h̄`` the mean horizontal acceleration
   over the same interval as ``Δv`` (between the two speed readings), so the
   evidence survives the running yaw fit
   flipping by π (the fit is an axis, mod π), and the sign is ``sign(u·F)``
   for the current axis ``u`` — **once the evidence clears a test**, and
   not before (``sign_unresolved``):

   * a vote counts only when the speed change clears the source's
     quantisation (``|Δv| ≥ 2`` steps: :data:`SPEED_QUANTUM_KPH`, or the
     binding's scale for a ``decoded:`` source), its rate clears
     :attr:`LiveImuFrame.min_vote_accel_g`, and ``|h̄|`` clears
     ``min_yaw_magnitude_g``: a 1 km/h-quantised PID at cruise steps by one
     count on jitter alone;
   * the evidence must clear a test that stays valid while it is checked
     after every vote, all drive long. Under the null (each vote's sign a
     fair coin, whatever ``h̄``) ``F`` is a 2-D martingale whose increments
     ``±h̄`` are sub-Gaussian with variance proxy ``h̄h̄ᵀ``, so the
     normal-mixture supermartingale (λ ~ N(0, I/ρ))
     ``sqrt(det ρI / det(S + ρI)) · exp(½ Fᵀ(S + ρI)⁻¹F)``, ``S = Σ h̄h̄ᵀ``,
     starts at 1 and, by Ville's inequality, ever reaches ``1/α`` with
     probability at most α (:func:`sign_log_evidence`). The sign resolves
     when it does, **α = :data:`SIGN_ALPHA` = 1e-3 per drive** (per
     ``reset``) for pure-noise votes resolving at all, either way. It is
     yaw-free (2-D), so the running yaw moving does not void it; the sign is
     then ``sign(u·F)``, held back while ``F`` lies more than 60° off the
     current axis (the yaw and the votes disagree about where forward is).
     ``tests/test_imu_lon_mean.py`` measures the rate over 10,000 simulated
     drives of random votes. The verifier's repro (2026-10-05): with the
     first vote deciding, 18 of 40 seeds of cruise jitter (0.05 g IMU noise,
     1 km/h PID at 50 km/h) signed the following 0.15 g pull backwards; an
     uncorrected 3σ look after every vote was the next attempt, at a few
     percent per drive.

   **Live/offline divergence, by design:** offline votes every row and
   takes the session's majority, with no quantisation, rate or margin test
   against the instantaneous sample (it sees the whole drive, so junk votes
   are outvoted); live averages ``h`` over the speed interval, gates each
   vote, weighs it by ``h̄`` and waits for the boundary (about 18 clean
   0.15 g votes from nothing), so it reports
   ``sign_unresolved`` — and no ``imu:ACCEL_LON_MEAN`` — for longer, often
   until a few pulls or stops have voted.

Until gravity has a quiescent sample and the yaw a straight manoeuvre there
is nothing to split into LON/LAT, so no aggregate is emitted; until the sign
has evidence the magnitudes are emitted but ``imu:ACCEL_LON_MEAN`` is not —
absent, never 0. :attr:`LiveImuFrame.status` names which step is waiting. Live values are not
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

import numpy as np

from src.decode.bindings import Bindings
from src.decode.engineering import UnscaledDecodedChannelError, physical_value
from src.preprocessing.imu_decimation import (
    ACCEL_LAT_MAX_COLUMN,
    ACCEL_LON_MAX_COLUMN,
    ACCEL_LON_MEAN_COLUMN,
    ACCEL_LON_RMS_COLUMN,
    JERK_MAX_COLUMN,
)
from src.preprocessing.imu_frame import (
    GRAVITY_LOW_PASS_ALPHA,
    HEADING_COLUMN,
    QUIESCENT_LOOKAHEAD_SECONDS,
    SPEED_SOURCES,
)
from src.signals.committees import STANDARD_GRAVITY

#: Raw axis channel names as the collector records them (``imu:<name>``).
_AXES = {"ACCEL_X": 0, "ACCEL_Y": 1, "ACCEL_Z": 2}

#: Channels :class:`LiveImuFrame` steers by; ``LiveFeatureSource`` forwards
#: each accepted value of these.
IMU_FRAME_INPUTS: frozenset[str] = frozenset({*SPEED_SOURCES, HEADING_COLUMN})

#: Offline ``estimate_gravity``'s smoothing factor, per sample.
GRAVITY_ALPHA = GRAVITY_LOW_PASS_ALPHA
#: Resolution of each non-``decoded:`` speed source, km/h. ``obd:SPEED`` is
#: J1979 PID 0x0D, one byte of whole km/h. GPS speed over ground is not
#: quantised (its jitter is what ``min_vote_accel_g`` screens). A
#: ``decoded:`` source's step is its binding's scale.
SPEED_QUANTUM_KPH: dict[str, float] = {"obd:SPEED": 1.0, "gps:speed": 0.0}

#: Per-drive probability that pure-noise sign votes ever resolve the sign
#: (Ville's inequality on :func:`sign_log_evidence`).
SIGN_ALPHA = 1e-3
#: The normal mixture's scale ρ, g²: the test is most sensitive once the
#: votes' summed ``h̄²`` is of this order (one vote of ~0.2 g).
SIGN_RHO_G2 = 0.05
#: ``|u·F| / |F|`` below this (F more than 60° off the yaw axis): no sign.
SIGN_MIN_ALIGNMENT = 0.5


def sign_log_evidence(
    f1: float | np.ndarray,
    f2: float | np.ndarray,
    sxx: float | np.ndarray,
    sxy: float | np.ndarray,
    syy: float | np.ndarray,
    rho: float,
) -> np.ndarray:
    """Log of the normal-mixture supermartingale for the sign votes.

    ``F = (f1, f2) = Σ sign·h̄`` and ``S = [[sxx, sxy], [sxy, syy]] = Σ h̄h̄ᵀ``.
    With every vote's sign a fair coin, ``exp`` of this is a non-negative
    supermartingale starting at 1 (sub-Gaussian increments; mixture over
    λ ~ N(0, I/ρ)), so it ever reaches ``ln(1/α)`` with probability at most
    α however often it is checked. Takes floats or numpy arrays (the test
    simulates many drives at once through this same function).
    """
    a11, a22 = sxx + rho, syy + rho
    det = a11 * a22 - sxy * sxy
    quad = (a22 * f1 * f1 - 2.0 * sxy * f1 * f2 + a11 * f2 * f2) / det
    return np.asarray(0.5 * quad + 0.5 * np.log(rho * rho / det), dtype=float)


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
    #: A sign vote's speed change must be at least this fast, g.
    min_vote_accel_g: float = 0.05
    #: Votes needed before the sign is trusted, and the margin (in standard
    #: deviations of ``u·F`` under random votes) it must clear.
    #: The sign test's error rate per drive and its mixture scale ρ (g²,
    #: about one 0.2 g vote): see :func:`sign_log_evidence`.
    sign_alpha: float = SIGN_ALPHA
    sign_rho_g2: float = SIGN_RHO_G2
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
    #: Sign evidence, ``Σ sign(Δv)·h`` in the horizontal basis (module doc).
    _forward: list[float] = field(init=False, default_factory=lambda: [0.0, 0.0])
    #: ``S = Σ h hᵀ`` over the votes (xx, xy, yy) and their count.
    _vote_s: list[float] = field(init=False, default_factory=lambda: [0.0, 0.0, 0.0])
    _quantum: dict[str, float] = field(init=False, default_factory=dict)
    #: ``(t, Σh1, Σh2, n)`` per sample since gravity, the last few seconds:
    #: a vote's mean ``h`` over the interval between two speed readings.
    _h_cum: deque[tuple[float, float, float, int]] = field(init=False, default_factory=deque)
    _last_sample_at: datetime | None = field(init=False, default=None)
    #: Per speed source, the instant of the newest reading that has had its
    #: one chance to vote.
    _voted_reading_t: dict[str, float] = field(init=False, default_factory=dict)
    _samples: int = field(init=False, default=0)
    # Per-tick accumulators.
    _lon_max: float | None = field(init=False, default=None)
    _lon_sq: float = field(init=False, default=0.0)
    _lon_signed_sum: float = field(init=False, default=0.0)
    _lon_signed_n: int = field(init=False, default=0)
    _lat_max: float | None = field(init=False, default=None)
    _jerk_max: float | None = field(init=False, default=None)
    _tick_n: int = field(init=False, default=0)
    _last_lon: tuple[float, float] | None = field(init=False, default=None)
    _last_resolved_at: datetime | None = field(init=False, default=None)

    #: Seconds of ``Σh`` history kept: a speed reading interval is at most
    #: ``input_max_age_s`` (else the source is not fresh), plus slack.
    _H_HISTORY_S = 6.0

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
        self._forward = [0.0, 0.0]
        self._vote_s = [0.0, 0.0, 0.0]
        self._h_cum = deque()
        self._voted_reading_t = {}
        self._last_sample_at = None
        self._samples = 0
        self._clear_tick()

    def _clear_tick(self) -> None:
        self._lon_max = None
        self._lon_sq = 0.0
        self._lon_signed_sum = 0.0
        self._lon_signed_n = 0
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
        self._last_sample_at = observed_at
        self._undecided.append((t, a))
        self._decide(t)
        if self._gravity is None:
            return
        h1, h2 = _horizontal(a, self._gravity)
        # Sign evidence is yaw-free (a vector), so it gathers before the yaw.
        self._h_cum.append(
            (t, self._h_cum[-1][1] + h1, self._h_cum[-1][2] + h2, self._h_cum[-1][3] + 1)
            if self._h_cum
            else (t, h1, h2, 1)
        )
        while self._h_cum and self._h_cum[0][0] < t - self._H_HISTORY_S:
            self._h_cum.popleft()
        self._vote_sign(t)
        if self._s_xy == 0.0 and self._s_xx_yy == 0.0:
            return
        yaw = 0.5 * math.atan2(2.0 * self._s_xy, self._s_xx_yy)
        cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
        lon = h1 * cos_yaw + h2 * sin_yaw
        lat = -h1 * sin_yaw + h2 * cos_yaw
        sign = self._sign(cos_yaw, sin_yaw)
        signed_lon = None if sign is None else sign * lon
        self._accumulate(t, lon, lat, observed_at, signed_lon)

    def _cumulative_h(self, t: float) -> tuple[float, float, int] | None:
        """Running ``Σh`` and sample count up to and including ``t``;
        ``None`` when ``t`` precedes the kept history."""
        for entry in reversed(self._h_cum):
            if entry[0] <= t:
                return entry[1], entry[2], entry[3]
        return None

    def _vote_sign(self, t: float) -> None:
        """Cast a sign vote at the first sample after a new reading of the
        best fresh speed source (the offline rule's row): the speed change
        since the previous reading against the mean ``h`` over the same
        interval, so both sides describe the same stretch of road."""
        speed_name, speed = next(
            ((name, s) for name, s in self._speeds.items() if self._fresh(s, t)),
            (None, None),
        )
        if speed_name is None or speed is None:
            return
        # The newest reading at or before t and the one before it: readings
        # from after t can already be in (the IMU FIFO drains late).
        history = speed.history
        k = len(history) - 1
        while k >= 0 and history[k][0] > t:
            k -= 1
        if k < 1 or self._voted_reading_t.get(speed_name) == history[k][0]:
            return
        self._voted_reading_t[speed_name] = history[k][0]
        delta = history[k][1] - history[k - 1][1]
        elapsed = history[k][0] - history[k - 1][0]
        start = self._cumulative_h(history[k - 1][0])
        end = self._cumulative_h(history[k][0])
        if elapsed <= 0.0 or start is None or end is None or end[2] <= start[2]:
            return  # no IMU samples (with gravity known) cover the interval
        n = end[2] - start[2]
        h1, h2 = (end[0] - start[0]) / n, (end[1] - start[1]) / n
        if h1 * h1 + h2 * h2 < self.min_yaw_magnitude_g**2:
            return
        if abs(delta) < 2.0 * self._speed_quantum(speed_name) or delta == 0.0:
            return  # one quantisation step is jitter, not a speed change
        if abs(delta) / 3.6 / elapsed / STANDARD_GRAVITY < self.min_vote_accel_g:
            return
        sign = 1.0 if delta > 0.0 else -1.0
        self._forward[0] += sign * h1
        self._forward[1] += sign * h2
        self._vote_s[0] += h1 * h1
        self._vote_s[1] += h1 * h2
        self._vote_s[2] += h2 * h2

    def _speed_quantum(self, channel: str) -> float:
        """One count of ``channel`` in km/h (cached)."""
        quantum = self._quantum.get(channel)
        if quantum is None:
            quantum = SPEED_QUANTUM_KPH.get(channel, 0.0)
            if channel not in SPEED_QUANTUM_KPH:
                try:
                    one = physical_value(1.0, channel, self.bindings)
                    two = physical_value(2.0, channel, self.bindings)
                    if one is not None and two is not None:
                        quantum = abs(two - one)
                except (UnscaledDecodedChannelError, KeyError, ValueError):
                    quantum = 0.0
            self._quantum[channel] = quantum
        return quantum

    def _sign(self, cos_yaw: float, sin_yaw: float) -> float | None:
        """``±1`` for the axis ``(cos_yaw, sin_yaw)`` once the votes clear
        the sequential test and ``F`` lies within 60° of the axis (module
        doc); ``None`` before."""
        f1, f2 = self._forward
        sxx, sxy, syy = self._vote_s
        evidence = float(sign_log_evidence(f1, f2, sxx, sxy, syy, self.sign_rho_g2))
        if evidence < -math.log(self.sign_alpha):
            return None
        along = cos_yaw * f1 + sin_yaw * f2
        if abs(along) < SIGN_MIN_ALIGNMENT * math.hypot(f1, f2):
            return None
        return 1.0 if along > 0.0 else -1.0

    def _accumulate(
        self,
        t: float,
        lon: float,
        lat: float,
        observed_at: datetime,
        signed_lon: float | None = None,
    ) -> None:
        self._lon_max = abs(lon) if self._lon_max is None else max(self._lon_max, abs(lon))
        self._lat_max = abs(lat) if self._lat_max is None else max(self._lat_max, abs(lat))
        self._lon_sq += lon * lon
        if signed_lon is not None:
            self._lon_signed_sum += signed_lon
            self._lon_signed_n += 1
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
        if self._lon_signed_n:
            # Over the samples with sign evidence only; none -> absent, never 0.
            values[ACCEL_LON_MEAN_COLUMN] = self._lon_signed_sum / self._lon_signed_n
        observed_at = self._last_resolved_at
        self._clear_tick()
        return values, observed_at

    @property
    def status(self) -> str:
        """Which step the frame is at: ``no_imu_samples``,
        ``no_quiescent_period`` (no gravity reference yet), ``yaw_unresolved``
        (no straight manoeuvre yet), ``sign_unresolved`` (magnitudes flow, but
        no speed change has voted for forward, so no ``imu:ACCEL_LON_MEAN``)
        or ``resolving``."""
        if self._samples == 0:
            return "no_imu_samples"
        if self._gravity is None:
            return "no_quiescent_period"
        if self._s_xy == 0.0 and self._s_xx_yy == 0.0:
            return "yaw_unresolved"
        yaw = 0.5 * math.atan2(2.0 * self._s_xy, self._s_xx_yy)
        if self._sign(math.cos(yaw), math.sin(yaw)) is None:
            return "sign_unresolved"
        return "resolving"

    def lon_mean_absence(self, now: datetime) -> str:
        """Why ``imu:ACCEL_LON_MEAN`` may have no value at ``now``: the
        :attr:`status` step that is waiting, or, once resolving, how long the
        IMU has been silent. For a committee's no-verdict detail."""
        status = self.status
        if status != "resolving":
            return f"live IMU frame {status}"
        last = self._last_sample_at
        if last is None:
            return "live IMU frame resolving, no IMU sample"
        silent = (now - last).total_seconds()
        return f"live IMU frame resolving; newest IMU sample {silent:.1f} s before this tick"


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


__all__ = [
    "GRAVITY_ALPHA",
    "IMU_FRAME_INPUTS",
    "SIGN_ALPHA",
    "SIGN_RHO_G2",
    "SPEED_QUANTUM_KPH",
    "LiveImuFrame",
    "sign_log_evidence",
]
