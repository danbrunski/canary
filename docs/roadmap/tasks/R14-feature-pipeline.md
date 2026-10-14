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

- [ ] **R14.1 Map responsibilities and contracts.**
  - [ ] Inventory imports, constants, re-exports, worker globals, dataclasses,
    private caller dependencies and monkeypatch targets.
  - [ ] Map `FeaturePolicy`, `build_feature_frame`, `featurize_wide_frame`,
    `featurize_perturbed_frame`, selection helpers, `build_feature_spec`, health
    and reconciliation to their target modules; record dependency direction.
  - [ ] Pin feature/schema outputs from existing parity fixtures, including
    profile-scoping refusal and IF versus sequence selection differences.
- [ ] **R14.2 Extract policy and selection.**
  - [ ] Move eligibility, exclusions, coverage and candidate/served selection
    with their types and constants; share lower-level policy primitives.
  - [ ] Preserve IF row coverage versus sequence grid density, deviation-family
    exclusion, structural exclusions and expansion restrictions.
  - [ ] Ensure spec/health and selection share definitions without mutual imports.
- [ ] **R14.3 Extract transformation and perturbation.**
  - [ ] Move the one segment → derived/committee/behavior → rolling transform
    and `FeaturizedFrame` to its owner; retain required explicit model/catalog inputs.
  - [ ] Preserve `last_row_only` behavior and the declared floating tolerances
    of incremental mean/std; preserve exact ROC and missing masks.
  - [ ] Move dependency traversal and perturbation optimization separately;
    preserve the fallback to complete featurization and its decision conditions.
  - [ ] Preserve flash history at each recorded timestamp, AFR calibration,
    DID decode units and scoped behavior-model provenance.
- [ ] **R14.4 Extract offline loading and spec/health.**
  - [ ] Move decode input resolution, bounded resampling, worker initialization,
    worker-count policy and serial/failure fallback using R13's reader owners.
  - [ ] Keep worker entrypoints importable under multiprocessing spawn;
    preserve cleanup and partial-failure behavior.
  - [ ] Move feature-spec creation, health, reconciliation and their record
    types; preserve serialized fields, flags and fingerprints.
  - [ ] Keep cache key/version ownership in `feature_cache`; a pure split must
    not accidentally change cache contents or drop an input from provenance.
- [ ] **R14.5 Migrate consumers and enforce direction.**
  - [ ] Move internal `features` imports to owning modules, including `live`;
    sweep models, analysis, trainers, validation harness and tools.
  - [ ] Keep direct re-exports of existing APIs and retarget patches deliberately.
  - [ ] Add targeted checks that extracted modules do not import the facade
    or upper orchestration; include nested/type-only imports.
  - [ ] Keep existing `features`/`behavior`/`models` layer relationships intact;
    do not move reusable feature policy into the model layer.
- [ ] **R14.6 Verify and close out.**
  - [ ] Run `test_feature_pipeline.py`, `test_feature_pipeline_incremental.py`,
    `test_feature_policy.py`, `test_reconcile_feature_frame_parity.py`,
    `test_features_last_row_parity.py`, `test_features_batched_parity.py`,
    `test_behavior_deviations_reach_training.py` and affected live/selection tests.
  - [ ] Compare full versus last-row and perturbed versus complete outputs:
    column order, dtypes, values, NaN/arrival/gap masks and selection explanations.
  - [ ] Compare feature specs, provenance/fingerprints and warm-cache behavior.
  - [ ] Replay the same fixed session before/after; record operating labels,
    emitted event differences and rows. No retrain or refit for a pure move.
  - [ ] Run layering, relevant slow and `ml_training` tiers and shared gates.
  - [ ] Record final module map, caller migration, parity evidence and commits.

## Acceptance

The facade contains exports rather than independent business logic. Feature
policy, IO orchestration, transformation, perturbation and spec health have
clear owners; all callers use the same shared transform and existing artifacts
retain the same feature contracts.
