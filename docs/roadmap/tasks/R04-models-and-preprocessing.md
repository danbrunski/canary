# R04 — models, preprocessing, validation, plausibility

Opened 2026-09-24 from the refactor review ([R00](R00-refactor-README.md)).
The costliest duplication in this area is between training, calibration,
evaluation and serving. That is the train/serve parity AGENTS.md cares
about: a rule copied four times is a rule that four places can disagree on.

## Rules

- **Tier cost.** Any change under `src/models/` or `src/preprocessing/` runs
  the `ml_training` tier (`scripts/ml_paths.txt`): minutes on push, because CI
  has no torch. Changes to `src/validation`, `src/plausibility` and
  `src/datasets` do not. Batch the tier-triggering items where you can.
- **Torch stays lazy.** Nothing here may import torch at module level in a
  module that serve or CI imports. Torch tests `importorskip`.
- **Serve-path items** (`models/inference.py`, `preprocessing/live.py`,
  anything `score_window` calls) replay a pinned session before and after.
  Scores must match to float tolerance, and categorical fields must be
  identical.
- **Relation to the A-series.** R04.1 and R04.2 sit under A02 (honest
  evaluation) and A01 (serve-time scoring). They don't reopen A-decisions;
  they make the offline tools score what A01 and A03 made the daemon serve.

---

## Correctness first

- [ ] **R04.1 The false-positive benchmark must score the detector that is
  served.** *Drift bug, offline but published.*
  - `models/false_positive_benchmark._reconstruction_errors` scores with
    `model.reconstruction_error(reduction="sample")`, which averages over
    *all* channels. It calls the model a second time for `"channel"`, so
    there are two forward passes.
  - The served rule (`models/inference.score_window`) scores only the
    columns left after removing suppressed, uncalibrated (A02.12, `c866488`)
    and context (A03.2) columns, and ORs in the family path. The benchmark
    does neither. It was last touched 07-19, before either change.
  - It compares that all-channel mean against thresholds calibrated over
    calibrated channels only.
  - Its output is printed as the FPR against the v1 budget by
    `scripts/train_sequence.py --evaluate` and `scripts/fp_benchmark.py`, and
    it feeds the training ledger.
  - Its `_error_distribution` is `sequence_training.error_distribution` with
    the quantile branch removed. `_window_modes` re-shapes
    `sequence_training.window_modes`.

  **Do:** take the global and family verdicts from the R04.2 kernel, or at
  minimum apply the engine's `_score_excluded` mask and the family path.
  Replace both copies with the canonical functions. **Accept:** a failing
  test first: a model with one suppressed, noisy channel shows the same
  flagged-window count in the benchmark as in `score_window` over the same
  windows. `tests/test_false_positive_benchmark.py` passes. Size **S–M**.

- [ ] **R04.2 One kernel for the served sequence-scoring rule.** *Parity,
  serve path.*
  - The rule: masked global MAE over scored channels → mode band with global
    fallback → family max normalized by the family band. It is written four
    times:
    - `inference.score_window` (serve, torch).
    - `evaluation_detectors.BatchedSequenceScorer.score`.
    - `sequence_training.calibrate_sequence_thresholds`, the calibration
      side.
    - The drifted copy in R04.1.
  - The mode-band lookup `per_mode_errors or {}` appears in three files. The
    family grouping is built three times from three suffix-stripping
    functions (R04.3).
  - `sequence_parity` catches drift between serve and the batched scorer at
    run time. Nothing checks calibration against serve for families.

  **Do:** in a torch-free `models/sequence_scoring.py`, add:
  - `SequenceThresholdCalibration.threshold_for_mode(mode)`;
  - `family_index(columns) -> dict[str, list[int]]`;
  - `score_batch(error, score_mask, channel_thresholds, calibration, modes)
    -> (global, channel, family_max, verdict)` in numpy.

  Serve calls it with B=1 after its forward pass. The batched scorer and the
  benchmark call it per batch. Calibration builds its distributions over the
  same `family_index`.

  **Accept:**
  - Replay parity.
  - Per-tick cost measured before and after on the laptop: numpy on one row
    must stay cheap. Record the numbers.
  - `test_lstm_inference.py`, `test_evaluation_detectors.py` and
    `test_sequence_calibration.py` pass.

  Size **M**.

- [ ] **R04.3 Define the rolling-suffix / base-channel parser once.** *Latent
  parity bug, serve path.*
  - `("__mean", "__std", "__roc")` is re-declared or inlined 9 times, though
    the producer, `preprocessing/features.py`, already exports
    `_FEATURE_SUFFIXES`. The copies:
    - `_base_channel` in `evaluation_detectors`, `isolation_forest` and
      `inference`
    - `sequence_training._feature_family`
    - `feature_pipeline._base_feature_name` and `_ROLLING_SUFFIXES`
    - `sequence_feature_set._ROLLING_SUFFIXES` / `_split_rolling`
    - `spec_producibility.base_feature_name`
    - `decode/behavior_decoder`
  - They are identical today.
  - Serve-path consumers: the `__gap` lookup, the family key (which must
    match calibration's), `expand_suppress_channels` (A01.5 suppression) and
    `arrival_column_for`.
  - Adding one rolling feature would currently lose its freshness mask at
    serve, leak around suppression, and misfile its family, all silently.

  **Do:** make `features.base_channel()` / `split_rolling()` public beside
  the suffix tuple, and point every copy at them. **Accept:** `grep -rn
  '"__roc"' src` returns only `features.py`. Size **S**.

- [ ] **R04.4 One decode path for CAN records: live, storage, backfill,
  offline.** *Drift, serve path.*
  - `preprocessing/live._decode_signal_raw` is a line-for-line copy of
    `decode/bindings.decode_raw`.
  - `LiveFeatureSource.decode_for_telemetry` feeds the dashboard stream and
    inference. It re-implements `decode/frame_decoder.FrameDecoder.decode`
    (the storage path), and has drifted from it:
    - metadata key `"confidence"` vs `"binding_confidence"`;
    - unit `"unknown"` → `""` vs raw;
    - value int vs float.
  - `preprocessing/decode._extract` and `tools/backfill_decoded._extract` /
    `decode_session_frame` are AST-identical re-implementations of the
    extraction. Backfill's rows are the "stored rows win" input to
    `decode_can_rows`, and backfill drops NaN rows where src keeps them.

  **Do:**
  - The live source holds a `FrameDecoder` (all confidences) and uses it for
    both jobs.
  - `decode.py` exposes a vectorized `decode_signal_column(payloads, signal)`
    built on `decode_raw`, and backfill calls it.
  - Add `Signal.display_unit` to replace the 5 `!= "unknown"` copies.
  - R05.9 unifies the byte-slice layer underneath.

  **Accept:**
  - A test that live and storage decode the same frame to the same record.
  - `test_preprocessing_live.py`, `test_frame_decoder.py`,
    `test_preprocessing_decode.py` and `test_decoded_mode_signals.py` pass.
  - Nothing in the frontend reads either confidence key (the reviewer
    checked); confirm again at implementation time.

  Size **S–M**.

- [ ] **R04.5 Promotion gate: a withheld detector must fail, not pass.**
  *Drift bug, offline gate.*
  - `promotion_gate.evaluate_promotion_gate` skips any detector whose recall
    is missing: `if golden_recall is None or candidate_recall is None:
    continue` (verified 2026-09-24).
  - `detector_benchmark.py` emits no row for a withheld detector. So a
    candidate whose sequence or fused detector was withheld passes "recall
    non-regression" against a golden where it was graded.
  - A missing `per_hour` defaults to 0.0 and passes the FP budget.

  **Do:** split the 164-line function into `_check_*` functions, each
  returning `(ok, reason, detail)`. A candidate cell that is missing where the
  golden has one fails and names the reason. **Accept:** failing tests first
  for both silent passes. `test_promotion_gate.py` passes. Size **S**.

- [ ] **R04.6 Evaluate the detectors on default-profile data only.**
  *Operator decision 2026-09-24: tuning logs don't represent the default
  profile, so they are not in the anomaly detectors. See R03.12 for the live
  half.*

  This item was first written the other way round: "make fault windows find
  engine time on Mode-01-less captures". That direction is withdrawn.

  **Today (checked 2026-09-24):** `scripts/evaluate_detectors.discover_session_dirs`
  is "Every recorded session under `roots`, captures included". It skips
  only `_archive` and `_replay`, so `_capture-tuning-*` sessions become
  evaluation drives, and the A02 per-drive table scores IF and sequence on
  tuning data. `evaluation.engine_running` coalesces RPM candidates for
  exactly that case ("a capture whose profile disables Mode 01 still has
  engine time").

  **Do:**
  - Keep captures in *drive grouping*. `drives_of_sessions` bridges a
    capture with its sibling recordings, and that keeps a drive's default
    parts together, so held-out stays honest.
  - Exclude non-corpus-eligible sessions (`read_capture_manifest(...).corpus_eligible`,
    and `_tuning-*`) from the rows that are scored, from fault injection
    (`choose_fault_window`) and from every metric.
  - A drive whose only rows are a tuning capture is reported as `skipped: no
    default-profile rows`, not silently dropped.
  - `synthetic_fault_eval._running_mask`'s `obd:RPM`-only view is then
    sufficient, because default-profile rows carry Mode 01. Leave it, or
    route it through `engine_running` only for consistency.

  **Accept:**
  - A failing test first: a fixture drive made of a default session and a
    `_capture-tuning-*` sibling is scored on the default rows only, and the
    capture's rows appear in no metric.
  - The A02 baseline table (A02.11) is regenerated, and the change is
    recorded next to it.
  - Record the decision in A00's decisions list too.

  Size **S–M**.

- [ ] **R04.7 Write the fast/slow max-gap rule once.**
  - The resampler honours `ResampleConfig.fast_channels`/`slow_channels`.
  - `live._gap_flags` (serve) and
    `synthetic_fault_eval._max_gap_seconds_for` re-derive the rule from the
    default hints only.
  - Nothing sets those fields today, so the drift is latent.

  **Do:** add `ResampleConfig.max_gap_seconds_for(channel)`, used by all
  three. **Accept:** a test with a non-default `fast_channels` gets identical
  gap flags from resampler and live. Size **S**.

## Structure

- [ ] **R04.8 Move `HealthMonitor` and the system probes out of
  `src/models/`.**
  - `models/health.py` (930 lines) is daemon/system health: cgroup memory,
    CPU temperature, power, can0 counters, throttling, telemetry queues,
    collector handoff, OBD scheduler and capture quality. It imports
    `src.collection.*` and `src.startup`.
  - Only `feature_drift`, `reconstruction_error_drift` and the IF blind-mode
    tracking are model concerns.
  - 6 of the 8 commits to it since 08-01 were collection or daemon work, and
    each paid the `ml_training` tier for it.

  **Do:** move it to `src/daemon/health.py` and keep the drift functions in
  models. Update the 15 importing files directly; `test_health.py` and
  `test_daemon.py` import `_MODE_BLIND_WINDOW`. **Accept:** the tier stops
  triggering on health changes. The move itself triggers it once. Size **M**.

- [ ] **R04.9 Break up `fit_isolation_forest` (552 lines).** Its phases:
  1. validate and resolve the spec;
  2. the per-mode planning loop (eligibility, joint completeness, split,
     drive floors, skip records) → `_plan_modes(...) -> (dict[str,
     _ModeFitPlan], list[SkippedModeSummary])`;
  3. scaler fit on the union of fit rows → `_fit_scaler(plans, ...)`;
  4. per-mode forest fit, holdout calibration, seed agreement and the
     25-kwarg `_mode_summary` → `_fit_mode(plan, scaler, ...) -> (model,
     ModeTrainingSummary)`;
  5. the dropped-fraction gate and manifest → `_build_manifest(...)`.

  Optionally, move the calibration cluster (`exceedance_episodes`,
  `_calibration_split`, `_Calibration`, `_calibrate_holdout_threshold`,
  `_seed_agreement`, `seed_stability_sweep`, `*_flag_rate`; ~350 lines) to
  `isolation_forest_calibration.py` with re-exports. Only 2 test files import
  those names.

  A whole-file split is **still not justified**: 28 importing files and 25
  names (July's call stands).

  **Accept:** a manifest byte-diff on a fixed-seed fit before and after. The
  10 test files that call `fit_isolation_forest` pass. Size **M**.

- [ ] **R04.10 Break up `train_sequence_autoencoder` (367 lines).** Its
  phases:
  1. preflight refusals: in-sample, mask mismatch, unknown context;
  2. validation split and envelopes → `_TrainingPlan`;
  3. model, loader, AMP and compile setup;
  4. the epoch loop with early stop → `_run_epochs(...) -> _FitOutcome`;
  5. manifest;
  6. calibrate and stamp.

  Torch stays lazy: the helpers receive the modules they need. **Accept:** a
  fixed-seed CPU training run gives an identical manifest before and after,
  and the 9 test files pass. Size **M**.

- [ ] **R04.11 Split `sequence_training.py` (1,423 lines) by concern, and give
  serve a torch-free home for thresholds.**
  - It mixes:
    - manifest dataclasses;
    - loop primitives (`train_one_epoch`, `evaluate_loss`,
      `cpu_state_dict`);
    - the drive-grouped validation split (`choose_validation_sessions`, 118
      lines);
    - envelopes;
    - calibration;
    - torch/device helpers.
  - Serve (`inference.py`) imports `SequenceThresholdCalibration` from this
    *training* module.

  **Do:** create `sequence_calibration.py` (distributions,
  `SequenceThresholdCalibration`, `calibrate_*`, blocks and modes; R04.2's
  kernel lives beside it), `sequence_validation_split.py` and
  `sequence_envelope.py`, re-exported from `sequence_training` (17
  importers). `evaluation.block_maxima` and `sequence_training._block_maxima`
  share their "max finite per block" tail, so merge them here. **Accept:**
  serve imports nothing from `sequence_training`. Size **M**.

- [ ] **R04.12 One mixin for the LSTM/TCN autoencoder modules.**
  - `reconstruction_error`, `_masked_mean`, `_validate_inputs` and `forward`
    are AST-identical in `lstm_autoencoder.py` and `tcn.py`.
  - `_module_base` and `_torch_modules` are structurally identical.
  - `lstm_training`/`tcn_training` `save_*`/`calibrate_*` are identical, but
    those are the deliberate shims (retrain.py and the job allow-list import
    them) and stay.

  **Do:** create a torch-free `MaskedReconstructionMixin`
  (`class LSTMAutoencoder(MaskedReconstructionMixin, module_base())`), and add
  `reconstruction_error` to the `SequenceAutoencoder` protocol, since the
  benchmark reaches it through `Any` today. **Accept:** the `state_dict` keys
  are unchanged, and a saved model from before loads after. Size **S**.

- [ ] **R04.13 Validation harness: reuse the daemon's wiring and break up
  `run_scenario`.**
  - `validation/harness.run_scenario` is 324 lines: setup, task start,
    collector callbacks, arm, stop/finalize.
  - Its `on_receive` copies `daemon/runtime._can_receive_stats_observer`
    field for field.
  - The drain kwargs are hand-copied and have drifted: there is no
    `gps_tracker`, no `storage_path`/`storage_warning_mb`, and no
    `queue_warning_fraction`. The harness therefore validates slightly
    different wiring from what the daemon runs.

  **Do:** runtime exposes its observer and drain-kwargs factories (R03.8),
  and the harness composes them. Split the function into `_start_pipeline`,
  `_start_collector`, `_arm` and `_finish`. `_SCRATCH_DAEMON_PATHS` is
  replaced by R07.2's `rebase_state_paths`. **Accept:**
  `test_sustained_validation.py` and `test_replay_row_accounting.py` pass.
  This item does not trigger the ml tier. Size **M**.

- [ ] **R04.14 `build_feature_frame`: break it up and make the segmenter
  inputs public.**
  - `build_feature_frame` (167 lines) splits into `_load_cached`,
    `_load_decode_inputs` and `_resample_sessions` (pool + `BrokenExecutor`
    fallback). R01.2 adds its `dids_path`.
  - `_SEGMENTER_SIGNAL_COLUMNS` is private, but `plausibility/offline.py`,
    `analysis/behavior_validation.py` and a test import it, and it is
    flattened twice. Make it public as `segmenter_input_columns()`.
  - Two bounded CAN readers have drifted:
    - `plausibility/offline.session_long` falls back to `parse_can_channel`
      because channel spelling varies across eras.
    - `behavior_validation.build_evaluation_frame` pushes down
      `f"0x{id:X}"` only, so it misses those eras.

  **Do:** create one `preprocessing.decode.read_session_decoded_subset(...)`
  and use it for both readers. **Accept:** a test on an old-spelling fixture
  gets the same columns from both callers. There are 53 importers, so
  re-export everything. Size **S–M**.

- [ ] **R04.15 One z-score denominator rule.**
  - The rule `std if std >= std_floor else 1.0` is re-derived in
    `isolation_forest._scaled_array` / `_scaled_medians`, `inference.py` and
    `evaluation_detectors.py`.
  - IF training scales in float32, while serve goes through
    `ZScoreScaler.transform`.

  **Do:** add `ZScoreScaler.vectors(columns) -> (means, denoms)` and use it
  everywhere. **Accept:** replay parity on IF scores. Size **S**.

## Looked at and fine

- `feature_pipeline.featurize_wide_frame` is the single wide → features
  transform for batch, live and eval. `daemon/inference.py` re-derives no
  features.
- The observation-mask/Δt logic is written three times
  (`sequence_dataset`, `SequenceWindowBuffer`,
  `BatchedSequenceScorer._windows`), but `test_sequence_observation_mask.py`
  and `sequence_parity` pin it. Consolidate only alongside R04.2.
- `evaluation_detectors` `window` vs `inject`: Protocol stubs, not
  duplication.
- The `models/__init__` and `preprocessing/__init__` `__getattr__` shims:
  7 lines each, not worth a helper.
- The `_torch_module` copies: distinct exception types, decided in July.
- The two fault harnesses (`synthetic_fault_eval` + `detector_benchmark` vs
  A02's `evaluate_detectors`): the split is deliberate per A02. Retiring
  `detector_benchmark` is R06.5's operator question.
- Drive grouping has one source, `analysis.drives`.
- `validation/evidence.limiting_stage` (132 lines): an ordered, commented
  decision list.
- `plausibility/limits.resolve_limits` (185 lines): it splits cleanly, but
  it has had 1 commit since August. Split it only if you are in there.
- `TrainingEnvelope.check` (serve) vs `envelope_coverage*` (training): they
  answer different questions.
