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

1. **Arm/disarm semantics.** Arm: the first record on a configured channel
   (`RPM`/`SPEED` by default — the same signal family
   `classify_vehicle_state` uses to call a vehicle "running", not the same
   code path — see the module docstring) whose value clears
   `arm_threshold` (default `0.0`, i.e. any nonzero reading) arms the gate
   for the rest of the current session. Disarm: only at a session-rotation
   boundary — the *existing* `rotate_session()` call already triggered by
   `_rotate_if_engine_run_time_reset` every key cycle (a RUN_TIME reset).
   The gate **never re-disarms mid-session**. A continuous
   disarm-on-idle-tick design was considered and rejected: a brief RPM dip
   mid-drive (e.g. a stall, or idling at a light) would start silently
   dropping data again right when a fault might be happening, and avoiding
   that would need the pre-arm-buffer machinery from #2 anyway — unwarranted
   complexity for a "KOEO produces hours of low-value idle data" problem,
   which "wait for the vehicle to move, once, per key cycle" already
   solves without it.
2. **Pre-arm buffer handling.** Dropped, not buffered — no ring buffer in
   v1. This follows the same "fix at source, quarantine bad data" project
   convention the rest of the pipeline uses: KOEO chatter is a data-*volume*
   nuisance, not a fault the daemon needs to reconstruct the seconds before.
   If a future need for "the 30 s before the anomaly" shows up, that's a
   bounded ring buffer as its own follow-up — bolting it onto this gate
   would trade a data-volume problem for a memory-bound / eviction-policy
   one.

Config knobs: `collection.record_arming.enabled` (bool, default `false`),
`arm_channels` (default `("RPM", "SPEED")`), `arm_threshold` (default
`0.0`). `RecordArmingGate.dropped_count` is tracked in-process for
diagnosability but is not yet surfaced on the dashboard/status endpoint —
a reasonable v2 addition once the gate has real drive-cycle mileage on it.

Live verification against the real daemon/truck is explicitly out of
scope for the initial landing (an idle-data-volume problem, not a
correctness one) — covered by unit tests on `RecordArmingGate` and
`ParquetTelemetryWriter` fixtures only
(`tests/test_record_arming.py`, `tests/test_parquet_writer.py`).
