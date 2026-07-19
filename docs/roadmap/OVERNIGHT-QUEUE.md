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
operator history (ledger backfill facts); or coordination with the
other session (`mcp-and-power-hardening.md` — never touch).

---

## Queue

- [ ] **Q1 — refactoring.md Phase 1.3 + 1.4** (drifted dups:
  `_numeric_value_columns` + shared resample/segment prefix; shared
  reconnect loop with GPS fail-fast). Do these before feature work
  touches the same files. *(1.1 and 1.2 come later — see Q10, Q13.)*
- [ ] **Q2 — ml-correctness-hardening.md Phase 1** (stale-data
  visibility, live parity, vehicle-state gating). Task 1.6: laptop
  retrain + offline replay diff only; the Pi deploy half is
  blocked-overnight.
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
