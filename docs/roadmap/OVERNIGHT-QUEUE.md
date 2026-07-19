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
- [ ] **Q3 — ml-correctness-hardening.md Phase 8 + eval-longitudinal
  Phase 2** (eval realism + the fault-injection benchmark, built on
  Q2's corrected pipeline). Includes the 2.1/2.2 parity pre-flight
  fixes. Archive a baseline benchmark report to `data/analyses/`.
- [ ] **Q4 — ml-correctness-hardening.md Phases 2 + 3** (IF hardening;
  calibration enforcement). Use the Q3 benchmark to decide 2.5
  (feature-redundancy trial) and 2.6 (ECOD — adopt only if it wins).
- [ ] **Q5 — ml-correctness-hardening.md Phase 5** (mode taxonomy:
  close fall-throughs + dead zone, hysteresis). Then laptop-side
  `label_sessions.py --rebuild` + retrain + before/after mode-second
  comparison logged to the overnight log. Pi config/deploy half
  blocked-overnight.
- [ ] **Q6 — ml-correctness-hardening.md Phase 4** (surface publish
  gates, schema sigma-floor + recipe migration, frozen 2D edges,
  bilinear serve, version-on-change, warmup per pre-made decision,
  segmenter-fallback diagnostic). Refit locally against stored
  sessions to validate the gates.
- [ ] **Q7 — ml-correctness-hardening.md Phases 6 + 7 + 9** (M-of-N
  persistence per doc; fusion per pre-made decision; benchmark rerun
  6.5 with intermittent-fault cases; replay in-memory isolation with
  the refuse-while-driving rule; portfolio tasks 9.1/9.3/9.4 per
  pre-made decisions; 9.2 DAMP spike only if time allows). This
  completes ml-correctness-hardening.md except Pi-deploy verifies.
- [ ] **Q8 — eval-longitudinal-and-dtc.md Phase 1** (DTC reader:
  transport, scheduler, endpoint, events, sidecar, MCP tool, DtcPanel).
  1.10 live verify blocked-overnight.
- [ ] **Q9 — eval-longitudinal-and-dtc.md Phases 3 + 4** (vehicle
  ledger — 3.6 backfill blocked-overnight, scaffold the file with a
  template entry instead; fingerprints + frozen reference epoch —
  freeze from all currently-`ready` classified sessions — trends,
  MCP/CLI/panel).
- [ ] **Q10 — engine-analysis-and-mapping.md Phase 1 + refactoring 1.1**
  (wire derived channels live + TripSummary surface/panel/CLI; pair
  the PID-table single-sourcing with this since Q11 touches the same
  files next).
- [ ] **Q11 — engine-analysis-and-mapping.md Phase 2, code-only slice**
  (2.1 wideband λ into `PID_DECODERS`; 2.3 absolute-load fix; 2.6
  custom-PID wiring + example file; 2.8 UI). 2.2/2.5/2.7 (captures)
  and 2.4's live verification blocked-overnight.
- [ ] **Q12 — eval-longitudinal-and-dtc.md Phases 5 + 6** (notifier +
  digest, shipped disabled-by-default — enabling/ntfy topic is a
  morning task; trip-end edge; session catalog + consumers incl. the
  `parquet_query` underscore-dir fix; provenance surfacing + load-time
  log; rx-overrun counter — live verify blocked-overnight; 6.6 remote
  skew-compare code yes, remote test blocked).
- [ ] **Q13 — refactoring.md Phase 2 (minus 2.2) + Phase 1.2**
  (LSTM/TCN trainer unification — deliberately AFTER Q4 so the
  calibration-enforcement edits land once in the unified trainer;
  `audited_post`; script CLI preamble; control.py boilerplate;
  companion↔control route dedup). 2.2 (MCP server table) stays blocked
  until the other session's server.py work is confirmed landed.
- [ ] **Q14 — engine-analysis-and-mapping.md Phase 3 code-only**
  (Mode 06 wiring on Q8's scheduler + MonitorsPanel; Mode 02 freeze
  frame into dtc.json; Mode 09 cal-ID at startup). All live-truck
  verification blocked-overnight.
- [ ] **Q15 — engine-analysis-and-mapping.md Phases 4 + 5, doable
  slice** (VE table, total-trim map, catalyst-efficiency index, O2
  health stats, gear detection with ratio table derived from stored
  sessions; duty cycle 4.2 blocked on the PW-scale capture; 4.8
  blocked on IMU. Then the UX tier: binned endpoint + Explore panel,
  historical session charting, A/B comparison, chart ergonomics,
  GPX/KML + export UI).
- [ ] **Q16 — refactoring.md Phases 3 + 4** (Detector protocol — safe
  now that Q7 changed persistence semantics; FE god-panel splits +
  ResourceGate/TokenField; JsonlStore; isolation_forest split +
  artifact IO; all quick sweeps not already absorbed).

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
plus the unchecked tail of `docs/implementation-tasks.md` Phase 6/8 and
the one open box in `data-collection-plan.md` §4.

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

- [ ] **Q17 — online-training-strategy.md Phase 1** (FSM physical
  limits, reject gate at train+score, `accepted_baselines.yaml`
  schema/loader, disposition classifier + ledger summary, disposition
  report CLI run over the stored corpus with the report committed).
- [ ] **Q18 — online-training-strategy.md Phase 2** (directional
  `DeviationConfig`, override→alert path incl. `review_by` expiry
  behavior, dashboard overrides card, docs).
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
- [ ] **Q24 — reconciliation sweep.** Audit `docs/implementation-tasks.md`
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
proof with the Pi, and the data-collection §2 drive backlog (cold-start,
load-sweep, A/C pairs, off-road) that Q17's report will make more
valuable.
