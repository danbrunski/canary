# Overnight implementation queue

Ordered work queue for unattended /goal runs (see
`.claude/commands/overnight.md` for the standing rules). **Task-level
truth lives in each roadmap doc's own checkboxes** — this file only
orders phase-level entries, records pre-made decisions, and pre-marks
what is structurally blocked overnight. Check an entry here only when
every non-blocked task in it is checked in its source doc.

Multi-night by design: the /goal condition bounds each night (turn
cap); progress persists in the source-doc checkboxes and git history.
Work strictly top to bottom — the order encodes real dependencies.

## Pre-made decisions (operator-approved defaults — do not re-litigate)

- **Fusion semantics** (ml-hardening 6.2): option (a) — fused events
  require `agreement >= 2`; per-source events remain the single-source
  channel. Update config docs accordingly.
- **Replay vs live** (ml-hardening 7.2): refuse `start_replay` while
  `vehicle_state == engine_running` (409 with reason), rather than
  pausing collectors.
- **Sequence backbone default** (ml-hardening 9.1): GRU-D LSTM becomes
  the default `sequence_detector`; TCN documented as NPU-export
  fallback.
- **Warmup confounding** (ml-hardening 4.6): implement option (a)
  (add a context axis) only if the corpus has >= 8 cold-start sessions
  with usable load/IAT spread — check first; otherwise do (b) widen
  bands + document, and log which path was taken.
- **`relative` → `sigma` rename** (ml-hardening 9.4): do it, with a
  backward-compat alias accepted at load and a deprecation note.

## Blocked-overnight markers (annotate in source doc, skip, move on)

Anything needing: the truck or a capture session; the Pi (ssh/scp/
deploy/restart/live-daemon verification); new hardware (IMU/baro);
operator history (ledger backfill facts)

---

## Queue

- [x] **Q1 — refactoring.md Phase 1.3 + 1.4** (drifted dups:
  `_numeric_value_columns` + shared resample/segment prefix; shared
  reconnect loop with GPS fail-fast). Do these before feature work
  touches the same files. *(1.1 and 1.2 come later — see Q10, Q13.)*
- [x] **Q2 — ml-correctness-hardening.md Phase 1** (stale-data
  visibility, live parity, vehicle-state gating). Task 1.6: laptop
  retrain + offline replay diff only; the Pi deploy half is
  blocked-overnight. — 1.1-1.5 landed; 1.6 laptop half landed
  (blocked-overnight: IF-side replay diff + Pi deploy).
- [x] **Q3 — ml-correctness-hardening.md Phase 8 + eval-longitudinal
  Phase 2** (eval realism + the fault-injection benchmark, built on
  Q2's corrected pipeline). Includes the 2.1/2.2 parity pre-flight
  fixes. Archive a baseline benchmark report to `data/analyses/`. —
  Phase 8 (8.1-8.4) + eval-longitudinal Phase 2 (2.1-2.7) all landed;
  baseline archived (commit `c5c9558`).
- [x] **Q4 — ml-correctness-hardening.md Phases 2 + 3** (IF hardening;
  calibration enforcement). Use the Q3 benchmark to decide 2.5
  (feature-redundancy trial) and 2.6 (ECOD — adopt only if it wins). —
  2.1-2.6 + 3.1-3.3 all landed (commits `3eda014`, `5a4a0c5`,
  `29e25e3`); 2.5/2.6 trialed on a fixed-seed synthetic corpus (real
  multi-session corpus retrial noted as future work) — both keep
  current defaults.
- [x] **Q5 — ml-correctness-hardening.md Phase 5** (mode taxonomy:
  close fall-throughs + dead zone, hysteresis). Then laptop-side
  `label_sessions.py --rebuild` + retrain + before/after mode-second
  comparison logged to the overnight log. Pi config/deploy half
  blocked-overnight. — 5.1-5.3 landed (commits `23413bb`, `9636926`,
  `5b51c14`); a real 0-5km/h dead zone caught by review before landing.
  Pi config sync + deploy blocked-overnight per this entry's own note.
- [x] **Q6 — ml-correctness-hardening.md Phase 4** (surface publish
  gates, schema sigma-floor + recipe migration, frozen 2D edges,
  bilinear serve, version-on-change, warmup per pre-made decision,
  segmenter-fallback diagnostic). Refit locally against stored
  sessions to validate the gates. — 4.1-4.7 landed (commit `5b68b9f`);
  `--dry-run` refit against real sessions confirms the publish gate
  refuses 4 genuinely-poor-fit recipes (`fcc2afb`).
- [x] **Q7 — ml-correctness-hardening.md Phases 6 + 7 + 9** (M-of-N
  persistence per doc; fusion per pre-made decision; benchmark rerun
  6.5 with intermittent-fault cases; replay in-memory isolation with
  the refuse-while-driving rule; portfolio tasks 9.1/9.3/9.4 per
  pre-made decisions; 9.2 DAMP spike only if time allows). This
  completes ml-correctness-hardening.md except Pi-deploy verifies. —
  6.1-6.5, 7.1-7.3, 9.1/9.3/9.4 all landed (commits `eb6f3f8`,
  `ac11f39`, `de126c4`, `c1efc75`); 9.2 skipped (optional, time). A real
  bug (7.1's unmatched-`exit_replay_mode()` silently wiping live
  dedupe state) caught by review and fixed before landing.
  `ml-correctness-hardening.md` is now fully checked off except
  Pi-deploy verifies (blocked-overnight throughout).
- [x] **Q8 — eval-longitudinal-and-dtc.md Phase 1** (DTC reader:
  transport, scheduler, endpoint, events, sidecar, MCP tool, DtcPanel).
  1.10 live verify blocked-overnight. — 1.1-1.9 landed (commits
  `f578cc0`, `df87965`); caught + fixed two real bugs in core OBD
  polling infra (DID-gating starvation, identifier=0 wire corruption).
  1.10 blocked-overnight, needs the Pi/truck.
- [x] **Q9 — eval-longitudinal-and-dtc.md Phases 3 + 4** (vehicle
  ledger — 3.6 backfill blocked-overnight, scaffold the file with a
  template entry instead; fingerprints + frozen reference epoch —
  freeze from all currently-`ready` classified sessions — trends,
  MCP/CLI/panel). — 3.1-3.5 + 4.1-4.6 all landed (commits `b18fc48`,
  `966b55a`, `cf7b650`); 3.6 blocked-overnight, scaffolded. Reference
  epoch really frozen from the 5 currently-`ready` local sessions
  (`7e27b2b`). Two real bugs (fabricated `voltage_at_crank_v`, an
  unscoped cross-vehicle-profile alert-state leak) caught by review and
  fixed before landing.
- [x] **Q10 — engine-analysis-and-mapping.md Phase 1 + refactoring 1.1**
  (wire derived channels live + TripSummary surface/panel/CLI; pair
  the PID-table single-sourcing with this since Q11 touches the same
  files next). — 1.1-1.7 + refactoring 1.1 all landed (commits
  `7cd9542`, `3530e2b`, `777fecc`). Two real bugs caught by review
  before landing: the initial derived-channel pass shipped WS-only,
  missing the task's explicit "and land in Parquet" clause (fixed);
  and refactoring 1.1's originally-offered direction (derive
  `PID_DECODERS` from the registry) turned out to be a genuine circular
  import, independently reproduced, so the reverse direction was used
  instead.
- [x] **Q11 — engine-analysis-and-mapping.md Phase 2, code-only slice**
  (2.1 wideband λ into `PID_DECODERS`; 2.3 absolute-load fix; 2.6
  custom-PID wiring + example file; 2.8 UI). 2.2/2.5/2.7 (captures)
  and 2.4's live verification blocked-overnight. — 2.1/2.3/2.6/2.8 all
  landed (commits `dfe24b4`, `aa0127d`, `bac96e4`). 2.2/2.4/2.5/2.7
  remain blocked-overnight, need the truck/real captures.
- [x] **Q12 — eval-longitudinal-and-dtc.md Phases 5 + 6** (notifier +
  digest, shipped disabled-by-default — enabling/ntfy topic is a
  morning task; trip-end edge; session catalog + consumers incl. the
  `parquet_query` underscore-dir fix; provenance surfacing + load-time
  log; rx-overrun counter — live verify blocked-overnight; 6.6 remote
  skew-compare code yes, remote test blocked). — 5.1-5.6 + 6.1-6.8 all
  landed (commits `5b513a6`, `5ef6212`, `5d93473`). Two real bugs
  caught by review: a digest-endpoint auth-consistency leak (Phase 5)
  and a digest-route catalog-wiring gap (Phase 6).
- [x] **Q13 — refactoring.md Phase 2 (minus 2.2) + Phase 1.2** — landed
  (commits `0d7498d`, `aaa696e`, plus 2.3/2.4/2.5 this task). 1.2
  (companion↔control dedup) and 2.1 (LSTM/TCN trainer unification)
  landed earlier; 2.3 (`audited_post` + write-tool pure-read move),
  2.4 (`scripts/_cli.py`), 2.5 (`control.py` `_require` helper) landed
  now. 2.2 (MCP server registration table) annotated `[~]
  blocked-overnight` in refactoring.md — still blocked on the other
  session's `server.py` work; correctly not attempted.
- [x] **Q14 — engine-analysis-and-mapping.md Phase 3 code-only** —
  landed (3.1-3.5 all done): Mode 06 wiring on Q8's scheduler
  (rate-limited independent of real-time PID bandwidth, increase-only
  misfire emission) + `GET /obd/monitors`/`get_monitors` MCP tool +
  `MonitorsPanel.tsx`; misfire counts in the fingerprint stat set; Mode
  02 freeze frame into `dtc.json` (one-shot trigger, atomic sidecar
  write); Mode 09 cal-ID/CVN at startup with corpus-boundary ledger
  entry on a genuine cal-ID change (not on first-ever profile probe).
  All live-truck verification correctly left blocked-overnight; code +
  fixture/mocked tests only.
- [x] **Q15 — engine-analysis-and-mapping.md Phases 4 + 5, doable
  slice** — landed (commits `e1fcf96`, `b843a3b`): 4.1/4.3/4.4/4.5/4.6
  (VE table, total-trim map, catalyst-efficiency index, O2 health
  stats, gear detection via a new per-vehicle-profile ratio table);
  4.2 and 4.8 confirmed still blocked on their stated dependencies,
  correctly left unchecked (4.9 UI panel deferred, not part of this
  doable slice). Phase 5 UX tier all landed: binned endpoint + Explore
  panel, historical session charting (also fixed a real companion-app
  gap), A/B comparison (cell-matching against the fitted surface's own
  bin edges, verified genuinely comparable across sessions), chart
  ergonomics (one real bug caught by review: the shared-Y-axis logic
  shipped without a reachable UI toggle — fixed same task), GPX/KML +
  export UI.
- [x] **Q16 — refactoring.md Phases 3 + 4** — landed (commit
  `e9bb206`, plus earlier `1d55739`/`85ecf40` for the FE-only slice):
  3.1 Detector protocol (replay-verified; one real bug caught by
  review — `SequenceDetector.model_version` not resetting on a gated
  tick — fixed and re-verified before landing), 3.4 JsonlStore, 3.5
  shared artifact IO (isolation_forest.py's literal file-split
  deliberately skipped, ~30 callers, artifact-IO duplication itself
  resolved), 4.1-4.4/4.7 quick sweeps. FE-only slice (3.2, 3.3, 4.5,
  4.6) landed earlier from the `canary-fe` worktree. This closes out
  refactoring.md entirely except 2.2, which stays
  open for the main worktree.

## Morning-review backlog this queue generates

Pi deploys + live verifies (Q2/Q5/Q8/Q12/Q14), ledger backfill facts
(Q9), ntfy topic + enable notifications (Q12), capture sessions for
wideband probe / PW scale / DID pinning (Q11), refactoring 2.2
coordination, and any `blocked-overnight` annotations added en route.

---

# Round 2 — after Q16

Starts only when Q1–Q16 are done. Sources: `online-training-strategy.md`,
`imu-gps-integration.md`, `llm-assist-and-compute-roadmap.md` (each now
has an "Implementation checklist" section — task truth lives there),
`dashboard-ux.md` (2026-07-19 UX review — surface built-but-invisible
backend features), plus the unchecked tail of
`docs/implementation-tasks.md` Phase 6/8 and the one open box in
`data-collection-plan.md` §4.

## Round-2 pre-made decisions (operator-approved defaults)

- **`accepted_baselines.yaml` ships empty** (schema + commented example,
  zero active overrides). Authoring real overrides is a morning task —
  never invent an override for the actual truck overnight.
- **Retrain scheduler default disabled** (`online_training.enabled:
  false`); systemd-timer alternative documented, not installed. Golden
  baseline: build the mechanics, but *declaring* the golden run is an
  operator morning action.
- **Promotion gate semantics** (online-training 3.3): challenger promotes
  only on held-out validation + agreement-with-golden within tolerance;
  a scheduled retrain can never auto-`active`.
- **6.38 Tauri/Electron packaging: not queued** — heavy new dependency
  surface; the repo-runner stays. Revisit interactively.
- **6.5 Mode 04 clear-DTC: not queued** — it is a deliberate write-path
  exception to read-only CANary and needs an explicit operator decision
  plus the truck. Leave unchecked; do not implement overnight.
- **All IMU work targets `source: disabled` default** and must be fully
  testable with synthetic data (BNO055 is the assumed sensor per the
  doc). New committees land `trainable=False` compute-but-exclude.

## Queue — round 2

- [x] **Q17 — online-training-strategy.md Phase 1** — landed (commit
  `8357da1`): FSM physical limits table traced to real decode formulas,
  per-cell reject gate at both train and score time flowing through
  existing missingness machinery, profile-scoped `accepted_baselines.yaml`
  schema/loader (zero active overrides, per pre-made decision),
  disposition classifier + ledger summary field, disposition report CLI
  run over the stored corpus and committed. Also fixed a pre-existing
  test-isolation bug (`test_log.py` leaking root-logger state) that had
  caused an intermittent CI failure, in its own commit (`0b7781f`).
- [x] **Q18 — online-training-strategy.md Phase 2** — landed (commit
  `c580cea`): directional `DeviationConfig`, override→alert path
  (`review_by` expiry verified inert), dashboard overrides card, docs.
  One real gap caught by adversarial review and fixed before landing:
  raw-channel-targeted overrides were parsed/counted but never
  evaluated live — wired into `_observe_behavior`, re-verified.
- [ ] **Q19 — online-training-strategy.md Phase 3** (rolling-window
  retrain, disabled-by-default scheduler, champion/challenger promotion
  gate, golden-baseline mechanics + divergence signal, runbook).
  Enabling on the Pi + declaring golden are morning tasks.
- [ ] **Q20 — companion offline workflow: implementation-tasks 6.34
  (remaining: resumable/chunked sync, checksums, retry) + 6.35 offline
  dashboard/library mode + 6.36 training target selector + 6.37
  (remaining: queue/retry semantics, compatibility report). Test
  against the local companion API with fixture sessions; 6.39 (full
  Pi round-trip proof) blocked-overnight.**
- [ ] **Q21 — corpus + capture hygiene: implementation-tasks 6.19
  (proprietary-protocol regression corpus from stored Mode 22/DTC
  exchanges) + data-collection-plan §4 Canable-native broad DID sweep
  CLI (bench verify blocked) + implementation-tasks 8.6 record-arming
  gate (design doc + config-gated implementation, default off, live
  verify blocked).**
- [ ] **Q22 — imu-gps-integration.md Phase 2a** (imu source plumbing,
  ImuConfig, decimation, imu_frame math, offline autotagger — GPS-only
  tags must work on existing sessions now — live tag hook, segmenter
  accel modes, committee declarations). Phase 2b entirely
  blocked-overnight (hardware).
- [ ] **Q23 — llm-assist-and-compute-roadmap.md seams checklist**
  (structured-card contract, card renderer with absent-state, enriched
  anomaly-review labels into feedback + retrain, import-isolation
  guard test). No model hosting.
- [ ] **Q24 — dashboard-ux.md, all phases** (vehicle_state badge +
  replay banner; replay control card; Sessions panel extraction from
  ControlPanel — coordinate with Q16's god-panel splits, which land
  first; replay_window re-score button on ModelEvidencePanel;
  shell-level token entry — merge with Q16's ResourceGate/TokenField,
  don't duplicate; DiagnosticsPanel deletion + README dashboard-section
  refresh). Everything testable against fixtures/local daemon; Pi live
  verify is a morning task.
- [ ] **Q25 — reconciliation sweep.** Audit `docs/implementation-tasks.md`
  checkboxes against everything rounds 1–2 landed and close the loops:
  6.4's live-DTC remainder vs the Q8 reader, 6.7's GPX/KML vs Q15,
  3.5's remaining note, 9.7's default-backbone decision vs Q7, 1.16 /
  4.E.4 status notes pointing at the capture backlog. Update
  cross-references and `docs/CANary-overview.md` checkmarks. Pure doc
  work, no code.

## Round-2 morning backlog

Hardware purchases + wiring (BNO055/BMP390/10 Hz GPS w/ PPS — Q22's 2b),
authoring real accepted-baseline overrides from the Q17 disposition
report, declaring the golden baseline + enabling the scheduler (Q19),
the 6.5 Mode 04 decision, 6.28/6.29 acceptance runs, 6.39 offline-loop
proof with the Pi, live verify of the Q24 dashboard surfaces on the Pi
(replay banner/card against a real `start_replay`, vehicle_state badge
across key-on/engine-off states), and the data-collection §2 drive
backlog (cold-start, load-sweep, A/C pairs, off-road) that Q17's report
will make more valuable.
