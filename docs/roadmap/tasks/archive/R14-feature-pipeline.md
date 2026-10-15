# R14 — Cohesive feature-pipeline modules

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size L. R13 precedes offline-loading moves; coordinate R15 consumers afterward.

## Problem and target

`src/features/feature_pipeline.py` was 2,701 lines at review. It owns feature
policy, session decoding and worker orchestration, the batch/live transform,
perturbation dependency traversal, detector feature selection, and feature-spec
health/reconciliation. These responsibilities have different consumers.

Keep `feature_pipeline` as a compatibility facade over focused modules within
`src/features/`: policy/selection, offline loading, transform, dependency and
perturbation handling, and spec/health. `feature_cache.py` and `live.py` already
have owners; reuse them. There must remain one shared `featurize_wide_frame`
implementation for training, serving and evaluation.

## Implementation checklist

- [x] **R14.1 Map responsibilities and contracts.**
  - [x] Inventory imports, constants, re-exports, worker globals, dataclasses,
    private caller dependencies and monkeypatch targets.
    Taken at `HEAD` (791b3ee7) with `ast` over `src scripts tools tests`.
    Importers of the facade: 21 `src` modules, 11 scripts, 0 tools, 45 test
    files, 58 distinct names (+ 16 `import feature_pipeline` statements that use
    it as a module). `feature_pipeline` defined 65 public names and 49 private
    ones (counting its `log`), and exposed 88 more by incidental import (`np`, `pd`, `Path`,
    `read_session`, `reduce_session`, ...). Dataclasses: `FeaturePolicy`,
    `FeaturizedFrame`, `CoverageExclusion`, `DetectorFeatureSelection`,
    `FeatureSpecReconciliation`, `_DecodeInputs`. Module state: `_DID_MAP_CACHE`,
    `_DECODE_INPUTS_MEMO`, `_MODEL_ROOTS`, `_FEATURE_WORKER_STATE` (spawn
    workers). Private names other files imported: `_SEGMENTER_SIGNAL_COLUMNS`,
    `_drop_fully_stale_rows`, `_resolve_structural_columns`, `_append_derived_channels`,
    `_column_root`, `_derived_ancestors`, `_without_committee_columns`,
    `_decode_session_to_resampled`, `_available_memory_bytes`
    (`scripts/evaluate_detectors`, whose own tests patch the script's name), and
    `reduce_session` (an incidental re-export, `test_change_only_can_storage`).
    Monkeypatch targets: `fp._available_memory_bytes`, `fp.os.cpu_count`,
    `fp._resolve_worker_count`/`_PARALLEL_WORKER_CAP` (reads),
    `feature_pipeline.load_dbc_dir`, `._DECODE_INPUTS_MEMO`, `.read_session`,
    `.resample_wide` (spy), and `mock.patch.object(feature_pipeline,
    "featurize_wide_frame")`. Logger `src.features.feature_pipeline`
    (`scripts/evaluate_detectors`). No string-path patch, no `tools/` importer.
  - [x] Map `FeaturePolicy`, `build_feature_frame`, `featurize_wide_frame`,
    `featurize_perturbed_frame`, selection helpers, `build_feature_spec`, health
    and reconciliation to their target modules; record dependency direction.
    Module map and direction in the close-out record; the direction is
    `tests/test_feature_pipeline_boundaries.py::DEPENDS_ON`.
  - [x] Pin feature/schema outputs from existing parity fixtures, including
    profile-scoping refusal and IF versus sequence selection differences.
    `tests/test_feature_pipeline_golden.py` against
    `tests/fixtures/feature_pipeline_golden.json`, generated from an untouched
    `git archive HEAD` tree before any edit (so the pin is the monolith's, not
    the split's): the real chain over the committed `drive-slice` plus seeded
    channels. 32 sections: full / `last_row_only` / no-model / no-DID transform
    (order, dtype, NaN-mask digest, sum, extremes, ends, index digest),
    candidate / trainable / IF / sequence selection at three coverage floors with
    named exclusions, an exact-boundary coverage frame, a 35-channel policy
    probe, standing exclusions, the persisted spec (every field), health,
    four reconciliations (one with an object-dtype column), dependency
    traversal, 11 perturbed-frame fallback decisions, the worker-count grid
    (72 cases) and a cold/warm cache build. Profile-scoping refusal: the five
    entry points' messages are in the fixed-input baseline (0 diffs) and
    `test_featurization_profile_scope.py` runs green. Mutation proof: more than
    20 single edits to the owners (a constant, a gate, a fallback condition, a
    `<` to `<=`, a dropped `reset_index`, a dropped committee filter, ...) were
    each reported by the golden or the focused worker tests, after the first
    pin missed six of them (constants the frame never carried, an exact
    boundary, the index, an object-dtype column, single-mode, the fallback
    conditions) and was widened and regenerated from `HEAD`; `test_the_comparison_fails_on_a_real_change` shows the
    comparison itself reports a moved sum, mask, column place and selection.
- [x] **R14.2 Extract policy and selection.**
  - [x] Move eligibility, exclusions, coverage and candidate/served selection
    with their types and constants; share lower-level policy primitives.
    `feature_policy.py` (`FeaturePolicy`, the static exclusion constants,
    `resolve_feature_policy`, `feature_policy_exclusions`,
    `candidate_feature_columns`), `feature_selection.py` (coverage floor, IF and
    sequence selection, `expand_suppress_channels`), `column_health.py` (the one
    finite/constant definition) and `feature_paths.py` (repo-rooted defaults).
    Bodies were copied from `HEAD` by script (scratchpad `build.py`) and
    AST-compared (`verbatim.py`) with the rename map applied: 113 top-level
    definitions, 0 missing, 111 identical (one, `featurize_perturbed_frame`, only
    once `DEVIATION_PREFIX` is read as `DEVIATION_COLUMN_PREFIX`), and the other
    two, `DEVIATION_PREFIX` and `_catalog_stamp`, differing as listed in the
    close-out.
  - [x] Preserve IF row coverage versus sequence grid density, deviation-family
    exclusion, structural exclusions and expansion restrictions.
    Golden `floor_0.0/0.5/0.9`, `coverage_boundary` (columns at exactly the
    floor: the IF's `below_row_coverage` and both detectors' `sparse_for_grid`),
    `policy_probe`, `candidates`; the existing
    `test_feature_pipeline.py` selection tests and `test_feature_policy.py`.
  - [x] Ensure spec/health and selection share definitions without mutual imports.
    `feature_spec` imports `feature_selection`, `feature_policy` and
    `column_health`; selection imports `feature_policy` and `column_health` and
    never the spec (`test_selection_and_spec_share_definitions_without_importing_each_other`,
    `test_a_selection_module_importing_the_spec_is_found`, `test_the_map_has_no_cycle`).
- [x] **R14.3 Extract transformation and perturbation.**
  - [x] Move the one segment → derived/committee/behavior → rolling transform
    and `FeaturizedFrame` to its owner; retain required explicit model/catalog inputs.
    `feature_transform.py` (a leaf: no `src.features` import). Signatures of every
    retained name are identical in the baseline's surface compare
    (`behavior_models` and `did_map` stay required keywords).
    `test_one_featurize_wide_frame_serves_batch_live_and_the_fast_path`: the
    facade, `feature_build`, `feature_perturbation` and `live` hold the same
    object and `def featurize_wide_frame(` exists once in `src scripts tools`.
  - [x] Preserve `last_row_only` behavior and the declared floating tolerances
    of incremental mean/std; preserve exact ROC and missing masks.
    Bodies identical; golden `last_row_features` / `last_row_operating`;
    `test_features_last_row_parity.py` and `test_preprocessing_features` green;
    baseline `last_row_{40,200}` frames equal to `HEAD` exactly.
  - [x] Move dependency traversal and perturbation optimization separately;
    preserve the fallback to complete featurization and its decision conditions.
    `feature_dependencies.py` (reach and model-input traversal) and
    `feature_perturbation.py` (`featurize_perturbed_frame`, `_changed_columns`).
    Golden `fallback_decisions` pins, from `HEAD`, whether the complete transform
    ran for 11 inputs (unchanged, mode column present, extra column, reordered
    columns, row structure moved, airflow fault, small / large coolant change,
    rpm zeroed, stored derived channel, undeclared input) and that the result
    equals the complete one with `check_exact`; `test_feature_pipeline_incremental.py`
    (27 tests, incl. the 16 realistic faults) green.
  - [x] Preserve flash history at each recorded timestamp, AFR calibration,
    DID decode units and scoped behavior-model provenance.
    Bodies identical (`append_derived_channels` moved verbatim); baseline
    `flash_history_none_known` operating frame and the scoped spec's
    `afr_calibration` / `behavior_models` / fingerprints equal to `HEAD`;
    `test_target_afr_revision.py`, `test_target_afr_load_axis.py`,
    `test_base_fuel_schedule.py`, `test_featurization_profile_scope.py` green.
- [x] **R14.4 Extract offline loading and spec/health.**
  - [x] Move decode input resolution, bounded resampling, worker initialization,
    worker-count policy and serial/failure fallback using R13's reader owners.
    `session_resampling.py` (decode catalogs, `resample_session_bounded`,
    `resample_long_session`, `drop_fully_stale_rows`, `load_resampled_session`;
    imports `session_reader` / `wide_frame`, no longer the `loader` facade),
    `feature_workers.py` (worker state, sizing, pool, serial fallback) and
    `feature_build.py` (`build_feature_frame`, `disposition_screen_frames`).
    `tests/test_feature_workers.py` (new, 8 tests) pins what a parallel/serial
    output equality cannot: the threshold, the `spawn` context, the initializer
    arguments, order, skip-and-name, "no readable session", and the fall back
    after `BrokenExecutor` (pool results and skip list dropped, serial rebuild).
    The same 8 tests, ported by sed, pass against the `HEAD` monolith, so they
    describe its behavior, not the split's; 7 edits to `feature_workers` are each caught.
  - [x] Keep worker entrypoints importable under multiprocessing spawn;
    preserve cleanup and partial-failure behavior.
    `test_the_spawn_entry_points_are_top_level_and_importable_by_name` (pickle
    round trip and a fresh interpreter resolving `src.features.feature_workers`),
    `test_build_feature_frame_parallel_matches_serial` (a real `spawn` pool) green.
  - [x] Move feature-spec creation, health, reconciliation and their record
    types; preserve serialized fields, flags and fingerprints.
    `feature_spec.py`; golden `spec`, `spec_minimal`, `health`, `reconcile`
    (every serialized field, with machine paths reduced to the repo) unchanged;
    `test_reconcile_feature_frame_parity.py`, `test_behavior_lifecycle.py` green.
  - [x] Keep cache key/version ownership in `feature_cache`; a pure split must
    not accidentally change cache contents or drop an input from provenance.
    `feature_cache.py` is unmodified (`git status`); `FEATURE_FRAME_CACHE_VERSION`
    is still 10; baseline cache key, sidecar JSON (every hashed input with its
    mtime and sha256), file names, cold frame, warm frame and the stored pickle
    equal to `HEAD`; a corrupt cache still rebuilds.
    `test_the_feature_cache_key_and_version_stay_in_feature_cache`.
- [x] **R14.5 Migrate consumers and enforce direction.**
  - [x] Move internal `features` imports to owning modules, including `live`;
    sweep models, analysis, trainers, validation harness and tools.
    `live.py` imports `feature_transform`. Importers of the facade after: 0 in
    `src`, 0 in `scripts`, 0 in `tools`, 44 test files (was 21 / 11 / 0 / 45).
    Migrated by script (`migrate.py`) to the owner of each name:
    daemon (`components`, `inference`, `detectors/{common,isolation_forest,sequence}`),
    `datasets`, `models` (9 modules), `analysis` (`behavior_validation`, `fingerprint`,
    `replay`, `session_deviations`), `plausibility.offline`, 11 scripts. Docstring
    and comment references in 18 files now name the owner. `resample_config_for`
    is imported from `src.preprocessing.resampler`.
    `test_nothing_in_the_repo_but_tests_imports_the_facade`.
  - [x] Keep direct re-exports of existing APIs and retarget patches deliberately.
    `feature_pipeline.__all__` lists 75 names, each the owner's object
    (`test_the_facade_exports_the_owners_object`, 75 cases, `is` identity), all 65
    public names of `HEAD` plus eight kept private ones, `FEATURE_FRAME_CACHE_VERSION`,
    `build_feature_frame_cache_key` and `resample_config_for`.
    Retargeted patches: `test_feature_pipeline.py` worker-count tests ->
    `feature_workers.available_memory_bytes`; `test_feature_pipeline_incremental.py`
    full-path spy -> `feature_perturbation.featurize_wide_frame` (its
    `whole.called is moved_mode` assertion fails if aimed at the facade) and the
    decode-catalog tests -> `session_resampling`; `test_train_anomaly_model.py`
    reader trip-wire -> `session_resampling.{read_session,reduce_session}` (it was
    vacuous at `HEAD`: the bounded chain never calls `read_session`; a
    `force_rebuild` assertion now proves the patch is on the executed path);
    `test_resample_config_construction.py` spy -> `feature_build`;
    `test_change_only_can_storage.py` `reduce_session` -> `session_decode`.
    `scripts/evaluate_detectors.py` keeps its own `_available_memory_bytes` name
    (its tests patch it) and silences the five owner loggers instead of one.
  - [x] Add targeted checks that extracted modules do not import the facade
    or upper orchestration; include nested/type-only imports.
    `tests/test_feature_pipeline_boundaries.py` (171 tests): `DEPENDS_ON` map
    over all 14 `src/features` modules, AST scan of top-level, function,
    `TYPE_CHECKING`, `try`, `from src.features import feature_pipeline` and the
    relative forms `from .x import`, `from . import x`, resolved against the
    file's package (22 scanner cases incl. look-alikes), no cycle, no owner
    importing `scripts`, `tools` or a package at or above the `features` layer
    other than `behavior` (so no `models`), a subprocess per owner proving that
    importing it loads exactly its dependency closure and never the facade,
    the facade defines nothing (AST), one definition per class / function.
  - [x] Keep existing `features`/`behavior`/`models` layer relationships intact;
    do not move reusable feature policy into the model layer.
    `tests/test_layering.py` 11 passed, `OWED` / `OWED_PAIRS` untouched; no new
    top-level package; `feature_policy` and `feature_selection` stay in
    `src/features` and import no `src.models`.
- [x] **R14.6 Verify and close out.**
  - [x] Run `test_feature_pipeline.py`, `test_feature_pipeline_incremental.py`,
    `test_feature_policy.py`, `test_reconcile_feature_frame_parity.py`,
    `test_features_last_row_parity.py`, `test_features_batched_parity.py`,
    `test_behavior_deviations_reach_training.py` and affected live/selection tests.
    Before the first edit: 193 passed (those 7 files, fast marker). After:
    `pytest -n 4` on them plus the golden, boundary, worker, layering, cold-start,
    target-AFR, profile-scope, live-preprocessing, decoded-mode, GPS-exclusion,
    paths and bounded-resample files: 616 passed. Then every test file that
    imports a changed module (265 files), `-m "not integration and not slow"`:
    5,334 passed, 3 skipped, 2 failed (`test_evaluate_detectors_script`: I had
    renamed the script's patched `_available_memory_bytes`); restored, and the
    file re-run: 21 passed.
  - [x] Compare full versus last-row and perturbed versus complete outputs:
    column order, dtypes, values, NaN/arrival/gap masks and selection explanations.
    Fixed-input baseline (`r14_baseline.py`, scratchpad; 87 items, 79 frames,
    35,188 rows; `assert_frame_equal(check_exact=True, check_dtype=True)`, column
    order, index, attrs, `isna()` masks) captured at `HEAD` and compared after
    the split: 0 differences. It includes last-row at 40 and 200 rows against the
    whole-frame transform, 20 realistic-fault perturbed frames against the
    complete transform, and every selection with its coverage record. The
    baseline was run twice at `HEAD` first (0 differences, `PYTHONHASHSEED=0`).
    `test_features_last_row_parity.py`, `test_features_batched_parity.py` green.
  - [x] Compare feature specs, provenance/fingerprints and warm-cache behavior.
    Baseline `spec_scoped`, `spec_minimal`, `health`, `consumed_fingerprints`,
    `consumed_missing`, `cache_key`, `cache_sidecar`, `cache_files`,
    `cache_cold`, `cache_warm`, `cache_pkl_loaded`: 0 differences. Golden `spec`
    sections unchanged.
  - [x] Replay the same fixed session before/after; record operating labels,
    emitted event differences and rows. No retrain or refit for a pure move.
    `tools/replay_event_diff.drive_session` (the deterministic session-clock
    driver of R03.9) through `LiveInferencePipeline` and `LiveFeatureSource`, run
    from a `git archive HEAD` tree and from this tree, with a stub IF that fires
    at 800 rpm and a recorder on `live.featurize_wide_frame`. Session
    `2026-08-01T120000` (a copy of the committed real `drive-slice`): 420 ticks,
    420 live featurizations of 283 columns, operating labels `{city 239, highway
    154, idle_transient 13, all 7, idle 7}` before and after, 12 events,
    840 score messages, 8 counters: 0 differences (frames exact; events, scores and
    counters compared with zero tolerance). A seeded OBD session
    (`2026-09-01T080000`, 89 ticks, 3 events, 178 messages): 0 differences. No
    daemon replay was started, so replay state is not applicable; no artifact was
    trained, refit, restamped or deployed.
  - [x] Run layering, relevant slow and `ml_training` tiers and shared gates.
    `src/features/` is a directory entry in `scripts/ml_paths.txt`, so the 11
    new modules are gated exactly as the monolith was; nothing left the tier.
    `pytest -m slow` on the 265 files plus the 3 new ones: 428 passed, 4 skipped.
    `pytest -m ml_training -n 4` (whole tier): 125 passed, 2 skipped. `ruff check .`,
    `ruff format --check .` (1,253 files), `mypy` (581 source files): clean.
    `tests/test_layering.py`, `tests/test_state_dir.py`: 22 passed.
  - [x] Record final module map, caller migration, parity evidence and commits.
    Implementation commit: the commit that archives this file. Close-out below.

## Acceptance

The facade contains exports rather than independent business logic. Feature
policy, IO orchestration, transformation, perturbation and spec health have
clear owners; all callers use the same shared transform and existing artifacts
retain the same feature contracts.

## Close-out record (2026-10-15)

Module map (a module imports only the `src.features` modules to its right;
enforced by `tests/test_feature_pipeline_boundaries.py::DEPENDS_ON`):

| Module | Lines | Owns | `src.features` imports |
|---|---|---|---|
| `feature_paths` | 22 | `DEFAULT_BINDINGS_PATH`, `DEFAULT_DIDS_PATH`, `DEFAULT_FEATURE_CACHE_DIR`, `DEFAULT_CAPTURE_PROFILES_PATH` (leaf) | none |
| `column_health` | 36 | finite / constant / all-NaN column health (leaf) | none |
| `feature_transform` | 337 | `ALL_MODE`, `SEGMENTER_SIGNAL_COLUMNS`, `segment_or_constant`, `FeaturizedFrame`, `configured_behavior_models`, `featurize_wide_frame` (leaf) | none |
| `feature_policy` | 529 | `FeaturePolicy`, exclusion constants, `resolve_feature_policy`, `feature_policy_exclusions`, `candidate_feature_columns` | paths |
| `feature_selection` | 409 | `trainable_feature_columns`, coverage floor and records, IF / sequence selection, `expand_suppress_channels` | column_health, policy |
| `feature_spec` | 425 | `build_feature_spec`, health, `align_features_to_schema`, `FeatureSpecReconciliation`, `reconcile_feature_frame_to_spec` | column_health, policy, selection |
| `feature_dependencies` | 124 | `column_root`, derived / behavior-model reach | transform |
| `feature_perturbation` | 224 | `featurize_perturbed_frame` and its full-transform fallback | dependencies, transform |
| `session_resampling` | 286 | decode catalogs, bounded resample, `drop_fully_stale_rows`, `load_resampled_session` | paths |
| `feature_workers` | 289 | worker state, sizing, spawn entry points, pool, serial fallback | session_resampling |
| `feature_build` | 193 | `build_feature_frame`, `disposition_screen_frames` | cache, paths, transform, workers, session_resampling |
| `feature_cache` | 227 | cache key and version (unchanged) | none |
| `live` | 1,054 | `LiveFeatureSource` (one import changed) | transform |
| `feature_pipeline` | 327 | facade: direct re-exports and `__all__` (was 2,701 lines) | all of the above |

Placement decisions: `disposition_screen_frames` sits in `feature_build`, not in
`session_resampling`, because it also calls `segment_or_constant`, which would
have put loading above the transform. `align_features_to_schema` sits in
`feature_spec`: it is the offline sibling of reconciliation against a
manifest's schema. Defaults for the repo-rooted catalog paths are a leaf so
policy and loading share one definition without importing each other.

Renames made because a private helper now crosses a module boundary (bodies
unchanged, verified by AST): `_column_health` -> `health_of_column`,
`_is_constant_column` -> `is_constant_column`, `_column_root` -> `column_root`,
`_derived_ancestors` -> `derived_ancestors`, `_segmenter_read_columns` ->
`segmenter_read_columns`, `_ROW_COLUMNS` -> `ROW_COLUMNS`,
`_append_derived_channels` -> `append_derived_channels`, `_feature_config` ->
`resolve_feature_config`, `_without_committee_columns` -> `without_committee_columns`,
`_DecodeInputs` -> `BuildDecodeInputs`, `_load_decode_inputs` -> `load_decode_inputs`,
`_decode_session_to_wide` / `_decode_session_to_resampled` -> `decode_session_to_wide`
/ `decode_session_to_resampled`, `_resample_sessions` -> `resample_sessions`,
`_available_memory_bytes` -> `available_memory_bytes`. (`_column_health` first
became `column_health` and collided with a local variable of that name in
`summarize_feature_health`; mypy caught it.) The facade keeps the old spelling of
the five that other files import.

Differences from `HEAD`'s text, besides the renames, imports and the facade:
- `DEVIATION_PREFIX = "deviation:"` is now `DEVIATION_PREFIX =
  DEVIATION_COLUMN_PREFIX` (the behavior decoder's constant, the same string),
  and `featurize_perturbed_frame` reads `DEVIATION_COLUMN_PREFIX` directly.
- `_catalog_stamp`'s local `import hashlib` is gone (the module imports it).
- Readers come from `session_reader` / `wide_frame`, not the `loader` facade.

Compatibility: every one of the 65 public names `HEAD` defined is importable from
`src.features.feature_pipeline` as the owner's object, plus eight private
aliases (`_SEGMENTER_SIGNAL_COLUMNS`, `_drop_fully_stale_rows`,
`_resolve_structural_columns`, `_append_derived_channels`, `_column_root`,
`_derived_ancestors`, `_without_committee_columns`, `_decode_session_to_resampled`).
A pickle that names `src.features.feature_pipeline.CoverageExclusion` still loads
(`test_a_pickle_naming_the_old_module_still_loads`).

Fixture differences: none. `feature_pipeline_golden.json` was generated from the
monolith before any code moved; a second generation adding the index digest and
the new sections was also made from an untouched `HEAD` tree (the sections
common to both generations are identical apart from that digest), and the split
passes it unmodified. The fixed-input baseline and the two replays are
scratchpad scripts, not committed.

Behaviour: unchanged for every numeric result, selection, spec field, refusal,
cache key and replayed row. Deliberate side effects, listed so none is a surprise:
- `__module__` of the moved classes and functions names the owner
  (`src.features.feature_policy.FeaturePolicy`, ...). `repr`s are unchanged. New
  pickles of those classes name the owner module; old pickles still load.
- `feature_pipeline.__all__` is new (75 names). Names that were attributes of the
  module and are not any more: its 88 incidental imports (`np`, `pd`, `Path`,
  `read_session`, `reduce_session`, `_feature_frame_cache_metadata`, ...) and 41
  private names (worker state and sizing, the exclusion constants, caches and
  helpers). Nothing in `src`, `scripts` or `tools` used them; the tests that
  did are retargeted above.
- Logger names: records that carried `src.features.feature_pipeline` now carry
  the owner (`...feature_build`, `...feature_transform`, `...feature_workers`,
  `...feature_spec`, `...session_resampling`). `scripts/evaluate_detectors.py`
  silences all five.
- `tests/test_cold_start_imports.py` now also holds `feature_transform`,
  `feature_policy`, `feature_build` and `session_resampling` off the recording
  path.
- Docs: `docs/daemon-architecture.md` has a "Feature pipeline" row;
  `docs/data-sources.md` and `docs/physics-informed-anomaly-features.md` point at
  the owners.

Measured limitations: the golden fixture is the committed drive slice plus
seeded channels, not a full corpus; the worker pool tests use a recording fake
pool (a real `spawn` pool is exercised by `test_build_feature_frame_parallel_matches_serial`
and the baseline's two-worker build, which equals the serial frame exactly).
The replay is offline on the laptop with a stub detector: it establishes the
rows the detectors are handed, the operating labels and the emitted events, not
Pi throughput or memory. The fixed-input baseline compares values exactly; it
does not compare peak memory, and none is claimed.
