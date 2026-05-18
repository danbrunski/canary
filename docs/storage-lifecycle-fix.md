# Storage Lifecycle & Disk Safety — Fix Plan

Derived from the 2026-05-17 disk investigation. Two defects, disk-bounding
first.

## What's actually wrong

### A. Retention exists but is not wired into the production daemon (P0)

`delete_expired_sessions()` (`src/storage/parquet_writer.py:199`) is correct
and tested, but the production run path never calls it:

* `src/main.py:run()` defaults to `full_daemon=True` →
  `src/daemon.py:run_daemon()`. `run_daemon` builds the writer
  (`daemon.py:683`), spins `drain_queue_with_broadcast`, `poll_model_managers`,
  inference, and the dashboard — **and never calls
  `delete_expired_sessions`**. `session_retention_days: 30` is dead config.
* The only caller is `src/main.py:204`, inside `run_collection()` — reached
  only via `--collection-only` / `full_daemon=False` (test/headless paths),
  *not* production. Even there it runs **once, in the shutdown `finally`
  block**. Done-Definition v1 #2 requires "≥1 week without manual
  intervention" — a process that never restarts never sweeps, even on the
  non-production path. The sweep also fires *after* the disk may already be
  full, which is too late.

### B. No byte cap — there is no real ring buffer (P0)

Task 1.12 is titled "Ring buffer" and `daemon-architecture.md:98` claims
retention "sweeps old sessions on shutdown", but the implementation is
**age-based only**. Nothing bounds total bytes:

* `ParquetTelemetryWriter` appends chunks indefinitely; engine-run-time reset
  (`parquet_writer.py:118` `_rotate_if_engine_run_time_reset`) spawns *new*
  session dirs, so dirs multiply even within one daemon run.
* There is no `storage_max_mb` config and no eviction-by-size function.
* Measured KOEO rate ≈ 52 MB/h ≈ 1.25 GB/day; `/` has ~46 GB free ⇒ ~37 days
  of runway, faster engine-on. With no automatic cleanup the SD card
  eventually fills; a full disk → failed Parquet writes → the unclean state
  that already corrupted the git repo.

### C. Low-space guard only logs (P1)

`warn_if_storage_low()` (`src/storage/monitor.py:27`) logs a line and returns
the free MiB. `drain_queue_with_broadcast` (`daemon.py:239/249/270`) calls it
at start, on each flush-due timeout, and at close, and does nothing with the
result. It never stops, rotates, or deletes — it writes until physically
full.

### D. Sessions auto-start with no operator intent (P2, secondary)

`run_daemon` constructs `ParquetTelemetryWriter(storage_path)`
unconditionally (`daemon.py:683`), so a session dir is created and recording
begins whenever the daemon runs. KOEO the engine ECUs sleep but body/chassis
CAN keeps chattering, so the bus is never idle and full-rate low-value data
accumulates. This is partly *by design* (Done-Definition v1 #2 wants
continuous logging) — the fix is intent-gating, not disabling capture.

## The fix

The existing pin mechanism is the safety contract and must be preserved:
a session dir containing `.canary-pin` (created by `canaryctl sessions pin`)
is never auto-deleted. `delete_expired_sessions` already honors it; every new
eviction path must too. The **active** session dir must also never be
deleted, and because engine-run-time reset moves the active dir
(`rotate_session`), the sweeper must read `writer.paths.root` *live* each
tick, not a startup snapshot.

### 1. Periodic retention sweeper in the production daemon (P0)

Add `run_retention_sweeper(...)` to `src/daemon.py`, modeled exactly on
`poll_model_managers` (`daemon.py:550`): a `while not stop_event.is_set()`
loop with `asyncio.wait_for(stop_event.wait(), timeout=interval)` cadence,
launched as a task in `run_daemon` alongside `poll_task`, with the same
cancel/await shutdown handling (`daemon.py:761`). Each tick:

1. age sweep: `delete_expired_sessions(storage_path, session_retention_days,
   protect={writer.paths.root})`;
2. byte cap (step 2 below);
3. structured log of what was deleted and reclaimed.

Cadence is a new `daemon.retention_sweep_seconds` (default `3600`), next to
`model_poll_seconds`. Add a `protect: set[Path] | None` parameter to
`delete_expired_sessions` to exclude the live dir (pinned dirs already
excluded). Keep `run_collection`'s shutdown sweep as a final pass for parity,
but it is no longer the only sweep.

### 2. Byte-bounded storage cap — the real ring buffer (P0)

Add `collection.storage_max_mb` (Pydantic + every `canary*.example.yaml`;
suggested Pi default sized to the SD card, e.g. a few GB — chosen with
headroom below the partition size). Add
`enforce_storage_cap(sessions_root, max_mb, *, protect=...)` to
`parquet_writer.py`: if total bytes under `sessions_root` exceed `max_mb`,
delete oldest-first (by timestamp dir name) **unpinned, non-protected**
session dirs until under cap or only pinned/active remain; return the deleted
list. Called from the sweeper after the age sweep. This is the bounded ring
buffer 1.12 claimed.

### 3. Acting low-space guard (P1, optional half)

When the sweeper observes `free_space_mb(storage_path) < storage_warning_mb`,
run an emergency oldest-first eviction (same pin/active rules, aggressive
target) and log structured before/after. `warn_if_storage_low` stays the
cheap per-flush log so the hot drain path remains non-blocking and log-only;
the *acting* logic lives only in the sweeper.

### 4. Tests (P0/P1)

* `enforce_storage_cap`: eviction order, pin exclusion, active/protect
  exclusion, stop-when-only-pinned.
* sweeper task: interval honored, age + cap both run, emergency path
  triggers below threshold, active dir survives across an engine-run-time
  rotation.
* regression: assert the assembled `run_daemon` task set includes the
  retention sweeper — the missing-wiring gap that let this ship.

### 5. Config + doc reconciliation (P0)

* `storage_max_mb` (under `collection:`) and `retention_sweep_seconds`
  (under `daemon:`) added to `src/config.py` and **all** of
  `canary.example.yaml`, `canary.pi.example.yaml`, `canary.dev.example.yaml`,
  `canary.sim.example.yaml`, plus `docs/CANary-overview.md` (Cross-Cutting
  "Configuration drift" rule — no magic constants).
* Fix `docs/daemon-architecture.md:98`: retention is a periodic in-process
  sweep with a byte cap, **not** "sweeps old sessions on shutdown" (false on
  the production path).
* Annotate `implementation-tasks.md` 1.12/1.13 as regressed/superseded by
  Phase 8; add Risk rows.

### 6. Operator-intent / idle-gating (P2, secondary)

Design task. Optional record-arming gate: defer opening a full-rate session
until a "vehicle active" signal (engine RPM > 0, vehicle speed, or a
configurable activity predicate), so KOEO body/chassis chatter doesn't
accumulate full-rate sessions — without breaking Done-Definition v1 #2
continuous logging. Carries open questions (what arms/disarms, how to treat
the pre-arm buffer); documented, not necessarily all implemented in v1.

## Sequence

8.1 → 8.2 → 8.4 → (8.3 ∥ 8.5) → 8.6
