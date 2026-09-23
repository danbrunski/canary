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

The trip-end edge (Task 5.4) is **not** GPS-only (D03.29). On 2026-09-23 the
receiver reported ``fix_quality`` 0 on all 3,278 samples of a 27-minute drive;
the collector withholds ``gps:speed`` without a fix, so no trip started, none
ended and no digest — or refusal — was written, while road speed was on the
bus the whole time. Motion is now read from, in priority order:

1. **vehicle speed** — Mode 01 PID 0x0D (``obd:SPEED``) or the binding the
   vehicle's ``bindings.yaml`` declares ``quantity: road_speed``, decoded here
   from its raw frame. No CAN id lives in this module; a vehicle without a
   declared binding simply has one fewer source.
2. **GPS speed**, only when no vehicle speed is fresh.

Silence after a stop is the key going off, so the stationary clock keeps
running when every source goes quiet after one. Silence after *motion* cannot
be read either way: sustained for the trip-end window, the drive is closed as
unobservable (:attr:`TripEnd.observed` ``False``) so the caller can record
which kind of empty it is. Vehicle speed drives the edge only — never the
odometer: an ECU/ABS speed that under-reads true ground speed (the Xterra's
reads ~8 % low) is fine for "is it moving" and wrong for distance.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.can_ids import parse_can_channel
from src.decode.bindings import Bindings, Signal, apply_scaling, decode_raw, is_plausible
from src.telemetry import CAN_SOURCE, GPS_SOURCE, OBD_SOURCE, TelemetryRecord

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

#: Mode 01 PID 0x0D's telemetry channel (``can_obd_client.PID_DECODERS``;
#: pinned by ``tests/test_trip_speed_sources.py``) plus the legacy ELM327
#: lower-case spelling the segmenter also accepts.
MODE01_SPEED_CHANNELS: frozenset[str] = frozenset({"SPEED", "speed"})
#: The bindings ``quantity`` the trip edge reads as vehicle road speed.
ROAD_SPEED_QUANTITY = "road_speed"
SPEED_SOURCE_MODE01 = "obd:SPEED"
SPEED_SOURCE_GPS = "gps:speed"
#: A vehicle-speed reading older than this no longer counts as the current
#: speed and GPS may stand in. Generous because Mode 01 cadence varies with
#: the capture profile; a 300 s edge does not need a fresh reading each second.
_VEHICLE_SPEED_FRESH_SECONDS = 30.0
#: Road-speed frames arrive at ~100 Hz on the Xterra (0x6F7, 151,131 frames
#: in 25 min on 2026-09-21); the edge needs a couple per second. Frames closer
#: together than this are not enqueued to the trip sink at all.
_ROAD_SPEED_MIN_INTERVAL_S = 0.5


@dataclass(frozen=True)
class TripEnd:
    """One end-of-drive edge, and whether a speed source actually saw it.

    ``observed`` ``True``: a speed source read the vehicle stopped (or stopped
    and then went quiet — key-off) for the whole window; the caller computes
    the digest. ``False``: no usable speed source could tell whether or when
    the drive ended; the caller records that as the reason there is no digest.
    """

    observed: bool
    #: The source that last read the vehicle's motion (``obd:SPEED``,
    #: ``can:<BINDING>``, ``gps:speed``); ``None`` when none ever did.
    speed_source: str | None
    detail: str
    #: The session this end belongs to: where the stop was seen (observed),
    #: where the vehicle was last seen moving (speed lost), or the session
    #: that closed blind. ``None`` when the sink never named a session. The
    #: edge fires ``trip_end_stationary_seconds`` after the stop, often in a
    #: *later* session — a capture that stopped, a key cycle — so the caller
    #: must use this, never "whatever session is active now" (2026-09-23: the
    #: digest landed on a 257 s parked session while the drive, in the capture,
    #: got none).
    session: Path | None = None
    #: Every session the trip was seen moving in, plus the stop's session, in
    #: the order the tracker met them (the caller re-orders by parsed start
    #: time). A capture started mid-drive or a daemon restart splits a drive.
    sessions: tuple[Path, ...] = ()
    #: Record-time epochs: first reading of motion, and the stop.
    started_epoch: float | None = None
    stopped_epoch: float | None = None


@dataclass
class _SessionEvidence:
    """Per-session speed-source bookkeeping for :meth:`GpsTracker.close_session`.

    A *blind* stretch is time a drive could have started and ended unseen:
    from the session's first record to its first usable speed reading (the
    2026-09-23 shape), or after a reading of **motion** with nothing since.
    Silence after a *stationary* reading is not blind — the vehicle was last
    seen parked, and "no trip happened" is the honest answer; counting it
    turned every quiet driveway or garage session into "could not see".
    Only records advance ``last_epoch``: an idle wall-clock tick is not
    evidence of anything.
    """

    first_epoch: float | None = None
    last_epoch: float | None = None
    last_usable_epoch: float | None = None
    last_usable_moving: bool = False
    saw_motion: bool = False
    longest_blind_s: float = 0.0
    trip_edges: int = 0
    fix_rows: int = 0
    fix_rows_with_fix: int = 0
    usable_readings: dict[str, int] = field(default_factory=dict)

    def note_record(self, epoch: float) -> None:
        if self.first_epoch is None:
            self.first_epoch = epoch
        if self.last_epoch is None or epoch > self.last_epoch:
            self.last_epoch = epoch

    def note_usable(self, source: str, epoch: float, *, moving: bool) -> None:
        self.note_record(epoch)
        self.usable_readings[source] = self.usable_readings.get(source, 0) + 1
        since = self._blind_since()
        if since is not None:
            self.longest_blind_s = max(self.longest_blind_s, epoch - since)
        if self.last_usable_epoch is None or epoch >= self.last_usable_epoch:
            self.last_usable_epoch = epoch
            self.last_usable_moving = moving
        self.saw_motion = self.saw_motion or moving

    def _blind_since(self) -> float | None:
        """Where the current blind stretch began, or ``None`` if not blind."""
        if self.last_usable_epoch is None:
            return self.first_epoch
        return self.last_usable_epoch if self.last_usable_moving else None

    def longest_blind_seconds(self) -> float:
        """Longest blind stretch, the session's open end included."""
        if self.last_epoch is None:
            return 0.0
        since = self._blind_since()
        tail = self.last_epoch - since if since is not None else 0.0
        return max(self.longest_blind_s, tail)


@dataclass
class TripTotals:
    """Cumulative, persisted trip odometer."""

    distance_km: float = 0.0
    moving_time_s: float = 0.0


class GpsTracker:
    """GPS live state, a persistent GPS odometer, and the trip-end edge.

    The class name predates D03.29: the odometer and fix snapshot are still
    GPS-only, but the trip edge reads vehicle speed first (module docstring).
    """

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        persist_interval_seconds: float = 30.0,
        trip_end_stationary_seconds: float = DEFAULT_TRIP_END_STATIONARY_SECONDS,
        bindings: Bindings | None = None,
    ) -> None:
        self._state_path = state_path
        self._persist_interval_s = persist_interval_seconds
        self._trip_end_stationary_s = trip_end_stationary_seconds
        self._totals = TripTotals()
        self._latest: dict[str, float] = {}
        self._latest_ts: datetime | None = None
        self._last_speed_epoch: float | None = None
        self._last_persist_epoch: float | None = None
        self._road_speed: Signal | None = None
        self._road_speed_accepted_epoch: float | None = None
        #: Trip state saved by :meth:`suspend` while a replay runs.
        self._suspended: dict[str, Any] | None = None
        #: The recording session the sink says records now belong to.
        self._session_path: Path | None = None
        #: Process-lifetime: has any live (non-replay) usable speed reading
        #: ever arrived? Separates "this install cannot read speed" from "the
        #: vehicle is parked and the sources went quiet" for the shutdown
        #: fallback (:meth:`session_never_moved`).
        self._ever_read_speed = False
        self._reset_trip_state()
        self.use_bindings(bindings)
        if state_path is not None:
            self._load()

    #: Everything :meth:`suspend` sets aside — the trip edge's state, never the
    #: odometer or the fix snapshot.
    _TRIP_STATE_ATTRS: tuple[str, ...] = (
        "_vehicle_speed",
        "_gps_speed",
        "_moving",
        "_trip_active",
        "_stationary_since_epoch",
        "_last_motion_source",
        "_last_usable_epoch",
        "_session",
        "_trip_sessions",
        "_trip_started_epoch",
        "_stop_session",
    )

    def _reset_trip_state(self) -> None:
        #: Latest vehicle speed ``(epoch, km/h, source)`` and GPS speed
        #: ``(epoch, km/h)`` — the trip edge's two inputs, in priority order.
        self._vehicle_speed: tuple[float, float, str] | None = None
        self._gps_speed: tuple[float, float] | None = None
        #: Task 5.4 moving/stationary tracking — separate from the odometer
        #: totals, which persist across trips; this is per-trip and never
        #: saved to the state sidecar.
        self._moving = False
        self._trip_active = False
        self._stationary_since_epoch: float | None = None
        self._last_motion_source: str | None = None
        self._last_usable_epoch: float | None = None
        self._session = _SessionEvidence()
        #: The trip's sessions (see :attr:`TripEnd.sessions`), its first motion,
        #: and the session its stop was seen in.
        self._trip_sessions: list[Path] = []
        self._trip_started_epoch: float | None = None
        self._stop_session: Path | None = None

    def suspend(self) -> None:
        """Set the live trip aside while a replay feeds the pipeline.

        Replayed records are another drive's, with another drive's clock: a
        replay stopped mid-drive must not leave its "moving" state behind for
        the live session to end as unobservable, and a live stop must not be
        timed against replayed timestamps. The replay runs on fresh state;
        :meth:`resume` discards it and restores the live trip. Nested calls
        are no-ops.
        """
        if self._suspended is not None:
            return
        self._suspended = {name: getattr(self, name) for name in self._TRIP_STATE_ATTRS}
        self._reset_trip_state()

    def resume(self) -> None:
        """Discard the replay's trip state and restore the live one."""
        if self._suspended is None:
            return
        for name, value in self._suspended.items():
            setattr(self, name, value)
        self._suspended = None

    @property
    def suspended(self) -> bool:
        """Whether a replay currently has the live trip set aside."""
        return self._suspended is not None

    # ------------------------------------------------------------------
    # speed sources
    # ------------------------------------------------------------------

    def use_bindings(self, bindings: Bindings | None) -> None:
        """Read road speed from the binding ``bindings`` declares as
        ``quantity: road_speed`` (``None`` / no declaration: Mode 01 + GPS)."""
        self._road_speed = (
            None if bindings is None else bindings.quantity_binding(ROAD_SPEED_QUANTITY)
        )
        self._road_speed_accepted_epoch = None

    @property
    def road_speed_binding(self) -> str | None:
        """Name of the declared road-speed binding in use, if any."""
        return None if self._road_speed is None else self._road_speed.name

    def accepts(self, record: TelemetryRecord) -> bool:
        """Sink filter: the records :meth:`observe` acts on.

        Every ``gps`` record (fix health keeps the edge's clock running while
        parked), Mode 01 vehicle speed, and the declared road-speed frame —
        decimated to one per :data:`_ROAD_SPEED_MIN_INTERVAL_S`, because the
        filter runs in the fanout for every raw frame on the bus and a 100 Hz
        frame would otherwise be enqueued a hundred times a second for an
        edge measured in minutes.
        """
        source = record.source
        if source == GPS_SOURCE:
            return True
        if source == OBD_SOURCE:
            return record.channel in MODE01_SPEED_CHANNELS
        if source != CAN_SOURCE or self._road_speed is None:
            return False
        if parse_can_channel(record.channel) != self._road_speed.can_id:
            return False
        epoch = record.timestamp.timestamp()
        last = self._road_speed_accepted_epoch
        if last is not None and 0.0 <= epoch - last < _ROAD_SPEED_MIN_INTERVAL_S:
            return False
        self._road_speed_accepted_epoch = epoch
        return True

    def _road_speed_kmh(self, record: TelemetryRecord) -> float | None:
        signal = self._road_speed
        if signal is None or record.payload is None:
            return None
        if parse_can_channel(record.channel) != signal.can_id:
            return None
        raw = decode_raw(signal, bytes(record.payload))
        if raw is None:
            return None
        kmh = apply_scaling(signal, raw)
        if kmh is None or is_plausible(signal, kmh) is False:
            return None
        return kmh

    def observe(self, record: TelemetryRecord) -> TripEnd | None:
        """Apply one telemetry record; anything :meth:`accepts` would refuse
        is ignored.

        Returns a :class:`TripEnd` exactly on the tick the trip-end edge fires
        (Task 5.4: sustained moving -> stationary, or D03.29's unobservable
        end) — the daemon's trip sink checks this to trigger end-of-drive
        digest assembly or record why there is none. ``None`` every other call.
        """
        epoch = record.timestamp.timestamp()
        if record.source == GPS_SOURCE:
            value = record.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            self._latest[record.channel] = float(value)
            self._latest_ts = record.timestamp
            if record.channel == "fix_quality":
                self._session.fix_rows += 1
                self._session.fix_rows_with_fix += int(value > 0)
            elif record.channel == "speed":
                self._gps_speed = (epoch, float(value))
                self._session.note_usable(
                    SPEED_SOURCE_GPS, epoch, moving=float(value) >= _MOVING_THRESHOLD_KMH
                )
                self._accumulate(float(value), epoch)
        elif record.source == OBD_SOURCE and record.channel in MODE01_SPEED_CHANNELS:
            value = record.value
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            self._vehicle_speed = (epoch, float(value), SPEED_SOURCE_MODE01)
            self._session.note_usable(
                SPEED_SOURCE_MODE01, epoch, moving=float(value) >= _MOVING_THRESHOLD_KMH
            )
        elif record.source == CAN_SOURCE and self._road_speed is not None:
            kmh = self._road_speed_kmh(record)
            if kmh is None:
                return None
            label = f"can:{self._road_speed.name}"
            self._vehicle_speed = (epoch, kmh, label)
            self._session.note_usable(label, epoch, moving=kmh >= _MOVING_THRESHOLD_KMH)
        else:
            return None
        self._session.note_record(epoch)
        if self._session.usable_readings and self._suspended is None:
            self._ever_read_speed = True
        return self._evaluate(epoch)

    def tick(self, epoch: float) -> TripEnd | None:
        """Advance the edge's clock to ``epoch`` with no record.

        ``epoch`` is on the records' timebase: the trip sink derives it from
        the last record's timestamp plus *monotonic* time elapsed since that
        record arrived, so a wall-clock step (NTP/chrony, a GPS time fix)
        cannot end or split a trip.

        Silence after key-off is exactly when nothing arrives: a GPS-less
        install, or a receiver that emits nothing, sends no rows at all, and a
        record-driven clock then ends the trip at the *next* key-on — against
        the next session. The trip sink calls this whenever its queue has been
        idle for a poll interval. A no-op while suspended (:meth:`suspend`): a replay's
        timestamps are not the wall clock.
        """
        if self._suspended is not None:
            return None
        # Deliberately not ``self._session.note_record``: a tick is the clock,
        # not a record, and counting it made key-off silence after a parked
        # reading read as a blind session.
        return self._evaluate(epoch)

    def _current_speed(self, epoch: float) -> tuple[str, float] | None:
        """The speed the edge reads now: fresh vehicle speed, else fresh GPS."""
        vehicle = self._vehicle_speed
        if vehicle is not None and epoch - vehicle[0] <= _VEHICLE_SPEED_FRESH_SECONDS:
            return vehicle[2], vehicle[1]
        gps = self._gps_speed
        if gps is not None and epoch - gps[0] <= _FIX_STALE_SECONDS:
            return SPEED_SOURCE_GPS, gps[1]
        return None

    def _accumulate(self, speed_kmh: float, epoch: float) -> None:
        """The GPS odometer. Vehicle speed never reaches it (module docstring)."""
        last = self._last_speed_epoch
        self._last_speed_epoch = epoch
        if last is not None:
            dt = epoch - last
            if 0 < dt <= _MAX_INTEGRATION_GAP_S and speed_kmh >= _MOVING_THRESHOLD_KMH:
                self._totals.distance_km += speed_kmh * (dt / 3600.0)
                self._totals.moving_time_s += dt
        self._maybe_persist(epoch)

    def _evaluate(self, epoch: float) -> TripEnd | None:
        """Track moving/stationary state; return the edge exactly once.

        A trip "starts" the first time any source reads motion, and "ends"
        once stationary has been sustained for ``trip_end_stationary_seconds``
        after that — one edge per trip, not one per stationary tick. Every
        accepted record advances the clock, so a stop followed by silence
        (key-off: the vehicle sources stop, GPS fix-health rows continue)
        still ends the trip.
        """
        current = self._current_speed(epoch)
        if current is not None:
            source, speed_kmh = current
            self._last_motion_source = source
            self._last_usable_epoch = epoch
            if speed_kmh >= _MOVING_THRESHOLD_KMH:
                if not self._trip_active:
                    self._trip_sessions = []
                    self._trip_started_epoch = epoch
                self._moving = True
                self._trip_active = True
                self._stationary_since_epoch = None
                self._stop_session = None
                self._note_trip_session(self._session_path)
                return None
            if self._moving:
                # Just transitioned to stationary — start the sustain clock,
                # and remember whose session the stop belongs to.
                self._moving = False
                self._stationary_since_epoch = epoch
                self._stop_session = self._session_path
                self._note_trip_session(self._session_path)
                return None
            return self._stationary_edge(epoch)
        if not self._trip_active:
            return None
        if not self._moving:
            # Stopped, then every source went quiet: the key went off.
            return self._stationary_edge(epoch)
        # Last read moving, and nothing has read anything since.
        blind_since = self._last_usable_epoch if self._last_usable_epoch is not None else epoch
        if epoch - blind_since < self._trip_end_stationary_s:
            return None
        self._trip_active = False
        self._moving = False
        self._session.trip_edges += 1
        return TripEnd(
            observed=False,
            speed_source=self._last_motion_source,
            detail=(
                f"no usable speed source for {epoch - blind_since:.0f} s after "
                f"{self._last_motion_source or 'a speed source'} last read the vehicle "
                f"moving, so where the drive ended was not observed "
                f"({self._source_inventory()})"
            ),
            session=self._trip_sessions[-1] if self._trip_sessions else self._session_path,
            sessions=tuple(self._trip_sessions),
            started_epoch=self._trip_started_epoch,
        )

    def _stationary_edge(self, epoch: float) -> TripEnd | None:
        if not self._trip_active or self._stationary_since_epoch is None:
            # Never moving this trip (or already ended) — nothing to end.
            return None
        if epoch - self._stationary_since_epoch < self._trip_end_stationary_s:
            return None
        self._trip_active = False
        self._session.trip_edges += 1
        return self._observed_end(
            f"stationary for {epoch - self._stationary_since_epoch:.0f} s "
            f"on {self._last_motion_source}"
        )

    def _observed_end(self, detail: str) -> TripEnd:
        return TripEnd(
            observed=True,
            speed_source=self._last_motion_source,
            detail=detail,
            session=self._stop_session if self._stop_session is not None else self._session_path,
            sessions=tuple(self._trip_sessions),
            started_epoch=self._trip_started_epoch,
            stopped_epoch=self._stationary_since_epoch,
        )

    def _note_trip_session(self, path: Path | None) -> None:
        if path is not None and path not in self._trip_sessions:
            self._trip_sessions.append(path)

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    def enter_session(self, path: Path | None) -> None:
        """Records from now on belong to recording session ``path``.

        The trip sink calls this on every rotation, after :meth:`close_session`.
        It names sessions only; it never ends a trip.
        """
        self._session_path = path

    def pending_trip(self) -> TripEnd | None:
        """The trip still open right now, as an end, for the shutdown fallback.

        The daemon is stopping mid-trip (power loss, a restart): the drive
        ends here whatever the speed says. ``None`` when no trip is open —
        the fallback then must not invent one (:meth:`session_never_moved`).
        """
        if not self._trip_active:
            return None
        if self._moving or self._stationary_since_epoch is None:
            return TripEnd(
                observed=True,
                speed_source=self._last_motion_source,
                detail=f"daemon stopped while {self._last_motion_source} read the vehicle moving",
                session=self._trip_sessions[-1] if self._trip_sessions else self._session_path,
                sessions=tuple(self._trip_sessions),
                started_epoch=self._trip_started_epoch,
            )
        return self._observed_end(
            f"daemon stopped with the vehicle stationary on {self._last_motion_source}"
        )

    def session_never_moved(self) -> bool:
        """The current session showed no motion, and this tracker can read speed.

        The shutdown fallback's refusal: a digest on such a session describes
        no drive (2026-09-23: a 257 s parked session after a capture stopped).
        Judged on the tracker's *history*, not only the active session: after
        key-off the parked session typically holds no usable reading at all —
        GPS fix-health rows with no fix, or nothing — and reading that as
        "cannot tell" put a near-empty digest on it, the live finding again.
        ``False`` only when no usable speed has arrived since start (an
        install that cannot read speed keeps the old fallback) or when the
        session did show motion.
        """
        return self._ever_read_speed and not self._session.saw_motion

    def close_session(self) -> TripEnd | None:
        """The active session ended; say so if its drive could not be seen.

        Called by the trip sink when the recording session rotates (a key
        cycle, a capture boundary). An open trip is **not** ended here: it
        may continue in the next session (a capture stopped at a traffic
        light, a restart mid-drive), and when its edge fires the end carries
        the session the stop was seen in (:attr:`TripEnd.session`), so the
        digest lands on the drive, not on whatever session is active by then.

        Returns an unobservable :class:`TripEnd` when no trip edge fired in
        the session *and* no usable speed source covered it for at least the
        trip-end window — a drive could have started and ended in that blind
        stretch (the 2026-09-23 no-fix shape). ``None`` otherwise. Per-session
        evidence resets every time.
        """
        session, self._session = self._session, _SessionEvidence()
        if session.trip_edges or session.first_epoch is None:
            return None
        if self._ever_read_speed and not session.saw_motion and not session.usable_readings:
            # Speed has been readable since start, and this session never
            # produced a single usable reading: the key was off (the road-speed
            # source goes quiet with the bus), not a drive hidden from view. A
            # parked post-drive session is "no trip happened", not "could not
            # see" — otherwise every key-off would file a refusal.
            return None
        blind = session.longest_blind_seconds()
        if blind < self._trip_end_stationary_s:
            return None
        return TripEnd(
            observed=False,
            speed_source=None,
            detail=(
                f"no usable speed source for {blind:.0f} s of this session and no "
                f"trip edge was seen, so whether a drive ended in it was not "
                f"observed ({self._source_inventory(session)})"
            ),
            session=self._session_path,
        )

    def _source_inventory(self, session: _SessionEvidence | None = None) -> str:
        """Which speed sources this session had — the "which kind of empty"."""
        evidence = self._session if session is None else session
        road = (
            f"can:{self._road_speed.name} (declared road_speed binding)"
            if self._road_speed is not None
            else "no binding declares quantity: road_speed"
        )
        parts = [
            f"{source}: {evidence.usable_readings.get(source, 0)} readings"
            for source in (SPEED_SOURCE_MODE01, SPEED_SOURCE_GPS)
        ]
        if self._road_speed is not None:
            label = f"can:{self._road_speed.name}"
            parts.append(f"{road}: {evidence.usable_readings.get(label, 0)} readings")
        else:
            parts.append(road)
        parts.append(
            f"gps fix_quality: {evidence.fix_rows_with_fix} of {evidence.fix_rows} rows with a fix"
        )
        return "; ".join(parts)

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


__all__ = [
    "DEFAULT_TRIP_END_STATIONARY_SECONDS",
    "MODE01_SPEED_CHANNELS",
    "ROAD_SPEED_QUANTITY",
    "GpsTracker",
    "TripEnd",
    "TripTotals",
]
