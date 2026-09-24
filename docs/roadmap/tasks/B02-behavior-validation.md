# B02 — reproducible behavior validation and publication gates

**Depends on:** B01 for final serving parity; evaluator/corpus inventory can
start immediately. Follow [B00](B00-behavior-models-README.md).

The existing holdouts reproduced, but some cover only 47–79 rows. A 2D model
can pass with negative predictive R², and a legacy 1D artifact can pass on
training R² with no held-out samples. Quiet alerting is not proof of accuracy.

## Checklist

- [x] **B02.1 — Make the review reproducible from the repo.** Turn the
  temporary review evaluator into a bounded offline command using shared
  preprocessing/evaluation code. Inputs explicitly name profile, config,
  artifact and sessions; output records their hashes/versions and selection
  rules. Do not rely on `/tmp` helpers or an implicit local config. Check mode
  taxonomy against current operator-approved config (including the existing
  decision to fold `suburban` into `city`); distinguish historical reproduction
  from evaluation with corrected configuration.
  *Done 2026-09-21:* `scripts/evaluate_behavior_models.py` (`--config`,
  `--profile`, `--artifact` required; sessions explicit or `--manifest` with
  content hashes, refused on mismatch). Shared code only:
  `src/analysis/behavior_validation.py` (evaluator, `build_evaluation_frame`
  over `feature_pipeline.resample_long_session`/`featurize_wide_frame`, raw CAN
  read only for the ids of needed bindings). The JSON records config/artifact/
  criteria/bindings/dids hashes, git commit, session content hashes, event
  policy and selection rules. `--mode historical` (artifact's own held-out,
  catalog-recipe contract for legacy artifacts, `--decode-can none`) vs
  `--mode corrected` (refused unless the config matches
  `approved_operating_modes`; `suburban` listed as retired into `city`).
  Evidence: all seven persisted holdouts reproduce exactly, [report](../../../data/analyses/2026-09-21-behavior-validation-report.md) §1.
- [x] **B02.2 — Pin known answers and counterexamples.** Preserve a suitably
  sized real-data fixture or reproducible evidence manifest for the July
  holdout reproduction and September failures. Verify the ground-truth chain
  first. Include off-map cooler-IAT data, legacy catalyst evidence absence,
  a broad-band poor predictor and a useful constant idle baseline. Do not
  relabel unverified September exceedances as false positives.
  *Done 2026-09-21:* ground-truth chain first
  (`tests/test_analysis_ground_truth.py`, 21 passed). Manifests
  `tests/fixtures/behavior_validation/evidence_{historical,corrected}.yaml`
  pin 23 sessions by content hash; 272 KB of `--export-rows` slices (J26,
  air-charge holdout, S17, S18, S21) are committed beside them.
  `tests/test_behavior_validation_known_answers.py` asserts the July holdout
  reproduction, cooler-IAT off-map refusal (0 evaluated, IAT 13–22 °C vs a
  29 °C floor), catalyst evidence absence (0 held-out rows, training R² only)
  and its S18 cold-start numbers (n=34, R² −0.737, MAE 55 °C), the broad-band
  timing/IAT predictor failing its criteria (R² −0.046, ±31.3° bands), and the
  constant idle baseline meeting its accuracy criteria on S18 (MAE 13.6 rpm)
  while lacking a second drive. Exceedances are reported as row rates/events,
  never false positives. Found on the way: the B01.5 start tracker refused
  S18's cold start (RUN_TIME answered five rows after RPM) — fixed in
  `session_context.EngineStartTracker`.
- [x] **B02.3 — Split by independent physical drive.** Replace string-sorted
  last-session holdout with explicit grouped train/calibration/test splits.
  Use parsed timestamps and retain parent-drive identity across captures and
  `#segment` pieces. No pieces of one drive may cross folds. Fit bin edges,
  normalization, context selection and thresholds from training/calibration
  data only; final test drives cannot choose the winning model or bands.
  *Done 2026-09-21:* `behavior_validation.group_drives` (parent directory +
  `#segment` pieces, adjacent directories joined within
  `drive_join_gap_seconds`, ordered by `session_started_at`/timestamps;
  unordered sessions refuse), `chronological_split` (newest drives test, then
  calibration, rest train), `explicit_split` refuses a drive in two folds.
  `fit_recipe` fits bins/cells/baseline on train only, reads band acceptance
  from calibration when present, and refuses reused prior edges fitted on a
  test drive. Evidence: `tests/test_behavior_validation.py::test_prefix_sorted_chronology_is_refused`,
  `::test_drive_fragments_never_cross_folds`, `::test_fit_recipe_keeps_segments_of_one_drive_in_one_fold`,
  `::test_prior_edges_fitted_on_a_test_drive_are_refused`. It showed the July
  held-out J26 is a 35 s continuation of training session `223147` ([report](../../../data/analyses/2026-09-21-behavior-validation-report.md) §2).
- [x] **B02.4 — Enforce provenance and adequate evidence.** Validate tune
  boundaries, source/decoder identity, mode labels and capture recipe grants.
  Unknown revisions and sessions crossing a flash need a named resolution,
  not an assumed calibration. Require multiple independent validation drives
  for an alerting model; record per-recipe minimum drives, eligible observations
  and operating-envelope coverage. If the corpus cannot meet them, report the
  missing evidence and retain diagnostic-only status.
  *Machinery done 2026-09-21:* refit CLI refuses unknown revisions and
  sessions recording across a flash unless `behavior_validation.yaml`
  `provenance_resolutions` names a revision or exclusion with a reason;
  evidence records decoder identity (bindings/dids/dbc hashes), mode taxonomy
  (non-approved → cannot pass), capture grants, drives and stale mode-label
  sidecars (classified before `f8b7d38` → excluded from event gating).
  Per-recipe `min_validation_drives`, `min_eligible_observations`,
  `min_envelope_coverage` are profile data. **Remaining:** the corpus cannot
  meet them — no model has two qualifying independent drives on one current
  calibration; `2026-07-26T214348` (crosses the 22:31 flash) is now excluded
  by operator decision 2026-09-22 in `behavior_validation.yaml`
  `provenance_resolutions`; event-gated warmup recipes are blocked by **D03.22** (stale
  sidecar events); cat_warmup's May training sessions are absent locally. All
  eight shipped models stay diagnostic-only ([report](../../../data/analyses/2026-09-21-behavior-validation-report.md) §4).
  *Remainder resolved 2026-09-23:* the machinery was applied to the operator's
  current-calibration corpus (ZS44A-20260920, decision 2026-09-23;
  [manifest](../../../data/analyses/2026-09-23-behavior-corpus-manifest.yaml)).
  - Every session resolves to one revision (13 via their capture manifests),
    with no unknown revisions and no flash crossings (checked per row by the
    refit CLI).
  - D03.22 is done for this corpus: 40 sidecars at classifier 0.3.0, none
    stale.
  - Capture grants are recorded per capture.
  - Correction to the 09-21 remainder: the May sessions *are* local (B07.1).
  - Each of the 10 fitted recipes now has **2 qualifying independent test
    drive groups**, so the "no two qualifying drives on one calibration"
    remainder no longer holds. Checked on the promoted artifact: every
    `evidence.test.drives == 2`, and no failure reads `test_drives`.

  Where evidence is still missing, it is reported and the model stays
  diagnostic-only: the warmup recipes (2 cold starts), cam (commanded angle
  never polled), TPS (244 rows), manifold VE (no MAP), LTFT balance (LTFT
  constantly 0.0). All 12 shipped models are diagnostic-only because they
  *failed* or lack evidence, never by default
  ([refit report](../../../data/analyses/2026-09-23-behavior-refit-validation-report.md) §0, §2).
- [x] **B02.5 — Report denominators and practical accuracy.** Per model,
  drive and relevant regime, emit input-present/fresh/eligible/evaluated counts,
  refusal reasons, coverage, MAE, signed bias, error quantiles, R² where useful,
  and warning/critical half-widths in physical units. Report actual event rates
  after persistence/deduplication separately from per-row bands. Use drive-level
  uncertainty where supported; do not treat adjacent seconds as independent.
  *Done 2026-09-21:* `FoldMetrics` (rows, input-present, fresh, eligible,
  evaluated, coverage, named refusal counts, MAE, bias, |error| p50/p90/p95,
  R² as descriptor, warning/critical half-widths via
  `behavior_decoder.band_half_widths`, per-row warning/critical rates, events
  after the daemon's `PersistenceGate` + emitter dedupe per evaluated hour,
  envelope coverage, per-drive MAE range, drive-resampled bootstrap from three
  drives) per model, drive and regime, in the evidence and the report. Evidence:
  `test_no_prediction_is_not_a_valid_observation`,
  `test_row_exceedance_is_not_an_event`; [report](../../../data/analyses/2026-09-21-behavior-validation-report.md) §3.
  *Re-run 2026-09-22 under `EVALUATOR_VERSION 2`* ([v2 report](../../../data/analyses/2026-09-22-behavior-validation-report-v2.md)):
  v1 read `datetime64[us]` row times 1000x short, so the 09-21 event counts,
  control rates and synthetic windows were wrong; per-row metrics reproduce
  exactly. Event rates are now real, over wall-clock watched time rather
  than rows×1 s (`_evaluated_hours`, `test_evaluated_hours_is_wall_clock_exposure_not_a_row_count`):
  timing/load 3 events in 1.02 h (2.9/h) and air mass 10 in 0.95 h (10.6/h)
  both exceed the declared 2/h control rate; fuel trim 1→0, catalyst 3→0
  (PID 0x3C on the slow tier: 1 s pairs ~17 s apart, so 3-of-5-in-8 s can
  never latch); synthetic detection now
  over 11–65 real windows with delays (warm idle 4/4 within 2 s; every
  surface < 0.8). No verdict flipped. The 09-21 numbers are preserved with a
  banner.
- [x] **B02.6 — Gate on useful skill and sensitivity.** Require predictive
  surfaces to improve on an appropriate training-fitted simple baseline on
  untouched drives and resolve a declared practically meaningful deviation.
  Give robust constant baselines their own error/coverage/sensitivity criteria;
  never impose a universal R² floor. Specify each criterion and physical noise
  floor in recipe/profile data before final testing. Include no-fault controls
  and explicitly labelled synthetic fault magnitude/duration cases; distinguish
  synthetic sensitivity from real fault recall.
  *Done 2026-09-21:* `data/nissan-xterra-2008/behavior_validation.yaml`
  declares, before testing, per-recipe criteria with a required noise-floor
  basis and meaningful deviation (validated > noise floor): predictive surfaces
  need `min_skill_vs_baseline` against a training-fitted median/mode-median
  baseline on the same rows; constant baselines use `max_mae`/`max_abs_error_p95`;
  every family needs MAE and median warning half-width ≤ the meaningful
  deviation, a no-fault control event rate (only judged past
  `min_control_hours`), and labelled `synthetic: true` step faults detected
  on control-quiet windows. No R² floor. Real fault recall is never claimed
  (no labelled faults). Criteria for cam/TPS/bank-balance/AFR/cold-start-idle
  recipes are left to B04–B07 (undeclared → diagnostic-only). Evidence:
  `test_broad_band_useless_surface_is_refused`, `test_useful_surface_passes_and_alerts`,
  `test_constant_idle_baseline_can_pass_its_own_criteria`, `test_criteria_are_profile_data_and_validated`.
- [x] **B02.7 — Apply gates to every publication route.** Fresh fit, partial
  refit, carry-over, model load/hot reload and promotion must agree about
  trusted versus diagnostic-only artifacts. Missing held-out evidence cannot
  fall back to training R² for alerting. Reject/flag incompatible contract,
  decoder or context changes even if the training-row hash is unchanged.
  Preserve old evidence instead of rewriting historical validation stamps.
  *Done 2026-09-21:* `serving_status` is the one trust decision (daemon load,
  hot reload, behavior detector, dashboard, MCP, refit report): `active` needs
  the schema-2 contract, held-out rows and `ValidationEvidence` with verdict
  `passed` whose `contract_fingerprint` matches the model, and no
  `publication_flags`. Training R² remains only a publication sanity check.
  `refit_all` carry-over (partial refit, skipped refit) appends
  `contract_changed:recipe`, `decoder_changed:<catalog>`,
  `context_changed:mode_taxonomy`, `criteria_changed` flags without touching
  the evidence; a same-hash refit bumps its version when decoder, taxonomy or
  criteria changed. The control-API refit job runs the same CLI; there is no
  separate behavior promotion route (`promotion_gate.py` is IF/sequence only).
  Evidence: `test_missing_holdout_never_falls_back_to_training_r_squared`,
  `test_partial_refit_carry_over_flags_changed_decoder_contract_and_criteria`,
  `test_identical_rows_with_a_changed_decoder_bump_the_version`,
  `test_load_and_hot_reload_agree_with_the_fit`,
  `tests/test_behavior_serving_contracts.py::test_contract_round_trips_and_unsupported_schema_rejected`.
- [x] **B02.8 — Test the actual failures.** Assert refusal of the broad-band
  useless surface, missing holdout, leaked drive fragments and prefix-sorted
  chronology. Assert the constant idle model can pass appropriate criteria.
  Exercise CLI report output, partial-refit carry-over and hot reload, plus
  B01's no-prediction versus valid-observation distinction.
  *Done 2026-09-21:* `tests/test_behavior_validation.py` (21 tests: broad-band
  surface, missing holdout, leaked fragments, prefix chronology, constant idle
  pass, carry-over, hot reload, denominators, events vs rows, taxonomy,
  start-tracker fix), `tests/test_evaluate_behavior_models_script.py` (CLI
  report inputs/hashes, historical reproduction, corrected taxonomy refusal and
  leak exclusion, manifest mismatch, flash-crossing/exclusion/stale-sidecar
  provenance), `tests/test_behavior_validation_known_answers.py` (21 real-data cases
  assertions), refit CLI report evidence in
  `tests/test_refit_behavior_models_script.py::test_main_writes_output_yaml`.

## Code and evidence

Start at `fit_recipe`, `_validate_held_out`, `_check_publish_gate`, `refit_all`
in `src/analysis/refit.py`, `scripts/refit_behavior_models.py`,
`src/decode/behavior_models.py`, `src/models/promotion_gate.py` and
`src/control/sessions.py`. Reuse existing evaluation/report mechanisms where
they fit; do not create a second publication system.

**Done when:** a reproducible report shows accuracy, applicability and useful
sensitivity, and all publication/carry-over routes enforce the same evidence
contract. Insufficient data remains an explicit incomplete validation item.
