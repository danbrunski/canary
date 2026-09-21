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
- The DTC reader adds Modes 03/07/0A reads only — no clear button in
  this scope. Mode 04 is reachable via `POST /control/obd_request` on the
  Canable path (the legacy ELM327 path still blocks it via
  `WRITE_COMMAND_MODES`); wiring a UI for it is Task 6.5, not this.
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

- [x] **1.1 — Mode 07 / 0A reads.** Add `query_mode_07` and
  `query_mode_0A` to `can_obd_client.py` beside `query_mode_03` (:112) —
  same wire shape, reuse `parse_dtc_payload` (docstring at :564 already
  notes 03/07 share the encoding). Unit-test against captured payloads
  including the null-filler case. — landed: reuses `pid=None` +
  `parse_dtc_payload` exactly like `query_mode_03`.
- [x] **1.2 — MIL / DTC-count via PID 0x01.** Add Mode 01 PID 0x01
  (decoder already in `obd_registry.py:379` — bit 7 = MIL, bits 0–6 =
  count) to the default slow-tier poll set so MIL state and DTC count
  are cheap, continuous signals. — landed: PID 0x01 added to
  `can_obd_client.PID_DECODERS` — the table `build_poll_requests`'s
  default/auto set actually sources from (the `_MODE01_SPEC` entry
  alone, from an earlier commit, never drove real polling).
- [x] **1.3 — Periodic DTC poll.** Extend `build_poll_requests`
  (`src/collection/obd_scheduler.py:398`) with `kind="dtc"` requests for
  services 03/07/0A on a new configurable very-slow interval (default
  ~300 s; config under `collection.`). The unused `on_demand` tier
  scaffolding (:24-28) exists but don't build an injection API — a slow
  periodic poll is enough. Works in `ignition_only` (KOEO) too; skip
  cleanly when OBD is stale. — landed:
  `collection.obd_dtc_poll_interval_seconds` (default 300s). Caught and
  fixed two real bugs: `DidWindowController.request_eligible` only let
  `kind="pid"` through outside the DID window, which would have starved
  dtc-kind requests forever on a DID-gated ECU (this project's actual
  Xterra); and dtc-kind requests need `identifier=None` (Mode 03/07/0A
  take no parameter byte) — the placeholder `identifier=0` corrupted
  the wire request. Both verified independently by the reviewer via
  direct `isotp.py` tracing. Minor gap noted: no test combines an
  active DID session + a short DTC interval end-to-end (verified via
  direct code tracing instead) — worth a follow-up test.
- [x] **1.4 — Cached report in daemon state.** On each poll, assemble via
  `build_dtc_report(stored, pending, permanent, mil_on)` and hold the
  latest `DtcReport` in daemon state (pattern: how OBD coverage state is
  held for `/obd/coverage`, `public.py:135`). — landed: new
  `src/control/dtc_state.py::DtcState`, wired through
  `daemon/drain.py`'s new `run_dtc_sink`.

### 1B. Surfacing

- [x] **1.5 — `GET /obd/dtc`.** Public (unauthenticated read) route in
  `src/dashboard/routes/public.py` beside `/obd/coverage` (:135)
  returning the cached `DtcReport.to_dict()` + `captured_at` + a
  `stale: bool`. Response schema in `src/dashboard/schemas.py`. —
  landed: `DtcCodeEntry`/`DtcReportResponse`; empty-but-shaped payload
  when no `DtcState` is wired (tested, not dead code).
- [x] **1.6 — New-code events.** When a poll shows a code not present in
  the previous report (per session), emit an `AnomalyEvent` with
  `source="dtc"` through `AnomalyEventEmitter._emit`
  (`src/alerts/emitter.py:240`) so new DTCs land in the anomaly JSONL,
  the WS stream, and (Phase 5) notifications. Pending→stored transitions
  count as new. No persistence gating needed — a DTC is already ECM-debounced.
  — landed: `emit_dtc` goes straight through `_emit`, confirmed to
  never touch `PersistenceGate` (which lives entirely outside the
  DTC/emitter call path); pending→stored re-fires as `"escalated"`.
- [x] **1.7 — Session sidecar.** On change, write `dtc.json` into the
  active session dir (atomic tmp+rename like `control.json`,
  `src/control/sessions.py:107`) so post-hoc analysis and the Phase 6
  catalog can see per-drive DTC history. — landed: written only when
  the report's signature actually changes; a new session resets the
  "already seen" baseline without losing the ECU's real stored/pending/
  permanent state.
- [x] **1.8 — MCP tool.** `get_dtc` read tool: new
  `src/mcp_server/tools/dtc.py` calling `GET /obd/dtc`, registered in
  `server.py` `_register_read_tools` (:168). Document in
  `docs/mcp-server.md`. — landed: plain passthrough, confirmed
  registered in the built tool list, documented consistently with
  other read tools.
- [x] **1.9 — Dashboard panel.** `DtcPanel.tsx` under
  `src/dashboard/static/src/panels/` (model on an existing polling
  panel): MIL banner, three code lists (stored/pending/permanent) with
  `description`/`system` from the report, `captured_at`/stale indicator.
  Replace the `PlaceholderPanel` in `Workbench.tsx:143-150`, drop
  `placeholder: true` at `nav.ts:24`, add `getDtc` to `api/http.ts` +
  types, add `DtcPanel.test.ts`. **No clear-codes control.** — landed:
  TS types verified field-for-field against the real `DtcReportResponse`
  schema; MIL banner distinguishes on/off/unknown; stale indicator
  reads the backend's own `stale` flag, not frontend-computed; no
  clear-codes control anywhere in the component.
- [x] **1.10 — Deploy + live verify.** Landed 2026-07-22: pulled +
  restarted on the Pi (commit `3f6f8bd`); confirmed engine-running
  read (`vehicle_state: engine_running`, real RPM/coolant climbing
  through a genuine idle warm-up) and KOEO read (`ignition_only`,
  RPM 0, coolant holding, battery voltage dropping without the
  alternator — all consistent). Real Mode 03/07 DTC reads against the
  ECM confirmed working (stored/pending both non-timeout); Mode 0A
  (permanent) and Mode 06 (monitor) got zero responses across several
  attempts — inconclusive whether that's this ECM simply not
  supporting those services (plausible for a 2008 vehicle) or a
  framing gap, since the request-building code path is identical to
  the working Mode 03/07 case; flagged as a follow-up, not blocking
  this checkbox. **Follow-up resolved (code side) same day**: root
  cause found — a negative ISO-TP response (`0x7F <service> <NRC>`,
  the ECU's explicit "request rejected") was being silently discarded
  as if it were a true no-frame timeout, both landing in the same
  `timeouts` counter with the actual NRC byte thrown away before it
  ever reached the counting logic (fixed, commit `2f2dc6b`: NRC now
  threaded through to a distinct `negative_responses` counter,
  surfaced in `/status`). Still needs one more live re-check against
  the real ECU to see which of the two explanations it actually is —
  a morning task, not blocking this checkbox. `_replay` sessions confirmed structurally incapable
  of polling — `obd_scheduler`/`can_obd_collector` have zero
  replay-mode awareness, so replay cannot influence the live bus
  regardless of timing.

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

- [x] **2.3 — Sequence-detector coverage.** Extend
  `synthetic_fault_eval.evaluate_detectors` to also run
  `SequenceInferenceEngine` (`src/models/inference.py:269`) over the
  injected frame (windowed `observe`/`score_window`), not just IF +
  fusion. — landed: optional `trained_sequence` param; when supplied,
  ticks the frame row-by-row through the real production
  `engine.observe()` contract (buffer cleared per injection run),
  gated through the same persistence machinery as IF/fused via a
  generalized `_first_sustained_detection(signals={...})`; absent
  artifact is the default and produces IF/fused-only output unchanged.
- [x] **2.4 — Time-to-detect.** Add first-detection latency (seconds
  from injection-window start to first over-threshold, post
  `PersistenceGate`) to `_DetectorMetrics` (:57) alongside
  `detection_rate`. Run scoring through the production gate config so
  the number reflects what would actually alert. — landed:
  `latencies_seconds`/`median_time_to_detect_seconds` on
  `_DetectorMetrics`, measured via a fresh
  `PersistenceGate(config.alerts.min_consecutive_ticks)` per injection
  run (commit `825ae0a`).
- [x] **2.5 — Corpus-wide CLI.** `scripts/detector_benchmark.py`: grid of
  (held-out clean sessions × fault kinds × target channels), loading
  sessions via `discover_sessions`/`read_session`, injecting with
  `AnomalyInjector`, evaluating per 2.3/2.4, plus the clean-pass FP
  block. Output: per-detector × per-fault-class table of recall /
  precision / median time-to-detect / alerts-per-hour-clean, printed +
  written as JSON to `data/analyses/benchmark-<ts>.json`. Deterministic
  seeds. — landed: SHA-256-seeded grid over real sessions, precision
  estimated from clean-pass alert rate projected over exposure time
  (documented as an estimate — no labeled negatives exist), optional
  sequence-detector coverage via `sequence_registry`; also fixed a
  latent `AnomalyInjector.drift()` dtype bug (float32 columns raised
  `LossySetitemError` on real resampled frames, masked by float64-only
  test fixtures).
- [x] **2.6 — Regression compare.** `--baseline <report.json>` flag:
  diff against a previous run, flag any metric that regressed beyond a
  tolerance. This is the pre-deploy gate — run it in the retrain flow
  (`scripts/retrain.py`) and record the report path in the training
  ledger (`src/models/training_ledger.py`). — landed: `diff_reports()`
  compares per (detector, fault_kind) cell against a 15% tolerance
  (recall/precision down = bad, latency/alert-rate up = bad), exit
  codes 1/2/3 distinguish missing-artifact/unreadable-baseline/
  regression; `retrain.py`'s `_run_benchmark_gate` auto-discovers the
  latest prior `benchmark-*.json`, skips cleanly with no calibration
  holdouts, and fails the retrain run on regression while still
  writing the ledger record with the report path either way.
- [x] **2.7 — Tests.** Model on `tests/test_synthetic_fault_eval.py`
  (`_two_mode_resampled` builder :49): small seeded corpus, assert a
  gross injected fault is detected and reported latency is finite;
  assert report JSON round-trips. — already satisfied by
  `test_evaluate_detectors_grades_each_fault_kind_and_keeps_nominal_fp_low`
  (Phase 7.5): uses `_two_mode_resampled`, asserts nonzero detection
  rate for every fault kind × both detectors, finite latency
  bookkeeping, and `json.loads(json.dumps(report))` round-trip. No new
  code needed.

---

## Phase 3 — Vehicle maintenance / intervention ledger

**Goal.** An append-only, profile-scoped record of what physically
happened to the truck, so anomalies can be scored against outcomes and
training corpora can be segmented at intervention boundaries. Pattern:
`FeedbackStore` append-only JSONL (`src/alerts/feedback.py:93`).

- [x] **3.1 — Store.** `src/analysis/vehicle_ledger.py`:
  `VehicleEvent(BaseModel)` — `id`, `recorded_at`, `occurred_at` (date),
  `odometer_km: float | None`, `kind:
  maintenance|mod|repair|observation|dtc`, `title`, `details`,
  `affected_channels: list[str]`, `corpus_boundary: bool` (does "normal"
  change at this event?). Append-only JSONL store modeled on
  `FeedbackStore` (append, list, latest-by-id). — landed:
  `VehicleLedgerStore` (`.append`, `.records()` latest-by-id collapsed
  and sorted by `occurred_at`, `.latest_boundary()`); genuine append-only
  writes, verified `latest_boundary()` sorts by `occurred_at` not
  append/`recorded_at` order.
- [x] **3.2 — Config + scoping.** New `DaemonConfig.vehicle_ledger` path
  (default `data/control/vehicle_ledger.jsonl`), wired into
  `scope_config` (`src/profiles.py:349-361`) — per-vehicle, like
  `feedback_log`. Update example YAMLs. — landed: identical
  `_scope_file` call as `feedback_log`; confirmed genuine parity
  (`feedback_log` itself is only explicit in `canary.sim.example.yaml`,
  and `vehicle_ledger` was added there too).
- [x] **3.3 — MCP + CLI.** Write tool `record_vehicle_event`
  (audit-logged, `src/mcp_server/write_tools/`) + read tool
  `list_vehicle_events`; CLI `scripts/vehicle_log.py add|list` (the name
  `ledger.py` is taken by the training ledger). Document in
  `docs/mcp-server.md`. — landed: `record_vehicle_event` uses the
  identical audit-logging call as `record_feedback`; `POST`/`GET
  /control/vehicle-ledger` (503 unconfigured, 422 bad `kind`).
- [x] **3.4 — DTC auto-entries.** When Phase 1.6 sees a new stored DTC,
  auto-append a `kind="dtc"` ledger event — outcome tracking starts
  itself. — landed: only STORED (not pending-only) codes append,
  matching the task text exactly; a pending→stored escalation correctly
  triggers the append (traced the loop order to confirm no missed
  transition).
- [x] **3.5 — Corpus-boundary integration.** In corpus selection
  (`scripts/retrain.py` age filter, `src/analysis/refit.py` recipes):
  optionally exclude sessions older than the latest `corpus_boundary`
  event (`--since-boundary` flag). Surfaces in `get_dataset_health` as a
  note when a boundary bisects the corpus. — landed: shared
  `_filter_sessions_by_boundary` helper used by both
  `refit_behavior_models.py` and `retrain.py`; `analyze_corpus`'s
  bisection warning only fires when the session set is genuinely split
  (some older, some on/after) — not vacuously present/absent.
- [~] **3.6 — Backfill.** One-time entries for known history (mods
  analyzed in the 2026-07 drive comparisons, profile migration, etc.) so
  the ledger is useful on day one. blocked-overnight (per
  OVERNIGHT-QUEUE.md Q9): real backfill needs the operator's own
  history. Scaffolded instead — the (gitignored) ledger has one
  clearly-labeled placeholder entry pointing at `scripts/vehicle_log.py
  add` for real entries.

---

## Phase 4 — Session fingerprints + long-horizon drift

**Goal.** Catch the slow failures. A small per-drive stat vector,
trended over months against a **frozen reference epoch** that retraining
never touches. Existing per-session aggregate to extend:
`SessionClassification` (`src/analysis/session_classifier.py:118`).

- [x] **4.1 — Fingerprint computation.** `src/analysis/fingerprint.py`:
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
  stat) rather than emitting garbage. — landed: each stat function
  omits its key (never NaN/0) when its precondition fails; reuses the
  existing `compute_anomaly_signals`/`append_committee_residuals`/
  `append_deviation_columns` pipeline rather than recomputing. A real
  bug (`voltage_at_crank_v` fabricated from round-robin-polling RPM lag
  when no cranking actually occurred) caught by review and fixed before
  landing.
- [x] **4.2 — Sidecar + hook.** Write `fingerprint.json` into the
  session dir; compute it where classification already runs
  (`scripts/label_sessions.py` / `src/control/classification.py:61`) so
  there's one post-session pass. Backfill CLI flag for existing
  sessions. — landed: hooked into both the offline `label_sessions.py`
  batch script and the daemon's on-demand `classify_one_session`;
  fingerprint-write failures are isolated (logged, never block the
  classification sidecar) in both. Noted inefficiency (non-blocking):
  `compute_fingerprint` re-resamples the session frame independently of
  `classify_session`'s own load — two passes over the parquet per
  session, not truly shared.
- [x] **4.3 — Frozen reference epoch.**
  `data/control/reference_epoch.json` (profile-scoped via a
  `DaemonConfig` path + `scope_config`): `{frozen_at, sessions[],
  per-stat mean/std/quantiles, notes}`. `scripts/fingerprint.py freeze`
  builds it from current healthy sessions. **Never auto-updated** — a
  ledger `corpus_boundary` event is the prompt to consciously re-freeze.
  — landed: per-stat summaries computed only over the sessions that
  actually carried that stat (verified: a stat in 2/5 fingerprints gets
  n=2 stats, not zero-padded); `freeze` only pulls `ready`-classified
  sessions; confirmed the only writer of this path is the `freeze`
  command.
- [x] **4.4 — Trend tests.** `src/analysis/trends.py`: pure-numpy
  Mann-Kendall trend test + CUSUM against reference mean/std, per stat,
  over the session-ordered fingerprint series. Unit-test on synthetic
  drifting/stationary series. — landed: standard S-statistic/tie-
  corrected Mann-Kendall and two-sided tabular CUSUM; independently
  cross-checked against hand computation and `scipy.stats.kendalltau`.
- [x] **4.5 — Surfacing.** MCP read tool `get_trends` (per-stat: latest
  value, reference band, trend verdict, CUSUM state); CLI table via
  `scripts/fingerprint.py trends`. Dashboard `TrendsPanel.tsx`
  (sparkline per stat with reference band) — follows the Phase 1.9 panel
  recipe; may land after the MCP tool. — landed: all three (MCP tool,
  CLI, and the dashboard panel — not deferred, budget allowed it).
- [x] **4.6 — Alerting.** A stat in sustained CUSUM alarm emits a
  low-frequency `AnomalyEvent` (`source="trend"`) — at most once per
  session per stat. — landed: CUSUM recomputed fresh from the full
  fingerprint history each evaluation (no incremental state to go
  stale); a tiny `{stat: last_alerted_session_id}` idempotency marker
  prevents double-emit on re-evaluation while still firing fresh for a
  later still-alarmed session. A real cross-vehicle-profile state leak
  (the marker path wasn't wired into `scope_config`) caught by review
  and fixed before landing, along with a new exhaustive test that
  guards every `DaemonConfig` path field against the same class of bug
  in the future.

---

## Phase 5 — Notifications + end-of-drive digest

**Goal.** Alerts reach the operator without the dashboard open.
Greenfield (grep confirms zero notification code). Keep it small: ntfy
POST via httpx, best-effort, never blocks the pipeline.

- [x] **5.1 — Config.** `NotificationsConfig(_Strict)` in
  `src/config.py` (+ `CanaryConfig` default at ~:404): `enabled=false`,
  `ntfy_url`, `topic`, `min_severity`, `max_pushes_per_session` (default
  3), `digest_enabled`. Update example YAMLs. — landed: ships disabled
  (`enabled=False`, `topic=""`); example YAML block fully commented.
  Confirmed no real ntfy topic/URL invented — enabling is the
  operator's morning task.
- [x] **5.2 — Notifier.** `src/alerts/notifier.py`: async fire-and-forget
  httpx POST with short timeout; failures log once, never raise.
  Unit-test formatting; mock transport. — landed: the `try/except` is
  inside the scheduled `asyncio` task itself (not just around
  `create_task`, which wouldn't catch async failures) — verified a
  raised `httpx.ConnectError` inside the task never propagates to the
  caller.
- [x] **5.3 — Critical-alert hook.** Call from the emitter choke point
  (`src/alerts/emitter.py:240 _emit`) after dedupe/persistence gates,
  for events ≥ `min_severity`, capped per session. Replay mode never
  notifies (emitter already knows replay via logger redirect — assert
  this in a test). — landed: suppression flag is set/cleared by the
  same `enter_replay_mode`/`exit_replay_mode` this session's Phase 7
  work already uses (no second replay-detection mechanism); verified
  no notification attempt happens at all during replay, not even a
  swallowed one.
- [x] **5.4 — Trip-end edge.** No session-close event exists today. Add
  a "trip ended" edge from `GpsTracker` (`src/analysis/gps_tracker.py:114
  trip_status` moving→stationary sustained N minutes) with daemon
  shutdown (`src/daemon/runtime.py:189-199`) as fallback trigger. —
  landed: requires genuine prior motion (no spurious fire for a
  session that starts already parked); `handle_trip_end` is idempotent
  so the GPS edge and the shutdown fallback can't double-fire for the
  same trip.
- [x] **5.5 — Digest.** On trip end: assemble `digest.json` into the
  session dir — duration, mode seconds, gated anomaly counts by source,
  top behavior-surface deviations, fingerprint deltas vs reference (top
  movers), new DTCs, new ledger events. Push a compact rendering via the
  notifier when enabled. — landed: anomaly counts + new-ledger-events
  are correctly windowed to the session (verified with an out-of-window
  fixture record that's correctly excluded); fingerprint top-movers
  skip stats missing from the reference epoch rather than fabricating
  a z-score.
- [x] **5.6 — Surfacing.** `GET /sessions/{id}/digest` +
  `get_session_digest` MCP tool; dashboard shows the last digest (can
  ride in an existing panel; a dedicated panel is optional). — landed
  in `TripPanel.tsx`. Review caught a real auth-consistency leak before
  landing: the endpoint was initially public, but the digest includes
  ledger events and fingerprint z-scores that are otherwise only
  readable via authenticated `/control/*` routes — moved to
  `GET /control/sessions/{id}/digest` (auth-gated), with the FE
  gating the fetch behind a control-token prompt instead of emitting a
  401.

---

## Phase 6 — Session catalog, artifact provenance, capture tripwire

**Goal.** Architecture cleanup: one queryable index over session
metadata; self-diagnosing laptop↔Pi model skew; a cheap counter that
tells us if Python capture ever drops frames (the standing answer to
"should the capture path be rewritten?" is "not until this counter says
so").

### 6A. Session catalog

- [x] **6.1 — Schema + store.** `src/storage/catalog.py`: SQLite,
  modeled directly on `src/companion/library.py` (`_SCHEMA:53`,
  `SCHEMA_VERSION` + `meta` migration table, `_tx` helper). One
  `sessions` table: session_id PK, path, started_at, duration_s,
  row_count, size_bytes, pinned, disposition, classifier_version,
  mode_seconds/event_counts/quality/fingerprint/dtc/digest as JSON
  columns, updated_at. **Sidecars remain source of truth; the catalog is
  a rebuildable index** — corruption recovery is `rebuild`, never data
  loss. — landed: `record_session` re-derives every column from
  on-disk sidecars/filesystem facts at call time (only `updated_at` is
  catalog-owned wall-clock metadata); verified corruption-recovery
  round-trips a rebuilt row identical to the original.
- [x] **6.2 — Config + scoping.** `DaemonConfig.catalog_db` (default
  `data/control/catalog.db`) + `scope_config` wiring. — landed; caught
  by this session's own exhaustive `test_scope_config_covers_every_daemon_path_field`
  guard as intended.
- [x] **6.3 — Rebuild + incremental update.** `scripts/catalog.py
  rebuild|status` (walk `discover_sessions` + read sidecars); update the
  row wherever sidecars are written (classification persist, pin/unpin,
  fingerprint, dtc, digest) and delete rows in the retention sweeper
  paths (`src/storage/retention.py`). — landed: `catalog_db` defaults to
  `None` (skip) at every low-level function, only real production call
  sites pass it explicitly. Review caught one real gap before landing —
  the on-demand digest HTTP route wasn't threading `catalog_db` through
  to `get_or_compute_digest` — fixed and covered by a route-level test.
- [x] **6.4 — Consumers.** (a) `parquet_query._matching_files`
  (`src/mcp_server/parquet_query.py:93`) — **fix the standing bug**: the
  bare `rglob` sweeps `_replay-*`/`_archive` Parquet into query results;
  filter underscore-prefixed dirs and use the catalog for session/date
  pushdown. **Keep an explicit-name escape hatch**: a query whose
  session scope names an underscore-prefixed session explicitly must
  still resolve it — `engine-tuning-support.md` Phase 2 analyzes
  `_tuning-*` sessions through this layer; only default/unscoped
  sweeps exclude them. (This supersedes item 3.5 of
  `archive/mcp-and-power-hardening.md` — **do not edit that doc**, another
  session owns it; note the supersession in the commit message only.) (b) `list_sessions`
  control listing.
  (c) Phase 4 trend queries read fingerprints from the catalog instead
  of globbing sidecars. — landed: (a) verified the old bare-rglob logic
  really would have swept in `_replay-*`/`_archive`/`_tuning-*`, the
  fix genuinely excludes them by default while an explicit `session=`
  name still resolves regardless of prefix; (b) `list_sessions` left
  filesystem-based — the currently-recording session has no catalog
  row until first classified, so a filesystem fallback would be needed
  regardless; (c) trends genuinely read the catalog's fingerprint
  column when populated, falling back to sidecar globbing otherwise.

### 6B. Model artifact provenance

- [x] **6.5 — Expose loaded-model provenance.** Manifest fields already
  exist (`src/models/manifest.py:62-75`: `model_version`, `created_at`,
  `training_data_hash`, `training_sessions`, spec version) but nothing
  surfaces them at serve. Add a provenance block per loaded model to the
  status payload (`src/dashboard/status.py build_status`) + visible in
  `get_status`. — landed: `_model_provenance()` reads real
  `ModelManifestCommon` fields; MCP `get_status` needed zero changes
  (confirmed it's a genuine dumb proxy of the `/status` JSON).
- [x] **6.6 — Skew check CLI.** `scripts/verify_artifacts.py [--remote
  canary@192.168.137.2]`: compare local vs Pi artifact file sha256 +
  manifest `training_data_hash`/`model_version`; report mismatches. This
  makes the scp deploy path self-diagnosing. Record artifact sha256 in
  the training ledger at train time. — landed: real argv-list `ssh`
  invocation (no `shell=True`, path args `shlex.quote()`-escaped, only
  a hash crosses the wire); `--remote`/`--compare-dir` mutually
  exclusive, no network call fires unprompted. Local-only path fully
  tested for real; the actual remote round-trip against
  `canary@192.168.137.2` is untested tonight (mocked-runner only) per
  this queue entry's own "remote test blocked" framing.
- [x] **6.7 — Load-time log line.** `ModelManager._maybe_load`
  (`src/models/manager.py:135`) logs version + training window +
  data-hash on every (re)load, so `tail_daemon_log` shows exactly what
  went live and when. (No hard corpus-vs-hash verification — retention
  legitimately deletes training sessions; don't over-engineer.) —
  landed: fires on both initial load and every hot-swap (one shared
  method, confirmed both entry points route through it).

### 6C. Capture tripwire

- [x] **6.8 — rx-overrun counter in status.** Read `can0` rx
  overrun/drop counters (`/sys/class/net/can0/statistics/` or `ip -s`)
  in the status builder; expose in `get_status` + the SystemPanel row.
  Nonzero growth during drives is the (only) trigger to revisit the
  capture architecture. — landed: `can0_counters` returns `None`
  gracefully (never crashes) when the interface is absent — genuinely
  exercised for real on this laptop (no `can0` here). **Real-hardware
  path confirmed 2026-07-22**: with the truck idling and real bus
  traffic flowing, `/status` genuinely populates the counter block
  (`rx_over_errors`/`rx_dropped`/`rx_errors`, all zero — clean bus,
  no drops observed) — the previously-noted fixture-only gap is
  closed.

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
| Legacy ELM guard | `src/collection/obd_collector.py:55 WRITE_COMMAND_MODES` {0x04,0x2E,0x2F}; `custom_pids.py:59 READ_ONLY_MODES` — path-local legacy restriction, not the active Canable policy |
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
