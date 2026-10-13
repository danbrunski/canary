# R04 — models, preprocessing, validation, plausibility

Opened 2026-09-24 from the refactor review ([R00](../R00-refactor-README.md)).
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

- [x] **R04.1 The false-positive benchmark must score the detector that is
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

  *Done 2026-09-25, one caller remainder.* `benchmark_false_positives` takes
  `suppress_channels`, resolves the served exclusions with
  `sequence_scoring.score_exclusions` (suppressed + uncalibrated + context,
  exactly as the engine) and flags with the R04.2 kernel (global mode band OR
  family path), one forward pass per batch (`reconstruction_error(...,
  reduction="none")`). The k/quantile sweep recomputes the *global* bands and
  ORs the calibrated family flag in. `_error_distribution` is gone (uses
  `sequence_calibration.error_distribution`); `_window_modes` is a view of
  `sequence_calibration.window_modes`. The report gains
  `family_flagged_windows` and `excluded_columns`. `scripts/fp_benchmark.py`
  passes `models.<type>.suppress_channels`.
  - Failing test first:
    `test_benchmark_scores_the_served_rule_with_a_suppressed_noisy_channel`.
    Against the pre-change module the same fixture gives benchmark 0 flagged
    vs served 4 of 37 windows; now they agree, and the unsuppressed benchmark
    flags more (the count depends on the suppression).
  - `pytest -m "" tests/test_false_positive_benchmark.py`: 21 passed (the
    stub test now returns the `"none"` cell error).
  - *Remainder done 2026-09-25 (R06.4):* `train_sequence.py --evaluate`,
    `sequence_bakeoff.py` and `fp_benchmark.py` all benchmark through
    `src.models.training_entry.benchmark_held_out`, which passes
    `models.<type>.suppress_channels`.
    `tests/test_script_imports.py::test_benchmark_held_out_leaves_the_configured_suppress_list_out`
    pins the pass-through, and
    `test_scripts_benchmark_only_through_the_served_rule` refuses any script
    calling `benchmark_false_positives` itself (at HEAD three did).

- [x] **R04.2 One kernel for the served sequence-scoring rule.** *Parity,
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

  *Done 2026-09-25.* `src/models/sequence_scoring.py` (numpy, no torch):
  `family_index`, `score_exclusions`/`ScoreExclusions`, and
  `SequenceScoringRule.score_batch(error, score_mask, modes) -> BatchScores`
  (global, per-mode threshold, channel, family scores/max, verdict). It is a
  class built once per artifact rather than a bare function, so the per-tick
  call does no family grouping. `SequenceThresholdCalibration.threshold_for_mode`
  replaces the three `per_mode_errors or {}` lookups. Serve
  (`SequenceInferenceEngine.score_window`, B=1), `BatchedSequenceScorer.score`
  and the FP benchmark call it; calibration groups by the same `family_index`.
  - **Replay parity** (deployed 2026-09-20 IF + LSTM, `2026-09-23T002340`,
    937 featurized rows, row-by-row `observe`, before vs after): IF scores
    max diff 0.0, 0 categorical mismatches; sequence 908 windows, 798
    anomalies both sides, 0 categorical mismatches (`is_anomaly`,
    `anomalous_channels`, `top_channels`, `ranking_scope`), max score diff
    7.8e-6 absolute / 2.8e-7 relative (serve now sums float64 in numpy
    instead of float32 in torch); `BatchedSequenceScorer` bit-identical
    (max diff 0.0, 0 verdict mismatches).
  - **Per-tick cost** (laptop, 1 torch thread, deployed LSTM, T=30 F=361,
    min of interleaved runs): `score_window` 4.52 ms before, 4.16 ms after;
    the kernel alone 34.5 us per window (`np.maximum.reduceat` over families).
  - Tests: `tests/test_sequence_scoring.py` (8, new), and
    `pytest -m ""` on `test_lstm_inference.py`, `test_evaluation_detectors.py`,
    `test_sequence_calibration.py` pass (in the 788-pass run below).

- [x] **R04.3 Define the rolling-suffix / base-channel parser once.** *Latent
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

  *Done 2026-09-25 except one copy.* `features.ROLLING_SUFFIXES`,
  `split_rolling()` and `base_channel()` are public (`_FEATURE_SUFFIXES`
  removed; no other importer). Pointed at them: `_base_channel` in
  `evaluation_detectors`, `isolation_forest`, `inference`;
  `sequence_training._feature_family` (now `sequence_scoring.family_index`);
  `feature_pipeline._base_feature_name`/`_ROLLING_SUFFIXES`
  (`expand_suppress_channels` uses `split_rolling`);
  `sequence_feature_set._ROLLING_SUFFIXES`/`_split_rolling` (and `ROLLING_KEPT`
  uses `ROC_SUFFIX`); `spec_producibility.base_feature_name`;
  `sequence_dataset._channel_base`; `trainability._base` (lazy import:
  preprocessing imports trainability).
  - `grep -rn '"__roc"' src` now lists `features.py` and
    `decode/behavior_decoder.py:570` only (`analysis/behavior_input_inventory`
    uses `removesuffix("__roc")` on a condition channel, a different job).
  - **Remaining:** `decode/behavior_decoder.deviation_model_name`'s
    `("__mean", "__std", "__roc", "__gap")` — `src/decode/**` is the
    concurrent R05 agent's area.
  *Remainder done 2026-09-25.* `deviation_model_name` strips `GAP_SUFFIX`
  or else `features.base_channel` (identical results: a name ends in at most
  one of the four). `analysis/behavior_input_inventory` uses `ROC_SUFFIX`
  too. `grep -rn '"__roc"' src` → `src/preprocessing/features.py` only.
  `test_behavior_decoder.py`, `test_behavior_trim_contracts.py` pass.

- [x] **R04.4 One decode path for CAN records: live, storage, backfill,
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

  *Partial 2026-09-25.* `preprocessing/decode.decode_signal_column(payloads,
  signal)` is built on `decode/bindings.decode_raw`; `decode_can_rows` and
  `tools/backfill_decoded.decode_session_frame` both call it (the two
  AST-identical `_extract` closures are gone). Backfill still drops NaN rows;
  unchanged here. Test: `test_decode_signal_column_matches_decode_raw`;
  `test_preprocessing_decode.py`, `test_frame_decoder.py`,
  `test_decoded_mode_signals.py`, `test_backfill_decoded.py` pass.
  Frontend re-checked: no `.tsx`/`.ts` outside tests reads `confidence` or
  `binding_confidence` from a telemetry record (the three hits are DID/catalog
  confidence and the LLM hypothesis).
  - **Remaining:** (1) `LiveFeatureSource` holding a `FrameDecoder` for both
    jobs, dropping `_decode_signal_raw`, and the live-vs-storage same-record
    test — needs `src/preprocessing/live.py`, held by a human session with
    uncommitted work. (2) `Signal.display_unit` for the five `!= "unknown"`
    copies — `src/decode/**` is the concurrent R05 agent's (R05.9).
  *Remainder done 2026-09-25.* `LiveFeatureSource` holds
  `FrameDecoder.from_bindings(bindings)` (every confidence) and
  `decode_for_telemetry` returns its records, so the dashboard stream and the
  live features get exactly the record storage writes; `_bindings_by_id` and
  `_decode_signal_raw` are deleted. `bindings.Signal.display_unit` (`""` for
  the `"unknown"` placeholder, `UNKNOWN_UNIT`) replaces the five copies
  (`live`, `plausibility/limits`, `dashboard/readers`, `tools/emit_dbc`,
  `tools/decode_bindings`). The one record: metadata `binding_confidence`,
  float value, and `display_unit` — so `FrameDecoder`, `decode_can_rows` and
  `tools/backfill_decoded` now write `unit=""` where they wrote `"unknown"`
  (the stream already showed `""`; stored and recomputed rows still match,
  `test_matches_decode_can_rows_semantics`). Frontend re-checked: no non-test
  `.ts`/`.tsx` reads `confidence`/`binding_confidence` from a telemetry
  record. Test (fails on HEAD's `live.py`):
  `test_preprocessing_live.py::test_live_and_storage_decode_a_frame_to_the_same_record`
  (every bound CAN id of `data/bindings.yaml`; live == all-confidence decoder,
  and == the storage decoder for each persisted signal).
  `test_preprocessing_live.py`, `test_frame_decoder.py`,
  `test_preprocessing_decode.py`, `test_decoded_mode_signals.py`,
  `test_backfill_decoded.py`, `test_emit_dbc.py`, `test_plausibility.py` pass.
  Before/after on a real session (shared with R01.3/R04.7): the first 5.9
  minutes of chunks 100-180 of `sessions-0920/2026-09-20T205117` (546,953
  records: CAN frames with payloads, Mode 01, GPS) fed through HEAD's and this
  tree's `LiveFeatureSource` (repo bindings + DID map) with a tick per second:
  351 of 351 feature rows identical (`assert_frame_equal(check_exact=True)`,
  315 columns), and all 863,775 `decode_for_telemetry` records equal once the
  two named differences are normalised (metadata key, int→float).

- [x] **R04.5 Promotion gate: a withheld detector must fail, not pass.**
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

  *Done 2026-09-25.* `evaluate_promotion_gate` is a sequence of
  `_check_calibration`, `_check_contamination`, `_check_quarantine_trend`,
  `_check_recall`, `_check_fp_budget` and `_check_agreement`, each returning
  `(ok, reason, detail)`. A (detector, fault) cell the golden graded and the
  candidate did not fails with `missing: True` and a reason naming it; a
  detector with no `per_hour` fails the FP budget (it defaulted to 0.0) and
  the agreement check; the FP budget covers every detector either report
  names. A cell the golden never graded is still skipped (recall).
  - Behaviour change, agreement check: a missing rate fails on *either*
    side. A golden detector with no `per_hour` used to be compared as 0.0
    (a zero candidate "agreed"); it now fails with "golden recorded no
    held-out per_hour to compare against: <detector>" —
    `test_gate_fails_agreement_when_the_golden_recorded_no_rate`.
  - Failing tests first (3 failed before the split):
    `test_gate_fails_a_candidate_whose_detector_was_withheld_where_golden_was_graded`,
    `test_gate_fails_a_candidate_with_no_recorded_false_positive_rate`,
    `test_gate_fails_a_candidate_missing_a_nominal_detector_the_golden_has`;
    plus `test_gate_ignores_a_cell_the_golden_never_graded` and the
    golden-side test above. `tests/test_promotion_gate.py`: 25 passed.

- [x] **R04.6 Evaluate the detectors on default-profile data only.**
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

  *Code done 2026-09-25; table regenerated 2026-09-26 (below).*
  `src/models/evaluation.default_profile_exclusion(session_dir)` names why a
  session's rows are out: a `_tuning-*` session, or a capture whose manifest
  is not `corpus_eligible` (a capture with no readable manifest counts as
  eligible, as the R03.12 replay guard reads it). In
  `scripts/evaluate_detectors.py` captures still build drives; an excluded
  session is not loaded, scored, injected into, shift-tested or counted, and
  is listed per drive under `excluded_sessions` (not `missing_sessions`). A
  drive with nothing else is `skipped: no default-profile rows (<session>:
  <reason>)`. `--plan` prints each excluded session with its reason.
  `synthetic_fault_eval._running_mask` left as is (default-profile rows carry
  Mode 01).
  - Failing test first: `test_tuning_capture_rows_reach_no_metric` (a default
    session + an overlapping `_capture-tuning-*` sibling, plus a
    `_tuning-*`-only drive) failed on the old script (the tuning-only drive
    was scored as a second drive); now one drive is scored on the default
    session's rows only, the capture is named in `excluded_sessions`, the
    shift test and injections never see it, and the tuning-only drive is
    skipped with that reason. Also `test_default_profile_exclusion_names_each_kind`.
  - On the real corpus (`--plan`, the A02.11 command): 18 drives evaluated
    with 24 capture sessions inside them not scored, and 5 capture-only drives
    now skipped. The 2026-09-23 baseline scored 16 drives, 8 of them with
    capture sessions and 6 made of captures alone.
  - A00's decisions list records the decision and that both halves are closed.
  - The tuning rule is `src.session.session_kind(name) is
    SessionKind.TUNING`, not a restated prefix.
  - ~~Remaining: regenerate the A02.11 table.~~ *Done 2026-09-26.* It
    first needed the bounded decode (P01.1): two sessions OOM-killed the
    laptop at 12-15 GB, and now peak at 1.31 GB and 1.06 GB. The A02.11
    command (no `--cache-only`, `--workers 1`) wrote
    [`data/analyses/2026-09-25-a02-detector-baseline-default-profile.md`](../../../../data/analyses/2026-09-25-a02-detector-baseline-default-profile.md).
    A second run on the P01 derived frames gave an identical report. The
    full table is in A02.11, beside the superseded one. In-era,
    default-profile rows only, 8 drives with engine time, 6.70 engine-h:

    | Detector | FA/eh (was, 2026-09-23 with captures) |
    |---|---|
    | `sequence` as served (evidence) | 40.0 pooled (45.4) |
    | `sequence_global` (evidence) | 1.2 pooled (3.0) |
    | `isolation_forest` (evidence) | 2.4 pooled (1.2) |
    | `fused` | 0 (0) |
    | `robust_z` | 3.29 [1.71, 7.08] (39.0) |
    | `pca` | 0.15 [0, 1.01] (0.4) |
    | `untrained_autoencoder` | 0.30 [0, 0.37] (0.8) |
    | `plausibility` | 0 (0) |

    Taking the tuning captures out cut robust-z's false alarms twelvefold.
    The served sequence detector's family path still floods (40 vs 1.2
    global-only).

- [x] **R04.7 Write the fast/slow max-gap rule once.**
  - The resampler honours `ResampleConfig.fast_channels`/`slow_channels`.
  - `live._gap_flags` (serve) and
    `synthetic_fault_eval._max_gap_seconds_for` re-derive the rule from the
    default hints only.
  - Nothing sets those fields today, so the drift is latent.

  **Do:** add `ResampleConfig.max_gap_seconds_for(channel)`, used by all
  three. **Accept:** a test with a non-default `fast_channels` gets identical
  gap flags from resampler and live. Size **S**.

  *Partial 2026-09-25.* `ResampleConfig.is_fast(channel)`,
  `max_gap_for(channel)` and `max_gap_seconds_for(channel)` state the rule
  once (explicit `fast_channels` wins; else not-slow-hinted is fast). Both
  resampler paths use `max_gap_for` (`_fast_channels` deleted) and
  `synthetic_fault_eval._max_gap_seconds_for` returns
  `resample_config_for(config).max_gap_seconds_for(channel)`. Test:
  `test_max_gap_for_follows_explicit_fast_channels` (non-default
  `fast_channels` flips coolant to the grid ceiling and RPM to the slow one,
  in the method and in the resampler's `__gap` flags);
  `test_resample_config_construction.py` passes.
  - **Remaining:** `live._gap_flags` still re-derives from the default hints,
    and the resampler-vs-live acceptance test needs it — both need
    `src/preprocessing/live.py`, held by a human session with uncommitted work.
  *Remainder done 2026-09-25.* `LiveFeatureSource` keeps the recipe's
  `ResampleConfig` and `_gap_flags` reads `max_gap_seconds_for(channel)`
  (memoised per channel; `_max_gap_seconds`/`_slow_max_gap_seconds` stay as
  the recipe's two ceilings, which `test_resample_config_construction.py`
  reads); `is_slow_channel` is no longer imported by `live`. Test (fails on
  HEAD's `live.py`):
  `test_preprocessing_live.py::test_live_gap_flags_follow_a_non_default_fast_channels_like_the_resampler`
  (`fast_channels={"obd:COOLANT_TEMP"}`: live and `resample_wide` give coolant
  `[F,F,F,T,T,F]` and RPM all-fresh). Real-session before/after: see R04.4
  (351/351 ticks identical under the default recipe).

## Structure

- [x] **R04.8 Move `HealthMonitor` and the system probes out of
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

  *Done 2026-09-25 except four importers.* `src/daemon/health.py` holds the
  monitor and the host probes; `src/models/drift.py` holds
  `FeatureDriftReport`, `feature_drift` and `reconstruction_error_drift`
  (re-exported by `daemon.health`). `src/models/health.py` is a shim that
  replaces itself in `sys.modules` with `src.daemon.health`, so the old import
  path still works *and* `monkeypatch.setattr(src.models.health, ...)` patches
  the running code (`test_health.py` does that). The shim never changes, so a
  health change no longer touches `scripts/ml_paths.txt`'s `src/models/`.
  Importers moved: `control/profile_capture`, `daemon/{components,drain,
  polling,runtime,detectors/isolation_forest}`, `dashboard/{api,status,
  behavior_live}`, `dashboard/routes/control/{__init__,context}`, and tests
  `test_capture_profiles`, `test_role_gate`, `test_route_table_snapshot`,
  `test_scoring_continuity`. The dashboard modules and `profile_capture` import
  it under `TYPE_CHECKING` only: a runtime import of `src.daemon.health` loads
  the whole `src.daemon` package, which cycles through `profile_capture`.
  `test_health.py`, `test_daemon.py`, `test_cold_start_imports.py`,
  `test_layering.py`'s forbidden-edge test (the move adds none), `test_role_gate.py`,
  `test_route_table_snapshot.py`, `test_scoring_continuity.py` and
  `test_profile_capture.py` pass.
  - **Remaining:** `dashboard/routes/public.py` (human session) and
    `tests/{test_health,test_daemon,test_profile_capture}.py` (another
    session's uncommitted work) still import `src.models.health`; move them,
    then delete the shim.
  *Importers done 2026-09-25.* `dashboard/routes/public.py` imports
  `HealthMonitor` from `src.daemon.health` under `TYPE_CHECKING` (annotation
  only, like the other dashboard modules); `tests/test_health.py`
  (`import src.daemon.health as health_mod`), `test_daemon.py` and
  `test_profile_capture.py` import `src.daemon.health`. `grep -rn
  "models.health" src tests --include=*.py` → only the shim itself and
  `daemon/health.py`'s docstring. `test_health.py`, `test_daemon.py`,
  `test_profile_capture.py`, `test_route_table_snapshot.py`,
  `test_cold_start_imports.py`, `test_layering.py` pass (143).
  - ~~**Remaining:** delete the `src/models/health.py` shim~~ — done below.

  *Shim deleted 2026-09-25.* `src/models/health.py` is gone; `grep -rn
  "models.health\|models import health" src scripts tools tests
  --include=*.py` → only `daemon/health.py`'s docstring, which now says the
  old path no longer imports. Doc references moved to `src/daemon/health.py`
  (`docs/implementation-tasks.md` §5.21 note, `S02-pi-updater.md` S02.cgroup
  note; archived task docs left as history). `test_health.py`,
  `test_cold_start_imports.py`, `test_layering.py`, `test_profile_capture.py`,
  `test_route_table_snapshot.py`, `test_role_gate.py`,
  `test_scoring_continuity.py`, `test_capture_profiles.py` (152 passed, `-m
  ""`) and `test_daemon.py` (69 passed) pass; `mypy src` clean. The deletion
  touches `src/models/`, so the push that carries it pays the `ml_training`
  tier once (122 passed on 2026-09-25, below R07.2).

- [x] **R04.9 Break up `fit_isolation_forest` (552 lines).** Its phases:
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

  *Done 2026-09-25.* `fit_isolation_forest` is now five calls:
  `_resolve_fit_settings` (-> `_FitSettings`), `_plan_modes` (with a per-mode
  `_plan_mode` returning a plan, a `SkippedModeSummary` or nothing),
  `_fit_scaler`, `_fit_mode` (-> model, medians, `ModeTrainingSummary`) and
  `_build_manifest` (dropped-fraction gate + manifest). The optional move of
  the calibration cluster to `isolation_forest_calibration.py` was not done.
  - Manifest byte-diff: a fixed-seed fit (6 drives x 300 rows, 2 modes, 14
    features, external 3-drive calibration frame) before and after —
    `cmp` identical.
  - The 10 test files pass: `test_bearing_wear_validation`,
    `test_evaluation_detectors`, `test_evaluate_detectors_script`,
    `test_feature_pipeline`, `test_detector_benchmark`,
    `test_isolation_forest_a04`, `test_isolation_forest`,
    `test_retrain_script`, `test_replay_script`, `test_synthetic_fault_eval`.

- [x] **R04.10 Break up `train_sequence_autoencoder` (367 lines).** Its
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

  *Done 2026-09-25.* `_preflight` (-> `_Preflight`), `_plan_training`
  (-> `_TrainingPlan`), `_setup_execution` (-> `_Execution`, receives
  `torch`/`optim`/`data`), `_run_epochs` (-> `_FitOutcome`),
  `_build_sequence_manifest`, `_calibrate_and_stamp`; the RNG is consumed in
  the same order.
  - Fixed-seed CPU runs before and after: LSTM (no validation) and TCN
    (validation_fraction 0.34, 885 validation windows), held-out calibration,
    4 epochs — both manifests `cmp` identical and every `state_dict` tensor
    `torch.equal`.
  - `test_lstm_training`, `test_tcn_training`, `test_train_lstm`,
    `test_train_tcn`, `test_sequence_autoencoder_training`,
    `test_train_sequence_columns`, `test_sequence_novelty_screen`,
    `test_sequence_bakeoff`, `test_lstm_inference` pass (`-m ""`).

- [x] **R04.11 Split `sequence_training.py` (1,423 lines) by concern, and give
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

  *Done 2026-09-25.* `sequence_training.py` is 312 lines: the manifest
  protocol and `EpochLoss`, the loop primitives and the dataset-health
  helpers, re-exporting everything else. New: `sequence_calibration.py`
  (distributions, `SequenceThresholdCalibration`, `calibrate_*`, blocks and
  modes, `block_maxima`, `training_data_hash`, `coerce_timestamp`),
  `sequence_validation_split.py`, `sequence_envelope.py`, and
  `sequence_device.py` (the torch/device helpers, so calibration does not
  import the trainer). `evaluation.block_maxima` now computes its time blocks
  and calls `sequence_calibration.block_maxima`. The unused
  `_non_overlapping_window_count` was dropped. `models/__init__` lazy exports
  point at the new homes. No monkeypatch targets needed retargeting.
  - `EpochLoss` moved to `sequence_calibration` (the registry parses manifest
    histories with it) and `isolation_forest` takes `coerce_timestamp` from
    there too; both were pulling the trainer into serve.
  - Acceptance: `test_serve_imports_nothing_from_the_training_module`
    (fresh interpreter: `models.inference`, `sequence_scoring`,
    `sequence_feature_set`, the feature pipeline, `models.isolation_forest`,
    `sequence_registry`, `daemon.inference`, `daemon.inference_worker`,
    `daemon.health` and `control.sequence_detector` load neither
    `sequence_training` nor torch).

- [x] **R04.12 One mixin for the LSTM/TCN autoencoder modules.**
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

  *Done 2026-09-25.* `src/models/sequence_module.py`: `module_base()`,
  `masked_mean` and `MaskedReconstructionMixin` (`forward`,
  `reconstruction_error`, `_validate_inputs`), torch-free.
  `class LSTMAutoencoder(MaskedReconstructionMixin, module_base())`, same for
  TCN; `_torch_modules` stays per module (distinct exception types, July).
  `SequenceAutoencoder` gained `reconstruction_error`, and the benchmark
  reaches it typed. `ReconstructionReduction` lives in `sequence_module`
  (re-exported by `tcn` and `src.models`).
  - `state_dict` keys and values of fixed-seed LSTM/TCN fits identical before
    and after; the deployed 2026-09-20 `model.pt` (saved before) loads and
    scores 908 windows identically (R04.2 replay).
    `test_both_backbones_share_one_reconstruction_implementation`.

- [x] **R04.13 Validation harness: reuse the daemon's wiring and break up
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

  *Not started 2026-09-25.* Every part edits `src/validation/harness.py`,
  which carries the concurrent R07 agent's uncommitted hunks (its
  `rebase_state_paths` swap for `_SCRATCH_DAEMON_PATHS` is already there).
  **Remaining:** all of it, once R07 lands: compose runtime's observer and
  drain-kwargs factories, split `run_scenario` into `_start_pipeline`,
  `_start_collector`, `_arm`, `_finish`.
  *Done 2026-09-25.* `daemon/runtime` exposes
  `can_receive_stats_observer(health)`, `obd_handoff_stats_observer(health)`
  (were private; `test_daemon.py` calls the public names),
  `storage_decode_for(decoder)` and `drain_kwargs(config, components, *,
  active_session_path, storage_decoder, on_trip_complete=None,
  on_trip_unobservable=None)` — the daemon's own drain call is
  `drain_queue_with_broadcast(queue, writer, stop, live_source_ready=…,
  **drain_kwargs(…))`. The harness composes the same factory (trip hooks left
  unset: a trip end would schedule a whole-session digest mid-measurement), so
  it now wires the GPS tracker, `storage_path`/`storage_warning_mb` and the
  configured `queue_warning_fraction` it had drifted from; its `on_receive`/
  `on_handoff` call the daemon's observers (the handoff one replaced a
  `**stats.to_dict()` spread) and then record the artifacts. `run_scenario`
  is setup + `_start_pipeline` (writer, drain, dashboard/score consumers,
  heartbeat, inference; returns a `_Pipeline`) + `_start_collector` +
  warm-up + `_arm` (returns the timeline task) + run + `_finish`;
  `rebase_state_paths` was already used. `pytest -m "" -n 4
  tests/test_sustained_validation.py tests/test_replay_row_accounting.py`:
  26 passed, 1 xfailed (the xfail is the suite's own marker, unchanged).

- [x] **R04.14 `build_feature_frame`: break it up and make the segmenter
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

  *Partial 2026-09-25.* `build_feature_frame` is `_load_cached`,
  `_load_decode_inputs` (-> `_DecodeInputs`) and `_resample_sessions` (pool +
  `BrokenExecutor` serial fallback), with R01.2's `dids_path` threaded into
  loading and the cache key. `SEGMENTER_SIGNAL_COLUMNS` is public (the private
  name stays as an alias for its importers) with `segmenter_input_columns()`
  flattening it once; `plausibility.offline.segmenter_channels` uses it.
  `preprocessing.decode.read_session_decoded_subset(session, bindings=,
  decoded=)` is the one bounded reader: non-CAN rows plus only the raw CAN
  frames the named bindings read, pushed down under every era's spelling
  (`can_channel_spellings`: `0x551`, `0X551`, `551`, `0x0551`, `0551`, ...) and
  matched by parsed id. `plausibility.offline.session_long` reads through it
  (no more whole-bus read and fallback parse).
  - Tests: `test_read_session_decoded_subset_reads_every_channel_spelling`
    (5 spellings; the reader and `session_long` both decode the same values),
    `test_read_session_decoded_subset_without_wanted_signals_reads_no_can`;
    `test_plausibility.py`, `test_feature_pipeline.py`,
    `test_decoded_mode_signals.py`, `test_feature_policy.py`,
    `test_preprocessing_segmenter.py` pass.
  - **Remaining:** `analysis/behavior_validation.build_evaluation_frame` (and
    `evaluation_decoded_signals`, which flattens the segmenter columns a
    second time) still push down `0x{id:X}` only; switching them to
    `read_session_decoded_subset` / `segmenter_input_columns` is in
    `src/analysis/**`, the concurrent R05 agent's area. The cross-caller test
    lands with that switch.
  *Remainder done 2026-09-25.* `build_evaluation_frame` reads each session
  through `read_session_decoded_subset(session, bindings=, decoded=)` (the
  wanted set is empty for `decode_can="none"`), and
  `evaluation_decoded_signals` starts from `segmenter_input_columns()` instead
  of flattening `_SEGMENTER_SIGNAL_COLUMNS` again. Cross-caller test (3 of 4
  spellings fail on HEAD's `behavior_validation.py`, all pass now):
  `test_preprocessing_decode.py::test_both_bounded_readers_decode_an_old_spelling_session_alike`
  (`0x551`, `551`, `0x0551`, `0551`: `plausibility.offline.session_long` and
  `build_evaluation_frame` decode the same `ECM_COOLANT_TEMP` values, and the
  unwanted binding stays out). `test_behavior_validation*.py` pass.

- [x] **R04.15 One z-score denominator rule.**
  - The rule `std if std >= std_floor else 1.0` is re-derived in
    `isolation_forest._scaled_array` / `_scaled_medians`, `inference.py` and
    `evaluation_detectors.py`.
  - IF training scales in float32, while serve goes through
    `ZScoreScaler.transform`.

  **Do:** add `ZScoreScaler.vectors(columns) -> (means, denoms)` and use it
  everywhere. **Accept:** replay parity on IF scores. Size **S**.

  *Done 2026-09-25.* `ZScoreScaler.denominator(column)` states the rule once;
  `transform` and `vectors(columns) -> (means, denoms)` use it, and so do
  `SequenceInferenceEngine`, `BatchedSequenceScorer`,
  `isolation_forest._scaled_array` and `_scaled_medians`. IF training keeps
  its float32 arithmetic (the fit matrix is the largest training allocation)
  on the float64 vectors cast once, so the fixed-seed IF manifest stays
  byte-identical (R04.9's check).
  - Replay parity on IF scores: deployed IF over `2026-09-23T002340`, 937
    rows, max score diff 0.0, 0 categorical mismatches (R04.2's run).

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
- Drive grouping has one source, `preprocessing.drives`.
- `validation/evidence.limiting_stage` (132 lines): an ordered, commented
  decision list.
- `plausibility/limits.resolve_limits` (185 lines): it splits cleanly, but
  it has had 1 commit since August. Split it only if you are in there.
- `TrainingEnvelope.check` (serve) vs `envelope_coverage*` (training): they
  answer different questions.
