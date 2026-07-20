"""Live GPS state + trip odometer derived from the ``gps:*`` telemetry stream.

The daemon feeds every :class:`TelemetryRecord` through :meth:`GpsTracker.observe`.
GPS records update a latest-fix snapshot and accumulate a persistent trip
odometer (distance + time moving). Both are surfaced on the dashboard System
page via ``build_status``.

Distance is integrated from ``gps:speed`` (km/h) over the wall-clock interval
between consecutive fixes, gated on a small moving threshold so GPS jitter while
parked never accrues phantom miles. Totals persist to a JSON sidecar so they
survive daemon restarts — the "miles / time driven with the unit plugged in"
the operator asked for.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.telemetry import GPS_SOURCE, TelemetryRecord

log = logging.getLogger(__name__)

_KM_PER_MILE = 1.609344
#: GPS jitter while parked reports a few tenths of a km/h; ignore anything below
#: this for both distance and drive-time so we never accrue phantom motion.
_MOVING_THRESHOLD_KMH = 3.0
#: Don't integrate across a gap longer than this (daemon restart, fix loss,
#: tunnel) — there's no way to know what happened in between.
_MAX_INTEGRATION_GAP_S = 10.0
#: A latest fix older than this is reported as stale (no current fix).
_FIX_STALE_SECONDS = 10.0
#: Task 5.4 (eval-longitudinal-and-dtc.md): moving -> stationary sustained
#: this long fires the "trip ended" edge. Long enough that a stoplight or a
#: quick errand stop doesn't end the trip; short enough that the end-of-drive
#: digest lands within a few minutes of actually parking.
DEFAULT_TRIP_END_STATIONARY_SECONDS = 300.0


@dataclass
class TripTotals:
    """Cumulative, persisted trip odometer."""

    distance_km: float = 0.0
    moving_time_s: float = 0.0


class GpsTracker:
    """Accumulate GPS-derived live state and a persistent trip odometer."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        persist_interval_seconds: float = 30.0,
        trip_end_stationary_seconds: float = DEFAULT_TRIP_END_STATIONARY_SECONDS,
    ) -> None:
        self._state_path = state_path
        self._persist_interval_s = persist_interval_seconds
        self._trip_end_stationary_s = trip_end_stationary_seconds
        self._totals = TripTotals()
        self._latest: dict[str, float] = {}
        self._latest_ts: datetime | None = None
        self._last_speed_epoch: float | None = None
        self._last_persist_epoch: float | None = None
        #: Task 5.4 moving/stationary tracking — separate from the odometer
        #: totals above, which persist across trips; this is per-trip and
        #: never saved to the state sidecar.
        self._moving = False
        self._trip_active = False
        self._stationary_since_epoch: float | None = None
        if state_path is not None:
            self._load()

    def observe(self, record: TelemetryRecord) -> bool:
        """Apply one telemetry record; non-GPS and non-numeric records are ignored.

        Returns ``True`` exactly on the tick the "trip ended" edge fires
        (Task 5.4: sustained moving -> stationary) — the daemon's GPS sink
        checks this to trigger end-of-drive digest assembly. ``False``
        every other call.
        """
        if record.source != GPS_SOURCE:
            return False
        value = record.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        self._latest[record.channel] = float(value)
        self._latest_ts = record.timestamp
        if record.channel == "speed":
            return self._accumulate(float(value), record.timestamp)
        return False

    def _accumulate(self, speed_kmh: float, timestamp: datetime) -> bool:
        epoch = timestamp.timestamp()
        trip_ended = self._update_trip_state(speed_kmh >= _MOVING_THRESHOLD_KMH, epoch)
        last = self._last_speed_epoch
        self._last_speed_epoch = epoch
        if last is not None:
            dt = epoch - last
            if 0 < dt <= _MAX_INTEGRATION_GAP_S and speed_kmh >= _MOVING_THRESHOLD_KMH:
                self._totals.distance_km += speed_kmh * (dt / 3600.0)
                self._totals.moving_time_s += dt
        self._maybe_persist(epoch)
        return trip_ended

    def _update_trip_state(self, moving_now: bool, epoch: float) -> bool:
        """Track moving/stationary state; return ``True`` exactly on the tick
        the trip-end edge fires.

        A trip "starts" the first time we see motion, and "ends" once
        stationary has been sustained for ``trip_end_stationary_seconds``
        after that — one edge per trip, not one per stationary tick.
        """
        if moving_now:
            self._moving = True
            self._trip_active = True
            self._stationary_since_epoch = None
            return False
        if self._moving:
            # Just transitioned to stationary — start the sustain clock.
            self._moving = False
            self._stationary_since_epoch = epoch
            return False
        if not self._trip_active or self._stationary_since_epoch is None:
            # Never moving this trip (or already ended) — nothing to end.
            return False
        if epoch - self._stationary_since_epoch >= self._trip_end_stationary_s:
            self._trip_active = False
            return True
        return False

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def gps_status(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        """Latest GPS fix snapshot, or ``None`` if nothing has been observed."""
        if not self._latest or self._latest_ts is None:
            return None
        now_dt = datetime.now(UTC) if now is None else now
        age = (now_dt - self._latest_ts).total_seconds()
        fix_quality = int(self._latest.get("fix_quality", 0))
        status: dict[str, Any] = {
            "has_fix": fix_quality > 0 and age <= _FIX_STALE_SECONDS,
            "fix_quality": fix_quality,
            "num_satellites": int(self._latest.get("num_satellites", 0)),
            "age_seconds": round(age, 1),
        }
        for channel in ("latitude", "longitude", "altitude", "speed", "heading", "hdop"):
            status[channel] = self._latest.get(channel)
        return status

    def trip_status(self) -> dict[str, Any]:
        """Cumulative odometer in km and miles, plus time moving."""
        km = self._totals.distance_km
        return {
            "distance_km": round(km, 3),
            "distance_miles": round(km / _KM_PER_MILE, 3),
            "moving_time_seconds": round(self._totals.moving_time_s, 1),
        }

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _maybe_persist(self, epoch: float) -> None:
        if self._state_path is None:
            return
        if (
            self._last_persist_epoch is None
            or epoch - self._last_persist_epoch >= self._persist_interval_s
        ):
            self.persist()
            self._last_persist_epoch = epoch

    def persist(self) -> None:
        """Atomically + durably write the odometer totals to the state sidecar.

        fsync the temp file and the directory so the totals survive a hard
        power loss (key-off, no UPS); at worst we lose the deltas since the last
        persist (bounded by ``persist_interval_seconds``).
        """
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(f".{self._state_path.name}.tmp")
            payload = json.dumps(
                {
                    "distance_km": self._totals.distance_km,
                    "moving_time_s": self._totals.moving_time_s,
                }
            )
            with open(tmp, "w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(self._state_path)
            dir_fd = os.open(self._state_path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            log.warning(
                "trip odometer persist failed",
                extra={"path": str(self._state_path), "error": str(exc)},
            )

    def _load(self) -> None:
        assert self._state_path is not None
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
            self._totals = TripTotals(
                distance_km=float(data.get("distance_km", 0.0)),
                moving_time_s=float(data.get("moving_time_s", 0.0)),
            )
        except (OSError, ValueError, TypeError) as exc:
            log.warning("trip odometer load failed; starting fresh", extra={"error": str(exc)})


__all__ = ["DEFAULT_TRIP_END_STATIONARY_SECONDS", "GpsTracker", "TripTotals"]
