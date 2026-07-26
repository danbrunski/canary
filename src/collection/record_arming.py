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
  * Vehicle-active evidence (v3): any record from an ``arm_sources`` source
    (``can``/``obd`` by default — i.e. the vehicle bus is talking at all),
    or a record on an ``arm_channels`` channel whose value clears
    ``arm_threshold``. The operator's deciding factor is *bus
    connectivity*: plugged in with the bus alive → record (including KOEO
    diagnostics); bench / key-out / unplugged → the bus is silent and
    nothing that still chatters (GPS, derived) counts as evidence.
    Installs on vehicles whose bus stays awake while parked (keyless-entry
    polling etc.) can set ``arm_sources: []`` to fall back to
    channel-threshold evidence alone.
  * Arm: the first record carrying evidence arms the gate.
  * Disarm: at a session-rotation boundary (already triggered by
    ``ParquetTelemetryWriter._rotate_if_engine_run_time_reset`` on every key
    cycle), or — v2 — after ``disarm_after_seconds`` of records with no
    evidence. v1 was rotation-only, on the argument that a brief RPM dip
    (e.g. a stall) must not silently start dropping data right when a fault
    might be happening; field data showed the gap that leaves: key-off with
    the Pi still powered never rotates (rotation needs the *next* key-on),
    so a post-drive parked night stayed armed and recorded 23 h of
    stationary GPS chatter. The sustained-silence timeout closes that
    without re-introducing the stall concern — the window is minutes, and
    the bus keeps chattering through a stall so its evidence never goes
    stale. Ages are computed from record timestamps, not wall clock, so
    replayed sessions gate identically to live ones.
  * Pre-arm data: dropped, not buffered. No ring buffer — see the design
    doc for why buffering was rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.collection import tx_gate
from src.telemetry import TelemetryRecord

#: Sources whose mere presence proves the vehicle bus is connected and
#: talking. GPS/IMU/derived are deliberately absent — they chatter whenever
#: the Pi has power, which is exactly the parked/bench data this gate exists
#: to drop.
DEFAULT_ARM_SOURCES: tuple[str, ...] = ("can", "obd")

#: Channel fallback for installs that narrow ``arm_sources`` — engine RPM,
#: the same signal ``src.dashboard.status.classify_vehicle_state`` uses to
#: call a vehicle "running". Not reused directly: that classifier does
#: freshness bookkeeping against wall-clock reads, awkward to fit inline
#: per-record in the storage write path — this is the same *signal*, just
#: not shared code.
DEFAULT_ARM_CHANNELS: tuple[str, ...] = ("RPM",)


@dataclass
class RecordArmingGate:
    """Tracks whether the current session has seen a "vehicle active" record.

    ``observe()`` runs once per drained record, inline in the storage write
    path; while disarmed it counts (and reports via ``dropped_count``)
    drops rather than raising or blocking — a misconfigured gate must never
    stop capture outright, only skip it.
    """

    arm_sources: tuple[str, ...] = DEFAULT_ARM_SOURCES
    arm_channels: tuple[str, ...] = DEFAULT_ARM_CHANNELS
    arm_threshold: float = 0.0
    #: Disarm after this many seconds of records with no vehicle-active
    #: evidence (an ``arm_sources`` record, or an arm channel clearing
    #: ``arm_threshold``). ``None`` keeps the v1 rotation-only disarm.
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
                and not tx_gate.is_passive()
            ):
                # Vehicle went quiet (key out / adapter unplugged) but the Pi
                # is still powered and other sources (GPS…) keep chattering.
                # Rotation can't save us — it needs the *next* key-on.
                #
                # Suppressed while passive mode is armed (`tx_gate.is_passive`):
                # this branch infers "vehicle is parked" from an absence of
                # arm-source records, and during an operator-armed passive
                # window that inference is invalid. We stopped polling
                # ourselves, and the event being observed may itself quiet the
                # bus — reprogramming tools routinely issue CommunicationControl
                # / ControlDTCSetting to stop normal message transmission during
                # erase/write. Disarming there would drop recording for exactly
                # the window passive mode was armed to capture.
                self.armed = False
                self.dropped_count += 1
                return False
            return True
        self.dropped_count += 1
        return False

    def _is_active_evidence(self, record: TelemetryRecord) -> bool:
        if record.source in self.arm_sources:
            return True
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
