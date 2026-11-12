# R27 — Detection stack: live feature path, training entries, evaluation

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Scope:
`src/models/`, `src/preprocessing/`, `src/features/`, `src/behavior/`,
`src/plausibility/`, `src/alerts/`, and the ML scripts (`retrain`,
`label_sessions`, `evaluate_*`). Structure, duplication and efficiency only.
Counts are review snapshots; locate code by symbol. Nothing here has been
implemented. Shared acceptance rules are in R22.

**State of the area.** R13, R14 and R15 held. `feature_pipeline.py` is a
327-line facade nothing in `src/`, `scripts/` or `tools/` imports; `src/`
imports nothing from `scripts/` or `tools/`; `featurize_wide_frame` exists
once; serve, evaluation and the FP benchmark share one `SequenceScoringRule`.
The batch and live twins (mask and delta, trip integrals, gap flags, IMU
frame) are each pinned equal by a named test. What is left: two things the
live path still does every second that it need not, a refusal that only the
CLI enforces, a script that copies a library pass, and three files R15 did not
reach.

Everything here is `scripts/ml_paths.txt` territory: run `pytest -m
ml_training` by hand, and replay a fixed session before and after any change
on the serve path.

Timings below are laptop microbenchmarks on synthetic frames. Read them as
ratios; the Pi is several times slower and the truck's real column count was
not measured.

- [ ] **R27.1 The IF trainer accepts a blank calibration-override reason.**
  `isolation_forest._plan_mode` tests `if not calibration_override:`, so a
  whitespace reason from any caller that is not the CLI publishes a mode
  calibrated on fewer than three drives and stamps `"  "` into the manifest.
  R06.2's close-out named this and left it. The rule "an override needs a
  non-blank reason" is written five times in three forms
  (`isolation_forest._plan_mode`, `sequence_autoencoder_training` with
  `.strip()`, `alert_budget`, `budget_level`, `scripts/_cli.override_reason`).
  The refusal holds only while every caller goes through argparse.
  - [ ] One `require_override_reason(value) -> str` (strips, raises
    `ValueError`) in `src/models/`, called by the three trainers and gates and
    used as the argparse `type` in `_cli`.
  - [ ] Library-level tests passing `"  "` to each trainer.
  - Must not change: manifest fields, refusal messages
    (`test_accept_flag_refuses_a_blank_reason`).

- [ ] **R27.2 `scripts/label_sessions.py` re-implements the classification
  pass.** Its `main` runs classify → `write_classification` →
  `compute_fingerprint` / `write_fingerprint` → `evaluate_and_alert_trends` →
  `record_session_best_effort`, the same five steps as
  `control.classification.classify_and_write` + `finish_classification`.
  `classify_one_session` is documented "for scripts and tests" and no script
  calls it. The two have already drifted (`sessions_root=`, `--dry-run`). The
  library pass grew from two steps to four; the next step added there will be
  missing from the corpus backfill.
  - [ ] Give the library pass a `sessions_root` override and a `write` flag
    (or extract `post_session_pass`) and call it from the script, which keeps
    its arguments, the `is_stale` check, the tally and the exit code.
  - Pinned by `tests/test_control_classification.py`,
    `tests/test_storage_catalog.py`.

- [ ] **R27.3 `prepare_tick` rebuilds a whole-buffer DataFrame on the event
  loop every second.** `LiveFeatureSource.prepare_tick` does
  `pd.DataFrame(list(self._buffer))` over up to ~181 dicts of
  `{value, __gap, __arrived}` per channel, and `daemon/inference._snapshot`
  calls it before handing work to the executor. Its docstring calls it
  "deliberately cheap". At 181 rows × 452 columns the build measured
  17–19 ms; the same frame from per-dtype blocks, 1.3 ms; pickling it to the
  worker, 0.17 ms, so the build is the cost and not the IPC.
  `docs/sustained-validation.md` already records the feature stage holding
  the loop 40–60 ms per tick. See R23 for the other things on that loop.
  - [ ] Measure on the Pi first, with the truck's real channel count.
  - [ ] Keep the ring buffer columnar (a float block, two bool blocks, a
    column list that grows when a channel first appears) and build the frame
    from the blocks, or ship the blocks and build in the worker.
  - Must not change: the resulting dtypes, which must equal what
    `DataFrame(list_of_dicts)` gives today. Pinned by
    `test_features_last_row_parity`, `test_batched_timing_parity`,
    `test_preprocessing_live`, `test_feature_pipeline_golden`.

- [ ] **R27.4 `ZScoreScaler.transform` loops over columns in pandas, and the
  IF scores one row per tick through it.** `for col in self.columns:
  out[col] = (df[col] - mean) / denom` costs about 230 µs of pandas overhead
  per column: 19–20 ms for a one-row frame with 88 fitted columns, against
  0.7 ms as `(row - means) / denoms` on `scaler.vectors(cols)`.
  `TrainedIsolationForest.score` then copies the frame again.
  - [ ] Build one `(n_rows, n_cols)` float64 block and concatenate it back in
    column order; drop the second copy in `score`.
  - Acceptance is bit-equality on a real scored session, not `allclose`.
    Pinned by the IF score tests and `test_scoring_continuity`.

- [ ] **R27.5 The z-score rule is applied by three code paths and nothing
  pins train equal to serve.** `training_entry.build_sequence_dataset`
  hand-copies the std-floor rule R04.15 made "the one statement"
  (`ZScoreScaler.denominator`) and does the arithmetic in float32;
  `inference.score_window` and `evaluation_detectors` use `scaler.vectors()`
  in float64 and cast afterwards. The numerical difference is irrelevant
  (4.8e-7 in z-space). The gap is that `test_scoring_continuity` compares
  serve to evaluation, not to the training dataset, and
  `build_sequence_dataset` has no direct test.
  - [ ] `ZScoreScaler.apply_block(values, columns)` beside `vectors`, used by
    all three. Keep the compact-frame memory trick in the training path.
  - [ ] One test: training-dataset values equal serve-scaled values for the
    same rows.

- [ ] **R27.6 `LiveFeatureSource` is five state groups with a hand-written
  19-assignment `reset()`.** 847 lines, 43 fields, 27 methods: ingest and
  decode; the tick and ring buffer; session trip integrals (eight fields and
  the 64-line `_accumulate_derived_integrals`); cold-start trackers; bus
  freshness. `reset()` re-assigns each attribute by name, so an accumulator
  added later and forgotten there leaks across a replay boundary, and no test
  enumerates the fields.
  - [ ] First a test that diffs `vars(fresh)` against `vars(after_reset)` for
    the non-config fields. It fails the day a field is forgotten.
  - [ ] Extract `TripIntegrals` (pure, `accrue(now, point)`, a fresh instance
    on reset; its batch twin is pinned by
    `test_live_trip_integrals_match_batch_compute_derived_channels`), then
    `BusFreshness`. M01.2 changes what `TripIntegrals` integrates; do the
    extraction first.
  - Must not change: the public surface (35 test files import the class).

- [ ] **R27.7 `models/evaluation.py` (1,589 lines) is four unrelated
  clusters.** R15 created twelve `evaluation_*.py` modules and left this file
  alone. By content: corpus structure (`Era`, `Drive`, `HoldoutSplit`,
  `holdout_by_drive`, `pick_calibration_holdouts`); alert persistence
  (`PersistenceSpec`, `sustained_alerts`, `alert_episodes`); budget and
  detection metrics (`engine_hours`, `block_maxima`, `bootstrap_ratio_ci`,
  `vus_pr`); distribution shift (`shift_columns`, `mode_spreads`,
  `_shift_verdict`); plus the unrelated
  `refuse_non_default_profile_sessions`, which `training_entry` imports from
  here.
  - [ ] `evaluation_corpus.py`, `evaluation_alerts.py`,
    `evaluation_metrics.py`, `evaluation_shift.py`; `evaluation.py`
    re-exports, with an `is`-identity test like
    `test_loader_names_are_the_owners_objects`. No body edits.
  - Note in the close-out: the score cache's `code_version` hashes every
    `src/**/*.py`, so the next evaluation misses its cache once.

- [ ] **R27.8 `train_sequence_detector` is one 322-line function and the IF
  entry copies its preamble.** It interleaves acquiring the frame, deciding
  the columns (pure given the frame: coverage, validation drives, scaler
  rows, the unobserved and suppressed drops, feature-set resolution), building
  the datasets, and train/stamp/save. In `training_entry.py` the five-argument
  `build_feature_frame(...)` call is written four times, the
  `replace(trained, manifest=replace(…, evaluation_sessions=…))` block twice,
  and the "no trainable feature columns" error three times (the IF copy
  hard-codes `data/bindings.yaml` where the others interpolate the path).
  `load_behavior_models(p) if p.exists() else None` appears ten times across
  `src/`, `tools/` and `scripts/` although
  `feature_transform.configured_behavior_models(config)` is the named form.
  - [ ] `_sequence_columns(...) -> SequenceColumnPlan`, testable without a
    model; shared `_acquire_frame` and `_with_evaluation_sessions`;
    `load_behavior_models_if_present(path)` in `behavior_models.py`.
  - Keep the public names `test_train_sequence_columns`,
    `test_train_anomaly_model`, `test_sequence_autoencoder_training` and
    `test_retrain_script` patch.

- [ ] **R27.9 Which rows train is still decided in `scripts/retrain.py`.**
  R06.3 split `main` into phases; the data policy stayed in the script:
  `_prepare_inputs` (174 lines), `_apply_quarantine_exclusion`,
  `_compute_disposition_summary`, `_hold_out`, the two gate runners, and the
  `calibration_policy` dict stamped into the IF manifest. Any other caller of
  `train_isolation_forest` (`train_anomaly_model.py`, `sequence_bakeoff.py`)
  trains on an unscreened frame and records no policy. Four
  `try / except Exception → _mark_requested_failures → _print_report →
  return 1` blocks are copies. `scripts/evaluate_plausibility.py` `run_job`
  (159 lines) is library logic that belongs beside
  `src/plausibility/benchmark.py`.
  - [ ] `src/models/training_inputs.py`:
    `prepare_training_inputs(...) -> TrainingInputs`, raising a typed error
    the script maps to its report and exit code. After R27.8.
  - Large and on the deploy path. `test_retrain_script` has 52
    `monkeypatch.setattr(retrain, …)` sites to retarget on purpose, and the
    acceptance is a small-corpus retrain that stages the same artifacts and
    ledger row before and after.

- [ ] **R27.10 `synthetic_fault_eval.evaluate_detectors` looks superseded
  (suspected, not confirmed).** Production imports three helpers from the
  module (`_fused_anomalous`, `_if_anomalous`, `fusion_withheld_reason`, two
  of them private). The 153-line `evaluate_detectors`, its helpers and an
  877-line test have no caller outside `tests/`; the report it produced is now
  written by the R15 pipeline.
  - [ ] Confirm nothing live cites its report shape
    (`docs/roadmap/tasks/P02-…` mentions the module; read that first).
  - [ ] If dead: move the three helpers and `_persistence_gate` under public
    names, delete the rest and its test, re-point
    `test_sequence_cadence.py`.

- [ ] **R27.11 Long gate chains; split when next touched.**
  `isolation_forest._plan_mode` (193 lines: feature and row selection, the
  training-drive floor, calibration-row resolution with the drive-count
  refusal, plan assembly). `imu_session.resolve_imu_frame` (230 lines: input
  preparation, the two solvers, the pitch-reference block, sidecar assembly).
  `plausibility/limits.resolve_limits` (214 lines, two boolean flags: collect
  builders from five sources, then resolve each channel). The whole-file split
  of `isolation_forest.py` stays declined (R00, R10).

- [ ] **R27.12 Dead symbols and small items.**
  - No reference outside their own definition and `__all__`:
    `spec_producibility.load_manifest_feature_columns`,
    `evaluation.DriveDetectorResult`, `preprocessing/derived.reduced_channels`,
    `sequence_training.SequenceModelLike`.
  - Test-only compatibility aliases: `behavior_models.NoiseFloor`
    (`= ResolutionFloor`, and not `analysis.noise_floor.NoiseFloor`),
    `sequence_calibration._block_maxima`,
    `feature_transform._SEGMENTER_SIGNAL_COLUMNS`, the `LSTMAutoencoder*` and
    TCN twins. Move the tests to the real names, then delete.
  - `sequence_calibration.training_data_hash` caches by setting
    `dataset._training_data_hash` on another class and reading it back with
    `getattr`. It belongs on `SequenceWindowDataset`.
  - `append_deviation_columns` runs over the whole 181-row live buffer each
    tick, with a per-row `evaluate_2d` loop for the 2-D models: 14 ms per call
    on the real `behavior_models.yaml`, in the worker. It may be limitable to
    the rolling window, but eligibility reads `__roc` and `context:range_*`
    lookbacks, so derive the safe slice from the models. Suspected only.
  - `plausibility/limits.j1979_encoding_ranges`: 75 ms of input-free decoder
    sweeps per `resolve_limits` call. `lru_cache`.
  - `alerts/emitter.emit_behavior` / `emit_trend` re-list 12–13 keyword
    arguments only to forward them to `AnomalyEvent.from_*`.
  - `sequence_dataset._frame_mask_delta` is a segment × column × row Python
    loop (1.7 s on 7.2 M cells against 0.47 s vectorized). Cold; do it only if
    the corpus grows.
  - Local copies of `src.coerce.optional_str` in
    `models/retraining_policy.py` and `companion/discovery.py`.

The cross-cutting copies found here are in
[R24](R24-shared-primitives.md): canonical-JSON hashing (R24.5), naive-to-UTC
coercion (R24.4).

## Looked at and fine

- `isolation_forest.py` (2,244 lines) and `behavior_models.py` (1,684 lines,
  101 importers): whole-file splits declined before; nothing new argues for
  one.
- `retrain.parse_args` (287 lines) and `evaluate_detectors.parse_args` (165):
  flag definitions pinned by `test_script_cli_snapshot`.
- GRU-D is not dead: `GRUDEncoderCell` is the LSTM backbone's encoder. Only
  comment wording in `live.py` is stale.
- Every `import torch` in `src/` is function-local.
- The IF holdout quantile, sequence block maxima and change-detection freeze
  are different statistics, not copies.
- `compute_features_last_row` beside `compute_features` is a deliberate second
  kernel with a declared tolerance (`last_row_mismatches`).
- `LiveFeatureSource.observe` per CAN frame: the repeated
  `parse_can_channel` is sub-microsecond.

## Not reviewed

`evaluation_scorer` / `evaluation_workers` / `evaluation_summaries`,
`promotion_gate`, `fusion`, `adaptive_candidate`, `candidate_store`,
`budget_level`, `alert_budget` and `sequence_calibration` bodies;
`preprocessing/{derived_store,anomaly_injection,drives,segmenter,resampler}`;
`src/datasets/`; `src/trainability.py`;
`behavior/{accepted_baselines,behavior_eligibility,local_range,surface_eval}`;
`plausibility/evaluator.py`. "No finding" there means not read.
