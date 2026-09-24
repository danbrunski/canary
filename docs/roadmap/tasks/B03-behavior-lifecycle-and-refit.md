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
- [x] **B03.4 — Preserve downstream compatibility.** Sweep feature specs,
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
  *Completed 2026-09-22:* `feature_pipeline.build_feature_spec(...,
  behavior_models=)` records `behavior_model_fingerprints` — the
  `artifact_fingerprint` of every behavior model a consumed `deviation:*`
  column names (`consumed_behavior_fingerprints`; a consumed column whose
  producer is not in the recorded set refuses as a stale feature cache). Both
  trainers pass the loaded set (`scripts/train_anomaly_model.py`,
  `scripts/train_sequence.py`). `spec_producibility.behavior_fingerprint_provenance`
  compares the record against the serving set per model — `match` /
  `mismatch` (refit since training: `tools/check_manifest_producibility.py`
  exits 1 and names the retrain, D02.5/D03.25) / `unknown` (no record —
  every tracked manifest today; reported, not judged) / `absent`; the
  inventory carries it as `behavior_fingerprint_provenance`. Evidence:
  `tests/test_behavior_lifecycle.py::test_fresh_spec_records_the_behavior_artifact_that_produced_each_consumed_column`,
  `::test_refit_behavior_artifact_is_flagged_against_the_consuming_manifest`
  (same contract fingerprint, different artifact → mismatch),
  `::test_manifest_without_fingerprints_is_unknown_provenance_not_a_pass`,
  `tests/test_manifest_producibility.py::test_cli_flags_a_refit_behavior_artifact_and_reports_unknown_provenance`.
  No retrain to coordinate with D02.5/D03.25 today (deferred, operator
  2026-09-22).

## Baseline refit

- [x] **B03.5 — Freeze the corpus manifest.** Reuse the operator's existing
  corpus decisions and tune registry; record eligible physical drives,
  calibration, dispositions and capture recipe grants. Complete the D03.22
  mode-label prerequisite and regenerate affected derived artifacts/caches.
  Name missing corpus facts rather than selecting across calibration boundaries
  by convenience. Inspect legacy fits with absent tune provenance explicitly.
  *Done 2026-09-23 (operator corpus decision 2026-09-23: current calibration
  only).* [Manifest](../../../data/analyses/2026-09-23-behavior-corpus-manifest.yaml): `resolve_session_revision() == ZS44A-20260920`
  selects **53 directories** (40 sessions + 13 captures, 482 MB) under
  `~/canary-data/sessions/nissan-xterra-2008`. Each one is pinned by content
  hash with its start time, row range, revision source, capture profile and
  sidecar grants, disposition and events, and drive. Of the 86 directories
  *named* 2026-09-20..23, the resolver places 33 on ZS44A-k106 (started
  before 2026-09-21T03:08:45Z). There are no flash crossings. The drives are
  6 groups at the 600 s join gap; the parked overnight recording
  `2026-09-23T032755` joins an evening and a morning drive (recorded).
  D03.22 prerequisite: `label_sessions.py` (classifier 0.3.0, Pi taxonomy) ran
  on every non-capture session. 38 sidecars were written and 2 were already
  current; dispositions came out 14 ready / 7 review / 17 quarantine,
  recorded rather than used as a filter. Capture grants: explicit-path
  selection (the CLI's operator-grant semantics), with each sidecar
  allowlist recorded beside it. Legacy fits: 7 of 8 published models carry
  no `tune_revision` and trained on ZS04C-stock/ZS44A-osiris-initial
  sessions, and `air_charge` is ZS44A-k106. None is on the current
  calibration ([report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §0).
- [~] **B03.6 — Produce isolated candidates.** Refit qualified retained
  recipes on the laptop, preserving grid/version provenance. Run B02 validation
  with untouched held-out drives and archive per-model pass/refusal reasons.
  An unsuccessful candidate cannot overwrite the qualified baseline or restore
  a quarantined prior through carry-over. Missing data is a partial task, not
  grounds to relax criteria.
  *Candidates produced 2026-09-23; none qualifies.* `scripts/refit_behavior_models.py`
  over the 53 manifest paths wrote to `data/nissan-xterra-2008/_candidates/2026-09-23/`,
  seeded with a copy of the published artifact so versions bump and prior
  grids carry (`grid_version` 1). The refit did not touch the published
  artifact; it was replaced afterwards by the operator-approved promotion
  (B03.9). 10 recipes fitted with B02 evidence on untouched drives; every verdict is
  `failed`, and per-model reasons are archived in the [report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §2 and
  `behavior_models.yaml.report.json`. All 10 candidates' persisted held-out
  numbers reproduce exactly through the independent evaluator frame path.
  The carried-over `cat_warmup` stays quarantined and `timing_advance_vs_rpm_iat`
  stays demoted, both flagged `contract_changed:recipe`. **Found and fixed:**
  exposure counted 11 h of a parked truck inside one drive group, so the
  first run stamped warm idle and STFT bank balance city/highway `passed`
  with alert. With the fix (`EVALUATOR_VERSION` 3,
  `tests/test_behavior_validation.py::test_parked_hours_inside_one_drive_are_not_exposure`)
  they fail at 8.89 / 2.86 / 13.9 control events/h (§1). **Remaining
  (data-limited, criteria unchanged):** warmup recipes (2 cold starts on
  this calibration, `min_sessions` 3), cam (commanded cam never polled),
  TPS (244 rows < 300), manifold VE (no MAP), LTFT balance (LTFT
  constantly 0.0).
- [~] **B03.7 — Replay before promotion.** Compare baseline and candidate
  on the same known recordings, with isolated `_replay-*` outputs. Record
  coverage, refusal reasons, residuals, persisted events and fusion contribution,
  including no-data and calibration/context mismatches. Verify IF/sequence
  compatibility or deploy their coordinated replacements; no vehicle-health
  conclusion follows from a synthetic replay fault.
  *Offline equivalent done 2026-09-23.* The evaluator (`evaluate()`: serving
  contract, then the daemon's `PersistenceGate` + dedupe) ran the published
  artifact on each candidate's own test drives, on identical eligible rows.
  [Report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §3 and [JSON](../../../data/analyses/2026-09-23-behavior-refit-comparison.json) `baseline_vs_candidate_same_test_drives`:
  - coverage and off-map: timing 6976→7261, air mass 6516→7238
  - residuals: fuel-trim MAE 5.39→4.09 %, timing warning width 23.1→13.2°
  - persisted events: fuel trim 44→6, VE 17→0; warm idle 6→6 (bias
    +4.8→+17.3 rpm)
  - refusals: no-data refusals dominate (`engine_state_stale:obd:RPM`,
    ~53k rows of the parked recording); calibration context is ZS44A-20260920
    only
  - fusion: no vote on either side, since no model alerts; groups are per
    output channel

  IF/sequence: no tracked or concurrently retrained
  (`models/_candidates/2026-09-23-na`) manifest consumes a `deviation:*`
  column. **Remaining:** runtime replay through the daemon with `_replay-*`
  outputs. It needs a running daemon, and `scripts/replay.py` has no
  behavior-event path. With an empty qualified set it gates nothing yet.

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
- [~] **B03.9 — Deploy and verify the selected qualified set.** Use the
  existing artifact publication/transfer workflow and verify hashes on the Pi.
  Use the guarded restart script if a restart is necessary. Observe the
  requested versions and ordinary eligible observations; confirm demoted models
  remain non-alerting and off-map does not read as healthy. Record vehicle
  state, active capture and deployed build with the result.
  *Promoted on the laptop 2026-09-23; deployed to the Pi 2026-09-24.* **Operator decision
  2026-09-23:** promote the 2026-09-23 candidate set as a **diagnostic-only
  refresh**, even though the qualified set is empty.
  - **Changed:** `data/nissan-xterra-2008/behavior_models.yaml` +
    `.report.json` are byte-identical to `_candidates/2026-09-23/`
    (artifact `sha256:816c150b…`, report `45b62be3…`; was `9cd174ce…` /
    `5dc73028…`).
    - Refit on ZS44A-20260920 with a contract and failed B02 evidence:
      warm_idle v2→v3, timing_rpm_load v2→v3, fuel_trim v2→v3, air_mass
      v2→v3, ve v2→v3, air_charge v1→v2.
    - New: total_trim_b1/b2 v1, stft_bank_balance_city/highway v1.
    - Carried over and flagged `contract_changed:recipe`: timing_rpm_iat v2
      (still demoted) and cat_warmup v4 (still quarantined by fingerprint).
    - `behavior_policy.yaml` is unchanged.
    - **No model gains `alert`**, so fusion has no behavior votes, as before.
  - **Rollback:** the replaced pair is in
    `data/nissan-xterra-2008/_prior/2026-09-24T024700/` (gitignored, laptop
    only) and in git at `HEAD`. Copy it back; hot reload picks up an
    older-mtime artifact (B03.10).
  - **Known answers re-pointed, not rewritten:**
    `tests/test_behavior_validation_known_answers.py` now loads the replaced
    set from `tests/fixtures/behavior_validation/behavior_models_2026-09-22.yaml`
    (byte-identical to the `HEAD` artifact). The review's numbers belong to
    the artifact it measured, and a promotion must not silently re-point
    them.
  - **Why `tests/test_behavior_models.py` changed:**
    - `test_shipped_artifact_loads_diagnostic_only` asserted every shipped
      model was a *legacy no-contract* artifact. The refits now carry
      contracts and are diagnostic-only because they *failed validation*,
      so it asserts each model is diagnostic-only with a stated reason.
    - `test_shipped_air_charge_surface_is_revision_scoped` asserted k106's
      numbers (`tune_revision == ZS44A-k106`, warning rate < 0.03). It now
      asserts the shipped surface is scoped to the newest registry revision
      with held-out rows. It will fail on the next flash until a refit, and
      that is intended: **kept as a tripwire, operator decision
      2026-09-23.**
    - `tests/test_behavior_lifecycle.py` looks up the deployed-snapshot entry
      by name, not position, because the artifact's model order changed.
  - **Deployed 2026-09-24 ~01:10 MDT:** `vehicle_state` `no_data`, no
    capture active; build `2971d79`, pulled on the Pi (the artifact is
    tracked, so git carried it) and restarted with
    `scripts/restart_canary.sh` (the same restart also swapped the LSTM,
    A06.3). Hash on the Pi `816c150b…e565` equals the laptop's.
    `/models/behavior` lists the 12 refreshed models (for example
    `stft_bank_balance_city` v1, `total_trim_vs_rpm_load_b1` v1,
    `warm_idle_rpm_baseline` v3). **Remaining:** on the next drive, confirm
    every model reports not-alerting with its reason and that off-map does
    not read as healthy. The pre-refresh set is recoverable from git
    (`2971d79^`) and the laptop's `_prior/2026-09-24T024700`.
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
