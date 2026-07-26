# Storage Lifecycle & Disk Safety — shipped (Phase 8)

The 2026-05-17 disk-safety fix plan that lived here has shipped. The
daemon now runs a periodic in-process retention sweep — age expiry, a
byte-cap ring buffer, and an emergency low-space eviction — that never
deletes the active or a pinned session.

- Sweep loop: `src/daemon/retention.py` (`run_retention_sweeper`)
- Implementations: `src/storage/retention.py` (`delete_expired_sessions`,
  `enforce_storage_cap`, `emergency_evict_for_free_space`,
  `delete_expired_replays`, `delete_empty_sessions`)
- Config: `collection.storage_max_mb`, `collection.storage_warning_mb`,
  `daemon.retention_sweep_seconds`
- Durable description: `docs/daemon-architecture.md` and
  `docs/data-sources.md` (storage section)

## 8.6 — operator-intent / idle-gating for auto-started sessions

`run_daemon` opens a Parquet session and records every drained telemetry
record for the whole daemon uptime, with no operator-intent check. The
Xterra's body/chassis CAN keeps chattering the moment the key goes to ON
even with the engine off (KOEO — see `vehicle_state` in AGENTS.md), so
hours of full-rate, low-value idle data accumulate between actual drives
(R14 in `docs/implementation-tasks.md`). Continuous logging is otherwise a
deliberate Done-Definition v1 choice (dashboard/status visibility depends
on it, and the retention sweeper above already caps total bytes), so the
fix is **intent-gating**, not disabling capture.

**v1 ships config-gated and defaulted off** — `collection.record_arming`
(`src.config.RecordArmingConfig`, default `enabled: false`). When left
unset, `ParquetTelemetryWriter` behaves exactly as before (byte-identical —
no gate object is even constructed). When enabled, an optional
`RecordArmingGate` (`src/collection/record_arming.py`) sits inline in
`ParquetTelemetryWriter.write()`: every drained record passes through
`gate.observe(record)` before it reaches the buffer.

Task 8.6 flagged two open questions; v1's answers:

1. **Arm/disarm semantics.** Vehicle-active evidence (v3, 2026-07-26):
   any record from an `arm_sources` source (`can`/`obd` by default) — i.e.
   the vehicle bus is connected and talking at all — or a record on an
   `arm_channels` channel (`RPM` by default) whose value clears
   `arm_threshold` (default `0.0`). The operator's deciding factor is bus
   connectivity: plugged in with the bus alive → record, KOEO diagnostics
   included; bench / key-out / unplugged → the bus is silent, and Pi-side
   sources that still chatter (GPS, IMU, derived) never count as evidence.
   `SPEED` was dropped from the channel defaults at the same time — it
   isn't engine evidence (towing, coasting engine-off). Installs on
   vehicles whose bus stays awake while parked can set `arm_sources: []`
   to gate on channel values alone. Arm: the first record carrying
   evidence arms the gate. Disarm: at a session-rotation
   boundary — the *existing* `rotate_session()` call already triggered by
   `_rotate_if_engine_run_time_reset` every key cycle (a RUN_TIME reset) —
   or (v2, 2026-07-26) after `disarm_after_seconds` (default 300 s) of
   records carrying no arm-channel evidence.

   v1 was rotation-only: a continuous disarm-on-idle-tick design was
   rejected because a brief RPM dip mid-drive (e.g. a stall, or idling at
   a light) would start silently dropping data right when a fault might be
   happening. Field data then exposed the gap rotation-only leaves: disarm
   needs the *next* key-on, so key-off with the Pi still powered (UPS,
   bench) never disarms — a post-drive parked night recorded 23 h of
   stationary GPS chatter (2 M rows) that went on to dominate training
   memory. The sustained-silence timeout closes that without
   re-introducing the stall concern: the window is minutes rather than
   ticks, an idling engine keeps emitting RPM > 0 so its evidence never
   goes stale, and a restart's cranking RPM re-arms instantly. Ages are
   computed from record timestamps (not wall clock), so replay gates
   identically to live. `disarm_after_seconds: null` restores the v1
   rotation-only behavior.
2. **Pre-arm buffer handling.** Dropped, not buffered — no ring buffer in
   v1. This follows the same "fix at source, quarantine bad data" project
   convention the rest of the pipeline uses: KOEO chatter is a data-*volume*
   nuisance, not a fault the daemon needs to reconstruct the seconds before.
   If a future need for "the 30 s before the anomaly" shows up, that's a
   bounded ring buffer as its own follow-up — bolting it onto this gate
   would trade a data-volume problem for a memory-bound / eviction-policy
   one.

Config knobs: `collection.record_arming.enabled` (bool, default `false`),
`arm_sources` (default `("can", "obd")`; `[]` = channel gating only),
`arm_channels` (default `("RPM",)`), `arm_threshold` (default `0.0`),
`disarm_after_seconds` (default `300.0`; `null` = rotation-only). `RecordArmingGate.dropped_count` is tracked in-process for
diagnosability but is not yet surfaced on the dashboard/status endpoint —
a reasonable v2 addition once the gate has real drive-cycle mileage on it.

Live verification against the real daemon/truck is explicitly out of
scope for the initial landing (an idle-data-volume problem, not a
correctness one) — covered by unit tests on `RecordArmingGate` and
`ParquetTelemetryWriter` fixtures only
(`tests/test_record_arming.py`, `tests/test_parquet_writer.py`).
