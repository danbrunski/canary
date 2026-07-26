"""Operator-intent / idle-gating for auto-started sessions (Task 8.6).

``run_daemon`` opens a Parquet session and records every drained telemetry
record for the whole daemon uptime, with no operator-intent check — the
Xterra's body/chassis CAN keeps chattering the moment the key goes to ON
even with the engine off (KOEO), so hours of full-rate low-value idle data
accumulate between drives. Continuous logging is otherwise a deliberate
Done-Definition v1 choice, so this is an *intent gate*, not a way to disable
capture: config-gated (``CollectionConfig.record_arming``), **defaults
off**, and purely additive — :class:`~src.storage.parquet_writer.
ParquetTelemetryWriter` behaves exactly as before when no gate is supplied.

See ``docs/storage-lifecycle-fix.md`` for the full design writeup (the
arm/disarm semantics and pre-arm-buffer open questions Task 8.6 raised, and
why each v1 answer below was chosen over the alternatives).

Semantics, in brief:
  * Arm: the first record on a configured channel (RPM/SPEED by default)
    whose value clears ``arm_threshold`` arms the gate for the rest of the
    current session.
  * Disarm: at a session-rotation boundary (already triggered by
    ``ParquetTelemetryWriter._rotate_if_engine_run_time_reset`` on every key
    cycle), or — v2 — after ``disarm_after_seconds`` of records with no
    vehicle-active evidence. v1 was rotation-only, on the argument that a
    brief RPM dip (e.g. a stall) must not silently start dropping data right
    when a fault might be happening; field data showed the gap that leaves:
    key-off with the Pi still powered never rotates (rotation needs the
    *next* key-on), so a post-drive parked night stayed armed and recorded
    23 h of stationary GPS chatter. The sustained-silence timeout closes
    that without re-introducing the stall concern — the window is minutes,
    a stall's cranking RPM re-arms instantly, and an idling engine keeps
    emitting RPM > 0 so its evidence never goes stale. Ages are computed
    from record timestamps, not wall clock, so replayed sessions gate
    identically to live ones.
  * Pre-arm data: dropped, not buffered. No ring buffer — see the design
    doc for why buffering was rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.telemetry import TelemetryRecord

#: Channels checked by default — engine RPM and ground speed, the same
#: signal family ``src.dashboard.status.classify_vehicle_state`` uses to
#: call a vehicle "running". Not reused directly: that classifier does
#: freshness bookkeeping against wall-clock reads, awkward to fit inline
#: per-record in the storage write path — this is the same *signal*, just
#: not shared code.
DEFAULT_ARM_CHANNELS: tuple[str, ...] = ("RPM", "SPEED")


@dataclass
class RecordArmingGate:
    """Tracks whether the current session has seen a "vehicle active" record.

    ``observe()`` runs once per drained record, inline in the storage write
    path; while disarmed it counts (and reports via ``dropped_count``)
    drops rather than raising or blocking — a misconfigured gate must never
    stop capture outright, only skip it.
    """

    arm_channels: tuple[str, ...] = DEFAULT_ARM_CHANNELS
    arm_threshold: float = 0.0
    #: Disarm after this many seconds of records with no vehicle-active
    #: evidence (an arm channel clearing ``arm_threshold``). ``None`` keeps
    #: the v1 rotation-only disarm.
    disarm_after_seconds: float | None = None
    armed: bool = field(default=False, init=False)
    dropped_count: int = field(default=0, init=False)
    _last_active_at: datetime | None = field(default=None, init=False)

    def observe(self, record: TelemetryRecord) -> bool:
        """Update state from ``record``; return ``True`` if it should be recorded."""
        if self._is_active_evidence(record):
            # max() because sources drain slightly out of order — an older
            # GPS row must not roll the evidence clock backwards.
            if self._last_active_at is None or record.timestamp > self._last_active_at:
                self._last_active_at = record.timestamp
            self.armed = True
            return True
        if self.armed:
            if (
                self.disarm_after_seconds is not None
                and self._last_active_at is not None
                and (record.timestamp - self._last_active_at).total_seconds()
                > self.disarm_after_seconds
            ):
                # Vehicle went quiet (key out / adapter unplugged) but the Pi
                # is still powered and other sources (GPS…) keep chattering.
                # Rotation can't save us — it needs the *next* key-on.
                self.armed = False
                self.dropped_count += 1
                return False
            return True
        self.dropped_count += 1
        return False

    def _is_active_evidence(self, record: TelemetryRecord) -> bool:
        value = record.value
        return (
            record.channel in self.arm_channels
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > self.arm_threshold
        )

    def reset_for_new_session(self) -> None:
        """Disarm. Called on session rotation (new key cycle) so a later
        idle period is gated again rather than left permanently armed."""
        self.armed = False
        self._last_active_at = None
