# B03 — model demotion, baseline refit and rollout

**Depends on:** B01/B02 for validated publication. Inventory and preparing
demotions can start now. Each later B04–B07 release reuses the rollout steps.
Follow [B00](B00-behavior-models-README.md).

## Artifact lifecycle and consumers

- [~] **B03.1 — Inventory the served and dependent artifacts.** Record local
  and, when reachable, deployed behavior-model versions, config/profile,
  calibration identity, overrides and validation evidence. Read vehicle state
  first; no access means deployed identity remains unknown. Enumerate IF/LSTM
  manifests using behavior deviation columns and all UI/API/MCP consumers.
  *Local side done 2026-09-21; deployed identity unknown.* `tools/inventory_behavior_artifacts.py`
  over `src/analysis/behavior_inventory.py` records the artifact (hash, per-model
  `artifact_identity` fingerprint/version/fitted_at/tune), policy, criteria, tune
  registry (in force `ZS44A-20260920`; **no shipped model carries a
  `tune_revision`**), overrides (none), every scoped IF/sequence manifest with
  the `deviation:*` columns it consumes (none do) and a deployed comparison
  that is `unknown` without a saved `/models/behavior` snapshot. The Pi was not
  contacted, so vehicle state was not read. UI/API/MCP consumers enumerated in
  the [inventory](../../../data/analyses/2026-09-21-behavior-artifact-inventory.md).
  **Remaining:** read `/status.vehicle_state`, save the deployed
  `/models/behavior` and run `--deployed-snapshot` (with B03.9).
- [x] **B03.2 — Separate display, learned features and direct alerts.** Make
  the artifact's permitted uses explicit with a small typed policy shared by
  loader, preprocessing and daemon. A diagnostic-only surface stays available
  for inspection but cannot emit trusted standalone/fused alerts. Decide and
  validate its use as a learned feature separately; hiding an alert must not
  silently delete a column needed by an existing detector. Unknown/unvalidated
  artifacts expose that state instead of presenting a clean health result.
  *Done 2026-09-21:* `behavior_models.PermittedUse` (`display`,
  `learned_feature`, `alert`) is part of `serving_status` (additive
  `lifecycle` validated/unvalidated/exploratory/demoted/quarantined,
  `permitted_uses`, `policy`). Profile data `behavior_policy.yaml` holds typed
  `LifecyclePolicyEntry`s that can only withdraw uses (`alert` refused,
  `display` required), applied inside `load_behavior_models` — the one loader
  daemon, hot reload, dashboard, MCP, replay, refit and feature pipeline use.
  Daemon: `BehaviorDetector` emits/fuses only `alert`. Preprocessing: IF
  selection (`scripts/train_anomaly_model.train`) drops deviation columns whose
  model withholds `learned_feature` (`behavior_decoder.withheld_learned_feature_columns`,
  recorded as `behavior_learned_feature_withheld` on the spec) while
  `append_deviation_columns` keeps producing every column. Learned-feature
  decision: unvalidated models keep `learned_feature` (their deviation is a
  deterministic transform an IF learns the distribution of; removing it would
  change current training behaviour), the three demoted/quarantined/exploratory
  models withhold it. Evidence: `tests/test_behavior_lifecycle.py::test_policy_can_only_withdraw_uses`,
  `::test_validated_model_is_the_only_alerting_lifecycle`,
  `::test_unvalidated_artifact_exposes_that_state`.
- [x] **B03.3 — Apply the unconditional demotions.** Remove
  `timing_advance_vs_rpm_iat` from standalone alerting and quarantine the current
  `cat_warmup` artifact. Keep `timing_advance_vs_rpm_load` exploratory until B04
  qualifies it. Preserve historical fits/reports and raw channels. Prevent a
  partial refit or old artifact reload from silently restoring trusted status;
  test that demoted models do not contribute fusion votes.
  *Done 2026-09-21:* [`data/nissan-xterra-2008/behavior_policy.yaml`](../../../data/nissan-xterra-2008/behavior_policy.yaml):
  `timing_advance_vs_rpm_iat` demoted and `timing_advance_vs_rpm_load`
  exploratory by model name (a future refit stays covered until the entry is
  removed), the current `cat_warmup` quarantined by `artifact_fingerprint`
  (name+kind+output+whole fit; follows the artifact through carry-over and
  restore, a new fit must earn trust through B02). All three: `display` only.
  The artifact, fits, reports and raw channels are untouched. Hot reload
  includes the policy file in its stamp and reloads on any change (a
  restored older-mtime artifact too). Evidence: `tests/test_behavior_lifecycle.py::test_shipped_demotions_apply_through_the_one_loader`,
  `::test_demotion_survives_a_refit_that_passes_validation`,
  `::test_quarantine_follows_the_artifact_through_carry_over`,
  `::test_a_new_catalyst_fit_is_not_covered_by_the_quarantine`,
  `::test_demoted_model_contributes_no_fusion_vote` (validated control votes,
  demoted copy emits nothing and adds no fusion source),
  `::test_hot_reload_applies_a_policy_edit_and_a_rollback`,
  `tests/test_refit_behavior_models_script.py::test_main_report_applies_profile_policy_and_preserves_prior`.
- [~] **B03.4 — Preserve downstream compatibility.** Sweep feature specs,
  model manifests, expected/deviation consumers and hot-reload caches before
  removing any column or changing its meaning. Use existing producibility
  checks to expose affected IF/sequence artifacts and coordinate their retrain
  with D02.5/D03.25. Do not reproduce the binding-retraction blindness incident
  by silently feeding missing or changed behavior features to old artifacts.
  *Swept 2026-09-21:* no tracked IF/sequence manifest consumes any
  `deviation:*` column ([inventory](../../../data/analyses/2026-09-21-behavior-artifact-inventory.md)),
  so the demotions strand nothing; demotion withdraws selection only, never
  production. `spec_producibility.behavior_feature_conflicts` (reported by
  `tools/check_manifest_producibility.py` as `WARN` beside the unproducible
  columns) names a consumed column whose model withholds `learned_feature`.
  Evidence: `tests/test_behavior_lifecycle.py::test_demoted_deviation_column_is_still_produced_but_not_selectable`,
  `::test_tracked_manifests_consume_no_demoted_behavior_column`.
  **Remaining:** manifests do not record which behavior artifact produced a
  consumed deviation column, so a refit of an unvalidated model under a future
  consumer would change its meaning undetected — record behavior fingerprints
  in `feature_pipeline.build_feature_spec` and compare them in the
  producibility check (left out: `feature_pipeline.py` was being edited
  concurrently). No retrain to coordinate with D02.5/D03.25 today.

## Baseline refit

- [ ] **B03.5 — Freeze the corpus manifest.** Reuse the operator's existing
  corpus decisions and tune registry; record eligible physical drives,
  calibration, dispositions and capture recipe grants. Complete the D03.22
  mode-label prerequisite and regenerate affected derived artifacts/caches.
  Name missing corpus facts rather than selecting across calibration boundaries
  by convenience. Inspect legacy fits with absent tune provenance explicitly.
  *Not started — deferred by the operator 2026-09-22 ("don't retrain yet").*
  The D03.22 laptop relabel it depended on is done (116 sidecars at classifier
  0.3.0); the corpus choice itself is still open. The one provenance question
  is settled: `2026-07-26T214348` is excluded in `behavior_validation.yaml`.
- [ ] **B03.6 — Produce isolated candidates.** Refit qualified retained
  recipes on the laptop, preserving grid/version provenance. Run B02 validation
  with untouched held-out drives and archive per-model pass/refusal reasons.
  An unsuccessful candidate cannot overwrite the qualified baseline or restore
  a quarantined prior through carry-over. Missing data is a partial task, not
  grounds to relax criteria.
  *Not started:* needs B03.5 and a refit.
- [ ] **B03.7 — Replay before promotion.** Compare baseline and candidate
  on the same known recordings, with isolated `_replay-*` outputs. Record
  coverage, refusal reasons, residuals, persisted events and fusion contribution,
  including no-data and calibration/context mismatches. Verify IF/sequence
  compatibility or deploy their coordinated replacements; no vehicle-health
  conclusion follows from a synthetic replay fault.
  *Not started:* needs B03.6 candidates.

## Rollout and observability

- [x] **B03.8 — Surface model status consistently.** Extend the existing
  behavior endpoint, dashboard and MCP response additively with artifact
  identity, permitted use, validation scope and current reason for abstaining.
  Test empty, demoted, failed, off-map and eligible states. Coordinate with
  W02/W06 instead of requiring their entire navigation redesign.
  *Done 2026-09-21:* `GET /models/behavior` adds per model `artifact`
  (`artifact_identity`), `validation_scope` (verdict, drives, tune, failures,
  contract match; `verdict: none` when absent) and the extended
  `serving_status`, plus a top-level `policy` block (file, present, unmatched
  entries). `/models/behavior/live` adds `lifecycle`, `permitted_uses`,
  `artifact_version` and `abstain_reason` (mode mismatch, the tick's refusal,
  else `not_alerting:<lifecycle>`). MCP `get_behavior_models`/`get_behavior_live`
  pass these through; `get_behavior_deviations` carries the extended
  `serving_status`. Model › Behavior panel shows a lifecycle chip,
  alerting/not alerting, uses, policy reason, artifact identity (version,
  fingerprint, fitted date, tune or "tune unknown"), validation scope ("none
  recorded" in warn colour) and "no prediction — <reason>" for an ineligible
  tick instead of calling it off-map. Documented in `docs/headless-api.md`.
  Evidence: `tests/test_behavior_lifecycle.py::test_behavior_endpoint_states`,
  `::test_live_abstain_reasons`, `tests/test_dashboard_api.py` (empty state),
  `src/dashboard/static/src/panels/BehaviorModelsPanel.test.ts` "status (B03.8)"
  (empty, eligible, demoted, failed, off-map, ineligible).
- [ ] **B03.9 — Deploy and verify the selected qualified set.** Use the
  existing artifact publication/transfer workflow and verify hashes on the Pi.
  Use the guarded restart script if a restart is necessary. Observe the
  requested versions and ordinary eligible observations; confirm demoted models
  remain non-alerting and off-map does not read as healthy. Record vehicle
  state, active capture and deployed build with the result.
  *Not started:* needs the qualified set (B03.6/B03.7) and the Pi.
- [~] **B03.10 — Retain a recoverable prior and close exact debts.** Preserve
  the previous compatible artifact set and its evidence; exercise existing
  rollback/loading behavior locally. Update D03 only for overlapping work
  actually completed. Record outstanding hardware/data checks explicitly.
  *Local part done 2026-09-21:* `scripts/refit_behavior_models.py::preserve_prior_artifact`
  copies the artifact being replaced and its `.report.json` (the evidence
  lives inside the YAML) to `<profile>/_prior/<UTC stamp>/` before any
  write; rollback is copying them back, which hot reload picks up even with
  the older mtime, and the policy still governs the restored artifact
  (`tests/test_behavior_lifecycle.py::test_hot_reload_applies_a_policy_edit_and_a_rollback`,
  `::test_refit_preserves_the_prior_artifact_and_its_report`). The currently
  shipped set is also recoverable from git (`b9356bd`). No D03 item was closed
  by this work. **Remaining:** preserve the Pi's deployed set before B03.9
  (deployed identity unknown), and the outstanding hardware/data checks —
  deployed hash verification, live observation of demoted models staying
  non-alerting, D03.22 relabel.

**Done when:** the chosen qualified set is deployed with measured replay/live
evidence, demotions cannot silently reverse, and dependent models remain
compatible. Optional B04–B07 candidates may remain withheld with named reasons.
