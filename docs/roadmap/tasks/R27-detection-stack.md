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

- [x] **R27.1 The IF trainer accepts a blank calibration-override reason.**
  `isolation_forest._plan_mode` tests `if not calibration_override:`, so a
  whitespace reason from any caller that is not the CLI publishes a mode
  calibrated on fewer than three drives and stamps `"  "` into the manifest.
  R06.2's close-out named this and left it. The rule "an override needs a
  non-blank reason" is written five times in three forms
  (`isolation_forest._plan_mode`, `sequence_autoencoder_training` with
  `.strip()`, `alert_budget`, `budget_level`, `scripts/_cli.override_reason`).
  The refusal holds only while every caller goes through argparse.
  - [x] One `require_override_reason(value) -> str` (strips, raises
    `ValueError`) in `src/models/`, called by the three trainers and gates and
    used as the argparse `type` in `_cli`.
    New `src/models/override_reason.py` (imports nothing): `require_override_reason(value,
    *, refusal=...)` keeps each gate's own wording; `stated_override_reason` (None
    for absent or blank) serves the two trainers whose refusal is their own. Called
    by `isolation_forest.fit_isolation_forest` + `_plan_mode`,
    `sequence_autoencoder_training`, `alert_budget.check_alert_budget`,
    `budget_level.unbudgeted_record`, `_cli.override_reason` (re-raises as
    `ArgumentTypeError`, same message) and a sixth copy found by grep,
    `control/jobs.JobController.submit` and `_build_command` (via
    `stated_override_reason`). Regrowth guard:
    `tests/test_override_reason.py::test_no_override_reason_check_is_written_outside_the_shared_rule`
    matches `.strip()` on `*override*` / `accept_*` / `reason`, `if not` on
    those names, and the `isinstance(reason, str) or not` shape;
    `::test_the_guard_matches_each_retired_form` pins all six pre-R27.1 copies
    (the HEAD `_cli` and `jobs` forms included). Three non-override hits are
    allowlisted by file and function (`rom_editor.TypicalLeftEmpty._reason_is_text`,
    `rom_levers._plant_text`, `evidence/records.WhatIfPoint._both_or_reason`);
    the allowlist is shrink-only (a stale entry fails the test).
  - [x] Library-level tests passing `"  "` to each trainer.
    `test_isolation_forest_a04.py::test_a_blank_calibration_override_is_refused_like_a_missing_one`
    (`""`/`"  "`/`"\t\n"` → `SKIP_TOO_FEW_CALIBRATION_DRIVES`, failed before:
    `"  "` published), `::test_a_calibration_override_reason_is_stamped_stripped`;
    `test_sequence_calibration.py::test_a_blank_override_is_refused_like_a_missing_one`;
    `alert_budget` / `budget_level` already had `"  "` tests, now also assert the
    stored reason is stripped.
  - Must not change: manifest fields, refusal messages
    (`test_accept_flag_refuses_a_blank_reason`). Held: no field added or
    renamed, every refusal string unchanged; a valid reason is now stored
    stripped on every path (the CLI already stripped). Runs 2026-11-12:
    `pytest -n 4 -m "not integration" tests/test_override_reason.py
    tests/test_isolation_forest_a04.py tests/test_sequence_calibration.py
    tests/test_alert_budget.py tests/test_budget_level.py
    tests/test_script_cli_snapshot.py tests/test_control_api.py
    tests/test_layering.py tests/test_retrain_script.py` — 257 passed (after
    the guard widening); `pytest -n 4 -m "not integration"
    tests/test_lstm_training.py tests/test_tcn_training.py
    tests/test_train_lstm.py tests/test_sequence_cadence.py
    tests/test_isolation_forest.py` — 121 passed;
    `pytest -n 4 -m "not integration" tests/test_train_anomaly_model.py
    tests/test_isolation_forest_evaluation.py` — 23 passed;
    `pytest -n 4 -m ml_training tests/test_budget_level.py
    tests/test_sequence_autoencoder_training.py tests/test_lstm_training.py
    tests/test_tcn_training.py tests/test_train_lstm.py tests/test_train_tcn.py`
    — 58 passed. The new tests carry no `ml_training` marker.

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

- [~] **R27.3 `prepare_tick` rebuilds a whole-buffer DataFrame on the event
  loop every second.** `LiveFeatureSource.prepare_tick` does
  `pd.DataFrame(list(self._buffer))` over up to ~181 dicts of
  `{value, __gap, __arrived}` per channel, and `daemon/inference._snapshot`
  calls it before handing work to the executor. Its docstring calls it
  "deliberately cheap". At 181 rows × 452 columns the build measured
  17–19 ms; the same frame from per-dtype blocks, 1.3 ms; pickling it to the
  worker, 0.17 ms, so the build is the cost and not the IPC.
  `docs/sustained-validation.md` already records the feature stage holding
  the loop 40–60 ms per tick. See R23 for the other things on that loop.
  - [~] Measure on the Pi first, with the truck's real channel count.
    *Laptop only, 2026-11-12; the Pi measurement is owed* (no Pi access from
    this run). Real column sets, from stored sessions driven through
    `LiveFeatureSource` (`observe` + 1 Hz `prepare_tick`, 400 ticks, buffer
    full at 181 rows): `2026-11-11T173212` 284 columns, `2026-10-27T173734`
    308, `_tuning-2026-08-05T142720` 197. None reached the review's 452; a
    copy of the 308-column buffer with 48 channels cloned is the 452 case.
    `prepare_tick` itself at HEAD vs this change, full-buffer ticks of
    `2026-10-27T173734` (181 × 308), two runs each: **7.27–8.10 ms → 0.58–0.71
    ms** median. The frame build alone (quiet laptop, 120 interleaved reps,
    medians): `pd.DataFrame(list_of_dicts)` + `to_datetime` 4.84 / 6.82 /
    10.46 ms at 197 / 308 / 452 columns; now `append` + `rows()` on the loop
    0.17 / 0.20 / 0.25 ms, pickling `WideRows` 0.31 / 0.33 / 0.37 ms (141 /
    218 / 318 KB), and unpickle + `frame()` + `to_datetime` in the worker
    1.87 / 1.96 / 2.09 ms. To do on the Pi: the same `prepare_tick` median
    on a full buffer with the truck's live channel set.
  - [x] Keep the ring buffer columnar (a float block, two bool blocks, a
    column list that grows when a channel first appears) and build the frame
    from the blocks, or ship the blocks and build in the worker.
    *Done 2026-11-12:* `src/features/live.py` `WideRowRing` (replaces the
    `deque` of dicts as `_buffer`): a float block, **one** bool block (the
    `__gap` and `__arrived` flags together; two would only add a part to
    concatenate), per-row dicts for the values that are neither (`timestamp`,
    `session`, framed by pandas itself so their dtypes are its inference),
    and a slot per key, added when the key is first seen. **Shipped, built in
    the worker:** `PreparedLiveTick.rows` is a `WideRows` (copied blocks + a
    cached column plan); `PreparedLiveTick.wide` is a property that builds the
    frame where it is read, which in the daemon is the feature process.
    Building on the loop would still cost ~2 ms a tick there (an object
    sub-frame, a concat and a column `take`); shipping costs ~0.2 ms and the
    pickle is the same size. `daemon/inference._snapshot` needed no change.
    `maxlen` eviction is the ring's capacity (`_max_rows`); `reset()` installs
    a fresh `WideRowRing`, and `WideRowRing.__eq__` (capacity, keys seen,
    frame) lets `test_live_feature_source_reset` compare it by value. A value
    that changes kind (float column given a bool or an int) raises
    `TypeError`: the dict path would have changed that column's dtype.
  - Must not change: the resulting dtypes, which must equal what
    `DataFrame(list_of_dicts)` gives today. Pinned by
    `test_features_last_row_parity`, `test_batched_timing_parity`,
    `test_preprocessing_live`, `test_feature_pipeline_golden`.
    *Met:* `tests/test_wide_row_ring.py` (18 tests) builds both ways from the
    same rows with `assert_frame_equal(check_exact=True, check_index_type=
    True, check_column_type=True)` plus `dtypes.equals`. Cases: a 241-tick
    drive through the source (cold-start contract, CAN decode) where
    `obd:MAF` first appears at tick 40 and GPS at 70, coolant goes stale at
    120, and the buffer evicts past 181. It asserts the flag is `object` with
    NaN while older rows lack it, sorted after their keys, and `bool` in its
    family's place once those rows are evicted; it also checks after a pickle
    round trip of the `PreparedLiveTick`. Start-context columns that vanish
    on a reload and return; six random key-appear/vanish sequences on rings
    of 1–8; int and string keys; empty ring; kind-change refusal; a snapshot
    detached from later appends. Proven able to fail: a scratch plugin
    casting one bool column to float64 failed 8 of 18 (the drive test on
    `obd:RPM__gap`), and one casting the partial-flag `object` column to
    float64 failed 8 of 18 (`obd:MAF__gap`). On real data the bench asserts
    `check_exact` + dtypes equality on all three captured buffers. A
    live-path A/B over `2026-10-27T173734` (400 full-transform ticks, old
    dict build vs ring, everything else equal) gave identical feature rows
    (400 × 521 columns), operating points and trip integrals. `pytest -m ""`
    over the 32 test files importing `src.features.live` plus
    `test_feature_pipeline_golden`, `test_behavior_serving_contracts` and the
    new file: 730 passed. `pytest -m ml_training` (the whole tier,
    `src/features/` is in `scripts/ml_paths.txt`): 125 passed, 1 skipped.

- [x] **R27.4 `ZScoreScaler.transform` loops over columns in pandas, and the
  IF scores one row per tick through it.** `for col in self.columns:
  out[col] = (df[col] - mean) / denom` costs about 230 µs of pandas overhead
  per column: 19–20 ms for a one-row frame with 88 fitted columns, against
  0.7 ms as `(row - means) / denoms` on `scaler.vectors(cols)`.
  `TrainedIsolationForest.score` then copies the frame again.
  - [x] Build one `(n_rows, n_cols)` float64 block and concatenate it back in
    column order; drop the second copy in `score`.
    *Done 2026-11-12:* one block **per result dtype**, not one float64 block.
    The served features are float32 (manifest `feature_dtypes`: 295 ×
    float32), and the loop computed float32 in float32: pandas turns a numpy
    scalar mean into a Python float, so it promotes weakly. A float64 block
    is not bit-identical; the perturbation test proves it. The daemon's
    one-row frame (`row.to_frame().T`) is all object dtype holding
    `np.float32`, so object columns get one object block with the same
    element arithmetic. Extension dtypes and an object column holding `None`
    keep the Series expression. `(x - mean) / denom` stays the exact
    expression (no reciprocal). `ZScoreScaler.transform_fitted` returns only
    the fitted columns, and `score` uses it, so `out = frame.copy()` is the
    only full copy. A/B against HEAD on a loaded laptop (load avg 16, medians):
    one row × 88 cols 15.3–18.0 ms → 4.6–5.6 ms; one row × 295 cols (the real
    artifact) 48–50 ms → 6.9–9.0 ms; `engine.observe` one row 309 ms → 35 ms;
    a 12,959 × 894 frame `transform` 445 → 68 ms and `score` 1764 → 1112 ms.
    Quiet in-process: one row × 295 at 1.1 ms (`transform_fitted`) to 2.3 ms
    (`transform`, typed).
  - Acceptance is bit-equality on a real scored session, not `allclose`.
    Pinned by the IF score tests and `test_scoring_continuity`.
    *Met:* the deployed artifact `isolation_forest_2026-09-20T054703.694923Z`
    (`models/nissan-xterra-2008`, same manifest as `backups/pi-20261112`) was
    run over both cached real feature frames: 12,959 rows / 10 sessions
    (2026-09-21..23) and 11,084 rows / 15 sessions. Old and new code were
    compared on bytes (`tobytes`) and `np.array_equal`, per column and dtype,
    for `transform` and `score` on each whole frame and on three single
    sessions from each. For session `2026-09-22T233855` (474 rows) the serve
    path was compared row by row: `reconcile_feature_frame_to_spec` →
    `engine.observe`, `feature_contributions` (326 non-empty), and the
    object-frame `transform`. Every output was IDENTICAL. Committed pin:
    `tests/test_zscore_block.py` (10 tests), against the old loop kept as
    `_reference_transform`. It covers float32/float64/int64/int8/bool columns,
    NaN, ±inf, an all-NaN column, zero std and below-floor std, `std_floor=0`
    (÷0), interleaved passthrough, a permuted and a duplicate row index, an
    empty frame, one-row typed and object frames, `np.float64` stats,
    `Float64` and `None` columns, attrs/flags, the missing-column refusal,
    and `transform_fitted` == the fitted slice. Perturbations fail it (counts
    out of the 9 tests that existed then): reciprocal multiply (7/9 fail), all-float64 block (7/9), dropping the
    numpy-scalar→Python conversion (1/9), and an object block via float64
    (both object tests). `pytest -m ""` over 35 files that import the scaler
    or IF module, `test_scoring_continuity` included (71 `ml_training` tests
    among them): 771 passed. Independently reproduced by the verifier on two
    other sessions (`2026-09-21T231724`, `2026-09-21T142126`): 28/28 outputs
    identical. One refusal changed: `score` (via `transform_fitted`) now
    raises `SchemaMismatchError` (a `ValueError`) on duplicate feature or mode
    labels, where it raised a numpy broadcast `ValueError` or a pandas
    grouper `ValueError`; `reconcile_feature_frame_to_spec` already refuses
    duplicates, so no real caller reaches it.

- [x] **R27.5 The z-score rule is applied by three code paths and nothing
  pins train equal to serve.** `training_entry.build_sequence_dataset`
  hand-copies the std-floor rule R04.15 made "the one statement"
  (`ZScoreScaler.denominator`) and does the arithmetic in float32;
  `inference.score_window` and `evaluation_detectors` use `scaler.vectors()`
  in float64 and cast afterwards. The numerical difference is irrelevant
  (4.8e-7 in z-space). The gap is that `test_scoring_continuity` compares
  serve to evaluation, not to the training dataset, and
  `build_sequence_dataset` has no direct test. A fourth path since R27.4: the
  IF's `transform` / `transform_fitted` scales float32 features *in float32*,
  per dtype block, deliberately, to stay bit-identical to the old per-column
  loop. `apply_block` must not silently move the IF onto float64 (that changes
  served scores); either keep the IF on its own block path or make the switch
  an explicit, measured change.
  - [x] `ZScoreScaler.apply_block(values, columns)` beside `vectors`, used by
    all three. Keep the compact-frame memory trick in the training path.
    *Done 2026-11-13:* `apply_block` returns float32
    `(float64(x) - mean) / denom`, rounded once: serve's expression, kept so
    served scores do not move. It takes any shape whose last axis is the
    columns, computes in float64 chunks of at most 2^22 elements (32 MB), and
    writes in place when `out=values`. Its `(means, denoms)` are cached per
    column tuple, and the cache stays out of pickles and equality, like
    R27.13's plans. `build_sequence_dataset` still copies only the structural,
    feature and flag columns (the compact frame) and scales the float32 matrix
    in place (`out=values`), so the only extra memory is the 32 MB chunk.
    `score_window` and `BatchedSequenceScorer.score` call it on the window or
    batch. The IF stays on `transform` / `transform_fitted` (float32 in
    float32, `_block_plan`), and `isolation_forest` still uses `vectors` for
    its fit. **The training dataset changes; served scores do not.** Run on the
    two cached real feature frames (24,043 rows, 1,563,658 finite cells, the
    deployed LSTM's 254 columns and scaler), the old float32 loop and
    `apply_block` differ on 790,628 cells (50.6 %). The largest |Δz| is
    5.96e-6, not the 4.8e-7 quoted above. Under NumPy 2's weak-scalar
    promotion (NEP 50) the old float32 loop rounded the mean itself to
    float32 before subtracting, and that rounding dominates. The
    worst case relative to |z| is 7.9 %, on a near-zero z. No finiteness
    changed. A retrain on the same data is therefore not bit-identical to one
    before R27.5. Score impact: the deployed `lstm_autoencoder_2026-09-24T035125.284392Z`
    scored the stored drives `2026-09-21T201836` (segments 001 and 002) and
    `2026-09-22T163415` (5,622 rows from the cached frame, 5,535 windows),
    once at HEAD in a worktree and once in this tree. Both the batched scorer
    and the live engine, row by row through `observe`, gave bit-identical
    `global_score` (max |Δscore| 0.0) and 0 verdict flips (133 flagged either
    way). Per-tick scaling of a 30 × 254 window: 16 µs → 26 µs (the column
    tuple and cache lookup), against a forward of about 13 ms.
  - [x] One test: training-dataset values equal serve-scaled values for the
    same rows. *Done:*
    `tests/test_evaluation_detectors.py::test_training_windows_equal_the_windows_serve_and_evaluation_score`.
    It builds the dataset with `build_sequence_dataset` (scaler given), trains
    a tiny LSTM on it, and records what `model.forward` receives from
    `SequenceInferenceEngine.observe` and from `BatchedSequenceScorer.score`.
    For every window (more than 100, across a 60 s gap) it asserts the
    dataset's float32 values equal both **bitwise** (`tobytes`). The equality
    is exact because all three paths now take the same float32 raw values
    through `apply_block`. With the old float32 training loop put back, the
    test fails on the first window (one float32 ulp, `\x97` against `\x98`).
    `tests/test_zscore_block.py::test_apply_block_is_float64_rounded_once_whatever_the_shape_or_chunking`
    pins the expression for 1-D, 2-D, 3-D and chunked in-place input,
    including NaN, ±inf and the std floor. It asserts the float32-in-float32
    result differs, so the test can fail. It also covers the width and `out`
    refusals and that the cache is never pickled. Gate: `pytest -m
    ml_training -n 4` on `test_lstm_training`, `test_train_lstm`,
    `test_tcn_training`, `test_sequence_autoencoder_training`,
    `test_sequence_bakeoff`, `test_false_positive_benchmark` and
    `test_budget_level`: 80 passed. `pytest -m ""` on `test_scoring_continuity`,
    `test_zscore_block`, `test_preprocessing_normalization`,
    `test_evaluation_detectors`, `test_lstm_inference` and
    `test_sequence_dataset`: 106 passed, 1 skipped (the no-torch case). On
    `test_lstm_inference`, `test_tcn`, `test_train_tcn`, `test_train_sequence_columns` and
    `test_behavior_deviations_reach_training`: 58 passed.
  - Side effect: `training_data_hash` (sequence manifest and calibration)
    hashes the scaled frame, so it changes for identical input data. Nothing
    compares sequence-model hashes (verifier, 2026-11-13).

- [x] **R27.6 `LiveFeatureSource` is five state groups with a hand-written
  19-assignment `reset()`.** 847 lines, 43 fields, 27 methods: ingest and
  decode; the tick and ring buffer; session trip integrals (eight fields and
  the 64-line `_accumulate_derived_integrals`); cold-start trackers; bus
  freshness. `reset()` re-assigns each attribute by name, so an accumulator
  added later and forgotten there leaks across a replay boundary, and no test
  enumerates the fields.
  - [x] First a test that diffs `vars(fresh)` against `vars(after_reset)` for
    the non-config fields. It fails the day a field is forgotten.
    *Done 2026-11-12:* `tests/test_live_feature_source_reset.py`. It drives a
    source through every state group (OBD/CAN/DID/GPS/IMU ingest, a quality-gate
    reject, replayed-record metadata, 12 ticks, trip integrals, a cold-start
    tracker, the bus-freshness seed), asserts every compared field moved off
    its fresh value (so a new field fails until the test drives it), resets,
    and compares every dataclass field and instance attribute recursively by
    value against a fresh source. `NOT_RESET` names each exclusion and why
    (constructor inputs, objects built from them, `_generation`, which reset
    must advance and is asserted to). Written before the extraction and run
    against the old `reset()`, it found one field reset leaves as driven:
    `_start_context_recording` (B07.7). Resetting it is not output-identical,
    so it stays as it was and is a named exclusion pending a decision. Dropping
    one assignment from `reset()` fails it, naming the leaf:
    `self._trip = TripIntegrals()` gives `_trip.trip_distance_km`, `_trip.
    fuel_used_l` and five more; `self._reject_counts = {}` gives
    `_reject_counts['obd:RPM']`; the `BusFreshness` line gives
    `_bus_freshness.last_frame_at`.
  - [x] Extract `TripIntegrals` (pure, `accrue(now, point)`, a fresh instance
    on reset; its batch twin is pinned by
    `test_live_trip_integrals_match_batch_compute_derived_channels`), then
    `BusFreshness`. M01.2 changes what `TripIntegrals` integrates; do the
    extraction first.
    *Done 2026-11-12:* `src/features/trip_integrals.py`: `TripSample.from_point
    (point, bindings)` is the one read of the operating row (the place M01.2's
    speed correction and M01.4's fuel-cut flag go), and `TripIntegrals.accrue
    (now, sample, fuel)` takes the fuel constants per call (the config
    hot-reloads; M01.3 changes them). `src/features/bus_freshness.py`:
    `BusFreshness` (`observe`, `seed`, `frame_age_seconds`). `reset()` replaces
    both with fresh instances. Neither needs a `LAYERS` entry (inside
    `src/features`). Serve-path replay: the first 600 s of
    `~/canary-data/sessions/nissan-xterra-2008/2026-10-26T003705` (a drive
    with OBD speed, MAF and CAN), observed on recorded stamps, ticked at 1 Hz
    session time, with a `seed_bus_last_heard` and a `reset()` halfway,
    through a `LiveFeatureSource` with the repo bindings, behavior models and
    DID map, before and after: 601 ticks, 600 feature rows, 399,974 feature
    cells, every tick's `latest_derived_channels`, operating point,
    `vehicle_state_inputs`, reject counts and segmenter fallback identical
    (`assert_frame_equal(check_exact=True)`, floats by `float.hex`): 0 diffs.
    Tests: the 34 test files importing `features.live`/`LiveFeatureSource`
    (the new file among them), `pytest -n 4`: 789 passed (the batch twin among
    them). The verifier's re-run with `test_layering` and `test_source_rules`:
    824 passed; 48,000 randomized ticks through HEAD's and the new accumulator
    side by side were identical by `float.hex`, and its own replay of the same
    window (HEAD import confirmed) gave 0 diffs.
    *Closed 2026-11-12, its own change:* `reset()` now clears
    `_start_context_recording`, and `NOT_RESET` no longer excludes it, so the
    reset test covers it (dropping the new line fails it: `reset() left these
    fields as driven, not fresh: ['_start_context_recording']`). Reproducer
    `tests/test_behavior_serving_contracts.py::
    test_start_context_after_reset_matches_a_fresh_source` (recording R1,
    reset, a cold start watched with no recording, then R2) failed before the
    fix at the first R2 tick: reset source `(0.0, None)`, fresh source
    `(1.0, 20.0)`; it passes after. What it feeds: only the `context:*`
    start-evidence columns, read by behavior eligibility rule 4
    (`cold_start_no_start_evidence`) of a model with a `cold_start` contract.
    They are never a model feature (`candidate_feature_columns` and the rolling
    builder skip `context:*`). Three shared recipes do set a cold-start
    contract (`coolant_warmup`, `cold_start_idle_rpm_vs_coolant`, `cat_warmup`,
    via `recipe_builders.WARMUP_1D_FIELDS` `max_initial_coolant_c: 40.0`), so
    the next refit ships them; no model file loaded today has one (repo, desk
    and the `pi-20261112` backup all give `start_context_specs == []`). What
    bounds the impact is the wiring, not the models: in the daemon
    `session_identity()` is `None` only before the storage writer exists,
    before any replay can reset the source, and `reset()` already empties
    `_start_trackers`, so the old "new recording, clear trackers" on the first
    tick after a reset cleared trackers that were already empty. The fix
    changes output only when a `None` recording follows a reset (tests and
    tools, not the daemon), whatever models are loaded. Replay, HEAD (912c3238)
    against the fix: the first 900 s of `2026-10-26T003705` (engine start at
    3.8 s, stop at 522.5 s, restart at 643.3 s), the shipped models with a
    cold-start contract added to the first, recording R1, `reset()` at 300 s,
    no recording until 700 s, then R2: 901 ticks, 764 feature rows, 523,226
    cells. Every non-`context:*` cell, `latest_derived_channels` and
    non-context operating-point value is identical (`check_exact`); the two
    `context:*` columns differ on the 65 rows from 700 s (HEAD `(0.0, NaN)`,
    fixed `(1.0, 73.0)`, the restart watched at 643 s). The contract model's
    eligibility verdict changed on 0 ticks (it refuses earlier, on operating
    mode). Tests: the 35 files importing `features.live`/`LiveFeatureSource`
    plus `test_feature_pipeline_boundaries`, `pytest -n 4`: 963 passed.
    `pytest -m ml_training tests/test_lstm_inference.py`: 20 passed;
    `tests/test_train_lstm.py tests/test_tcn_training.py`: 26 passed.
  - Must not change: the public surface (35 test files import the class).
    Kept: every public name. Compatibility shims for outside readers of
    private names: `_last_broadcast_frame_at` (a property and setter over
    `BusFreshness.last_frame_at`; `tests/test_parked_state.py` writes it) and
    `_accumulate_derived_integrals(now, point)` (`tests/
    test_decoded_mode_signals.py` calls it; also the method `apply_built_tick`
    uses). The eight trip fields had no outside reader and are gone.

- [x] **R27.7 `models/evaluation.py` (1,589 lines) is four unrelated
  clusters.** R15 created twelve `evaluation_*.py` modules and left this file
  alone. By content: corpus structure (`Era`, `Drive`, `HoldoutSplit`,
  `holdout_by_drive`, `pick_calibration_holdouts`); alert persistence
  (`PersistenceSpec`, `sustained_alerts`, `alert_episodes`); budget and
  detection metrics (`engine_hours`, `block_maxima`, `bootstrap_ratio_ci`,
  `vus_pr`); distribution shift (`shift_columns`, `mode_spreads`,
  `_shift_verdict`); plus the unrelated
  `refuse_non_default_profile_sessions`, which `training_entry` imports from
  here.
  - [x] `evaluation_corpus.py`, `evaluation_alerts.py`,
    `evaluation_metrics.py`, `evaluation_shift.py`; `evaluation.py`
    re-exports, with an `is`-identity test like
    `test_loader_names_are_the_owners_objects`. No body edits.
    Done: `evaluation.py` 1,589 -> 275 lines (docstring, facade imports,
    reporting strata, `DriveDetectorResult`); corpus 685, alerts 133, metrics
    405, shift 266. `refuse_non_default_profile_sessions` and its error sit in
    `evaluation_corpus.py`: it is the corpus-eligibility rule applied at the
    trainer, the other half of "which sessions may a detector see".
    `MAX_ENGINE_STEP_SECONDS` lives in `evaluation_alerts.py` (the lowest
    module, so `evaluation_metrics.py` can import it without a cycle). Proof:
    all 70 module-level defs/classes/constants of HEAD's `evaluation.py` have an
    identical `ast.dump` and source segment in their new owner, and no new
    names were defined; `tests/test_evaluation_reexports.py` (57 tests)
    pins every re-export as the owner's object and each moved class's
    `__module__`. Pickles: score-cache entries are keyed by `code_version`
    (hash of every `src/**/*.py`), so old entries are never read; a pickle
    naming `src.models.evaluation.Drive` still loads through the facade.
    `tests/test_retrain_script.py` and `tests/test_sequence_bakeoff.py`
    monkeypatched `session_parquet_bytes` and friends on the facade, which
    would no longer reach `pick_calibration_holdouts`; they now import
    `evaluation_corpus as model_evaluation`. Ran with `-m ""`: every test
    importing `src.models.evaluation*`, the golden, `evaluate_detectors` script
    and layering tests, 665 passed; `-m ml_training` on the evaluation files,
    31 passed. The pick log line now logs under `src.models.evaluation_corpus`.
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

- [~] **R27.12 Dead symbols and small items.** *Partly done 2026-11-13; the
  rest is judgement or cold, left `[ ]` below.*
  - [x] No reference outside their own definition and `__all__`:
    `spec_producibility.load_manifest_feature_columns`,
    `evaluation.DriveDetectorResult`, `preprocessing/derived.reduced_channels`,
    `sequence_training.SequenceModelLike`. Re-grepped (`grep -rnw` over `src
    scripts tools tests docs data`, the dashboard TS, `docs/mcp-server.md`, and
    `grep -a` over `models/`, `data/`, `~/canary-data`: no pickle or manifest
    names any of them). All four deleted, with their `__all__` entries and the
    now-unused imports (`json`, `Protocol`). One test reference:
    `tests/test_evaluation_reexports.py` listed `DriveDetectorResult` in
    `OWNED_HERE` (the facade's `__all__` pin) and lost that line with the class.
  - [x] Test-only compatibility aliases, tests moved to the real names, then
    deleted:
    - `behavior_models.NoiseFloor` (`= ResolutionFloor`): 7 test files
      (`builders`, `axis_pairing`, `serving_contracts`, `bank_balance_persistence`,
      `evaluate_behavior_models_script`, `behavior_validation`,
      `behavior_resolution`) now import `ResolutionFloor`. The others that say
      `NoiseFloor` import `analysis.noise_floor.NoiseFloor`, the real one.
    - `sequence_calibration._block_maxima`: no reference at all; deleted.
    - `feature_transform._SEGMENTER_SIGNAL_COLUMNS` and its re-export (import
      and `__all__` entry) in `feature_pipeline`: `test_decoded_mode_signals`,
      `test_preprocessing_segmenter`, `test_plausibility`, `test_feature_policy`
      now use `SEGMENTER_SIGNAL_COLUMNS`; the `("_SEGMENTER_SIGNAL_COLUMNS",
      ...)` pin in `test_feature_pipeline_boundaries` is gone.
    - The twins: `lstm_training.{LSTMAutoencoderManifest,TrainedLSTMAutoencoder}`
      and `tcn_training.{TCNAutoencoderManifest,TrainedTCNAutoencoder}`, plus
      their four `src.models._EXPORTS` entries. The trainers' annotations say
      `TrainedSequenceAutoencoder`; `test_lstm_training`, `test_model_manifest`
      use `SequenceAutoencoderManifest`, `test_lstm_inference` and
      `test_false_positive_benchmark` use `TrainedSequenceAutoencoder`. Both
      real classes are what a pickle already names, so no artifact changes
      (none of the names occur in `models/`). `LSTMAutoencoder*` classes in
      `lstm_autoencoder.py` and the `*TrainingConfigLike` protocols are not
      aliases and stay.
    - Those three LSTM test files also read their own `REPO_ROOT =
      Path(__file__)...`; they now import `src.paths.REPO_ROOT`.
  - [x] `training_data_hash` now `SequenceWindowDataset.training_data_hash()`,
    cached in the `_training_data_hash` the constructor already declares; no
    `getattr`, no write from another class. Callers
    (`sequence_calibration`, `sequence_autoencoder_training`) call the method;
    the module function and its two re-exports (`sequence_training`,
    `sequence_autoencoder_training` import) are gone. Same digest bytes
    (`test_lstm_training` asserts manifest and calibration hashes match,
    `sha256:` prefix).
  - [ ] `append_deviation_columns` over the 181-row buffer: *suspected only*,
    needs the safe slice derived from the models' `__roc` and `context:range_*`
    lookbacks and a measurement. Not attempted.
  - [x] `plausibility/limits.j1979_encoding_ranges`: the sweep is now an
    `lru_cache`d private `_j1979_encoding_ranges`; the public function returns
    `dict(...)` of it, so a caller that mutates the result cannot change the
    next call's. Only caller is `resolve_limits`. No test patches
    `MODE01_REGISTRY`. The 75 ms was taken from this item, not re-measured.
  - [ ] `alerts/emitter.emit_behavior` / `emit_trend` keyword forwarding: the
    item gives no design (a `**kwargs` forward loses the typed signature, a
    parameter object changes every caller). Not attempted; judgement call.
  - [ ] `sequence_dataset._frame_mask_delta` vectorization: item says cold, do
    it only if the corpus grows. Not attempted.
  - [x] Local copies of `src.coerce.optional_str` in
    `models/retraining_policy.py` and `companion/discovery.py`: **not
    copies.** Both are `None if value in (None, "") else str(value)`;
    `coerce.optional_str` keeps `""` as `""`. Operator, 2026-11-13: keep
    `""` as absent there. The variant is now `src.coerce.nonempty_str`
    (same expression), both private copies are gone, and
    `tests/test_coerce.py` pins the difference between the two rules.

- [x] **R27.13 The IF runs its forest twice per tick, on an object-dtype row.**
  Found while doing R27.4 (2026-11-12), not yet measured on the Pi.
  *Done 2026-11-13 (laptop only; Pi per-tick time not measured):*
  `TrainedIsolationForest.score` calls `decision_function` once per mode group
  and takes the label from it through `_inlier_labels`, which is sklearn
  1.8.0's `predict` rule (`ones`, `-1` where `decision < 0`, zero an inlier).
  `IsolationForestInferenceEngine.observe` / `feature_contributions` /
  `contribution_reference` accept a one-row frame and refuse any other row
  count, and `_if_compute` / `_rank_feature_contributors` pass
  `aligned.iloc[[0]]`. The typed frame alone was *slower* than the object row:
  `ZScoreScaler` rebuilt its block plan on every call (two `np.result_type`
  and two operand conversions a column, 1.4 ms of a 295-column row's 2.8 ms).
  The plan is now cached per column layout (`_block_plan`, at most 8 layouts,
  kept out of pickles, equality and `repr`). That speeds up both paths.
  - Equivalence, deployed `models/nissan-xterra-2008/isolation_forest`
    (2026-10-08 artifact) on stored drives 2026-10-08T182550,
    2026-10-10T141532 and 2026-10-10T170455 (1,684 ticks, 363 forest-scored;
    the rest are withheld for missing features or the `unknown` mode, as at
    HEAD). The live `_if_compute` ran one row per tick at HEAD (worktree) and
    in this tree. Scores were bit-identical (max |Δscore| 0.0) and labels,
    calibrated verdicts, missing flags, thresholds and model modes all
    matched: 0 mismatches and 0 verdict flips. On the same 363 rows the
    occlusion contributions and reference matched frame against Series
    (0 mismatches). The first sample (2026-11-11 drives, 2,939 ticks) was
    all `unknown`/`koeo` and so all withheld. It matched too, but it never
    reached a forest and proves nothing.
  - Time, measured in one warm process with HEAD's work emulated per tick
    (object row, uncached plan, `predict` after `decision_function`) and
    interleaved with this tree's path. Same 1,684 ticks, load average about 1,
    `engine.observe` only. Forest-scored ticks: median 27.53 → 18.07 ms, p95
    30.22 → 20.50 ms. Withheld ticks: median 7.21 → 7.41 ms, p95 8.97 →
    9.26 ms. All ticks: median 7.39 → 7.63 ms, p95 28.65 → 19.04 ms. With the
    plan cached on both sides, the typed frame is still about 0.5 ms a tick
    slower than an object row (17.50 vs 18.03 ms scored, 6.91 vs 7.43 ms
    withheld). The extra time is the scaler's concat and reorder of two
    numeric blocks: the absent features `reconcile_feature_frame_to_spec`
    fills arrive as float64 NaN beside the float32 ones. Kept as specified.
    The operator decides between this and the object row.
  - Tests: `test_inlier_labels_match_predict` (the rule against sklearn's own
    `predict`, including the zero boundary),
    `test_score_runs_each_mode_forest_once_and_never_predict`,
    `test_engine_scores_a_one_row_frame_in_its_own_dtypes` (bit-identical to
    the Series path, refuses two rows),
    `test_block_plans_are_cached_per_layout_and_stay_bit_identical` and
    `test_block_plan_cache_is_bounded`. `test_scoring_continuity`, the IF,
    inference, daemon and detector-evaluation files pass (624 passed), and so
    does `-m ml_training` on the scaler and IF users (73 passed).
  - `TrainedIsolationForest.score` calls `model.decision_function(x)` and then
    `model.predict(x)`, and sklearn's `predict` calls `decision_function`
    again. On one row with the `city` model each pass took ~13 ms on the
    laptop. Derive the label from the score already computed
    (`predict` is `score < offset_`-style thresholding; read
    `.venv/.../sklearn/ensemble/_iforest.py` for the exact rule) and prove
    labels bit-identical on a real scored session.
  - `IsolationForestInferenceEngine.observe` (`src/models/inference.py`)
    builds the row with `row.to_frame().T`, which turns every column into
    object dtype, so the scaler takes its object path. Scoring
    `aligned.iloc[[0]]` keeps the float32 dtypes. Acceptance: scores
    bit-identical to the object path on a real session (the R27.4 scaler
    computes float32 features in float32 either way, but prove it).
  - Pinned by `test_scoring_continuity` and the IF score tests.

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
