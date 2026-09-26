# A04 — Isolation Forest defects and what to replace it with

**Depends on:** A02 for evaluation. A06.1 decides whether the IF keeps
alerting. Correctness fixes (A04.2, A04.6, A04.8, A04.9, A04.12) are worth
landing whatever that decision is, because a non-alerting IF still writes
evidence. Model-changing items take effect at the next retrain (deferred by
the operator on 2026-09-22). Follow [A00](A00-anomaly-detectors-README.md).

Reviewed artifact: `models/nissan-xterra-2008/isolation_forest/`, created
2026-09-20 from 19 sessions. It has 295 features and one model per
operating mode, each using 173–255 of them (`unknown` uses 20). This is not
the 88-feature July artifact the D02.5 blindness counts refer to. A02.11
confirms which artifact the Pi runs.

## Checklist

- [x] **A04.1 — Put single-quantity sensitivity in the harness as a known
  answer.** Shifting one quantity on held-out, unflagged rows flagged **0 %**
  of rows at every size tried:
  - Shift sizes: +3, +10, +30 and +100 standard deviations of the mode.
  - Shifted quantities: STFT_1 alone, the fuel-trim family, and all four
    coolant sources (raw plus `__mean`).
  - Sample: city n = 917, highway n = 1,585.
  - Score movement: +0.002 to +0.007, at most 0.15 of the held-out spread.
    The threshold sits about 2.4 spreads above the median.
  - Control: shifting every feature by 3σ flags 100 %, so the harness works.

  Mechanism: `max_samples` = 256 caps trees at 8 levels, so with ~250
  features any one feature lies on about 3 % of a point's path. A value
  beyond the training range follows the same branch as the training maximum,
  so size stops mattering. This matches the July benchmark (precision
  0.006–0.008 at 214 alerts/h) and the 2026-09-20 one (26 of 250 injected
  faults caught).
  - Add the shift test to A02.4 as a known-answer case every candidate
    detector must pass or be recorded as failing.

  *Done 2026-09-23.* `evaluation.single_quantity_shift` is a known-answer case
  every harness detector runs through: `scripts/evaluate_detectors.py` does it
  per in-era drive unless `--no-shift-test`, and `--shift-rows` (default 900)
  sets the rows. Each fault-channel role (`fault_injection.yaml`, profile
  data) is shifted by +3/10/30/100 sigma of its operating mode, raw channel
  plus `__mean`. It is graded on rows the detector scored quiet *and* where
  the quantity was freshly observed.
  - Verdicts: **pass** when +10 sigma in every single quantity flags at least
    50 %; **fail** is recorded, naming the blind quantities; **invalid** when
    the control (every numeric column shifted by the largest size) flags under
    90 %; **withheld** when nothing scored quiet.
  - Raw-stream detectors (A05 plausibility) are listed as not applicable.
  - **Limitation:** the shift moves only the raw column and its `__mean`.
    Derived channels built from the quantity (`derived:TOTAL_TRIM_*`, the bank
    balances, AFR) are left unchanged, so a detector watching only those
    cannot see the shift. That understates a derived-channel detector; it does
    not flatter one.
  - Tests: `test_a_max_z_detector_passes_the_single_quantity_shift`,
    `test_a_detector_that_dilutes_one_quantity_is_recorded_as_failing`,
    `test_a_detector_the_shift_never_reaches_is_invalid_not_failing`,
    `test_the_shift_is_graded_only_where_the_quantity_was_observed`, plus the
    end-to-end assertion in
    `test_main_scores_only_unseen_drives_and_names_every_skip`.
  - **Measured on the current artifact**
    ([report](../../../data/analyses/2026-09-23-a04-single-quantity-shift.md)):
    same frames and 13 in-era drives as A02.11. The IF **fails on all 6 drives
    it scores** (7 withheld). Its highest flag rate at any size, 3 to 100
    sigma, is airflow 4.2 %, calculated load 1.4 %, STFT_1 0 %, STFT_2 0.5 %,
    A/F sensor 2.4 % and coolant 2.2 %; flag rates do not grow with size past
    10 sigma. The 100 sigma control flags 100 % on every scored drive. This
    reproduces the review's known answer.
  - The other detectors, drives pass/fail/invalid/withheld: `sequence`
    0/9/0/4, `sequence_global` 0/13/0/0, `fused` 0/0/13/0 (the IF cannot fire,
    so fusion cannot agree), `robust_z` 0/10/3/0, `pca` 0/9/4/0,
    `untrained_autoencoder` 0/13/0/0. No detector passes on any drive.
- [~] **A04.2 — Attribute over the scoring model's own features, by effect on
  the score.** `_rank_feature_contributors` (`src/daemon/inference.py`)
  ranks |z| over `engine.feature_columns`, the 295-column union
  (`src/alerts/emitter.py`), not the mode's feature set.
  - The top-1 name was not a feature of the scoring model on 60 of 62 city,
    52 of 62 highway and 39 of 60 idle_transient rows.
  - Restricted to the mode's own features, the |z| top-1 matched the
    occlusion top-1 on only 0–7 of ~61 rows.

  Rank per mode by occlusion: replace each feature with the mode's median
  and score all variants in one batched `decision_function` call. Or use
  `shap.TreeExplainer`, which supports `IsolationForest`.
  - **Test:** every reported contributor belongs to the scoring model.

  *Code done 2026-09-23; the per-mode reference waits for the retrain.*
  `TrainedIsolationForest.feature_contributions` restricts attribution to the
  features of the mode model that scored the row. It replaces each one in turn
  with the imputation value and scores every variant in one batched
  `decision_function`. Only positive score effects are returned, largest
  first. `IsolationForestInferenceEngine.feature_contributions`
  (`src/alerts/emitter.py`) and the daemon's `_rank_feature_contributors` use
  it; the |z| ranking over the feature union is gone. The daemon computes it
  only for a row that can emit (calibrated, at or over its threshold).
  - **Reference, stated per event.** The occlusion value is the mode's
    median only on an artifact that recorded `mode_feature_medians` (A04.7).
    The deployed 2026-09-20 artifact recorded none, so today every feature is
    occluded toward the *pooled scaler mean*. Every event says which:
    `detail.feature_contributors_reference` is `mode_median` or `pooled_mean`
    (`TrainedIsolationForest.contribution_reference`).
  - **Cost** is host-dependent. The laptop measured ~12 ms per call in
    isolation and ~100 ms per call against ~240 ms per one-row `score()`
    under load; the verifier's host measured ~80 ms vs ~139 ms. It is a
    fraction of a scoring pass, paid only on over-threshold rows.
  - Tests: `test_contributors_are_the_scoring_models_own_features_ranked_by_score_effect`
    (a column only the other mode trained on, set to 1e6, is never reported;
    the shifted feature ranks first),
    `test_the_occlusion_reference_says_mode_median_or_pooled_mean`, and
    `test_score_and_emit_attaches_the_scoring_models_occlusion_contributors`
    (`tests/test_daemon.py`, which also asserts the reference tag).
  - Behaviour: `detail.feature_contributors` values are now score effects,
    not |z|, measured against the pooled mean until the retrain.
  - **Remaining:** the per-mode (median) reference takes effect at the next
    retrain (operator-deferred 2026-09-22).
- [~] **A04.3 — Stop quarantine from reducing training to the median
  moments.** Quarantine sets cells to NaN (`scripts/retrain.py`). Its
  6 × MAD screen quarantines every non-median value wherever MAD = 0
  (`training_disposition.py`); accelerator pedal at rest is the example. The
  IF then drops any row with a NaN in its features. Measured on real
  sessions:
  - 4.5 % of cells are quarantined, but 71.6 % of rows are lost.
  - Usable rows fall: city 69 % → 12 %, highway 80 % → 40 %,
    idle_transient 96 % → 54 %.

  That is the "train only on moments where everything sits at its median"
  trap `online-training-strategy.md` warns against.
  - Handle MAD = 0 explicitly: use a floor, or exempt the channel.
  - Never let a quarantined cell remove a whole row silently; record rows
    lost per mode per step in the manifest.
  - Put the calibration frame through the same feedback-label and
    quarantine policy as training, or record why not. Today it skips both,
    so thresholds include operator-labelled anomalies.

  *Done 2026-09-23.*
  - **MAD = 0.** `training_disposition.robust_spread` falls back to 1.2533 x
    the mean absolute deviation when MAD is 0 (Iglewicz-Hoaglin);
    all-identical values quarantine nothing. A frozen golden baseline with
    zero spread exempts the group instead of quarantining every non-median
    value. Tests: `test_mad_zero_does_not_quarantine_every_non_median_value`
    (pedal at rest 70 %: only the impossible 5,000 is quarantined),
    `test_all_identical_values_quarantine_nothing`.
  - **Rows lost, per mode.** `retrain._apply_quarantine_exclusion` hands the
    IF trainer the quarantined cells; each mode's `row_accounting` records
    `mode_rows`, `complete_without_quarantine`, `complete_rows`,
    `lost_to_quarantine`, `fit_rows`, `calibration_rows`, and a nonzero loss
    logs a warning. Test: `test_rows_lost_to_quarantine_are_counted_per_mode`.
  - **Calibration policy.** `retrain.py` applies the feedback-label policy to
    the IF's calibration frame. Only the IF gets the filtered frame: the
    sequence trainer keeps the unfiltered one, because its manifest cannot
    record the policy yet. Quarantine is deliberately not applied, because
    serve-time scoring never quarantines; the IF manifest records both under
    `calibration.policy`. Tests:
    `test_retrain_hands_the_if_trainer_its_calibration_policy_and_override`,
    `test_feedback_filtered_calibration_frame_goes_to_the_if_only`.
  - Effect on the model waits for the retrain.
  - **Remaining:** takes effect at the next retrain (operator-deferred
    2026-09-22), then judged on A02's harness against A02.11's baseline, per
    drive on the same rows.
- [~] **A04.4 — Record and enforce per-mode provenance.** Greedy joint-
  completeness feature selection keeps only sessions that polled every
  selected channel, so each mode quietly narrows:
  - city: 2,174 rows from 11 sessions, all on 2026-09-18;
  - highway: 1,348 rows from 7 sessions, all on 2026-09-18;
  - warmup: 192 rows from two August sessions.

  The header still says 19 training sessions. The idle (154 rows) and warmup
  (328) calibration holdouts come entirely from one session, `134345`, at
  71–73 kPa against a training median of 79.
  - Record per-mode training and calibration sessions and dates in the
    manifest.
  - Enforce `MIN_CALIBRATION_SESSIONS` per mode, not only globally, and
    refuse a mode model resting on fewer independent drives than the
    configured minimum.

  *Done 2026-09-23 (enforced at the next retrain).* Each mode summary records
  `training_drives`, `calibration_sessions`, the calibration span and
  `calibration_drives` (physical drives, `evaluation.group_into_drives`).
  - Config `min_training_drives_per_mode` (default 3) refuses a mode resting
    on fewer training drives (`too_few_training_drives`).
  - `min_calibration_drives_per_mode` (default 3, =
    `MIN_CALIBRATION_SESSIONS`) refuses a mode whose threshold would rest on
    fewer (`too_few_calibration_drives`), unless `retrain.py
    --accept-thin-calibration REASON` names why. The reason is stamped per
    mode and in `calibration.override`.
  - **One flag, two refusals.** A single `--accept-thin-calibration REASON`
    overrides both this IF per-mode floor and the sequence detector's A02.2
    calibration refusal: publishing a thin IF also publishes a thin sequence
    model, and the reason is stamped in both manifests.
  - Tests:
    `test_a_mode_calibrated_on_too_few_drives_is_refused_unless_overridden`,
    `test_a_mode_trained_on_too_few_drives_is_refused_and_named`,
    `test_main_refuses_a_single_drive_corpus_and_says_why`.
  - On the 2026-09-20 corpus this refuses at least idle and warmup (one
    calibration session).
  - *Physical drives, 2026-09-23 (verifier finding on the A06.3 candidate).*
    `_drive_count` grouped each mode's rows by session span. A capture
    between two sessions of one drive made it two, so the candidate's
    idle_transient "4 training drives" were 2 (D1, D4).
    - The count is now `src.analysis.drives.count_drives` over the frame's
      stamped physical drive, so captures bridge and pauses in activity
      split.
    - The r2 retrain applies it: idle rests on 1 drive; idle_transient is
      refused on fit rows (37).
    - Test:
      `tests/test_physical_drives.py::test_frames_carry_physical_drives_and_every_count_reads_them`.
  - **Remaining:** takes effect at the next retrain (operator-deferred
    2026-09-22), then judged on A02's harness against A02.11's baseline, per
    drive on the same rows.
- [~] **A04.5 — Make feature selection survive polling mix, and count
  blindness per mode.** On `134345` no spec column is absent, yet the IF
  withholds 99.7 % of city rows, 83.5 % of idle_transient and 100 % of
  unknown. `derived:STFT_BANK_BALANCE` (4 variants) and `TOTAL_TRIM` are NaN
  under that session's polling. With 4 variants per channel, three missing
  base channels blind a 255-feature model.
  - `structural_blindness` (`feature_spec_health.py`) divides by the global
    295, but withholding is decided per mode. Compute it per mode.
  - Choose features so a common polling profile cannot blind a mode, or
    state in the manifest which profiles a mode needs.

  *Done 2026-09-23 (feature choice takes effect at the next retrain).*
  - **Polling mix.** A feature joins a mode only when it reaches the per-mode
    coverage floor in at least 75 % of that mode's sessions
    (`MIN_MODE_SESSION_COVERAGE`), so a column one polling profile never
    produces cannot blind the mode. Each mode's `required_channels` names the
    base channels it needs. Test:
    `test_a_feature_one_polling_profile_never_produces_is_not_selected`, where
    one big session gives >50 % pooled coverage but 1 of 5 sessions.
  - **Per-mode blindness.** The loaded IF registers its mode feature sets
    (`feature_spec_health.record_load_audit`). The structural-blindness
    verdict then carries `by_mode`, `blind_modes`, and `scores_nothing` =
    every mode blind. The established/changed transition follows the same
    per-mode verdict, including a change in which modes are blind. The daemon
    log text (`describe_structural_verdict`, used by
    `inference._log_structural_verdict`) names the blind modes and each mode's
    absent share against the ceiling, not the union's share. It is ERROR only
    when every mode is blind. This part ships now, not at the retrain. Tests:
    `test_structural_blindness_is_counted_per_mode`,
    `test_structural_blindness_verdict_is_per_mode_for_a_loaded_if`,
    `test_blindness_transitions_and_messages_follow_the_per_mode_verdict`
    (3 of city's 20 features absent is a WARNING naming city, where the union
    rule, 3/40 = 7.5 %, printed "unsatisfiable").
  - **Remaining:** takes effect at the next retrain (operator-deferred
    2026-09-22), then judged on A02's harness against A02.11's baseline, per
    drive on the same rows.
- [~] **A04.6 — Enforce the feature policy at load, and treat sentinels as
  states.** 83 of the 295 features come from `model_feature: false` bindings
  (TPMS, steering, brake, `MOTION_SIGNAL_*`, wheel-speed pairs, HVAC). 44 come
  from `hypothesized` bindings. The policy (`a4a58a4`) runs only at training;
  `manager.py` and `test_manifest_producibility.py` do not check it. These
  columns dominate the attribution.
  - TPMS reads 0 (sensors not yet transmitting) on 35 % of `134345`,
    including every warmup row. A pooled scaler with μ = 100.6, σ = 56 treats
    that sentinel as a measurement.
  - Check the policy at load, and name violations in `/status`.
  - Decode sentinels to a state channel, per AGENTS.md's "a sentinel is a
    state, not a number".

  *Policy half done 2026-09-23.* `load_isolation_forest_model(...,
  feature_policy=)` names every trained feature
  `FeaturePolicy.exclusion_reason` forbids (`policy_violations`). The daemon
  loader (`components._load_isolation_forest`) resolves the policy from its
  scoped config: the bindings catalog plus the serving capture profile's DIDs.
  `/status` shows `detectors.isolation_forest.load_audit`, with
  `policy_violation_count`, the violations and the refused modes. The model is
  still served; the next retrain drops the features.
  - Tests: `test_feature_policy_is_checked_at_load_and_violations_are_named`,
    `test_the_daemon_loader_checks_the_feature_policy_against_its_config`.
  - **Remaining: sentinels as states.** Broadcast bindings have no
    `sentinel_raw` / `sentinel_meaning` (only `dids.yaml` DID/memory channels
    do). Decoding TPMS 0 to a `decoded:<NAME>_SENTINEL` state needs:
    - the field on `src/decode/bindings.py:Signal`;
    - the offline and live decode paths (`src/preprocessing/decode.py`, the
      engineering-unit scaling and the live source);
    - a parity test.
    That is outside this checklist's files, and the feature-pipeline side is
    the concurrent A03 area. TPMS is `model_feature: false` and
    `hypothesized`, so the policy above already keeps it out of the next
    retrain.

  *Sentinel mechanism done 2026-09-25 (desk).* `src/decode/slices.py` holds the
  rule once: `SentinelState` (`is_sentinel`), `check_sentinel` (raw and meaning
  set together, meaning non-blank, the word representable in the slice) and
  `sentinel_channel` (`<NAME>_SENTINEL`). `bindings.Signal` gains
  `sentinel_raw`/`sentinel_meaning` (same pair as a `dids.yaml` memory
  channel). A sentinel word decodes to a `decoded:<NAME>_SENTINEL` row carrying
  the meaning, and to no `decoded:<NAME>` number, on every decode path:
  `FrameDecoder` (live source, dashboard stream and storage),
  `preprocessing.decode.decode_can_rows` (batch/training; a signal whose only
  stored rows are `_SENTINEL` counts as stored), `decode_signal_column` (NaN),
  and `tools/backfill_decoded.py`. `apply_scaling`, `engineering.physical_value`
  and `physical_series` read it as None/NaN. Non-sentinel values decode exactly
  as before; the wide `_SENTINEL` column is text, so the feature builder skips it.
  - Tests: `tests/test_preprocessing_decode.py::test_a_sentinel_decodes_to_the_same_state_rows_on_the_live_and_batch_paths`
    (parity), `::test_non_sentinel_values_decode_exactly_as_without_the_declaration`,
    `::test_a_sentinel_column_is_a_text_state_and_the_number_column_skips_it`,
    `::test_stored_sentinel_rows_count_as_stored`,
    `::test_decode_signal_column_masks_the_sentinel_and_the_mask_finds_it`;
    `tests/test_frame_decoder.py::test_a_sentinel_word_decodes_to_a_state_row_not_a_number`;
    `tests/test_preprocessing_live.py::test_a_sentinel_word_never_becomes_a_live_feature_value`;
    `tests/test_backfill_decoded.py::test_backfill_stores_a_sentinel_as_the_live_decoders_state_row`;
    `tests/test_bindings_decoder.py::test_a_half_declared_or_impossible_sentinel_is_refused`,
    `::test_a_sentinel_scales_to_nothing_everywhere`.
    `tests/test_analysis_ground_truth.py` still passes.
  - **Remaining: declaring the TPMS sentinel in `data/bindings.yaml` (operator
    decision).** The evidence is in the repo: `data/analyses/2026-05-11T211240.md`
    ("uniformly 0 across all stationary/reverse windows, then all climb together
    during forward driving", bytes 2-5 of 0x385) and the 35 % of `134345` above.
    It is not declared yet because the served IF (manifest created
    2026-09-20) was trained on TPMS zeros: 16 of its features are TPMS (warmup
    16/217 = 7.4 %, idle 16/180 = 8.9 %, idle_transient 13/173, city 15/255,
    highway 16/249), all above `MAX_MISSING_FEATURE_FRACTION` = 5 %. A sentinel
    decode leaves those features missing while the sensors sleep, so the served
    model would withhold every such row and report it as a missing-features
    anomaly until the retrain drops TPMS (operator-deferred 2026-09-22).
    That consequence is worked out from the code and the manifest, not measured
    by replay. The YAML to add to each `TPMS_*_PRESSURE` entry is
    `sentinel_raw: 0` and `sentinel_meaning: wheel sensor not transmitting (0
    before forward driving; 2026-05-11T211240, A04.6 134345)`.
- [~] **A04.7 — Impute per mode, or not at all.** One scaler is fit over all
  modes, so `fillna(0)` imputes the *pooled* mean. Against each mode's own
  spread, that mean sits a median 9.4 standard deviations away for idle
  features (idle_transient p90 16.5). Imputing 5 % of features raised the
  mean score in 3 of 4 modes; per-mode medians lowered it.
  - Fit the scaler per mode, or impute per-mode medians.
  - Correct the docstring that says imputation pushes toward normal.
  - Low impact today only because of A04.1.

  *Done 2026-09-23 (takes effect at the next retrain).* The manifest records
  each mode's training median per feature (`mode_feature_medians`). `score()`,
  calibration and attribution impute a tolerated missing cell to the scoring
  mode's median, and older artifacts keep the pooled mean because they
  recorded nothing else. The `MAX_MISSING_FEATURE_FRACTION` docstring no
  longer says imputation pushes toward normal. Test:
  `test_a_tolerated_missing_cell_is_imputed_to_the_modes_own_median` (the NaN
  row scores exactly like the row at the idle median, and the pooled mean
  scores worse).
  - **Remaining:** takes effect at the next retrain (operator-deferred
    2026-09-22), then judged on A02's harness against A02.11's baseline, per
    drive on the same rows.
- [x] **A04.8 — Do not serve context-only mode models.** The artifact carries
  an `unknown` model over 20 context features (coolant, IAT, baro, GPS
  speed), thresholded on 91 rows at a 1.1 % achieved flag rate. Scoring
  routes to it, and the loader does not reject it; the comment saying it is
  "withheld at serve anyway" is false for this artifact.
  - Refuse such models at training and at load.
  - **Test:** such an artifact is refused.

  *Done 2026-09-23.* Training already skipped `unknown`
  (`SKIP_UNCLASSIFIED_MODE`) unless it is the whole corpus under
  `allow_unknown_only`. At load, `refused_mode_models` refuses an `unknown`
  model whenever the artifact has another model. Its rows are withheld like
  any unmodeled mode's and its threshold is dropped, and the refusal appears
  in `/status` `load_audit.refused_modes`. The artifact's other modes stay
  served: refusing the whole 2026-09-20 artifact would switch the IF off until
  the deferred retrain, which is A06.1's decision. Test:
  `test_an_artifact_serving_an_unknown_mode_model_is_refused_at_load`.
  - The reviewed `unknown` model is not strictly context-only: 1 of its 20
    features is `derived:AFR_CENSORED_B1`. Its defect is the unclassified
    bucket, and that is what the refusal keys on. No channel-list rule is
    added, because none exists as profile data to key on.
  - **Single-model fallback.** "Global model" is decided on the artifact as
    saved: an artifact with any refused mode never falls back. The first cut
    decided it on the models left after the refusal, so a saved
    `{city, unknown}` artifact routed `unknown` and `koeo` rows through city.
    Test: `test_refusing_unknown_never_turns_the_remaining_model_global`,
    which fails under the old rule.
  - Behaviour: `unknown` rows are now withheld, so while the truck is in
    `unknown` the mode shows in `/status` under
    `detectors.isolation_forest.blind_modes`, with
    `missing_feature_rate_by_mode.unknown` = 1.0. That is the refusal, not a
    sensor fault (checked against `HealthMonitor`).
- [x] **A04.9 — Fix the offline IF evaluation.**
      `isolation_forest_evaluation.py`
  scores against the sklearn `contamination` label, counts withheld rows
  (label −1) as detections, and recommends contamination values. None of
  that reflects what is served: emits use the calibrated threshold, and
  `contamination` only sets `offset_`. Evaluate at the served threshold,
  count withheld rows as withheld, and drop the contamination
  recommendation, or fold the module into A02.4.

  *Done 2026-09-23.* `isolation_forest_evaluation.evaluate_isolation_forest`
  grades `isolation_forest.served_verdicts`: the calibrated threshold, with
  uncalibrated modes non-emitting and withheld rows `None`. Withheld rows are
  counted beside the metrics (`withheld_rows`, `withheld_positive_rows`),
  never inside them. `recommend_contamination`/`ContaminationRecommendation`
  are removed, and `train_anomaly_model.py --evaluate` prints the threshold
  and withheld counts. `synthetic_fault_eval._if_anomalous` now calls the same
  `served_verdicts`: when no mode is calibrated it no longer falls back to
  sklearn's label (the loader refuses such artifacts anyway). Tests:
  `test_evaluation_grades_the_served_threshold_and_counts_withheld_rows_apart`,
  `test_no_contamination_recommendation_is_offered`.
- [~] **A04.10 — Choose hyperparameters by measured stability.**
  `n_estimators` = 200 is seed-sensitive. Refitting the same highway rows
  with seeds 0–3 flagged 147–214 rows (pairwise Jaccard 0.68); 1,000 trees
  gave 0.86. `max_samples` and `max_features` are sklearn defaults, never
  tuned.
  - Pick the ensemble size by seed stability on A02's evaluation drives.
  - Record seed-to-seed agreement in the manifest.

  *Measured 2026-09-23; the ensemble size is an operator decision because of
  serve cost.*
  - **Recorded in the manifest.** `stability_seeds` (config, default 4) refits
    each mode on the same rows with that many more seeds. It flags the same
    share of the calibration rows each time, and `seed_agreement` records the
    pairwise Jaccard (mean and min). `isolation_forest.seed_stability_sweep`
    runs the size sweep. Tests:
    `test_the_calibration_actually_used_and_seed_agreement_are_recorded`,
    `test_seed_stability_sweep_reports_agreement_per_ensemble_size`.
  - **Sweep on A02's evaluation drives.** The deployed artifact's per-mode
    features, scaler and median imputation were refit on the 4 IF training
    sessions in the A02.11 frame cache (complete rows). They scored the rows
    of the 16 held-out sessions the served model would score, flagged the top
    2 %, and used seeds 0-3. The whole sweep took 31 s. Mean Jaccard (min):

    | trees | city (1,048 fit / 558 eval rows) | highway (1,083 / 1,233) |
    |---|---|---|
    | 100 | 0.46 (0.22) | 0.69 (0.52) |
    | 200 (deployed) | 0.62 (0.47) | 0.71 (0.67) |
    | 500 | 0.79 (0.69) | 0.77 (0.67) |
    | 1,000 | 0.79 (0.69) | 0.88 (0.79) |

    Idle had no complete fit row in those sessions. This agrees with the
    review's 0.68 and 0.86.
  - **Serve cost.** One-row `decision_function` on the laptop over 255
    features takes 15 ms at 200 trees and 55-64 ms at 1,000 (`n_jobs` 4/1).
    The Pi is slower, and the IF sits in the out-of-process tick budget.
  - **Decided by the operator, 2026-09-23: 600 trees**, a middle ground
    between stability (city plateaus at 500) and per-tick cost (1,000 trees
    was 3.5-4x the IF cost). `n_estimators: 600` is set in all three example
    configs. It takes effect at the A06.3 retrain.
  - **Retrain's `seed_agreement` at 600 trees** (A06.3 candidate,
    2026-09-23, current-calibration corpus): mean/min Jaccard city 0.57/0.47
    (561 fit rows), warmup 0.68/0.50 (198 rows), idle_transient 1.00 (one
    flagged row of 14). The fit sets are about half the sweep's, so this is
    below the sweep's 500-tree plateau.
    [Report](../../../data/analyses/2026-09-23-a06-3-retrain-evaluation.md).
  - **Remaining:** the Pi's per-tick IF cost at 600. `max_samples` and
    `max_features` are not swept.
- [~] **A04.11 — Count exceedance episodes, not rows.** The rule of at least
  10 exceedances per calibration treats 1 Hz rows as independent, despite
  30 s rolling windows. On `134345` the warmup top 1 % was 4 rows forming 1
  episode. Count clustered exceedances as one episode for both calibration
  minimums and alert rates.

  *Done 2026-09-23 (takes effect at the next retrain).* `exceedance_episodes`
  treats exceedances within the spec's `rolling_window_seconds` (default 30 s)
  of each other in one session as one episode. Calibration first cuts at the
  resolvable row rate. If fewer than 10 episodes stand behind that threshold,
  it lowers the threshold row by row until they do, never past
  `MAX_RESOLVABILITY_FLAG_RATE` (5 %). Each mode records `exceedance_episodes`
  and `achieved_episodes_per_hour`; `target_unresolvable` now also means "too
  few episodes at the requested rate".
  - Tests: `test_adjacent_exceedances_are_one_episode`,
    `test_calibration_counts_clustered_exceedances_not_rows`.
    `test_small_holdout_calibrates_at_a_resolvable_rate_and_says_so` now
    spaces rows 60 s apart to isolate the row rule; at 1 Hz the same 400-row
    holdout reaches only 4 episodes at 5 %.
  - The harness already counts alert rates as persistence-gated episodes
    (A02.4).
  - **Remaining:** takes effect at the next retrain (operator-deferred
    2026-09-22), then judged on A02's harness against A02.11's baseline, per
    drive on the same rows.
- [x] **A04.12 — Record the calibration actually used.** The manifest
  records `calibration_holdout_fraction` 0.2 while external calibration forced
  it to 0 (`isolation_forest.py`). Record the effective values.

  *Done 2026-09-23.* `hyperparams.calibration_holdout_fraction` is the
  fraction actually held out: 0 when external calibration sessions replaced
  the tail split. The configured value moves to
  `configured_calibration_holdout_fraction`. A new `calibration` block records
  `source` (`external_sessions` / `internal_tail` / `none`), the effective and
  configured fractions, the episode gap, the drive floor, the override and the
  calibration policy. Test:
  `test_the_calibration_actually_used_and_seed_agreement_are_recorded`.
- [x] **A04.13 — Evaluate a replacement for the multivariate-outlier role.**
  If A06.1 keeps any multivariate outlier detector, the candidate is per-mode
  robust z or Ledoit-Wolf Mahalanobis over 10–20 curated physics residuals
  and core engine channels. Calibrate it per mode on held-out drives and
  count clustered exceedances as one event.
  - Note: the reviewed artifact has **zero** `deviation:*` columns
    (`behavior_model_columns: []`), so the docs' reliance on residual
    features to cover IF's axis-parallel limit does not hold today.
  - A naive max-|robust z| over the same 249 raw columns needed a threshold
    of ~80 and caught only a +20σ STFT shift. The gain comes from curating
    the inputs, not from swapping the algorithm.

  *Evaluated 2026-09-23. No candidate is adopted; this result feeds A06.1.*
  Two candidates run on the harness beside the deployed stack, on the same
  frames, drives and injections as A02.11: `robust_z_curated` (per-mode
  median/MAD) and `mahalanobis_curated` (per-mode Ledoit-Wolf,
  `evaluation_detectors.MahalanobisBaseline`). Their input is a 16-channel
  curated set held as profile data
  (`data/nissan-xterra-2008/curated_outlier_channels.yaml`): trims, bank
  balances and totals, lambda, airflow, VE, load, timing, coolant and IAT. The
  deployed rows reproduce A02.11's figures exactly (IF 1.18 FA/eh, sequence
  45.35, pca 0.39). The IF's withheld share moves from 81 % to 83 % because
  A04.8 now withholds `unknown` rows.
  [Report](../../../data/analyses/2026-09-23-a04-curated-candidates.md).
  - **False alarms per engine-hour (in-era drives, drive bootstrap):**
    robust_z_curated 12.4 [3.5, 28.2], withheld 32 %; mahalanobis_curated 6.1
    [0.2, 20.8], withheld 70 %. Both are over the budget of 1 per engine-hour.
    Mahalanobis withholds whole capture drives: the tuning-hybrid captures do
    not poll the Mode 01 channels in the set. Against these, pca 0.4 and
    untrained AE 0.8.
  - **Recall at the budget threshold:** 0 for both candidates on every
    realistic fault (MAF gain, lazy O2, thermostat, bank imbalance, idle air
    leak) at every severity.
  - **VUS-PR** (no skill 0.50): mahalanobis 0.57-0.63, robust_z_curated
    0.55-0.59. The IF gets 0.58-0.69 and the sequence detector 0.56-0.77.
  - **Single-quantity shift:** mahalanobis sees shifts where it scores
    (airflow up to 0.91, load 0.99, STFT 0.7 at +10 sigma on the best drive)
    but fails 5 drives and withholds 6. robust_z_curated fails 8 of 12 on the
    slow STFT/coolant channels.
  - The reviewed artifact's `behavior_model_columns: []` holds for the
    evaluation frames too: no `deviation:*` column is produced, so the set has
    no physics residual.
  - **Remaining:** the candidates' threshold is one alert-budget threshold
    over per-mode-standardized scores, cut on the deployed calibration drives
    (2 of 3 loaded). A per-mode, held-out-drive threshold (`ThresholdedScore`
    is single-threshold) is not built. Behaviour residuals are also absent.
    Both are for A06.1 to ask for if it keeps a multivariate detector.

  *Closed 2026-09-26.* The item asked for an evaluation, and that is done:
  the [report](../../../data/analyses/2026-09-23-a04-curated-candidates.md)
  exists, and no candidate is adopted. A06.1's answer came on 2026-09-23:
  the operator chose (b), evidence only.
  `DetectorRolesConfig` defaults `isolation_forest` to `evidence`, so no
  multivariate detector alerts. The two "Remaining" gaps above were
  conditional: they apply only if A06.1 keeps a multivariate detector, and
  it kept none in an alerting role. They are not owed work here. Reopen them
  if an alerting multivariate role is ever proposed again, for example after
  the supercharger corpus.

## Acceptance

Correctness items land with tests. Every model-changing item is judged on
A02's harness against A02.11's baseline, per drive and on the same rows.
A04.13 is an experiment whose result feeds A06.1, not a commitment to ship a
new detector.
