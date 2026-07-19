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

One residual design task remains open: **8.6 — operator-intent /
idle-gating for auto-started sessions** (tracked in
`docs/implementation-tasks.md`). The original diagnosis and fix plan are
in git history for this file.
