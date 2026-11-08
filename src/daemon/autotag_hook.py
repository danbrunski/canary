"""Live auto-tagger hook (Phase 2a.6, docs/roadmap/archive/imu-gps-integration.md).

Evaluates the exact same tag taxonomy ``scripts/autotag.py`` runs offline
(``src.analysis.autotag``'s threshold table) once per daemon tick, writing
through the same ``src.collection.tags.write_tag`` — so a live-tagged
session's ``tags.jsonl`` is indistinguishable in shape from one tagged by
the offline pass or by a human with ``scripts/tag.py``.

**Ships OFF by default** (``AutoTagConfig.live_enabled``) — a hard pre-made
decision (Round-2 OVERNIGHT-QUEUE): dormant until Phase 2b hardware
validation, independent of whether the underlying detection logic works
(it's the same logic 2a.5's offline pass already exercises against real
GPS sessions).

Unlike the offline pass — which scans a whole session's frame for
contiguous "condition held" runs and tags the peak/onset of each — live
serve only ever sees "now", one tick at a time. This module reimplements
the same predicates as **edge-triggered** state: a label fires once when
its condition transitions from not-holding to holding (or, for the
sustained labels ``launch``/``highway_cruise``, once the condition has held
continuously for the configured sustain duration), and won't fire again
until the condition drops and re-rises — the live analog of "one event per
contiguous run".
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from src.coerce import finite_float
from src.collection.tags import write_tag
from src.config import AutoTagConfig

log = logging.getLogger(__name__)

_SOURCE_FIELD = "source"
_SOURCE_VALUE = "autotag"


@dataclass
class _EdgeState:
    """Rising-edge tracker for one on/off condition: fires once per contiguous
    True run, not once per tick the condition holds."""

    active: bool = False

    def rising_edge(self, condition: bool) -> bool:
        edge = condition and not self.active
        self.active = condition
        return edge


@dataclass
class _SustainState:
    """Like :class:`_EdgeState`, but only fires once the condition has held
    continuously for ``sustain_ticks`` — for ``launch``/``highway_cruise``."""

    sustain_ticks: int
    streak: int = 0
    fired: bool = False

    def tick(self, condition: bool) -> bool:
        if not condition:
            self.streak = 0
            self.fired = False
            return False
        self.streak += 1
        if self.streak >= self.sustain_ticks and not self.fired:
            self.fired = True
            return True
        return False


@dataclass
class LiveAutoTagHook:
    """Per-tick auto-tag evaluator. One instance per live feature source.

    ``session_dir()`` is a callable rather than a fixed path because the
    active session rotates over the daemon's lifetime (storage rotation,
    replay) — the hook always tags whatever session is currently being
    written, never a stale path captured at construction time.
    """

    config: AutoTagConfig
    session_dir: Callable[[], Path | None]

    _hard_accel: _EdgeState = field(default_factory=_EdgeState)
    _hard_brake: _EdgeState = field(default_factory=_EdgeState)
    _cornering: _EdgeState = field(default_factory=_EdgeState)
    _launch: _SustainState = field(init=False)
    _highway: _SustainState = field(init=False)
    _stopped: _EdgeState = field(default_factory=_EdgeState)
    #: (timestamp, accel_lon, accel_lat) samples covering the highway
    #: sustain window, for the variance gate — bounded by wall-clock age,
    #: not a fixed row count, so it works at any tick rate.
    _recent_accel: deque[tuple[datetime, float | None, float | None]] = field(default_factory=deque)
    _last_speed: float | None = field(default=None, init=False)
    _last_speed_at: datetime | None = field(default=None, init=False)
    _tick_seconds: float = 1.0

    def __post_init__(self) -> None:
        launch_ticks = max(1, round(self.config.launch_sustain_seconds / self._tick_seconds))
        highway_ticks = max(1, round(self.config.highway_sustain_seconds / self._tick_seconds))
        self._launch = _SustainState(sustain_ticks=launch_ticks)
        self._highway = _SustainState(sustain_ticks=highway_ticks)

    def _speed_accel_sign(self, speed: float | None, now: datetime) -> float | None:
        sign: float | None = None
        if speed is not None and self._last_speed is not None and self._last_speed_at is not None:
            dt = (now - self._last_speed_at).total_seconds()
            if dt > 0:
                sign = speed - self._last_speed
        self._last_speed = speed
        self._last_speed_at = now
        return sign

    def _write(self, label: str, now: datetime, **fields: Any) -> None:
        session_dir = self.session_dir()
        if session_dir is None:
            return
        try:
            write_tag(
                Path(session_dir),
                label,
                timestamp=now.timestamp(),
                **{_SOURCE_FIELD: _SOURCE_VALUE},
                **fields,
            )
        except OSError:
            log.exception("live auto-tag write failed for %r", label)

    def tick(self, point: dict[str, Any] | None, now: datetime) -> None:
        """Evaluate one tick's operating point. No-op if the hook is disabled
        or there's nothing to evaluate — callers may tick unconditionally."""
        if not self.config.live_enabled or point is None:
            return

        obd_speed = finite_float(point.get("obd:SPEED"))
        gps_speed = finite_float(point.get("gps:speed"))
        accel_lon = finite_float(point.get("imu:ACCEL_LON_MAX"))
        accel_lat = finite_float(point.get("imu:ACCEL_LAT_MAX"))
        speed_accel = self._speed_accel_sign(obd_speed, now)

        if accel_lon is not None and speed_accel is not None:
            if self._hard_accel.rising_edge(
                accel_lon > self.config.hard_accel_g and speed_accel > 0
            ):
                self._write("hard_accel", now, peak_g=accel_lon)
            if self._hard_brake.rising_edge(
                accel_lon > self.config.hard_brake_g and speed_accel < 0
            ):
                self._write("hard_brake", now, peak_g=accel_lon)
            near_stop = obd_speed is not None and obd_speed <= self.config.launch_speed_max_kph
            if self._launch.tick(
                near_stop and accel_lon > self.config.hard_accel_g and speed_accel > 0
            ):
                self._write("launch", now, peak_g=accel_lon)

        if accel_lat is not None and self._cornering.rising_edge(
            accel_lat > self.config.cornering_g
        ):
            self._write("cornering", now, peak_g=accel_lat)

        if gps_speed is not None:
            if self._stopped.rising_edge(gps_speed <= self.config.stopped_speed_max_kph):
                self._write("stopped", now, gps_speed_kph=gps_speed)

            self._recent_accel.append((now, accel_lon, accel_lat))
            window = self.config.highway_sustain_seconds
            while self._recent_accel and (now - self._recent_accel[0][0]).total_seconds() > window:
                self._recent_accel.popleft()
            variance_ok = True
            samples = [v for _, lon, lat in self._recent_accel for v in (lon, lat) if v is not None]
            if samples:
                variance_ok = float(np.var(samples)) <= self.config.highway_max_accel_variance_g2
            if self._highway.tick(gps_speed >= self.config.highway_speed_min_kph and variance_ok):
                self._write("highway_cruise", now, gps_speed_kph=gps_speed)
