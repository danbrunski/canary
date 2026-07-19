# Roadmap — refactoring: dedup, structure, dead code

Status: **planned** (2026-07-18). Straight to `main`, no feature branches.

## Why this exists

A four-area refactor review (collection/daemon, models/analysis,
HTTP+MCP layer, frontend+scripts) on 2026-07-18. Overall shape: prior
consolidation passes exist and worked (FE `util/*`, `sequence_training.py`,
thin `tools/` wrappers, `BusReconnector`) — but several stopped one step
short, leaving scaffolding built and never wired through
(`sequence_registry.py`, `manifest.py` validation-without-IO,
`services.py` as an empty seam). Remaining debt is concentrated,
mostly mechanical, ~2,000–2,500 removable lines.

**Four duplications have already drifted** — those are latent bugs, not
style issues, and go first.

## Guiding constraints

- Whole-repo gate per commit: `ruff check . && ruff format --check . &&
  mypy src && pytest` (+ `pnpm test` for FE phases).
- Behavior-preserving refactors only; when a phase touches serve-path
  code (Phase 1.1, Phase 3), replay a pinned session before/after and
  diff events.
- Transports stay first-class and symmetric — the ELM327 path is
  **kept**, its duplicated machinery deduped (no "legacy is dead"
  editorializing in code or docs).
- **Coordinate before touching `src/mcp_server/server.py`** — another
  session has active work there (Phase 2.2 blocks on it).
- Don't move typed Python tables to YAML (`_MODE01_SPEC`, `refit.py`
  recipe catalog) — that trades away mypy coverage; prefer in-Python
  helpers/kwargs cleanups.
- Public APIs re-exported via `src/models/__init__.py` etc. keep
  working — add compatibility shims where imports are widespread.

---

## Phase 1 — Drifted duplications (correctness-relevant)

- [ ] **1.1 — Single-source the PID tables.** `PID_DECODERS`
  (`src/collection/can_obd_client.py:517-556`, 37 entries) re-types the
  channel names/units that `MODE01_REGISTRY`
  (`src/collection/obd_registry.py:373-1150`) also carries; nothing
  asserts agreement, and the scheduler reads names from one table
  (`obd_scheduler.py:443`) but poll tiers from the other (`:462-466`).
  Make the registry canonical; derive the decoder map from it (or
  generate the registry's name/unit columns from `PID_DECODERS`) and
  add a startup assertion. Keep a `PID_DECODERS` shim — it is imported
  by `can_obd_collector.py:91` and `obd_scheduler.py:31`.
- [ ] **1.2 — De-duplicate companion ↔ control routes.**
  `src/companion/routes.py` re-registers ~15 `/control/*` endpoints
  from `src/dashboard/routes/control.py` (sessions list/get/export/
  metadata/pin/unpin/delete `:147-216` vs `:262-413`; jobs `:254-298`
  vs `:478-526`; training-runs, datasets/health) and has already
  diverged (companion pin refreshes the library index + passes
  `metadata: {}`). Extract backend-agnostic handler functions
  (parameterized by resolver + serializer) into
  `src/companion/services.py` (the intended seam) and call them from
  both registrars. Also: replace the hand-rolled urllib client
  (`services.py:188-231`) with the existing `DaemonClient`
  (`src/mcp_server/client.py`) or a shared HTTP-client module — two
  daemon-client implementations exist today.
- [x] **1.3 — One `_numeric_value_columns` + shared resample/segment
  prefix.** The helper is defined twice with drifted bodies
  (`src/analysis/session_classifier.py:316` lacks the `GAP_SUFFIX`
  exclusion `src/models/sequence_dataset.py:385` has). Extract one
  canonical version (likely `src/preprocessing/`), and a shared
  `resample_and_segment(sessions, config)` for the hand-rolled
  `read_sessions → to_wide → resample_wide → segment_operating_modes`
  chain duplicated at `sequence_dataset.py:190-211` and
  `session_classifier.py:210-227`. Callers keep their own empty-frame
  fallbacks. — landed: canonical `numeric_value_columns` +
  `resample_and_segment` in `src/preprocessing/loader.py`; both
  callers switched over; closes the `GAP_SUFFIX` drift (gap flags no
  longer counted as feature columns in session-classifier quality
  stats).
- [x] **1.4 — Shared async reconnect loop.** The
  `while not stop_event…try build→run…except→backoff` skeleton is
  copied 4×: `can_collector.py:125-175`, `obd_collector.py:407-452`,
  `can_obd_collector.py:311-376`, `gps_collector.py:171-221` — and the
  GPS copy **doesn't re-raise `ValueError`** (bad config spins forever
  instead of failing fast). Extract
  `run_with_reconnect(build_and_run, *, stop_event, policy, teardown,
  reraise=(ValueError,))` into `src/collection/reconnect.py` (the
  `BusReconnector` precedent lives there); all four collectors adopt
  it; GPS gains fail-fast. This closes the deferred CAN-reconnect
  dedup (memory note) — ELM327 path included, not deleted. — landed:
  `run_with_reconnect` in `src/collection/reconnect.py`; all four
  collectors (`can_collector`, `obd_collector`, `gps_collector`,
  `can_obd_collector`) adopted; GPS now re-raises `ValueError`
  immediately instead of retrying it.

## Phase 2 — Big mechanical wins

- [ ] **2.1 — Unify the sequence trainers.** `scripts/train_lstm.py`
  (542) vs `train_tcn.py` (524): ~90% identical (`_print_evaluation`
  48 lines and `_resolve_training_sessions` 17 lines byte-identical;
  `train`/`build_dataset`/`parse_args`/`main` differ only in
  identifiers). `src/models/lstm_training.py` vs `tcn_training.py`:
  cloned manifest dataclass (`to_dict`/`from_dict` byte-identical;
  TCN's own comment at `tcn_training.py:73-78` admits
  field-compatibility), cloned save/load/validate scaffolding, cloned
  coercion helpers. Build one `scripts/train_sequence.py --backbone
  {lstm,tcn}` + one `SequenceAutoencoderManifest` + per-backbone
  registry entries in the existing `src/models/sequence_registry.py`
  (built for exactly this, never wired). **Preserve the two real
  divergences as backbone hooks:** LSTM `strict=False` decay-key load
  tolerance (`lstm_training.py:440-446`) and its
  calibrated-without-scaler guard (`:496-502`). Keep
  `train_lstm.py`/`train_tcn.py` as 5-line shims (retrain.py and the
  jobs allow-list import them: `scripts/retrain.py:43-45`,
  `src/control/jobs.py`). ~850 lines removed.
- [ ] **2.2 — MCP tool registration table.** *(Blocked on coordination
  — active work in `server.py` from another session.)* 34 of 48
  `@mcp.tool()` closures are pure delegation with docstrings
  duplicated from the `tools/`/`write_tools/` modules. Replace with a
  registration table (`(delegate, needs_audit, gate)`) using
  `functools.wraps` so FastMCP infers schema + docstring from the
  delegate — the module docstring becomes the single source. Keep the
  ~10 conditionally-registered and 2 real-logic wrappers hand-written.
  ~300-400 lines.
- [ ] **2.3 — `audited_post` helper for write tools.** The identical
  12-line build-body/try-post/audit-error/audit-ok block appears ~15×
  across `src/mcp_server/write_tools/*.py`. One helper collapses each
  to ~2 lines (~150 lines). Also move the two pure reads
  (`get_current_capture`, `get_replay_status`) out of `write_tools/`.
- [ ] **2.4 — Shared script CLI preamble.** `scripts/_cli.py` with
  `add_common_args(parser)`, `load_config(args)`,
  `resolve_sessions(args)`: 13 scripts hand-roll
  `CanaryConfig.from_yaml` + `discover_sessions` + `--config/--days`
  argparse; `_resolve_training_sessions` is pasted 3×
  (`train_lstm.py:460`, `train_tcn.py:442`, `sequence_bakeoff.py:386`);
  `label_sessions.py:93` has a private `_discover_sessions` copy;
  `_filter_sessions_by_age` is a private helper cross-imported by
  `retrain.py:46`; 5 scripts hand-roll the `sys.path` bootstrap.
  (2.1 shrinks this list first — do 2.1 → 2.4.)
- [ ] **2.5 — `control.py` route boilerplate.** In
  `src/dashboard/routes/control.py`: `resolve_session → 404` pasted 6×
  (`:274-277, 343-346, 358-361, 374-377, 389-392, 410-413`) → one
  FastAPI dependency; six identical null-guard closures (`:117, 122,
  127, 219, 572, 619`) → one generic `_require(component, name)`;
  standardize the 20 `publish_control_event(...); return` tails.
  ~150-200 lines.

## Phase 3 — Structural (do deliberately, pair with related feature work)

- [ ] **3.1 — Detector protocol in `src/daemon/inference.py`.**
  `run_live_inference` (`:48-239`, 191 lines) runs IF/sequence/behavior
  inline, each with the same observe → per-source emit → persistence →
  fusion-source shape (`:110-129`, `:131-166`, `:171-213`) converging
  on a shared fuse tail (`:215-232`); `_score_and_emit`
  (`:529-681`, 152 lines) mixes scoring, health, missing-feature
  fail-safe, contributor ranking, and emit. Extract a `Detector`
  protocol (`tick(row, ctx) -> list[FusionSource]`) + three detector
  classes + the shared `gate_and_collect` pairing
  (`downgrade_unsustained(...)` pattern at `:123-128`, `:161-165`).
  **Time this alongside `ml-correctness-hardening.md` Phase 6**
  (persistence semantics) — one restructure, one behavior change, one
  replay verification.
- [ ] **3.2 — Split the FE god-panels.** `ControlPanel.tsx` (580, ~8
  concerns) → `SessionsCard`/`JobsCard`/`SequenceDetectorCard`/
  `ModelUploadCard`/`CompanionConfig` (only 2 cards extracted today);
  move its local `parseJsonObject`/`fileToBase64`/`currentPiHost`
  (`:552-576`) to `util/`. `TestSessionPanel.tsx` (605) → adopt
  `useAsyncAction`/`useControlResource` (it predates them and
  hand-rolls both, `:114-137`, `:162-246`) + split `SetupScreen` /
  `DriveScreen`.
- [ ] **3.3 — Shared FE gate components.** `<ResourceGate>` for the
  token/loading/error 3-state repeated in 6 panels
  (`ProfilesPanel.tsx:42-52`, `TuningPanel.tsx:52-62`,
  `DatasetHealthPanel.tsx:35-45`, `PowerPanel.tsx:83-93`,
  `TrainingPanel.tsx:200-210`, `DataSourcesPanel.tsx:167-177`);
  a shared `TokenField` (hand-rendered divergently in
  `ControlPanel.tsx:268-308` and `TestSessionPanel.tsx:281-306`);
  optionally a multi-fetch `useControlResources` for the two panels
  that hand-roll `Promise.all` polling.
- [ ] **3.4 — `JsonlStore` base.** Nine append-only JSONL
  implementations; `FeedbackStore` (`src/alerts/feedback.py`),
  `TrainingLedger` (`src/models/training_ledger.py`), and the job
  ledger (`src/control/jobs.py:426-440`) are structurally identical
  (append+flush, tolerant `_iter_raw`, latest-per-key collapse); only
  `ControlAuditLogger` has a lock. One generic base unifies torn-line
  semantics; day-rotating `AnomalyEventLogger` and the audit loggers
  subclass or stay.
- [ ] **3.5 — Split `isolation_forest.py` + shared artifact IO.**
  804 lines mixing dataclasses/manifest (`:76-352`), training
  (`fit_isolation_forest` `:355-532`, 178 lines — extract the per-mode
  calibration block `:427-483`), and artifact IO (`:535-586`).
  Add `write_artifact`/`read_artifact` + one `manifest_path_for()` to
  `src/models/manifest.py` — the save/load skeleton exists 3×
  (isolation_forest, lstm_training, tcn_training),
  `default_*_manifest_path` 3×, and the `.with_suffix(".manifest.json")`
  literal in 6 more places (`control/models.py:25`,
  `control/preflight.py:110`, `control/sequence_detector.py:128`,
  `companion/routes.py:371`, `models/manager.py:86`). Folds into 2.1's
  unified manifest.

## Phase 4 — Quick sweeps (each ≤ an hour, batchable)

- [ ] **4.1 — Delete/demote `run_live_inference_lstm`.**
  `src/daemon/inference.py:272-352` — production-dead (only its own
  tests reference it; `runtime.py:171` spawns only
  `run_live_inference`). ~80 lines.
- [ ] **4.2 — `atomic_write_text(path, text)` util.** 9-10 copies of
  tmp+`replace` (`control/sessions.py:116`, `alerts/tuning.py:164`,
  `profiles.py:135`, `dashboard/ups.py:83`,
  `collection/custom_pids.py:313`, `preprocessing/feature_cache.py:138`,
  `analysis/gps_tracker.py:148`, `analysis/session_classifier.py:265`,
  `control/models.py:32`; binary variant `storage/parquet_writer.py:272`
  stays).
- [ ] **4.3 — `parse_iso_utc` into `src/timefmt.py`** (the designated
  home): the `fromisoformat(ts.replace("Z", "+00:00"))` idiom at
  `dashboard/status.py:273`, `mcp_server/tools/search.py:705`,
  `dashboard/readers.py:156`, +.
- [ ] **4.4 — Coercion/torch-guard helpers.** `coerce_timestamp` ×3
  (`sequence_training.py:519` canonical; `isolation_forest.py:772`,
  `daemon/inference.py:519`); `_optional_str` ×5, `_optional_float`
  ×2, `_required_int` ×2; `_torch_module` ×5 →
  `sequence_training.torch_module()` (per-backbone error types
  collapse with 2.1). ~120 lines total with 4.2/4.3.
- [ ] **4.5 — FE formatter sweep.** SystemPanel's 8 local `format*`
  helpers, ModelHealthPanel's shadowing `formatPercent`, and the 3×
  `formatDuration` (PowerPanel, SystemPanel, `util/time.formatElapsed`)
  → `util/format.ts` / `util/time.ts`.
- [ ] **4.6 — Shared vitest harness.** `test/harness.ts` with
  `mockHttp(names)` + `HttpErrorStub` — the hoisted-mock +
  reset/clear lifecycle is byte-identical in 6 test files
  (`PowerPanel.test.ts:7-25`, `ProfilesPanel.test.ts:6-24`, …).
- [ ] **4.7 — Misc singles.** `CollectorStarter` alias defined twice
  (`main.py:34`, `collection/replay.py:36`); `_need()` decoder guard
  twice (`can_obd_client.py:372`, `obd_registry.py:135`); shared
  parse-error counter for the 3× bounded-log pattern
  (`can_obd_collector.py:679-703`, `can_collector.py:104-121,294-315`
  — mirror `QueueDropCounter`, `queueing.py:17-36`); threadsafe-enqueue
  factory for the 3 copies (`obd_collector.py:400`,
  `can_obd_collector.py:319-329`); move `_default_collectors` from
  `main.py` into `src/collection/` (daemon→main import inversion,
  `daemon/runtime.py:83`); `list_events_by_kind` hand-builds a query
  string instead of `params=` (`mcp_server/tools/classification.py`).

## Explicitly no-action (reviewed and fine)

- `scripts/retrain.py` — real orchestration; dispatches to trainers as
  libraries, no reimplementation.
- `api/http.ts` — long by design (one function per route, consistent
  `fetchJson`/`controlJson` use). Optional `controlBlob` for the one
  hand-rolled download.
- `scripts/ups_shutdown_watch.py` — single-file by design (root
  systemd, minimal deps), well-sectioned.
- `tools/` RE utilities — thin wrappers over `src/analysis/*`.
- `dashboard/schemas.py`, `analysis/refit.py::fit_recipe` — clean.
- **Rejected ideas:** `_MODE01_SPEC` or the recipe catalog to YAML
  (loses type-checking; if touched, prefer kwargs-per-row / a
  `_warmup_recipe()` helper to kill the 3× warmup-recipe clone);
  deleting the ELM327 path (transports stay symmetric — dedupe via
  1.4 instead).

## Sequencing

1 (drift fixes) → 2.1 → 2.4 (preamble shrinks after trainer unify) →
2.3/2.5 → 4.x batch anytime → 3.1 with ML-hardening Phase 6 →
3.2/3.3 with the next FE feature work → 2.2 when the other session's
`server.py` work lands → 3.4/3.5 opportunistically.

## Cross-cutting acceptance

- [ ] Whole-repo gate green per commit; FE phases also `pnpm test` +
  `pnpm typecheck` (re-run `TuningFeedback.test.ts` on flake).
- [ ] Serve-path refactors (1.1, 3.1): pinned-session replay
  before/after with event diff — behavior-preserving means zero event
  changes.
- [ ] Line-count receipts: note the actual reduction per phase in the
  commit message (target ~2,000-2,500 total across Phases 1-4).
- [ ] No public-API breaks: `src/models/__init__.py` re-exports,
  `PID_DECODERS` imports, and jobs-allow-list script names keep
  working via shims until callers migrate.
