# A03 — sequence detector inputs and training

**Depends on:** A02 for evaluation. Items that change the trained model take
effect only at the next retrain, which the operator deferred on 2026-09-22
(D03.25). A06 decides whether that retrain happens for the naturally
aspirated engine at all. Code and tests can land before then. Follow
[A00](A00-anomaly-detectors-README.md).

The 2026-09-20 artifact reconstructs 361 columns: 98 channel families × raw,
`__mean`, `__std`, `__roc`, with duplicate sources for the same quantity.
Many are driving conditions (baro, IAT, speed, TPMS, steering, HVAC) rather
than engine responses. So its score measures "unusual trip", not "engine
responding abnormally to its inputs". Its "observed" mask also does not mean
what the code says it means.

## Checklist

- [~] **A03.1 — Make the mask mean "a sample arrived".** The mask is
  `¬<channel>__gap`, and `__gap` means "no sample within `max_gap`": 2 s for
  fast channels, 120 s for slow ones (`resampler.py`, `live.py`). So
  forward-filled cells read as observed, contrary to the docstrings in
  `sequence_dataset.py` and `lstm_autoencoder.py`. Measured on the
  calibration session:
  - Barometric pressure had 52 real samples in 1,085 s, yet mask = 1 on
    90.8 % of rows. About 95 % of its "observed" cells are forward-fills, and
    Δt is nonzero on only 10 % of them.
  - Coolant and IAT show the same pattern.
  - Fast-class PIDs polled every 4–6 s count each sample on about 2 rows.

  Consequences: GRU-D's decay engages only for dead channels, and slow
  regime channels are scored ~18 times per real sample.
  - Emit a separate per-cell "sample arrived this tick" flag from both the
    offline resampler and the live source. Use it for the mask and Δt.
  - Keep `__gap` for dead-sensor detection.
  - **Parity test:** the offline and live paths produce the same flag for
    the same input stream.

  *Code done 2026-09-23; takes effect at the next retrain.*
  - **The flag.** The resampler emits `<channel>__arrived`: true on row `t`
    when a quality-gated sample is stamped in `(t - interval, t]`
    (`resampler._arrival_flags`, both `resample_wide` and `resample_long`).
    `LiveFeatureSource` emits the same flag: the newest accepted observation
    is newer than the previous tick. Derived channels get one via
    `derived._append_dependency_gap` (any input arrived, none stale).
    `__gap` is unchanged and stays the dead-sensor signal.
  - **One exclusion helper.** Every column selector now uses
    `resampler.is_flag_column`, so the flag cannot become a feature, a
    rolling column or a scaler column. `FEATURE_FRAME_CACHE_VERSION` is 5.
  - **Gated per artifact.** The mask rule is recorded in the manifest schema
    (`observation_mask`: `arrived_sample`, plus the freshness-fallback
    columns). Serving reads it back (`SequenceInferenceEngine` →
    `SequenceWindowBuffer`). A schema without the record is
    `fresh_within_max_gap`, so the deployed 2026-09-20 artifact keeps its
    `¬__gap` mask. `build_sequence_windows` refuses `arrived_sample` on a
    frame with no arrival flags. `train_sequence.build_dataset` refuses a
    model schema without that model's rule. Calibration windows must carry
    the training rule.
  - **Parity tests:**
    - `test_arrival_flags_match_the_resampler_for_the_same_stream`: off-grid
      samples, a quiet gap, a slow channel and a quality-gate reject give
      identical flags offline and live.
    - `test_arrival_flags_survive_the_live_feature_transform`.
    - `test_serve_buffer_builds_the_offline_mask_and_delta_for_the_same_rows`
      (both rules).
    - Also `tests/test_sequence_observation_mask.py` (7) and the two
      `test_preprocessing_resampler.py` arrival tests.
  - **Known answer reproduced** on `2026-09-18T134345` (1,085 rows, deployed
    schema, 357 of 361 columns present). Baro has 52 arrivals. Legacy mask
    89.7 % vs arrival mask 4.8 %; 18.7 legacy "observed" rows per real
    sample; Δt nonzero on 10.2 % → 95.1 % of rows. Coolant 90.4 % → 14.7 %.
    IAT 90.1 % → 15.1 %. MAF and STFT1 go from 2.0 rows per sample to 1.
    Overall observed cells 57.1 % → 49.9 %. Only 8 columns (the
    `derived:AFR_B1`/`AFR_CENSORED_B1` families) fall back to freshness.
  - **Dropout injection.** `AnomalyInjector.dropout` clears `__arrived` over
    the whole window. Before that, an arrival-mask artifact saw an injected
    dropout as the held value arriving every tick (a stuck sensor), and
    derived channels inherited it. Test:
    `test_nothing_arrives_inside_an_injected_dropout`, which covers a derived
    dependent too.
  - **Export.** `src/datasets/export.py` resampled exports now carry
    `<channel>__arrived` beside `<channel>__gap`.
  - *Remaining:*
    - The retrain (operator-deferred, D03.25), then serving validation of the
      retrained artifact on the Pi.
    - The A02 harness follows the manifest's mask rule (A04, pending its
      commit).
- [~] **A03.2 — Condition on driving conditions; reconstruct engine
  responses.** Split the columns into:
  - *inputs*: operator commands and environment, such as rpm, throttle or
    pedal, speed or gear, baro, IAT, coolant, AC load and supply voltage;
  - *targets*: what the engine does in response, such as air mass, trims,
    lambda, timing, idle air and load.

  Score only target residuals, by operating point. This is the shape A06.4
  asks for; A06 decides whether to build it into the sequence model or leave
  it to the behavior models. Design and ablation here, not a rewrite ahead of
  that decision.

  *Design implemented as a feature-spec option 2026-09-23; ablation not
  run.* `src/models/sequence_feature_set.py` defines two feature sets:
  - `all_channels`: the deployed shape and the default, so nothing deployed
    changes.
  - `engine_response`: inputs condition the encoder; targets are the only
    loss (`masked_mse_loss(loss_weights=…)`), the only thresholds (context
    columns are uncalibrated "context input") and the only score (the engine
    excludes `context_columns`).

  Roles:
  - Generic J1979 and derived channels are assigned in code.
  - The Xterra's `decoded:`/`did:` channels are in
    `data/nissan-xterra-2008/sequence_roles.yaml`.
  - An unassigned channel is named in the record and never scored.

  Selected with `train_sequence.py --feature-set engine_response`. The
  resolution is recorded in the feature spec (`sequence_feature_set`).
  - Over the deployed 361 columns it keeps 82: 43 context columns and 39
    target columns. It drops 279, each with its reason. 13 unknown
    `decoded:` channels are unassigned. One same-quantity pair is
    unconfirmed: `engine_speed` from `did:ECM_RPM` and `obd:RPM`. These
    counts are pinned in
    `test_engine_response_over_the_deployed_schema_names_every_column`.
  - Tests: `tests/test_sequence_feature_set.py` (6), including
    `test_engine_response_conditions_on_inputs_and_scores_only_targets` and
    the accounting over the deployed schema.

  *Remaining:* the A02-protocol ablation. The harness follows the manifest's
  mask rule and context columns (A04, pending its commit). The training runs
  exceed this session's bound.

  Commands (same corpus, same held-out drives, per-drive report):
  `scripts/train_sequence.py --backbone lstm --config canary.example.yaml
  --profile nissan-xterra-2008 --sessions-root <roots> --calibration-sessions
  <A02 calibration drives> --evaluate <A02 evaluation drives> --feature-set
  {all_channels,engine_response} --out models/_ablation-a03/<set>`, then
  `scripts/evaluate_detectors.py --config canary.example.yaml --profile
  nissan-xterra-2008 --sessions-root <roots> --since 2026-08-03
  --sequence-model models/_ablation-a03/<set>/model.pt --out
  data/analyses/<date>-a03-ablation-<set>.json`. Compare per drive against
  A02.11. A06 decides which, if either, is deployed.
- [~] **A03.3 — De-duplicate sources and justify every derived column.**
  - Four coolant channels contribute 19.2 % of the global-score numerator
    from 7.1 % of the observed cells.
  - `BAROMETRIC_PRESSURE__std` is a step detector: its scaler σ is 0.21 and
    it is zero except when a 1 kPa step arrives. It leads the top-feature
    ranking even on in-distribution calibration windows.

  Keep one source per physical quantity, chosen by the ground-truth suite.
  Drop `__std`/`__roc` targets unless an ablation on A02's protocol shows
  they carry detection value. Record the choice in the feature spec, not in
  a comment.

  *Implemented inside `engine_response` 2026-09-23; ablation not run.*
  - **One source per quantity.** A broadcast channel is dropped for the PID
    it mirrors only when the ground-truth suite confirms the pair
    (`unit_agreement` in `tests/fixtures/ground_truth/expectations.yaml`).
    `test_every_dropped_mirror_is_confirmed_by_the_ground_truth_suite`
    enforces this. The four coolant channels become `obd:COOLANT_TEMP`, which
    is also context rather than a target. The same applies to rpm and wheel
    speed.
  - **Unconfirmed duplicates are listed, not dropped.** Same-quantity sources
    without a confirmation (`did:ECM_RPM` and `obd:RPM`) go in the record's
    `same_quantity_unconfirmed`.
  - **Rolling statistics.** Targets keep no `__mean`/`__std`/`__roc`. Inputs
    keep `__roc` only. `BAROMETRIC_PRESSURE__std` is gone.
  - **Derived columns.** Re-expressions of a kept target (fuel rate, economy,
    power, torque) are dropped with the reason. Every dropped column carries
    its reason in `sequence_feature_set.dropped`.

  *Remaining:* the ablation on A02's protocol (commands under A03.2) before
  any `__std`/`__roc` target comes back or the set is deployed.
- [x] **A03.4 — Fit the scaler on training sessions only.**
  `train_sequence.py` fits the `ZScoreScaler` over all sessions before the
  session-level validation split, so validation statistics leak into the
  scaling. Fit after the split, and test that validation rows do not move
  the scaler.

  *Done 2026-09-23.* `train_sequence.train` fixes the validation drives first
  (`choose_validation_sessions`, A03.5). It then fits the `ZScoreScaler` on
  the rows outside them (`_rows_outside`) and hands both to the library. The
  library refuses a scaler fit on a validation session and stamps
  `validation_holdout.scaler_fit_excludes_validation`. The library's own
  fallback scaler also fits on the training windows only.
  - Test: `test_validation_rows_do_not_move_the_scaler`
    (`tests/test_train_lstm.py`). Two corpora differ only by +5,000 rpm on
    one session. Both pick the same validation drive; the scaler moves
    exactly when the shifted session trained, and the manifest scaler equals
    one fit on the training rows only.
- [x] **A03.5 — Choose validation sessions by a stated rule.** The seeded
  shuffle in `sequence_training.py` is immediately re-sorted by window
  count; the sort is stable, so the shuffle only breaks ties. Validation is
  therefore always the smallest sessions. Pick validation sessions so they
  span the operating modes the model will score, grouped by physical drive.
  Record the chosen sessions in the manifest.

  *Done 2026-09-23.* The rule is `mode_coverage_by_drive_v1`
  (`sequence_training.choose_validation_sessions`; the statement is in
  `VALIDATION_RULE_STATEMENT` and in the manifest):
  - Hold out whole physical drives, in a seeded order.
  - First, cover every scored mode (at least 10 independent windows).
  - Then fill to `validation_fraction`.
  - Never go past twice the fraction, empty the training side, or leave a
    scored mode untrainable.
  - With one drive, fall back to a gapped tail block and say so.

  The manifest's new `validation_holdout` records the rule, the sessions,
  the drives, the target and actual windows, and the modes covered and not
  covered. Tests: `test_validation_spans_the_scored_modes_by_whole_drive`
  (not the two smallest sessions; a restart pair stays together; every mode
  stays trainable; deterministic) and
  `test_one_drive_falls_back_to_a_tail_block_and_says_so`.
- [x] **A03.6 — Refuse ambiguous holdouts at the CLI.** `_resolve_training_sessions`
  (`scripts/train_sequence.py`) has two holes:
  - It returns explicit positional sessions without removing
    `--calibration-sessions` or `--evaluate`.
  - When every discovered session is held out, it silently trains on all of
    them.

  Remove held-out sessions from explicit lists too. Refuse when nothing is
  left to train on, naming the cause.

  *Done 2026-09-23.*
  - **Already fixed by A02 (b037a51).** Explicit lists lose held-out drives
    (`holdout_by_drive` in `_resolve_holdout`), and the CLI no longer trains
    on everything.
  - **Left over.** The empty case exited 1 with "no sessions given and none
    found", which misnames the cause. `train_sequence._run` now exits 2 with
    `nothing_left_to_train`: every session is held out, with both lists
    named. `sequence_bakeoff._resolve_training_sessions` raises the same
    message, and so does its auto-carve path.
  - Tests: `test_main_refuses_when_every_explicit_session_is_held_out` (a
    restart sibling empties an explicit two-session list; no artifact is
    written) and the A03.6 addition to
    `test_bakeoff_holdouts_take_their_whole_drive`.
- [~] **A03.7 — Make "top feature" mean something or stop showing it.**
  Attribution today is each channel's MAE divided by that channel's own
  0.999 quantile, over every column including driving conditions and rolling
  statistics. On the altitude trip it named `BAROMETRIC_PRESSURE` on every
  event (D03.20).
  - After A03.2/A03.3, attribute over engine-response targets only, grouped
    by channel family, and label driving-condition columns as context.
  - Until then, label the dashboard field as "largest deviation from the
    training envelope", not as a cause.

  *Short-term half done 2026-09-23.*
  - **API.** Sequence events carry `detail.channel_ranking =
    "largest_deviation_from_training_envelope"` and a
    `channel_ranking_note` saying it is not a cause (`AnomalyEvent.from_lstm_result`).
  - **Dashboard.** The anomaly row reads "largest deviation from training
    envelope · …" instead of "N chan over band", which was also wrong: those
    are the top-by-ratio channels, not the over-band ones. The evidence card
    captions the channels "(not a cause)" (`lib/evidence.channelRankingCaption`).
  - **Scope-aware note.** The engine stamps `ranking_scope` on each result:
    `all_channels`, or `engine_response_targets` when the artifact records
    context columns. The note only says "driving conditions included" when
    they were ranked. The dashboard title uses the API's note; older events
    fall back to the all-channels wording.
  - Tests: `test_event_from_lstm_result` (both scopes),
    `test_context_inputs_are_never_scored_or_ranked` and
    `ModelEvidenceHealth.test.ts` (caption, and title from the note).

  *Remaining:* attribution over engine-response targets only, grouped by
  family, with driving conditions labelled as context. The engine already
  keeps `engine_response` context columns out of the ranking. The rest waits
  on the A03.2/A03.3 ablation and A06's decision.
- [x] **A03.8 — Record training-envelope coverage in the manifest.** For
  each operating mode, record the baro, IAT and speed ranges and the hours
  of data the model trained on. A02.2 uses this to refuse calibration that
  does not cover a mode, and A06.2's novelty-screen role reports "outside
  the envelope on X" against it.

  *Done 2026-09-23; appears in manifests from the next training.*
  - **Measured before scaling.** `train_sequence.build_dataset` measures
    per-session, per-mode envelopes on the unscaled frame
    (`sequence_training.session_envelopes`: generic J1979 baro, IAT and
    speed; rows, hours, min/max and a histogram).
  - **Recorded.** The trainer merges the training (non-validation) sessions
    into the manifest's `training_envelope`: per mode, hours plus each
    quantity's range, p05/p95, histogram, and what was `unmeasured`. The
    calibration sessions go into `calibration_envelope`.
  - **A02.2 refusal wired.** `calibration_coverage_violations` now refuses a
    held-out calibration whose range in a scored mode spans less than
    `MIN_ENVELOPE_COVERAGE` (80 %) of that mode's training rows. It names the
    mode, the quantity, both ranges and the hours. A calibration without an
    envelope record, beside a training envelope, is refused too.
  - Tests:
    - `test_envelope_records_hours_and_ranges_per_mode_and_merges_exactly`
    - `test_a_calibration_that_misses_the_training_envelope_is_a_violation`
      (the 2026-09-20 71–73 kPa shape covers 10 %)
    - `test_the_manifest_records_the_mask_rule_and_the_training_envelope`
    - `test_a_calibration_outside_the_training_envelope_is_refused`
      (end-to-end, 63 kPa calibration against an 80 kPa model)

## Acceptance

A03.4–A03.6 are code fixes with tests and no model decision. A03.1 needs a
parity test plus a retrain before it affects serving. A03.2, A03.3 and A03.7
are judged on A02's protocol against the current artifact, using the same
held-out drives and rows. Report per-drive results, not pooled totals.
