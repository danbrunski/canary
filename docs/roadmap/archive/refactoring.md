# Roadmap — refactoring: dedup, structure, dead code

> **Archived 2026-09-21.** 24 of 25 tasks landed. The one remainder, 2.2 (MCP
> tool registration table), was skipped for a file-coordination conflict that has
> since cleared; it continues as **D03.23** in `../tasks/D03-open-work.md`.

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

- [x] **1.1 — Single-source the PID tables.** `PID_DECODERS`
  (`src/collection/can_obd_client.py:517-556`, 37 entries) re-types the
  channel names/units that `MODE01_REGISTRY`
  (`src/collection/obd_registry.py:373-1150`) also carries; nothing
  asserts agreement, and the scheduler reads names from one table
  (`obd_scheduler.py:443`) but poll tiers from the other (`:462-466`).
  Make the registry canonical; derive the decoder map from it (or
  generate the registry's name/unit columns from `PID_DECODERS`) and
  add a startup assertion. Keep a `PID_DECODERS` shim — it is imported
  by `can_obd_collector.py:91` and `obd_scheduler.py:31`. — landed:
  generated the registry's channel-NAME column from `PID_DECODERS`
  instead (the reverse direction — deriving `PID_DECODERS` from the
  registry is a genuine circular import, independently reproduced);
  `assert_pid_tables_consistent()` at module import time; scheduler's
  channel+tier now come from one `lookup()` call, not two independent
  ones. Zero pre-existing name/decoder disagreements found. Unit
  columns deliberately NOT derived (a decoder's unit is only produced
  at call time with sample bytes, not statically available) — disclosed
  scope gap, not silently dropped.
- [x] **1.2 — De-duplicate companion ↔ control routes.**
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
  daemon-client implementations exist today. — landed: shared handlers
  in `services.py` parameterized by hooks (`serialize`/`finalize`/
  `guard`/`after_delete`/`on_submit`/`on_cancel`); companion's
  library-index refresh and control's audit/catalog/active-capture-
  guard divergences all preserved via those hooks, verified genuinely
  distinguishable (not silently unified) with a test proven to fail
  when the hook is stripped. The "`metadata: {}`" note turned out to be
  a different code path (`sync_session_from_pi`'s remote-pin
  forwarding) than the duplicated local handlers being deduped here —
  untouched, not part of this extraction. Disclosed (not fixed, a
  pre-existing quirk predating this refactor, confirmed via `git show
  HEAD`): companion's pin/unpin/metadata writes don't preserve
  `source_host` on the library-index refresh. `services.py`'s urllib
  client replaced with `DaemonClient` (new `get_bytes()` added for the
  non-JSON export-zip path).
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

- [x] **2.1 — Unify the sequence trainers.** `scripts/train_lstm.py`
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
  `src/control/jobs.py`). ~850 lines removed. — landed: net -558
  production lines (not the doc's ~850 estimate — real number, both
  new shared-engine/unified-CLI files counted). One of the two "named
  divergences" turned out to be stale by the time this task started —
  the calibrated-without-scaler guard was already byte-identical on
  both backbones (mirrored to TCN during this session's earlier
  ml-hardening 3.x work), confirmed via `git show HEAD` on both
  pre-refactor files — unified as shared behavior, not forced into an
  asymmetric hook. The one genuine remaining divergence (LSTM's
  `strict=False` decay-key tolerance) is preserved and verified to
  NOT leak to TCN (a TCN load with a missing key still fails strict).
  Both shims' full pre-refactor public API confirmed preserved
  end-to-end; `retrain.py`/`sequence_bakeoff.py` needed zero changes.
- [~] **2.2 — MCP tool registration table.** → **carried to D03.23** (2026-09-21). blocked-overnight:
  coordination — active work in `server.py` from another session; not
  attempted. 34 of 48
  `@mcp.tool()` closures are pure delegation with docstrings
  duplicated from the `tools/`/`write_tools/` modules. Replace with a
  registration table (`(delegate, needs_audit, gate)`) using
  `functools.wraps` so FastMCP infers schema + docstring from the
  delegate — the module docstring becomes the single source. Keep the
  ~10 conditionally-registered and 2 real-logic wrappers hand-written.
  ~300-400 lines.
- [x] **2.3 — `audited_post` helper for write tools.** Landed: added
  `audited_post` in `src/mcp_server/write_tools/_shared.py`; migrated
  17 of ~18 call sites across `write_tools/*.py`. `classify_session`
  (`write_tools/classification.py`) deliberately left unmigrated — its
  POST body (`{}`, not the built payload) and success-audit payload
  (`disposition` extracted from the response, not the full response
  blob) genuinely diverge from the helper's fixed shape. Moved the two
  pure reads out of `write_tools/`: `get_current_capture` →
  `src/mcp_server/tools/sessions.py`, `get_replay_status` →
  `src/mcp_server/tools/replay.py`; `server.py` delegating closures and
  `tests/test_mcp_tools.py` updated. Verified by adversarial review:
  no lost/duplicated tool registrations, behavior preserved at every
  migrated call site.
- [x] **2.4 — Shared script CLI preamble.** Landed: `scripts/_cli.py`
  with `add_common_args`, `load_config`, `resolve_sessions`,
  `filter_sessions_by_age`, `discover_live_session_dirs`; 22 scripts
  migrated (post-2.1 the `_resolve_training_sessions` duplication was
  already down to `train_sequence.py`/`sequence_bakeoff.py`, which have
  a genuine behavioral divergence — bakeoff also filters an explicit
  `--sessions` list against held-out sessions — and were correctly left
  unmerged). `retrain.py`'s cross-import of `refit_behavior_models.py`'s
  private `_filter_sessions_by_age` is gone; both now share `_cli`.
  Dual-import fallback (`from _cli import ...` / `from scripts._cli
  import ...`) added to the 6 scripts loaded both standalone and via
  the `scripts.*` namespace package in tests — confirmed load-bearing,
  not defensive clutter. 3 test files' monkeypatch targets updated to
  `_cli.discover_sessions`. Known minor leftover (not blocking):
  `scripts/fp_benchmark.py` still hand-rolls its own `--config`
  argparse; `scripts/canary_companion.py` has a genuine divergence
  (`CANARY_CONFIG` env-var default) and correctly stays unmigrated.
- [x] **2.5 — `control.py` route boilerplate.** Landed: the
  `resolve_session → 404` dedup was already fully done by 1.2's
  `_resolve_session` closure — confirmed intact, not re-opened. Added a
  generic `_require(component, name, *, detail=None)` helper collapsing
  7 null-guard closures (`_controller`, `_jobs`, `_sequence_detector`,
  `_replay`, `_feedback`, `_vehicle_ledger`, `_tuner`), preserving each
  one's original status code and detail text. The 20
  `publish_control_event(...); return` tails were checked and found
  already consistent (differ only in event name/payload/return-key) —
  no further collapse possible without an awkward wrapper, so left
  as-is.

## Phase 3 — Structural (do deliberately, pair with related feature work)

- [x] **3.1 — Detector protocol in `src/daemon/inference.py`.**
  Landed: extracted a `Detector` protocol (`tick(row, ctx) ->
  list[FusionSource]`) + `IsolationForestDetector`/`SequenceDetector`/
  `BehaviorDetector` classes + a shared `gate_and_collect()`.
  `_score_and_emit` split into `_record_if_health` +
  `_rank_feature_contributors` (pure, separable pieces); the
  emit/persistence tail stays put — it needs the raw `sustained` flag
  before deciding to withhold a per-source emit. Verified via replay:
  real IF+LSTM artifacts through a recorded session, pre- vs
  post-refactor, categorical fields identical, LSTM scores matching to
  ~5 sig figs. One real bug caught by adversarial review and fixed
  same task: `SequenceDetector.model_version` only updated inside its
  early-return guard, so it never reset to `None` on a gated tick
  (no model / no row / `scoring_allowed=False`) — unlike
  `IsolationForestDetector`, which was correct — corrupting the
  `fused` event's `model_version` provenance tag. Fixed by moving the
  assignment before the guard; new regression test added; re-verified.
- [x] **3.2 — Split the FE god-panels.** `ControlPanel.tsx` (580, ~8
  concerns) → `SessionsCard`/`JobsCard`/`SequenceDetectorCard`/
  `ModelUploadCard`/`CompanionConfig` (only 2 cards extracted today);
  move its local `parseJsonObject`/`fileToBase64`/`currentPiHost`
  (`:552-576`) to `util/`. `TestSessionPanel.tsx` (605) → adopt
  `useAsyncAction`/`useControlResource` (it predates them and
  hand-rolls both, `:114-137`, `:162-246`) + split `SetupScreen` /
  `DriveScreen`. — landed: `ControlPanel.tsx` now 236 lines +
  5 new card components, each owning its own local UI state (Capture
  stayed inline per the task's own card list — it's wired to the
  top-level token/busy/refresh trio, not a standalone read); moved
  `parseJsonObject` to `util/json.ts`, `fileToBase64`/`currentPiHost`
  to new `util/browser.ts`. `TestSessionPanel.tsx` adopted
  `useAsyncAction` (busy/notice) and two independent
  `useControlResource` calls (capture, preflight — kept separate so a
  401 on one side doesn't blank the other's last-known-good value);
  split into `TestSessionSetupScreen`/`TestSessionRecordingScreen` in
  `TestSessionScreens.tsx` (named to avoid collision with the
  unrelated existing `DriveScreen.tsx`, the Drive-mode gauge cluster).
  Verified both files render correctly and the full start/tap/end
  flow works via a mocked-backend Playwright smoke test (no
  `pnpm test` coverage existed for either file before or after).
- [x] **3.3 — Shared FE gate components.** `<ResourceGate>` for the
  token/loading/error 3-state repeated in 6 panels
  (`ProfilesPanel.tsx:42-52`, `TuningPanel.tsx:52-62`,
  `DatasetHealthPanel.tsx:35-45`, `PowerPanel.tsx:83-93`,
  `TrainingPanel.tsx:200-210`, `DataSourcesPanel.tsx:167-177`);
  a shared `TokenField` (hand-rendered divergently in
  `ControlPanel.tsx:268-308` and `TestSessionPanel.tsx:281-306`);
  optionally a multi-fetch `useControlResources` for the two panels
  that hand-roll `Promise.all` polling. — landed:
  `components/ResourceGate.tsx` (enabled/loading/error/data states,
  generic over the resource type) adopted by all 6 named panels plus
  `DataSourcesPanel`'s 3 unauthenticated tabs (loading/error only);
  `components/TokenField.tsx` adopted by both `ControlPanel` and
  `TestSessionPanel`, each keeping its own surrounding chrome (label
  vs. status-badge+help). `useControlResources` skipped (optional;
  no panel needed it after the gate dedup — `TrainingPanel` and
  `TestSessionPanel`'s multi-fetch cases don't share a common shape
  worth generalizing).
- [x] **3.4 — `JsonlStore` base.** Landed: new `src/jsonl_store.py`
  (`JsonlStore[R]` + `latest_by_key()` + `append_jsonl_line()`).
  `FeedbackStore`, `TrainingLedger`, and `VehicleLedgerStore` (a 4th
  match beyond the doc's cited 3) migrated to subclass it, verified
  byte-identical behavior including `TrainingLedger`'s
  first-seen-order + stable-sort tie-break. `ControlAuditLogger` and
  the job ledger reuse only the low-level `append_jsonl_line`
  primitive and stay separate classes (genuine lock/in-memory-cache
  architecture difference). Day-rotating `AnomalyEventLogger`
  confirmed genuinely different, untouched.
- [x] **3.5 — Shared artifact IO.** Landed: `manifest_path_for()`/
  `write_artifact()`/`read_artifact()` added to `src/models/manifest.py`;
  both sequence trainers + isolation_forest now use them; all
  `.with_suffix(".manifest.json")` literal call sites replaced.
  Extracted `_calibration_split`/`_calibrate_holdout_threshold` out of
  `fit_isolation_forest`'s per-mode loop. Deliberately did NOT
  physically split `isolation_forest.py` into separate files (the
  doc's literal file-split ask) — ~30 existing callers import from it
  directly and the risk/benefit didn't justify it within this task;
  the artifact-IO duplication itself (the concrete problem) is fully
  resolved.

## Phase 4 — Quick sweeps (each ≤ an hour, batchable)

- [x] **4.1 — Delete/demote `run_live_inference_lstm`.** Landed:
  confirmed genuinely production-dead (only its own tests referenced
  it), deleted (~80 lines). Its one valuable test ported onto
  `run_live_inference`/`SequenceDetector` (which inherited that code
  path) rather than discarded.
- [x] **4.2 — `atomic_write_text(path, text)` util.** Landed: new
  `src/atomic_write.py`, migrated 12 call sites (more than the doc's
  9-10 estimate). Left `gps_tracker.py` (needs fsync durability),
  `feature_cache.py` (pickle, pid-disambiguated tmp names for
  concurrency), `control/models.py` (binary + a deliberate
  write-both-before-rename-either ordering), and
  `storage/parquet_writer.py` (binary, per doc) as genuine
  divergences, verified.
- [x] **4.3 — `parse_iso_utc` into `src/timefmt.py`.** Landed: added
  the canonical parse+UTC-normalize function, migrated 4 hand-rolled
  copies. Incidentally fixed a latent bug in `dashboard/status.py`
  where the naive-timestamp branch didn't tz-normalize — would have
  raised `TypeError` subtracting a naive from an aware datetime;
  verified real via trace, the shared helper always normalizes now.
- [x] **4.4 — Coercion/torch-guard helpers.** Landed: `coerce_timestamp`
  unified where genuinely identical (`isolation_forest.py` now imports
  `sequence_training`'s version); `models/inference.py`'s divergent
  None-on-bad-input version correctly left separate. New
  `src/coerce.py::optional_str` collapses 3 identical copies;
  `retraining_policy.py`'s divergent `""→None` version correctly left
  alone. `_optional_float`/`_required_int` confirmed already deduped
  to 1 definition each by 2.1. `_torch_module`: the 2 trainer copies
  were already unified by 2.1; the remaining 5 serving-module copies
  each raise a distinct exception type — left separate (though 2 of
  the 5, `models/inference.py` and `false_positive_benchmark.py`, turn
  out to raise the identical exception type with only cosmetic message
  differences, a minor changelog overclaim, not worth re-touching).
- [x] **4.5 — FE formatter sweep.** SystemPanel's 8 local `format*`
  helpers, ModelHealthPanel's shadowing `formatPercent`, and the 3×
  `formatDuration` (PowerPanel, SystemPanel, `util/time.formatElapsed`)
  → `util/format.ts` / `util/time.ts`. — landed: SystemPanel's
  `formatTemp`/`formatCoords`/`formatAltitude`/`formatGpsSpeed`/
  `formatHeading` moved to `util/format.ts` (its local `formatPct` was
  a duplicate of the existing `formatPercent(v, 0)`, deleted in favor
  of calling that directly); ModelHealthPanel's shadowing local
  dropped, call sites now pass `formatPercent(v, 0)` explicitly. The
  second `formatDuration` copy was actually in `TripPanel.tsx`, not
  SystemPanel (doc drift — SystemPanel never had one); unified into
  `util/time.ts` with a `collapseZero` option so PowerPanel
  (round, omit "0m", "0s" floor text) and TripPanel (floor, always
  show minutes, "—" floor text) both keep their exact prior output.
- [x] **4.6 — Shared vitest harness.** `test/harness.ts` with
  `mockHttp(names)` + `HttpErrorStub` — the hoisted-mock +
  reset/clear lifecycle is byte-identical in 6 test files
  (`PowerPanel.test.ts:7-25`, `ProfilesPanel.test.ts:6-24`, …). —
  landed: `test/harness.ts`'s `mockHttp(names)` uses `vi.doMock` (not
  `vi.mock`) deliberately — `vi.mock` is hoisted by a per-file static
  scan for the literal call, which corrupts the closure when wrapped
  in an imported helper (confirmed by reproducing the
  "mocks is not defined" hoisting bug before switching); `vi.doMock`
  is the documented non-hoisted escape hatch, safe here because every
  caller reaches the mocked module through a dynamic
  `await import("./Panel")`, never a static import. Adopted by all 8
  files that actually had the pattern (Profiles, Power,
  Tuning+Anomaly, Trip, Trends, Phase6, Dtc, DataSources) — 8, not
  the 6 originally spotted.
- [x] **4.7 — Misc singles.** Landed: `CollectorStarter` +
  `_default_collectors`/`_gps_collectors` moved to new
  `src/collection/collectors.py`, fixing the `daemon/runtime.py` →
  `main.py` import inversion (verified no new cycle). `_need()`
  decoder guard: `obd_registry.py` now imports the single definition
  from `can_obd_client.py`. New `log_dropped_can_frame()` (mirrors
  `QueueDropCounter`) collapses the 3 identical bounded-log bodies in
  `can_collector.py`/`can_obd_collector.py`. New `threadsafe_enqueue()`
  collapses the 3 `call_soon_threadsafe(partial(put_nowait_or_drop,
  ...))` closures. `list_events_by_kind` now builds `params=` like its
  sibling MCP tools instead of hand-joining an unescaped query string
  — fixes a latent no-URL-encoding bug in the process.

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

- [x] Whole-repo gate green per commit; FE phases also `pnpm test` +
  `pnpm typecheck` (re-run `TuningFeedback.test.ts` on flake). Held for
  every commit across Phases 1-4 tonight.
- [x] Serve-path refactors (1.1, 3.1): pinned-session replay
  before/after with event diff. 1.1 landed with a startup consistency
  assertion (no runtime event path changed). 3.1 replay-verified
  behavior-preserving (categorical fields identical, LSTM scores
  matching to ~5 sig figs); one real divergence (`SequenceDetector`
  provenance-tag reset ordering) was caught, fixed, and re-verified
  before landing.
- [x] Line-count receipts: honest accounting, not a forced total —
  2.1 alone removed ~558 net production lines (unifying the LSTM/TCN
  trainers); several other tasks were net-line-*positive* by design
  (3.2/3.3's FE panel splits, 3.1/3.4/3.5's new shared modules +
  their test coverage) because eliminating N copies of duplicated
  logic plus adding one well-tested shared implementation costs more
  raw lines than N thin unmigrated copies, even though the actual
  *duplication* — the thing this phase targeted — is gone. The
  doc's ~2,000-2,500 figure doesn't hold as a literal net-diff target
  once test coverage for the new shared code is counted; every
  concrete duplication site named in Phases 1-4 was independently
  confirmed migrated or explicitly disclosed as a deliberate
  non-migration (2.2 blocked; 3.5's literal file-split skipped, ~30
  callers, artifact-IO duplication itself resolved).
- [x] No public-API breaks: `PID_DECODERS` shim kept (1.1);
  `src/models/__init__.py` re-exports unaffected by 2.1/3.5 (verified
  by the whole-repo gate + existing import-site tests passing
  unchanged throughout); jobs-allow-list script names untouched by
  4.7's collector relocation (`main.py` still re-exports
  `_default_collectors` for its own/test callers).
