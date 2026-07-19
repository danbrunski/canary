# Roadmap — detector evaluation, longitudinal memory, DTC reader

Status: **planned** (2026-07-18). Straight to `main`, no feature branches.

## Why this exists

A project-level review found that CANary's remaining gaps are not in the
models but in **evaluation and longitudinal memory**:

1. Every evaluation cycle so far measured *false positives* on held-out
   normal drives. Nothing measures whether the detectors would *catch a
   fault* — no recall, no time-to-detect. Deploys are still validated by
   "watch the Pi for a week."
2. The failures worth catching early (bearing wear, LTFT creep, cooling
   degradation) unfold over months. Windowed detectors structurally can't
   see them, and periodic retraining *absorbs* them. There is no
   slow-drift layer and no frozen reference to drift against.
3. There is no structured record of what happened to the vehicle
   (maintenance, mods, repairs), so anomalies can never be scored against
   outcomes and "normal" silently changes at every intervention.
4. Alerts only exist on a LAN dashboard the operator must open. No push,
   no end-of-drive summary.
5. Session metadata is scattered across directory names + three sidecar
   files, and `query_telemetry` rglobs every Parquet in the tree.
6. The dashboard "Trouble Codes" panel is a placeholder, even though the
   Mode 03 transport, DTC payload parser, and code-description catalog
   already exist in the repo with **zero callers**.

Exploration (2026-07-18) confirmed most of the bones already exist —
this roadmap is mostly *wiring*, not new subsystems. See the code anchor
index at the bottom.

## Guiding constraints

- Whole-repo gate before every commit: `ruff check . && ruff format
  --check . && mypy src && pytest` (+ `pnpm test` in
  `src/dashboard/static` for FE changes).
- Read-only on CAN. **Mode 04 (clear DTC) stays forbidden** (NFR-06);
  it is unconstructable in `can_obd_client.py` and guarded by
  `WRITE_COMMAND_MODES` on the legacy path. The DTC reader adds Modes
  03/07/0A reads only — no clear button, ever.
- Not safety-critical — size effort accordingly; don't over-engineer
  failure modes the system can't have.
- Everything per-vehicle-scoped must go through `scope_config`
  (`src/profiles.py:349-361`) — a new daemon path field that isn't added
  there is silently shared across profiles.
- Pi deploy: `git pull` + restart; model binaries are gitignored (scp).

## Sequencing rationale

Phase 1 (DTC) first: smallest, user-visible, and its output feeds the
ledger (Phase 3) and digest (Phase 5). Phase 2 (benchmark) before any
further model work — it de-risks every subsequent deploy. Phase 3
(ledger) is tiny and compounds with time, so it lands early. Phase 4
(fingerprints/trends) depends on nothing but produces the digest's best
content. Phase 5 (notifications/digest) consumes 1–4. Phase 6
(catalog + provenance + hardening) is architecture cleanup that gets
easier once 1–5 have defined what needs indexing.

---

## Phase 1 — DTC reader (make the placeholder panel real)

**Goal.** Live stored/pending/permanent trouble codes + MIL status in
the dashboard, over MCP, and persisted per session. Existing pieces:
`query_mode_03` + `parse_dtc_payload` (`src/collection/can_obd_client.py:112,564`),
`describe_dtc`/`build_dtc_report`/`DtcReport` (`src/decode/dtc_catalog.py:117,147,132`),
service metadata (`obd_registry.py` `SERVICE_REGISTRY:1193`), and the
FE placeholder (`nav.ts:24` `diag-dtc`, `Workbench.tsx:143-150`).

### 1A. Transport + scheduling

- [ ] **1.1 — Mode 07 / 0A reads.** Add `query_mode_07` and
  `query_mode_0A` to `can_obd_client.py` beside `query_mode_03` (:112) —
  same wire shape, reuse `parse_dtc_payload` (docstring at :564 already
  notes 03/07 share the encoding). Unit-test against captured payloads
  including the null-filler case.
- [ ] **1.2 — MIL / DTC-count via PID 0x01.** Add Mode 01 PID 0x01
  (decoder already in `obd_registry.py:379` — bit 7 = MIL, bits 0–6 =
  count) to the default slow-tier poll set so MIL state and DTC count
  are cheap, continuous signals.
- [ ] **1.3 — Periodic DTC poll.** Extend `build_poll_requests`
  (`src/collection/obd_scheduler.py:398`) with `kind="dtc"` requests for
  services 03/07/0A on a new configurable very-slow interval (default
  ~300 s; config under `collection.`). The unused `on_demand` tier
  scaffolding (:24-28) exists but don't build an injection API — a slow
  periodic poll is enough. Works in `ignition_only` (KOEO) too; skip
  cleanly when OBD is stale.
- [ ] **1.4 — Cached report in daemon state.** On each poll, assemble via
  `build_dtc_report(stored, pending, permanent, mil_on)` and hold the
  latest `DtcReport` in daemon state (pattern: how OBD coverage state is
  held for `/obd/coverage`, `public.py:135`).

### 1B. Surfacing

- [ ] **1.5 — `GET /obd/dtc`.** Public (unauthenticated read) route in
  `src/dashboard/routes/public.py` beside `/obd/coverage` (:135)
  returning the cached `DtcReport.to_dict()` + `captured_at` + a
  `stale: bool`. Response schema in `src/dashboard/schemas.py`.
- [ ] **1.6 — New-code events.** When a poll shows a code not present in
  the previous report (per session), emit an `AnomalyEvent` with
  `source="dtc"` through `AnomalyEventEmitter._emit`
  (`src/alerts/emitter.py:240`) so new DTCs land in the anomaly JSONL,
  the WS stream, and (Phase 5) notifications. Pending→stored transitions
  count as new. No persistence gating needed — a DTC is already ECM-debounced.
- [ ] **1.7 — Session sidecar.** On change, write `dtc.json` into the
  active session dir (atomic tmp+rename like `control.json`,
  `src/control/sessions.py:107`) so post-hoc analysis and the Phase 6
  catalog can see per-drive DTC history.
- [ ] **1.8 — MCP tool.** `get_dtc` read tool: new
  `src/mcp_server/tools/dtc.py` calling `GET /obd/dtc`, registered in
  `server.py` `_register_read_tools` (:168). Document in
  `docs/mcp-server.md`.
- [ ] **1.9 — Dashboard panel.** `DtcPanel.tsx` under
  `src/dashboard/static/src/panels/` (model on an existing polling
  panel): MIL banner, three code lists (stored/pending/permanent) with
  `description`/`system` from the report, `captured_at`/stale indicator.
  Replace the `PlaceholderPanel` in `Workbench.tsx:143-150`, drop
  `placeholder: true` at `nav.ts:24`, add `getDtc` to `api/http.ts` +
  types, add `DtcPanel.test.ts`. **No clear-codes control.**
- [ ] **1.10 — Deploy + live verify.** Pull + restart on the Pi; verify
  KOEO read (`vehicle_state: ignition_only`) and engine-running read;
  confirm `_replay` sessions don't poll (replay has no live OBD path).

---

## Phase 2 — Fault-injection detector benchmark

**Goal.** One command that answers "would we catch it, how fast, at what
FP cost" for every detector before a deploy. Existing bones:
`AnomalyInjector` (spike/drift/dropout/stuck/scale,
`src/preprocessing/anomaly_injection.py:60`), single-frame evaluator
`evaluate_detectors` (`src/models/synthetic_fault_eval.py:174`), FPR
harness (`src/models/false_positive_benchmark.py`), bake-off runner
(`scripts/sequence_bakeoff.py`), replay diff harness
(`src/analysis/replay_window.py:402 _diff_events`).

> See also `ml-correctness-hardening.md` (2026-07-18 ML review): its
> Phase 8 (eval-harness realism — dropout injection realism, post-window
> detection grace, threshold provenance) merges into this phase, and its
> Phase 1 (stale-data visibility) changes the production dropout
> signature this benchmark must reproduce.

### 2A. Parity pre-flight (do these first — they gate trust in results)

- [x] **2.1 — `scripts/replay.py` featurization divergence.** It calls
  `compute_features` directly (:99-102) instead of the shared
  `featurize_wide_frame` tail, so offline replay lacks anomaly-signal /
  committee / behavior-deviation columns. Route it through
  `featurize_wide_frame` (`feature_pipeline.py:430`). — landed:
  `replay_sessions` now calls `featurize_wide_frame`, with a new
  optional `behavior_models_path` param; regression test proves a
  `derived:*` anomaly-signal column that the old direct-`compute_features`
  path could never produce now appears (commit `05fc61a`).
- [x] **2.2 — Verify `build_feature_frame` tail identity.** The
  docstring (:439) claims batch calls `featurize_wide_frame`, but the
  body (:275-347) appears to open-code the tail. Confirm byte-identical
  columns or unify; add a parity test (same session through both paths →
  identical frames). — verified: the body already calls
  `featurize_wide_frame` (no divergence to fix); locked with a parity
  test comparing `build_feature_frame`'s output against an
  independently-reconstructed head + standalone `featurize_wide_frame`
  call (commit `05fc61a`).

### 2B. The benchmark

- [ ] **2.3 — Sequence-detector coverage.** Extend
  `synthetic_fault_eval.evaluate_detectors` to also run
  `SequenceInferenceEngine` (`src/models/inference.py:269`) over the
  injected frame (windowed `observe`/`score_window`), not just IF +
  fusion.
- [ ] **2.4 — Time-to-detect.** Add first-detection latency (seconds
  from injection-window start to first over-threshold, post
  `PersistenceGate`) to `_DetectorMetrics` (:57) alongside
  `detection_rate`. Run scoring through the production gate config so
  the number reflects what would actually alert.
- [ ] **2.5 — Corpus-wide CLI.** `scripts/detector_benchmark.py`: grid of
  (held-out clean sessions × fault kinds × target channels), loading
  sessions via `discover_sessions`/`read_session`, injecting with
  `AnomalyInjector`, evaluating per 2.3/2.4, plus the clean-pass FP
  block. Output: per-detector × per-fault-class table of recall /
  precision / median time-to-detect / alerts-per-hour-clean, printed +
  written as JSON to `data/analyses/benchmark-<ts>.json`. Deterministic
  seeds.
- [ ] **2.6 — Regression compare.** `--baseline <report.json>` flag:
  diff against a previous run, flag any metric that regressed beyond a
  tolerance. This is the pre-deploy gate — run it in the retrain flow
  (`scripts/retrain.py`) and record the report path in the training
  ledger (`src/models/training_ledger.py`).
- [ ] **2.7 — Tests.** Model on `tests/test_synthetic_fault_eval.py`
  (`_two_mode_resampled` builder :49): small seeded corpus, assert a
  gross injected fault is detected and reported latency is finite;
  assert report JSON round-trips.

---

## Phase 3 — Vehicle maintenance / intervention ledger

**Goal.** An append-only, profile-scoped record of what physically
happened to the truck, so anomalies can be scored against outcomes and
training corpora can be segmented at intervention boundaries. Pattern:
`FeedbackStore` append-only JSONL (`src/alerts/feedback.py:93`).

- [ ] **3.1 — Store.** `src/analysis/vehicle_ledger.py`:
  `VehicleEvent(BaseModel)` — `id`, `recorded_at`, `occurred_at` (date),
  `odometer_km: float | None`, `kind:
  maintenance|mod|repair|observation|dtc`, `title`, `details`,
  `affected_channels: list[str]`, `corpus_boundary: bool` (does "normal"
  change at this event?). Append-only JSONL store modeled on
  `FeedbackStore` (append, list, latest-by-id).
- [ ] **3.2 — Config + scoping.** New `DaemonConfig.vehicle_ledger` path
  (default `data/control/vehicle_ledger.jsonl`), wired into
  `scope_config` (`src/profiles.py:349-361`) — per-vehicle, like
  `feedback_log`. Update example YAMLs.
- [ ] **3.3 — MCP + CLI.** Write tool `record_vehicle_event`
  (audit-logged, `src/mcp_server/write_tools/`) + read tool
  `list_vehicle_events`; CLI `scripts/vehicle_log.py add|list` (the name
  `ledger.py` is taken by the training ledger). Document in
  `docs/mcp-server.md`.
- [ ] **3.4 — DTC auto-entries.** When Phase 1.6 sees a new stored DTC,
  auto-append a `kind="dtc"` ledger event — outcome tracking starts
  itself.
- [ ] **3.5 — Corpus-boundary integration.** In corpus selection
  (`scripts/retrain.py` age filter, `src/analysis/refit.py` recipes):
  optionally exclude sessions older than the latest `corpus_boundary`
  event (`--since-boundary` flag). Surfaces in `get_dataset_health` as a
  note when a boundary bisects the corpus.
- [ ] **3.6 — Backfill.** One-time entries for known history (mods
  analyzed in the 2026-07 drive comparisons, profile migration, etc.) so
  the ledger is useful on day one.

---

## Phase 4 — Session fingerprints + long-horizon drift

**Goal.** Catch the slow failures. A small per-drive stat vector,
trended over months against a **frozen reference epoch** that retraining
never touches. Existing per-session aggregate to extend:
`SessionClassification` (`src/analysis/session_classifier.py:118`).

- [ ] **4.1 — Fingerprint computation.** `src/analysis/fingerprint.py`:
  `compute_fingerprint(session_dir, config) -> dict[str, float]` over
  the wide/resampled frame. Initial stat set (config-extensible):
  - warm-up time constant (cold-start coolant → 80 °C seconds)
  - idle-at-operating-temp: mean/std RPM, mean MAF, mean engine load
  - LTFT and STFT means at idle and at cruise; combined trim
  - cranking duration (first OBD sample → RPM ≥ 400)
  - control-module voltage at crank and at idle (if PID present)
  - mean bank-balance residual; committee residual means (speed OBD↔GPS)
  - per-behavior-surface mean |deviation| (from `deviation:*` columns)
  Skip stats whose preconditions are absent (no cold start → no warm-up
  stat) rather than emitting garbage.
- [ ] **4.2 — Sidecar + hook.** Write `fingerprint.json` into the
  session dir; compute it where classification already runs
  (`scripts/label_sessions.py` / `src/control/classification.py:61`) so
  there's one post-session pass. Backfill CLI flag for existing
  sessions.
- [ ] **4.3 — Frozen reference epoch.**
  `data/control/reference_epoch.json` (profile-scoped via a
  `DaemonConfig` path + `scope_config`): `{frozen_at, sessions[],
  per-stat mean/std/quantiles, notes}`. `scripts/fingerprint.py freeze`
  builds it from current healthy sessions. **Never auto-updated** — a
  ledger `corpus_boundary` event is the prompt to consciously re-freeze.
- [ ] **4.4 — Trend tests.** `src/analysis/trends.py`: pure-numpy
  Mann-Kendall trend test + CUSUM against reference mean/std, per stat,
  over the session-ordered fingerprint series. Unit-test on synthetic
  drifting/stationary series.
- [ ] **4.5 — Surfacing.** MCP read tool `get_trends` (per-stat: latest
  value, reference band, trend verdict, CUSUM state); CLI table via
  `scripts/fingerprint.py trends`. Dashboard `TrendsPanel.tsx`
  (sparkline per stat with reference band) — follows the Phase 1.9 panel
  recipe; may land after the MCP tool.
- [ ] **4.6 — Alerting.** A stat in sustained CUSUM alarm emits a
  low-frequency `AnomalyEvent` (`source="trend"`) — at most once per
  session per stat.

---

## Phase 5 — Notifications + end-of-drive digest

**Goal.** Alerts reach the operator without the dashboard open.
Greenfield (grep confirms zero notification code). Keep it small: ntfy
POST via httpx, best-effort, never blocks the pipeline.

- [ ] **5.1 — Config.** `NotificationsConfig(_Strict)` in
  `src/config.py` (+ `CanaryConfig` default at ~:404): `enabled=false`,
  `ntfy_url`, `topic`, `min_severity`, `max_pushes_per_session` (default
  3), `digest_enabled`. Update example YAMLs.
- [ ] **5.2 — Notifier.** `src/alerts/notifier.py`: async fire-and-forget
  httpx POST with short timeout; failures log once, never raise.
  Unit-test formatting; mock transport.
- [ ] **5.3 — Critical-alert hook.** Call from the emitter choke point
  (`src/alerts/emitter.py:240 _emit`) after dedupe/persistence gates,
  for events ≥ `min_severity`, capped per session. Replay mode never
  notifies (emitter already knows replay via logger redirect — assert
  this in a test).
- [ ] **5.4 — Trip-end edge.** No session-close event exists today. Add
  a "trip ended" edge from `GpsTracker` (`src/analysis/gps_tracker.py:114
  trip_status` moving→stationary sustained N minutes) with daemon
  shutdown (`src/daemon/runtime.py:189-199`) as fallback trigger.
- [ ] **5.5 — Digest.** On trip end: assemble `digest.json` into the
  session dir — duration, mode seconds, gated anomaly counts by source,
  top behavior-surface deviations, fingerprint deltas vs reference (top
  movers), new DTCs, new ledger events. Push a compact rendering via the
  notifier when enabled.
- [ ] **5.6 — Surfacing.** `GET /sessions/{id}/digest` +
  `get_session_digest` MCP tool; dashboard shows the last digest (can
  ride in an existing panel; a dedicated panel is optional).

---

## Phase 6 — Session catalog, artifact provenance, capture tripwire

**Goal.** Architecture cleanup: one queryable index over session
metadata; self-diagnosing laptop↔Pi model skew; a cheap counter that
tells us if Python capture ever drops frames (the standing answer to
"should the capture path be rewritten?" is "not until this counter says
so").

### 6A. Session catalog

- [ ] **6.1 — Schema + store.** `src/storage/catalog.py`: SQLite,
  modeled directly on `src/companion/library.py` (`_SCHEMA:53`,
  `SCHEMA_VERSION` + `meta` migration table, `_tx` helper). One
  `sessions` table: session_id PK, path, started_at, duration_s,
  row_count, size_bytes, pinned, disposition, classifier_version,
  mode_seconds/event_counts/quality/fingerprint/dtc/digest as JSON
  columns, updated_at. **Sidecars remain source of truth; the catalog is
  a rebuildable index** — corruption recovery is `rebuild`, never data
  loss.
- [ ] **6.2 — Config + scoping.** `DaemonConfig.catalog_db` (default
  `data/control/catalog.db`) + `scope_config` wiring.
- [ ] **6.3 — Rebuild + incremental update.** `scripts/catalog.py
  rebuild|status` (walk `discover_sessions` + read sidecars); update the
  row wherever sidecars are written (classification persist, pin/unpin,
  fingerprint, dtc, digest) and delete rows in the retention sweeper
  paths (`src/storage/retention.py`).
- [ ] **6.4 — Consumers.** (a) `parquet_query._matching_files`
  (`src/mcp_server/parquet_query.py:93`) — **fix the standing bug**: the
  bare `rglob` sweeps `_replay-*`/`_archive` Parquet into query results;
  filter underscore-prefixed dirs and use the catalog for session/date
  pushdown. (This supersedes item 3.5 of `mcp-and-power-hardening.md`
  — **do not edit that doc**, another session owns it; note the
  supersession in the commit message only.) (b) `list_sessions`
  control listing.
  (c) Phase 4 trend queries read fingerprints from the catalog instead
  of globbing sidecars.

### 6B. Model artifact provenance

- [ ] **6.5 — Expose loaded-model provenance.** Manifest fields already
  exist (`src/models/manifest.py:62-75`: `model_version`, `created_at`,
  `training_data_hash`, `training_sessions`, spec version) but nothing
  surfaces them at serve. Add a provenance block per loaded model to the
  status payload (`src/dashboard/status.py build_status`) + visible in
  `get_status`.
- [ ] **6.6 — Skew check CLI.** `scripts/verify_artifacts.py [--remote
  canary@192.168.137.2]`: compare local vs Pi artifact file sha256 +
  manifest `training_data_hash`/`model_version`; report mismatches. This
  makes the scp deploy path self-diagnosing. Record artifact sha256 in
  the training ledger at train time.
- [ ] **6.7 — Load-time log line.** `ModelManager._maybe_load`
  (`src/models/manager.py:135`) logs version + training window +
  data-hash on every (re)load, so `tail_daemon_log` shows exactly what
  went live and when. (No hard corpus-vs-hash verification — retention
  legitimately deletes training sessions; don't over-engineer.)

### 6C. Capture tripwire

- [ ] **6.8 — rx-overrun counter in status.** Read `can0` rx
  overrun/drop counters (`/sys/class/net/can0/statistics/` or `ip -s`)
  in the status builder; expose in `get_status` + the SystemPanel row.
  Nonzero growth during drives is the (only) trigger to revisit the
  capture architecture.

---

## Cross-cutting acceptance

- [ ] Whole-repo `ruff` + `mypy` + `pytest` green on every commit;
  `pnpm test` for FE phases (re-run `TuningFeedback.test.ts` on flake
  before treating as a regression).
- [ ] `docs/mcp-server.md` updated for every new/changed tool
  (`get_dtc`, `record_vehicle_event`, `list_vehicle_events`,
  `get_trends`, `get_session_digest`, `query_telemetry` scoping).
- [ ] Every new per-vehicle path goes through `scope_config` — checked
  explicitly at review of Phases 3, 4, 6.
- [ ] Each phase deployed to the Pi and verified live before starting
  the next.
- [ ] Phase 2 benchmark run + report archived before any future model
  deploy.

## Code anchor index (from the 2026-07-18 exploration)

| Area | Anchor |
|---|---|
| DTC transport | `src/collection/can_obd_client.py:112 query_mode_03`, `:564 parse_dtc_payload` (03/07 same shape; 07/0A not yet implemented) |
| DTC catalog/report | `src/decode/dtc_catalog.py:117 describe_dtc`, `:147 build_dtc_report`, `:132 DtcReport` — currently zero callers |
| DTC service metadata | `src/collection/obd_registry.py:1193 SERVICE_REGISTRY` (0x02/03/06/07/0A, all `on_demand=True`); PID 0x01 MIL/count decoder `:379` |
| Read-only guard | `src/collection/obd_collector.py:55 WRITE_COMMAND_MODES` {0x04,0x2E,0x2F}; `custom_pids.py:59 READ_ONLY_MODES` |
| OBD poll plan | `src/collection/obd_scheduler.py:398 build_poll_requests` (pid/did only today); tiers `:24` (`on_demand: 0.0` unused) |
| FE placeholder | `nav.ts:24` (`diag-dtc`, `placeholder: true`); `Workbench.tsx:143-150` case; `PlaceholderPanel.tsx` |
| Fault library | `src/preprocessing/anomaly_injection.py:60 AnomalyInjector` (spike/drift/dropout/stuck/scale), `InjectionWindow.labels():93` |
| Single-frame eval | `src/models/synthetic_fault_eval.py:174 evaluate_detectors` (IF + fusion; no sequence detector yet), `_DetectorMetrics:57` |
| FPR harness | `src/models/false_positive_benchmark.py`; CLI `scripts/fp_benchmark.py`; bake-off `scripts/sequence_bakeoff.py:153 run_backbone` |
| Shared featurize tail | `src/preprocessing/feature_pipeline.py:430 featurize_wide_frame`; batch `:218 build_feature_frame` (verify tail identity); reconcile `:797` |
| Offline replay | `scripts/replay.py:71 replay_sessions` (bypasses shared tail — fix in 2.1); `src/analysis/replay_window.py:74`, `_diff_events:402` |
| Sequence inference | `src/models/inference.py:269 SequenceInferenceEngine`, `score_window:351`; gate `src/alerts/fusion.py:71 PersistenceGate`; live wiring `src/daemon/inference.py:48` |
| Emitter choke point | `src/alerts/emitter.py:240 _emit` (tuning gate → dedupe → log); event schema `src/alerts/events.py:33 AnomalyEvent` |
| Feedback store (JSONL pattern) | `src/alerts/feedback.py:93 FeedbackStore`; path via `DaemonConfig.feedback_log` (`src/config.py:351`) |
| Session sidecars | `control.json` (`src/control/sessions.py:17`), `classification.json` + `derived_tags.jsonl` (`src/analysis/session_classifier.py:55-56`), `.canary-pin` (`sessions.py:18`) |
| Session discovery | `src/preprocessing/loader.py:22 discover_sessions` (skips `_`-prefixed `:35`); id format `src/session.py:65` strftime |
| Retention | `src/daemon/retention.py:23 run_retention_sweeper`; impls `src/storage/retention.py` (pin checks :65,:157,:230,:278) |
| Catalog precedent | `src/companion/library.py:53 _SCHEMA`, `:199 _migrate`, `:222 _tx` (laptop-side; reuse the pattern daemon-side) |
| Parquet query bug | `src/mcp_server/parquet_query.py:93 _matching_files` — bare `rglob` includes `_replay-*`/`_archive` |
| Profile scoping | `src/profiles.py:336 scope_config` (daemon path block `:349-361`, `_scope_file:331`) |
| Config pattern | `src/config.py:16 _Strict`; `CanaryConfig:395` (defaulted sections ~:402-404) |
| Manifests / manager | `src/models/manifest.py:50 ModelManifestCommon` (`training_data_hash:66` cache-only today); `src/models/manager.py:135 _maybe_load` |
| Corpus hashes | `src/models/isolation_forest.py:732`, `src/models/sequence_training.py:481`, `src/analysis/surface_2d.py:223` |
| Trip tracker | `src/analysis/gps_tracker.py:114 trip_status`, `:137 persist`; instantiated `src/daemon/components.py:106` |
| Training ledger | `src/models/training_ledger.py` (JSONL, `data/control/training_runs.jsonl`); CLI `scripts/ledger.py` |
