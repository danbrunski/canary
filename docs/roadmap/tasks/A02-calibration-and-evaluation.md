# A02 — calibration and honest evaluation

**Depends on:** nothing for the evaluation harness. Recalibrating a deployed
model needs the retrain the operator deferred on 2026-09-22 (D03.25). Every
decision in A06 is judged on this protocol, so land the harness first.
Follow [A00](A00-anomaly-detectors-README.md).

Today no number in the repo says how well either detector finds a fault on a
drive it has not seen:

- The retrain's post-training benchmark scores false alarms on the same
  sessions the thresholds were cut from.
- The deployed sequence thresholds rest on one 18-minute session.
- The fault-injection benchmark injects faults a range check would catch
  (a spike of 5× the channel maximum plus 1,000).
- No simple baseline has ever been compared.

This checklist builds the yardstick.

## Checklist

- [x] **A02.1 — Separate calibration sessions from evaluation sessions.**
  `_run_benchmark_gate` (`scripts/retrain.py`) scores clean-drive alerts and
  precision on `calibration_sessions`, the same held-out sessions the
  thresholds were cut from. The sequence detector's clean-alert rate is
  therefore ~0.1 % by construction, which is why A02.2's flood was invisible.
  - Split the holdout into disjoint *calibration* and *evaluation* sessions,
    grouped by physical drive. The gate scores evaluation sessions only.
  - Record both lists in the manifest and the benchmark report.
  - **Test:** a session in both lists is refused.

  *Done 2026-09-22.* `scripts/retrain.py` now works in physical drives
  (`src.models.evaluation.group_into_drives`: sessions that overlap or start
  under 5 min apart).
  - The calibration picker takes one session per drive, and calibration
    keeps the rest of each drive it touches (`src.models.evaluation.holdout_by_drive`,
    which every holdout-taking CLI now uses).
  - The *evaluation* holdout is up to three of the newest healthy drives
    left (`_pick_evaluation_holdouts`). Training keeps at least 2 sessions.
  `HoldoutSplit` refuses a session on both sides and a drive split across
  them; `--evaluation-sessions` names them explicitly. `_run_benchmark_gate`
  now scores evaluation sessions only and writes `holdout: {calibration,
  evaluation}` into the report; both trainers stamp `evaluation_sessions`
  beside `calibration_sessions` (new manifest field, IF and sequence).
  - *Verifier fix 2026-09-23:* drive expansion was only inside
    `retrain.main`. It now lives in one helper every holdout caller must use,
    `evaluation.holdout_by_drive`. The helper expands named or picked
    sessions to their whole drive, refuses a drive on both sides, and
    returns training without either. The callers are `retrain.py`,
    `train_sequence.py` (`_resolve_holdout` for `--calibration-sessions` and
    `--evaluate`) and `sequence_bakeoff.py` (explicit and auto-carved). The
    shared picker `_pick_calibration_holdouts` returns whole drives, in pick
    order. `train_sequence` no longer trains on everything when every
    session is held out. Tests:
    `test_calibration_and_evaluate_sessions_take_their_whole_drive`
    (train_sequence) and `test_bakeoff_holdouts_take_their_whole_drive`.
  - *Verifier fix 2026-09-23:* the regression gate compared totals from
    different drives. `detector_benchmark.diff_reports` now returns
    `comparable: False` with the reason when the reports differ in decode
    chain (`loader_version`), in sessions, or in `holdout.evaluation`; it
    never reports that as `regressed`. The retrain gate diffs against the
    newest *comparable* prior report and logs "not compared" otherwise, so a
    retrain does not fail on it. Tests:
    `test_reports_from_other_drives_or_another_decode_chain_are_incomparable`
    and the extended
    `test_run_benchmark_gate_runs_real_grid_and_discovers_prior_baseline`.
  - Tests: `test_a_session_in_both_holdouts_is_refused` (retrain exits 2,
    names the session; unit twin in `tests/test_evaluation_harness.py`),
    `test_gate_scores_evaluation_drives_disjoint_from_calibration`,
    `test_a_calibration_drive_takes_its_sibling_sessions`,
    `test_calibration_picker_takes_one_session_per_physical_drive`,
    `test_run_benchmark_gate_skips_cleanly_with_no_evaluation_sessions`,
    `test_one_physical_drive_cannot_sit_on_both_sides`.
  - *Fix 2026-09-23 (found in the A06.3 retrain):* a training pool holds no
    captures, so live sessions either side of a capture window read as two
    drives. The automatic split put one physical drive on both the
    evaluation and the training side. `drives_of_sessions` now uses the
    `_capture-*` directories beside the sessions to bridge the grouping, and
    they never join a side. Test:
    `test_a_capture_window_keeps_the_sessions_either_side_one_drive`.
  - *One physical-drive definition, 2026-09-23 (verifier finding).* The
    bridge first reached only `drives_of_sessions`. The sequence validation
    split and the IF per-mode floors still grouped frame rows without it,
    and so did the harness and behavior validation.
    - All of them now use `src/analysis/drives.py`. A drive is *vehicle
      activity*: OBD/UDS responses and decoded engine broadcast, with raw
      CAN frames only for a recording that has neither.
    - Sibling recordings, captures included, bridge the grouping.
    - Operator decision: a pause in activity longer than the join gap splits
      one recording into two drives. `2026-09-23T032755` kept writing GPS
      all night, so a data-gap rule could not split it.
    - `build_feature_frame` stamps each row's drive (`physical_drive`). The
      frame-level counts read that stamp: IF floors, sequence validation
      units, sequence calibration drives and behavior `group_drives`.
    - A directory with activity in two drives is one holdout unit, since it
      cannot be split across sides.
    - The harness groups drives the same way. Its "seen" check strips
      `#segment-00N` suffixes (`record_seen`).
    - Tests: `tests/test_physical_drives.py` (6), covering:
      - the pause split;
      - holdout units;
      - stamped counts through `_drive_count`;
      - validation never sharing a drive with training (fails under the old
        grouping);
      - behavior grouping;
      - the seen check.
- [x] **A02.2 — Refuse thresholds that do not cover what they will score.**
  The deployed sequence artifact (2026-09-20) was calibrated on one session,
  `2026-09-18T134345`: city and warmup only, 71–73 kPa, 0–58 km/h. The
  retrain was asked for three; two produced no windows. `a4a58a4` now stamps
  that into `calibration_holdout` and logs a WARNING, but still publishes.
  Measured with the real `SequenceInferenceEngine` and serve-side channel
  suppression:

  | Drive | Condition | Windows flagged |
  |---|---|---|
  | `2026-09-20T205117` | highway, ~75 kPa | 1,111 of 1,477 (75 %) |
  | `2026-08-07T155139` | idle, 80 kPa | 528 of 1,397 (38 %) |
  | calibration session | — | 0.47 % |

  All of those flags came through the per-family path, none through the
  global score. The same path flags **16–100 % of windows on the model's own
  training sessions** (14 sessions); only the calibration session is quiet.
  The thresholds are one session's maxima, not a model of normal (27-session
  table in the review).
  - Find out why the two requested sessions produced no windows, and fix or
    name the cause.
  - Refuse to publish (not warn) when the calibration sessions do not cover
    every operating mode the model will score, or number fewer than
    `MIN_CALIBRATION_SESSIONS`. Coverage comes from A03.8's envelope record.
  - An operator override must be explicit and stamped.

  *Cause named 2026-09-22.* The two sessions did not lack windows. Under
  the deployed schema, today's decode gives 3,751 windows for
  `2026-08-06T162657` and 1,056 for `2026-09-18T134345`. The 1,056 matches
  the manifest's `calibration_health.window_count`, a known answer
  reproduced. `2026-09-18T213818` scored 4,464 windows in the review. The
  a4a58a4 commit message and `~/canary-data/desk-0920/RESUME-retrain.md`
  record what happened: the sequence trainer rebuilt the calibration frame
  privately and uncached, the second rebuild ran out of memory, and the LSTM
  was re-run on one holdout. Fixed in a4a58a4, which shares one calibration
  frame between the detectors.

  *Refusal done 2026-09-22.* `train_sequence_autoencoder` checks the
  calibration before training (`calibration_coverage_violations`) and raises
  `CalibrationCoverageError`. It checks three things:
  - fewer than `MIN_CALIBRATION_SESSIONS` *physical drives*;
  - a mode with >= 10 independent training windows but fewer in calibration;
  - a channel never observed in calibration.

  `--accept-thin-calibration REASON` publishes anyway. The flag is on
  `retrain.py`, `train_sequence.py` and `sequence_bakeoff.py`; the library
  takes it as `calibration_override`. It stamps the reason and every
  violation into `calibration_holdout["override"]`.
  - Tests: `test_a_thin_held_out_calibration_is_refused_before_training`
    (the 2026-09-20 shape trips all three),
    `test_sessions_of_one_drive_count_once`,
    `test_an_explicit_override_publishes_and_is_stamped`,
    `test_train_refuses_a_single_holdout_unless_the_override_is_stamped`.
  - The held-out tests in `test_lstm_training.py`, `test_tcn_training.py`
    and `test_sequence_bakeoff.py` pass the override and say why.

  *Operator-facing consequences:*
  - A `retrain_all` job whose sequence model is refused still exits 0 as a
    partial success (the IF artifact lands), so the online scheduler logs it
    as success. The refusal is in the job log and the retrain report
    (`failed: refusing to calibrate …`).
  - `retrain_lstm` and `retrain_tcn` jobs fail on a thin corpus. The control
    job API takes an optional `accept_thin_calibration` reason (non-empty
    string) for `retrain_lstm`/`retrain_tcn`/`retrain_all` and passes it as
    `--accept-thin-calibration`. Nothing adds it by default. Test:
    `test_sequence_retrain_jobs_pass_an_operator_thin_calibration_reason`.
    No dashboard control sets it yet.

  *Remaining:* coverage is judged on operating modes from the windows
  themselves. A03.8's envelope record (baro/IAT/speed ranges per mode) does
  not exist yet; when it lands, its ranges become further violations in the
  same function.

  *Envelope wired 2026-09-23 (A03.8).* `calibration_coverage_violations`
  takes the training and calibration envelopes and refuses a scored mode
  whose calibration range covers less than `MIN_ENVELOPE_COVERAGE` (80 %) of
  its training rows on baro, IAT or speed, naming both ranges and the hours.
  The same `--accept-thin-calibration` override stamps it. Test:
  `test_a_calibration_outside_the_training_envelope_is_refused`. It takes
  effect at the deferred retrain.

  *Closed 2026-09-26: `[x]`, not `[~]`.* Under A00, `[~]` means named
  validation, deployment or an operator decision still remains. None does:
  - **All three asks are in code, under test.** The cause of the missing
    windows is named and fixed (`a4a58a4`). The refusal replaces the
    warning. The override is explicit and stamped. Re-run 2026-09-26:
    `tests/test_sequence_calibration.py -k "thin_held_out or
    explicit_override or outside_the_training_envelope"`, 3 passed; and
    `-m ml_training tests/test_train_lstm.py -k single_holdout_unless`,
    1 passed.
  - **"Takes effect at the deferred retrain" has happened.** The operator
    brought the D03.25 retrain forward: A06.3 option (a), retrained
    2026-09-23. On that corpus the refusal fired, and the operator overrode
    it by name. The deployed r2 LSTM manifest
    (`models/_candidates/2026-09-23-na-r2/lstm_autoencoder/model.manifest.json`,
    sha256 `cc034239…`, the hash A06.3 records as served since 2026-09-24
    16:03Z) carries `calibration_holdout.override`. It holds the reason
    "operator chose current-calibration-only corpus (post ZS44A-20260920),
    2026-09-23" and 8 violations: scored modes with too few calibration
    windows, six A03.8 envelope-coverage misses on IAT and speed, and 13
    `__std` channels never seen in calibration.
  - **What remains is not A02.2's.** The deployed thresholds really are
    thin, and the manifest says so. Better coverage needs more post-flash
    drives, which is corpus growth and a later retrain (A06.3's
    "Remaining"). It is not a missing refusal.
- [~] **A02.3 — Set thresholds from the alert budget, over independent
  windows.** Each of the 98 families is cut at its 0.999 quantile over about
  36 non-overlapping windows, which is that session's maximum. Beating the
  maximum of ~36 samples gives about 3 alert episodes an hour on a drive
  identical to calibration. So the project's own budget of ≤ 1 alert per
  engine-hour (`V1_ALERTS_PER_HOUR_BUDGET`) cannot be met by construction.
  - Compute thresholds over non-overlapping windows or per-episode maxima,
    pooled across calibration sessions.
  - Choose the level that meets the budget on the *evaluation* sessions.
  - Give the per-family path per-mode bands, or remove it if A02.5's
    ablation shows it adds nothing over the global score.

  *Done 2026-09-22 (takes effect at the next retrain; nothing deployed
  changes).* `calibrate_sequence_thresholds` now builds every distribution
  from per-block maxima: global, channel, family, family-max and per-mode.
  Each block is the worst window ending in a non-overlapping
  `sequence_length`-row block, pooled across sessions. The manifest records
  `block_rows` and `effective_window_count`, so a quantile the blocks cannot
  resolve is visible. `evaluation.alert_budget_threshold` picks the lowest
  threshold whose persistence-gated alert episodes per engine-hour meet
  `V1_ALERTS_PER_HOUR_BUDGET` on given drives, drawing candidates from
  pooled block maxima. When exposure is too short to tell the budget from
  zero it says `unresolvable`. The harness uses it to threshold every A02.5
  baseline on the deployed calibration drives. It also reports
  `budget_threshold_on_evaluation`: the level each scored detector would
  need on the evaluation drives.
  - Tests: `test_thresholds_rest_on_independent_blocks_pooled_across_sessions`,
    `test_budget_threshold_clears_the_rare_excursions_it_must`,
    `test_budget_threshold_says_when_the_exposure_cannot_resolve_it`,
    `test_block_maxima_take_one_sample_per_block`; `test_lstm_training.py`
    now asserts block counts rather than one sample per window.

  *Measured on A02.11's evaluation drives:*
  - The per-family path is most of the sequence detector's false alarms:
    45.4 vs 3.0 FA/eh with it ablated.
  - It adds recall only where the global path already fires at the larger
    severities, and on a few windows the flood leaves ungraded.
  - The global threshold that would meet the budget there is 1.2845 (5
    episodes in 5.07 engine-h). The deployed global band is 1.2133.
  - Alert budgeting follows the A06.5 roles: only `alert`-role detectors
    are held to it (`summarize_false_alarms`/`budget_thresholds_on`); an
    `evidence` detector's episodes are reported, not counted. Test:
    `test_evidence_role_detectors_are_not_held_to_the_alert_budget`.

  *Remaining:*
  - Wire the budget-chosen level into the sequence artifact at retrain, so
    `retrain.py` sets the threshold from its evaluation holdout. This waits
    for the retrain the operator deferred.
  - Per-mode family bands versus removing the family path. The ablation is
    `sequence` vs `sequence_global` in A02.11's table. Either change lands in
    `src/models/inference.py` (the A01 agent's file) and in the calibration.
- [x] **A02.4 — Build the evaluation harness.** One command that scores any
  detector, including the baselines in A02.5, on the same rows:
  - folds grouped by physical drive and ordered by `session_started_at`;
  - tune-revision and hardware-intervention boundaries respected, with no
    fold mixing sides;
  - results stratified by baro and ambient temperature;
  - confidence intervals from a bootstrap over drives, never over correlated
    seconds;
  - metrics: event-level recall, time to detect, and false alarms per
    engine-hour at the deployed threshold. No point-adjust scoring. VUS-PR
    only for threshold-free comparisons;
  - a per-drive table in the report, not pooled totals;
  - operating modes labelled by the same segmenter the daemon runs, not by
    sidecars. Sessions without a mode sidecar, such as captures, cannot be
    scored offline today, while live serving labels them itself.

  Reuse `src/models/false_positive_benchmark.py` and
  `src/models/synthetic_fault_eval.py` where they fit; do not build a second
  benchmark beside them.

  *Done 2026-09-22.* `scripts/evaluate_detectors.py` is the one command.
  `src/models/evaluation.py` holds the protocol (pure, unit-tested) and
  `src/models/evaluation_detectors.py` the detectors.
  - **Drives.** Sessions are grouped by physical drive and ordered by parsed
    start time. `session_span` reads the parquet footers.
  - **Eras.** A drive belongs to a tune revision and hardware boundary
    (`era_resolver`, which reads `tune_revisions.yaml` and labelled
    `corpus_boundary` events). A drive that spans a flash is skipped.
    `Fold`/`grouped_folds` refuse a fold that mixes eras. Drives from an era
    the artifacts never trained in are reported per drive and kept out of
    every summary.
  - **Held out.** Any drive a compared detector trained or calibrated on is
    skipped, with the session and role named.
  - **Strata.** Baro and ambient bands per drive use freshly observed
    values. Ambient reports `unmeasured` when the vehicle has no PID 0x46;
    IAT is not a stand-in.
  - **Uncertainty.** Every interval is a bootstrap over drives
    (`bootstrap_ratio_ci`).
  - **Metrics.** Alert episodes after the production persistence gate;
    false alarms per engine-hour, where only a `fault` label makes an
    episode a catch and novelty or intervention episodes are broken out but
    still counted; and event recall and time to detect at the deployed
    threshold. A detection is an alert the fault caused on the same rows
    (`fault_attributable_detection`), which is not point-adjust.
    `vus_pr` is threshold-free and reported for comparison only.
  - **Reports.** Per-drive table in JSON and Markdown. Each result says
    whether it was withheld, skipped, quiet or flagged.
  - **Modes.** Labelled by `featurize_wide_frame`, the transform the daemon
    runs. No sidecar is read, so captures are scored.
  - **Reuse.** It reuses `synthetic_fault_eval` (fusion, IF verdicts, the
    fault injectors) and `detector_benchmark.load_resampled_session`.
  - **Behaviour change.** `load_resampled_session` now runs the trainers'
    per-session chain: bindings, DBC and DID decode, the quality gate, then
    `drop_fully_stale_rows`. It used to decode bindings only. Numbers from
    `scripts/detector_benchmark.py` and from the retrain gate therefore
    shift against reports made before this change. Reports carry
    `loader_version: 2`, and older ones are no longer compared against.
  - **Speed.** `BatchedSequenceScorer` applies the served sequence rule to
    batches, ~10x faster. `sequence_parity` checks it against
    `SequenceInferenceEngine.observe` on every run and records the result.
    On `2026-09-22T171544`: 0 verdict mismatches in 271 windows, maximum
    score difference 3.5e-7.
  - *Verifier fix 2026-09-23:* a module-level window cache keyed on
    `id(frame)` served one throwaway injection frame's windows for the next,
    because a freed id is reused (2 stale results in 40, reproduced). The
    cache is gone and windows are rebuilt per frame. Regression test:
    `test_sequential_frames_of_equal_length_and_start_never_share_windows`,
    which scores one frame, changes it in place and scores it again (same id,
    length and first timestamp), so the old cache patched back in fails it
    12 of 12 runs. The first A02.11 run's
    sequence, sequence_global, untrained_autoencoder and fused numbers were
    invalid, so A02.11 was re-run.
  - Tests: `tests/test_evaluation_harness.py` (17),
    `tests/test_evaluation_detectors.py` (8, including
    `test_batched_sequence_scoring_matches_the_served_engine` across a time
    gap), `tests/test_evaluate_detectors_script.py` (8, including an
    end-to-end run that skips a seen drive and names why).
- [x] **A02.5 — Compare against simple baselines on the same rows.**
  - per-channel robust z-scores per operating mode;
  - PCA reconstruction;
  - an untrained autoencoder of the same shape;
  - the A05 plausibility rules.

  The literature's consistent finding is that deep detectors rarely beat
  these on multivariate time series (Kim et al. AAAI 2022; Schmidl et al.
  PVLDB 2022; Sarfraz et al. ICML 2024; TSB-AD, NeurIPS 2024). A detector
  that does not beat them on A02.4 does not alert (A06).

  *Three of four done 2026-09-22* (`src/models/evaluation_detectors.py`).
  - `RobustZBaseline`: median/MAD per mode and channel over fresh samples.
    A zero-spread channel in a mode is left out rather than scoring
    infinite z.
  - `PCABaseline`: masked reconstruction error.
  - `UntrainedAutoencoder`: the deployed architecture with random weights,
    scored through the same batched rule.

  All three see the served models' raw channels and are fit on their
  training sessions. Each is thresholded by the alert budget on the deployed
  calibration drives, then scored on the same held-out rows. Results are in
  A02.11.
  - Tests: `test_robust_z_is_per_mode_and_skips_degenerate_channels`,
    `test_pca_reconstruction_scores_off_manifold_rows_higher`,
    `test_a_thresholded_baseline_withholds_rows_it_cannot_score`.

  *A05 added 2026-09-23* (A05 landed in 06a3188). `PlausibilityDetector`
  wraps `src.plausibility.offline` as a `RawStreamDetector`, a harness
  detector that reads arrived samples instead of the resampled grid.
  - A fresh `PlausibilityEvaluator` runs over each session's arrived-sample
    stream. Streams are cached as parquet under the harness cache and keyed
    on the session and the resolved limits catalog.
  - Each *confirmed* detection lands on the first grid row at or after it.
    Confirmation is A05's own persistence, so the verdicts are pre-gated.
  - A session with no observed checked channel is withheld. The detector has
    no score, so it gets no VUS-PR or budget threshold.
  - Injections use `inject_realistic_samples`, which applies the same A02.6
    fault to arrived samples. Clean and faulted streams are cut to the
    fault's window and to the answer groups of its role channels (A05.8's
    own restriction).
  - Tests: `test_plausibility_confirmations_land_on_the_shared_row_grid`, and
    the end-to-end run with `--baselines plausibility` in
    `test_main_scores_only_unseen_drives_and_names_every_skip`.
  - Results in A02.11.
- [x] **A02.6 — Inject realistic faults, not only plumbing tests.** Today's
  `_inject_one` (`src/models/synthetic_fault_eval.py`) injects:
  - spike = 5 × max + 1,000;
  - scale × 5;
  - drift = 10σ + 100.

  Keep those as plumbing tests. Add severity sweeps a range check cannot
  catch, applied consistently across related channels:
  - MAF gain error from 0.90 to 0.97;
  - a lazy O2 sensor response;
  - a slow coolant or thermostat trajectory;
  - bank-to-bank fuel imbalance;
  - a small unmetered-air leak signature at idle.

  Report recall against severity, not a single rate.

  *Done 2026-09-22.* `synthetic_fault_eval.inject_realistic_fault` adds five
  faults, each with four severities:

  | Fault | Severities | What changes |
  |---|---|---|
  | `maf_gain` | gain 0.90, 0.93, 0.95, 0.97 | airflow and load x g; closed-loop STFT +(1/g-1)x100 |
  | `lazy_o2` | lag τ 2, 5, 10, 20 s | bank-1 A/F sensor |
  | `thermostat` | plateau -3, -6, -10, -15 °C | coolant, reached over 120 s |
  | `bank_imbalance` | 2, 4, 6, 10 % | bank-1 STFT |
  | `idle_air_leak` | 0.2, 0.4, 0.6, 1.0 g/s | both banks' STFT +100·leak/MAF, capped at trim authority |

  - **Related channels.** Each fault moves every channel of the same
    quantity. The J1979 core is in code; the vehicle's DID and broadcast
    mirrors are in `data/nissan-xterra-2008/fault_injection.yaml`.
    `decoded:` counts go through the binding scale. A channel that cannot be
    perturbed is named with the reason (`absent`, `not observed`,
    `unscaled`).
  - **Windows.** `choose_fault_window` places each window where the fault
    can show (idle for a leak, closed loop for trim faults) or says why it
    cannot.
  - **Sweep.** The harness uses the same window for every severity, so
    recall against severity compares the same rows. A02.11 has the table.
  - Tests: `test_maf_gain_scales_every_airflow_channel_and_trims_compensate`,
    `test_thermostat_fault_moves_decoded_counts_through_the_binding_scale`,
    `test_lazy_o2_lags_the_sensor_without_changing_its_mean_level`,
    `test_idle_air_leak_signature_is_largest_at_idle`,
    `test_fault_window_names_why_a_drive_cannot_carry_the_fault`,
    `test_fault_channel_roles_refuse_an_unknown_role`.
  - The plumbing kinds (`_inject_one`) are unchanged.
- [~] **A02.7 — Label the real events the project already has.**
  - the flashes in `tune_revisions.yaml`;
  - the LTFT reset after 2026-09-20;
  - the 2026-06-28 battery disconnect and relearn;
  - the altitude trips, labelled *novelty, not fault*;
  - the supercharger install, once it happens.

  Each is an interval with provenance, stored as data (vehicle ledger or the
  second-brain intervention record), and scored by A02.4.

  *Done where the repo's data supports it, 2026-09-22.* The labels are in
  `data/nissan-xterra-2008/labelled_events.yaml`, with a strict schema in
  `src/analysis/labelled_events.py`. Every interval requires provenance and
  an `end_basis`. `kind` is `fault`, `novelty` or `intervention`, and only
  `fault` counts toward recall.
  - **Flashes.** One `intervention` per revision is derived from
    `tune_revisions.yaml` rather than copied.
  - **LTFT after the 2026-09-20 flash** (`novelty`). Measured from
    per-session LTFT: 0.0 in every local session after the flash through
    2026-09-22T171544. `end_basis: last_observation`; the relearn has not
    been seen yet.
  - **2026-06-28 battery disconnect** (`novelty`). The start comes from the
    ledger and is date-only. The end is 2026-09-18T134345, the first nonzero
    LTFT measured.
  - **Altitude trips** (`novelty`), measured from per-session baro medians
    against ~80 kPa: 2026-08-06 (72-82 kPa), 2026-09-18 (63-79 kPa, D03.20)
    and 2026-09-20T205117 (75 kPa, OBD rows only).
  - **Scoring.** The harness reports each label's flagged rows and episodes
    per detector. Only episodes inside a `fault` label leave the
    false-alarm count. A novelty episode is explained, not excused; the
    81-day LTFT label would otherwise hide whole drives.
  - Test: `test_only_fault_labels_take_episodes_out_of_the_false_alarms`.

  *Remaining:*
  - The supercharger install, once it happens (`intervention`,
    `corpus_boundary: true`).
  - An exact time for the battery disconnect, which only the operator has.
  - The project has no `fault` label. A02.8 is the only source short of a
    breakdown.
- [~] **A02.8 — Induce reversible faults on the parked truck.** *Truck and
  operator item.* A small metered vacuum leak, an unplugged sensor, or a
  restricted intake, each tagged as a physical action (key-on/engine-running
  as the procedure states). These are the only real fault labels the project
  can get short of a breakdown. The operator chooses which, if any, are
  acceptable. Record each as a capture with its own interval.

  *Not started (truck and operator).* The harness is ready to score them.
  Add each as a `kind: fault` entry in `labelled_events.yaml` with the
  capture's interval.
- [x] **A02.9 — Explain why fused recall was zero.** In the 2026-09-20
  benchmark (`~/canary-data/desk-0920/bench_new.json`: 10 cells, 5 channels ×
  2 sessions, 5 runs per fault kind) the Isolation Forest caught 26 of 250
  injections and the fused detector 0 of 250. Establish whether fusion's
  severity threshold, a missing sequence detector in the benchmark run, or
  something else suppresses every event. Fix the cause or name it.

  *Cause named and fixed 2026-09-22.* The sequence detector was missing,
  and on top of that the harness could never have fused it.
  - `bench_new.json` grades only `isolation_forest` and `fused`. No
    `sequence` entry exists: the run used `canary.dev.example.yaml`, which
    disables the LSTM (`RESUME-retrain.md`).
  - Fused `is_anomaly` needs `min_agreement: 2` independent firing sources
    (6.2, `canary.example.yaml`). `_fused_anomalous` handed fusion the IF
    alone, even when a sequence artifact *was* supplied.
  - So fused was structurally 0, and the severity threshold played no part.

  Fixed:
  - Fusion now gets the sequence source the daemon gives it: the global
    score, each source gated by its own persistence before fusing.
  - With fewer sources than `min_agreement`, fused is *withheld* with the
    reason (`report["withheld"]`) instead of reported as recall 0.
  - `detector_benchmark` carries the withheld reasons into its rollup.
  - `load_resampled_session` runs the training decode chain, so the
    sequence detector sees its `did:` channels.
  - Tests: `test_evaluate_detectors_grades_each_fault_kind_and_keeps_nominal_fp_low`
    (withheld, names `min_agreement`),
    `test_fused_fires_only_when_both_sources_are_sustained_together`,
    `test_evaluate_detectors_covers_sequence_detector_when_available` (fused
    graded with two sources).
  - Separately, the IF's 26 of 250 is A04.1's single-quantity blindness.
  - Since A06.5 (55a407c) both harness fusions build
    `FusionScorer(..., roles=config.models.roles.by_source())`, so fused
    verdicts follow the configured detector roles.
- [x] **A02.10 — Fix the record of what the calibration is.** The code holds
  out a fixed set of up to `MIN_CALIBRATION_SESSIONS` sessions
  (`_pick_calibration_holdouts`); it is not leave-one-out. D03.22 and D03.25
  say "LOO", and so does an operator memory note. Correct the docs, or
  implement grouped cross-validation if A02.4 shows the fixed holdout is too
  noisy. Say which.

  *Docs corrected 2026-09-22; grouped CV not implemented.*
  - D03.22's two "(LOO)" mentions (`D03-open-work.md`) now say fixed
    holdout.
  - The operator memory notes are corrected: the `MEMORY.md` index line,
    `project_out_of_sample_calibration.md` and
    `project_mode_labels_raw_counts.md`.
  - The fixed holdout stays. A02.11 shows why. Its intervals are already
    wide at the drive level (served sequence 45.4 [24.5, 61.3] FA/eh on 13
    drives), and there is one in-era corpus. Grouped CV would reuse the same
    few drives and leave the evaluation side thinner still. The calibration
    shortfall is the physical-drive count, which A02.2 now refuses.
  - `evaluation.grouped_folds` exists (leave-one-drive-out within an era)
    for when the corpus supports it.
- [~] **A02.11 — Baseline today's deployed models on the harness.** Before any
  retrain, score the current Pi artifacts on A02.4, with A02.5's baselines
  beside them. That table is the "before" every later change is compared
  against.
  - Record the artifact hashes, and verify them against the Pi rather than
    assuming the laptop copy matches (Pi item).

  *Laptop baseline archived 2026-09-23* (re-run after the verifier's
  window-cache fix, with A05 added):
  [`data/analyses/2026-09-23-a02-detector-baseline.md`](../../../data/analyses/2026-09-23-a02-detector-baseline.md).
  The `.json` beside it has every injection.
  - **Scope.** 16 held-out drives, 13 in-era with engine time (5.07
    engine-h). 12 drives were skipped, each with its reason. 232
    injections (5 realistic faults x 4 severities). 8 detectors on the
    same rows, all role `alert` (the `models.roles` default); fusion runs
    with the configured roles.
  - **Artifacts (laptop copies).** IF `model.joblib` sha256 `af1720e7…0e1d`,
    LSTM `model.pt` sha256 `c77165ad…1112`.
  - **Known answer first.** Batched and served sequence scoring agree: 0 of
    171 windows differ, maximum score difference 1.2e-7.
  - Command:
    `scripts/evaluate_detectors.py --config canary.example.yaml
    --profile nissan-xterra-2008 --sessions-root <6 roots> --since
    2026-08-03 --baseline-fit-sessions 8 --injections-per-drive 2
    --cache-only`.

  **Regenerated on default-profile rows (R04.6, 2026-09-26):**
  [`data/analyses/2026-09-25-a02-detector-baseline-default-profile.md`](../../../data/analyses/2026-09-25-a02-detector-baseline-default-profile.md)
  (`.json` beside it).
  - **Command.** `.venv/bin/python scripts/evaluate_detectors.py --config
    canary.example.yaml --profile nissan-xterra-2008 --sessions-root
    ~/canary-data/sessions/nissan-xterra-2008 --sessions-root
    ~/canary-data/sessions --sessions-root ~/canary-data/sessions-0917
    --sessions-root ~/canary-data/sessions-0918 --sessions-root
    ~/canary-data/sessions-0920 --sessions-root ~/canary-data/sessions-0921
    --since 2026-08-03 --baseline-fit-sessions 8 --injections-per-drive 2
    --workers 1 --out
    data/analyses/2026-09-25-a02-detector-baseline-default-profile.json`,
    no `--cache-only`. Run twice: on the bounded decode (1 h 43 min), and
    again on the P01 derived frames (`--log-level INFO` added, 19 min 40 s,
    all 67 frames `reused`). Both reports are identical apart from
    `generated_at` and `command`.
  - **Scope.** 18 drives evaluated: 12 in-era, 8 of them with engine time
    (6.70 engine-h), and 6 cross-era (reported, in no summary). 15 skipped
    with reasons: seen in training or calibration, or `no default-profile
    rows`. 184 injections. Every session loaded (`load_errors` empty),
    including `2026-09-20T152758` and `2026-09-18T213818`, so the baseline
    thresholds rest on 3 of 3 calibration sessions. Same artifacts as the
    superseded table (IF `af1720e7…0e1d`, LSTM `c77165ad…1112`). Batched and
    served sequence scoring agree: 0 of 171 windows differ, max score
    difference 1.2e-7.
  - **Roles changed since 2026-09-23.** `models.roles` now makes IF,
    sequence and sequence_global `evidence` (not held to the alert budget),
    so the harness gives them no bootstrap interval. Their row below is the
    pooled rate over the 8 in-era drives with engine time (episodes ÷ 6.70
    engine-h).

  False alarms per engine-hour, in-era drives:

  | Detector | Role | FA/eh | Rows withheld |
  |---|---|---|---|
  | `sequence` as served | evidence | 40.0 pooled (268 episodes) | 3 % |
  | `sequence_global` (family path ablated) | evidence | 1.2 pooled (8) | 3 % |
  | `isolation_forest` | evidence | 2.4 pooled (16) | 61 % |
  | `fused` | alert | 0 [0, 0] | 3 % |
  | `robust_z` | alert | 3.29 [1.71, 7.08] | 10 % |
  | `pca` | alert | 0.15 [0, 1.01] | 11 % |
  | `untrained_autoencoder` | alert | 0.30 [0, 0.37] | 3 % |
  | `plausibility` (A05) | alert | 0 [0, 0] | 1 % |

  (Alert rows: drive-bootstrap 95 %.)
  - **Budget.** Among alert-role detectors, fused, PCA, the untrained
    autoencoder and A05 stay under 1 per engine-hour; robust-z does not. The
    budget-meeting level on these drives: robust-z 2211.2, PCA 114858, the
    untrained AE 1.596 (each 5-6 episodes in 6.70 engine-h).
  - **Recall at the deployed threshold.** `maf_gain` and `lazy_o2`: 0 for
    every detector at every severity. `thermostat -15 °C`: served sequence
    4 of 4 graded (10 masked by its own flood), `sequence_global` 0.36,
    IF 0.17, untrained AE 0.14. `bank_imbalance 10 %`: served sequence 3 of 3
    (5 masked), untrained AE 0.12. `idle_air_leak 0.6–1.0 g/s`: served
    sequence 2 of 2 (6 masked); nothing else.
  - **VUS-PR** (no skill = 0.50): sequence 0.80 on idle leak and 0.77 on
    thermostat; IF 0.60–0.66 across faults.
  - **Against the superseded table.** Without the tuning captures, the served
    sequence detector's rate is 40.0 pooled (was 45.4 [24.5, 61.3]).
    `sequence_global` is 1.2 (was 3.0), IF 2.4 (was 1.2, withheld 61 % vs
    81 %), robust-z 3.29 (was 39.0), PCA 0.15 (was 0.4) and the untrained AE
    0.30 (was 0.8).

  **Superseded scope (R04.6, 2026-09-25).** This table scored tuning
  captures as evaluation rows: 8 of its 16 drives include capture sessions
  and 6 are captures alone. The operator decision of 2026-09-24 takes tuning
  logs out of the detectors, and `evaluate_detectors` now scores
  default-profile rows only. Read the numbers below as the pre-R04.6
  baseline; the regenerated table is above (R04.6, 2026-09-26).

  False alarms per engine-hour, in-era drives, drive-bootstrap 95 %:

  | Detector | FA/eh | Rows withheld |
  |---|---|---|
  | `sequence` as served | 45.4 [24.5, 61.3] | 2 % |
  | `sequence_global` (family path ablated) | 3.0 [1.7, 5.0] | 2 % |
  | `isolation_forest` | 1.2 [0, 3.9] | 81 % |
  | `fused` | 0 | 2 % |
  | `robust_z` | 39.0 [7.6, 91.2] | 4 % |
  | `pca` | 0.4 [0, 1.1] | 5 % |
  | `untrained_autoencoder` | 0.8 [0.2, 1.5] | 2 % |
  | `plausibility` (A05) | 0 | 0 % |

  - **Budget.** Only fused, PCA, the untrained autoencoder and A05 stay
    under 1 alert per engine-hour on unseen drives. The family path turns
    3.0 into 45.4.
  - **Recall at the deployed threshold.**
    - `maf_gain` and `lazy_o2`: 0 for every detector at every severity.
    - `bank_imbalance 10 %`: served sequence 2 of 2 graded (6 masked by its
      own flood); untrained AE 1 of 7.
    - `thermostat -15 °C`: served sequence 5 of 5 (9 masked),
      `sequence_global` 0.29, untrained AE 0.14, fused 0.07, IF 0.
    - `idle_air_leak 0.6–1.0 g/s`: served sequence 2 of 2 (4 masked).
    - A05's range, stuck, freshness and slew checks catch none of these by
      design. They are sub-range faults; A05's value is the gross faults
      (see A05.8).
  - **VUS-PR** (no skill = 0.50): sequence 0.77 on idle leak and 0.74 on
    thermostat. IF is 0.63–0.69 on the trim and airflow faults. The
    untrained AE is 0.68 and 0.71. PCA and robust-z sit at 0.54–0.71.
  - **Level that would meet the budget on these drives:** sequence global
    1.2845 (deployed band 1.2133); IF 0.0950.

  *Remaining:*
  - **Pi hash check (Pi item).** Verify the two sha256s above against the
    Pi's deployed files.
  - ~~Two sessions still not scored.~~ *Resolved 2026-09-26 (P01.1).*
    `2026-09-20T152758` and `2026-09-18T213818` now load at 1.31 GB and
    1.06 GB peak (they were OOM-killed at 12-15 GB) and both are in the
    regenerated table above; the baseline thresholds rest on 3 of 3.
    (Previously: `baseline_threshold_sessions_not_loaded` named `213818`;
    `test_a_baseline_session_that_did_not_load_is_named_not_counted` keeps
    that reporting.)

- [x] **A02.12 — A family or channel with no calibration data must not
  score.** In the 2026-09-20 sequence artifact, `did:ECM_INJECTOR_PULSE_1`
  and `_2` had no observations in the calibration session (1,056 windows,
  maximum error 0.0). So their family threshold is **0.0**, and any nonzero
  error flags.
  - Those DIDs joined the default profile on 2026-09-21. On the tuning
    capture `_capture-tuning-2026-09-21T232153` they produced 1,525 of the
    1,554 family flags.
  - A threshold computed from zero observations must be recorded as
    *uncalibrated*, and the family excluded from scoring and attribution
    until a calibration covers it.
  - Refuse to publish an artifact in which a scored family or channel has no
    calibration observations.
  - Apply the same rule to channels rarely observed in training: they drive
    the global score on the newest drives (28.6 % and 15.8 % of windows on
    the two 2026-09-22 drives, injector pulse the top global channel).
  - **Test:** a calibration frame with an all-unobserved family yields no
    threshold for it and no flags from it.

  *Serving half done 2026-09-22.* `SequenceInferenceEngine`
  (`src/models/inference.py`) treats a channel whose calibrated threshold is
  not positive as never observed. It leaves the score mask, the family path
  and the evidence, exactly as a suppressed channel does; the encoder still
  receives it. The engine names such channels at load in a WARNING
  (`uncalibrated_channels`). This makes the deployed 2026-09-20 artifact
  score the two injector-pulse families as calibration did (masked), rather
  than flagging them on any nonzero error.
  - Test: `test_a_channel_never_observed_in_calibration_is_not_scored`.
  - `test_inference_flags_anomaly_against_low_thresholds` used zero
    thresholds to force a flag; it now uses 1e-9.

  *Calibration half done 2026-09-22.* `calibrate_sequence_thresholds`
  computes each channel's error only over the timesteps it was observed, and
  its distribution only over the windows where it was. A channel is recorded
  in the manifest's `uncalibrated_channels`, with the reason and a zero
  distribution (threshold 0.0, which the engine does not score), when it is:
  - never observed in calibration;
  - observed in fewer than 10 independent calibration windows and under half
    of those available;
  - rare in training by the same rule (`training_observed_blocks`, taken
    from the training dataset's mask).

  The global, per-mode and family thresholds are computed without those
  channels, as the engine scores them. A family with no calibrated member
  goes into `uncalibrated_families`. A channel never observed in calibration
  refuses the publish before training (`CalibrationCoverageError`, A02.2)
  unless overridden and stamped.
  - Tests: `test_an_unobserved_channel_gets_no_threshold_and_never_flags`
    (the checklist test, through the real engine),
    `test_a_channel_rare_in_training_is_uncalibrated_with_the_reason`,
    `test_a_thin_held_out_calibration_is_refused_before_training`.
  - Under the deployed schema, `2026-09-18T134345` has 14 never-observed
    channels, matching the engine's 14 `uncalibrated_channels` at load.

## Acceptance

A02.1–A02.5, A02.9–A02.12 are desk work. A02.8 needs the truck and the
operator's choice of faults. Nothing here changes a deployed threshold until
the operator lifts the retrain deferral. A02.11's table plus A02.4's harness
are what A06's decisions cite.
