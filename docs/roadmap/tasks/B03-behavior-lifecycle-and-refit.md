# B03 — model demotion, baseline refit and rollout

**Depends on:** B01/B02 for validated publication. Inventory and preparing
demotions can start now. Each later B04–B07 release reuses the rollout steps.
Follow [B00](B00-behavior-models-README.md).

## Artifact lifecycle and consumers

- [ ] **B03.1 — Inventory the served and dependent artifacts.** Record local
  and, when reachable, deployed behavior-model versions, config/profile,
  calibration identity, overrides and validation evidence. Read vehicle state
  first; no access means deployed identity remains unknown. Enumerate IF/LSTM
  manifests using behavior deviation columns and all UI/API/MCP consumers.
- [ ] **B03.2 — Separate display, learned features and direct alerts.** Make
  the artifact's permitted uses explicit with a small typed policy shared by
  loader, preprocessing and daemon. A diagnostic-only surface stays available
  for inspection but cannot emit trusted standalone/fused alerts. Decide and
  validate its use as a learned feature separately; hiding an alert must not
  silently delete a column needed by an existing detector. Unknown/unvalidated
  artifacts expose that state instead of presenting a clean health result.
- [ ] **B03.3 — Apply the unconditional demotions.** Remove
  `timing_advance_vs_rpm_iat` from standalone alerting and quarantine the current
  `cat_warmup` artifact. Keep `timing_advance_vs_rpm_load` exploratory until B04
  qualifies it. Preserve historical fits/reports and raw channels. Prevent a
  partial refit or old artifact reload from silently restoring trusted status;
  test that demoted models do not contribute fusion votes.
- [ ] **B03.4 — Preserve downstream compatibility.** Sweep feature specs,
  model manifests, expected/deviation consumers and hot-reload caches before
  removing any column or changing its meaning. Use existing producibility
  checks to expose affected IF/sequence artifacts and coordinate their retrain
  with D02.5/D03.25. Do not reproduce the binding-retraction blindness incident
  by silently feeding missing or changed behavior features to old artifacts.

## Baseline refit

- [ ] **B03.5 — Freeze the corpus manifest.** Reuse the operator's existing
  corpus decisions and tune registry; record eligible physical drives,
  calibration, dispositions and capture recipe grants. Complete the D03.22
  mode-label prerequisite and regenerate affected derived artifacts/caches.
  Name missing corpus facts rather than selecting across calibration boundaries
  by convenience. Inspect legacy fits with absent tune provenance explicitly.
- [ ] **B03.6 — Produce isolated candidates.** Refit qualified retained
  recipes on the laptop, preserving grid/version provenance. Run B02 validation
  with untouched held-out drives and archive per-model pass/refusal reasons.
  An unsuccessful candidate cannot overwrite the qualified baseline or restore
  a quarantined prior through carry-over. Missing data is a partial task, not
  grounds to relax criteria.
- [ ] **B03.7 — Replay before promotion.** Compare baseline and candidate
  on the same known recordings, with isolated `_replay-*` outputs. Record
  coverage, refusal reasons, residuals, persisted events and fusion contribution,
  including no-data and calibration/context mismatches. Verify IF/sequence
  compatibility or deploy their coordinated replacements; no vehicle-health
  conclusion follows from a synthetic replay fault.

## Rollout and observability

- [ ] **B03.8 — Surface model status consistently.** Extend the existing
  behavior endpoint, dashboard and MCP response additively with artifact
  identity, permitted use, validation scope and current reason for abstaining.
  Test empty, demoted, failed, off-map and eligible states. Coordinate with
  W02/W06 instead of requiring their entire navigation redesign.
- [ ] **B03.9 — Deploy and verify the selected qualified set.** Use the
  existing artifact publication/transfer workflow and verify hashes on the Pi.
  Use the guarded restart script if a restart is necessary. Observe the
  requested versions and ordinary eligible observations; confirm demoted models
  remain non-alerting and off-map does not read as healthy. Record vehicle
  state, active capture and deployed build with the result.
- [ ] **B03.10 — Retain a recoverable prior and close exact debts.** Preserve
  the previous compatible artifact set and its evidence; exercise existing
  rollback/loading behavior locally. Update D03 only for overlapping work
  actually completed. Record outstanding hardware/data checks explicitly.

**Done when:** the chosen qualified set is deployed with measured replay/live
evidence, demotions cannot silently reverse, and dependent models remain
compatible. Optional B04–B07 candidates may remain withheld with named reasons.
