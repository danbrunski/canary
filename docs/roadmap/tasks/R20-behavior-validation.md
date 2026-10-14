# R20 — Behavior-validation responsibilities

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size L; lower priority. R17 precedes refit caller migration. Coordinate R15
because both touch evaluation consumers and evidence fixtures.

## Problem and target

`behavior_validation.py` was 2,206 lines. It mixes validation-document schemas,
profile exclusions, acquisition pairing, per-read frames, row grading, event
counting, baseline/metrics, synthetic sensitivity, criteria, provenance and
evidence assembly. R05.13 moved grouped splits out, but its optional facade
split was not attempted. This task addresses that remaining structural work.

Keep the public module as a facade. Establish owners under `analysis` for
schema/provenance, input pairing, row evaluation/events, metrics/sensitivity,
criteria and evidence orchestration. Start with fewer modules if the dependency
map shows inseparable pieces; do not create cycles to match a file-count goal.

## Implementation checklist

- [ ] **R20.1 Capture the validation contract.**
  - [ ] Inventory exports, re-exported split functions, imports from refit,
    reports, inventory, timing lookup and tests, including private patch targets.
  - [ ] Pin current validation documents and evidence fixtures, their schemas,
    required fields, taxonomy refusals, provenance and criteria versions.
  - [ ] Capture known-answer outputs for eligible/off-map/stale/missing-input
    rows, per-read axis pairing and independent-drive evidence.
  - [ ] Read archived R05.13/B02 decisions; retain `splits.Drive` distinct
    from `PhysicalDrive` and avoid repeating already completed split work.
- [ ] **R20.2 Assign owners and dependency direction.**
  - [ ] Put validation document types and file/provenance helpers in a leaf
    analysis module; reuse existing hash helpers rather than cloning them.
  - [ ] Put profile exclusion and per-read/axis pairing in an input owner,
    preserving timestamp basis and acquisition separation contracts.
  - [ ] Put `RowEvaluation`, grading and event projection in an evaluation
    owner; use the existing serving eligibility and persistence implementations.
  - [ ] Put physical metrics, drive bootstrap, baselines and sensitivity in
    owners downstream of row evaluation. Criteria consume those results.
  - [ ] Make evidence construction the orchestration boundary, importing
    schemas, evaluation, metrics and criteria in one direction.
  - [ ] Record the final module map before moving bodies; every owner stays
    at analysis layer or uses existing lower-layer shared functionality.
- [ ] **R20.3 Extract incrementally.**
  - [ ] Move schemas/input preparation first, then evaluation/events, metrics,
    sensitivity/criteria and evidence, maintaining direct facade exports.
  - [ ] Preserve missing/fresh/eligible/evaluated denominators and refusal
    distinctions; ineligible rows must not become healthy measurements.
  - [ ] Preserve axis-pairing skew and basis refusals, sample reuse rules and
    actual/expected engineering units.
  - [ ] Preserve event persistence/dedupe, band-excess accumulation, reset
    gaps and per-read versus per-grid evaluation behavior.
  - [ ] Preserve bootstrap units/seeds, split/leak checks, baseline fitting
    only on permitted folds, and synthetic detection versus observed fault recall.
  - [ ] Preserve passed/failed/insufficient-evidence criteria and publication
    behavior; no new threshold, tolerance or fallback criterion.
  - [ ] Preserve schemas and hashes of meaningful evidence fields; moving
    a class must not require rewriting stored validation documents.
- [ ] **R20.4 Sweep consumers and enforce boundaries.**
  - [ ] Migrate internal imports to owners, including refit and evaluation CLI,
    and retain existing split re-exports for compatible callers.
  - [ ] Retarget monkeypatches to execution owners and check alias identity.
  - [ ] Add focused checks against importing the facade from implementations
    or importing evidence orchestration into leaf schema/evaluation modules.
  - [ ] Inspect `scripts/ml_paths.txt` so previously covered moved logic keeps
    its coverage; do not infer a new training run merely from the split.
- [ ] **R20.5 Verify and close out.**
  - [ ] Run `test_behavior_validation.py`,
    `test_behavior_validation_known_answers.py`, `test_behavior_axis_pairing.py`,
    `test_behavior_regimes.py`, `test_behavior_serving_contracts.py`,
    `test_refit.py`, affected lifecycle and evaluation-script tests.
  - [ ] Compare fixed row arrays, refusal reasons, event times, physical metrics,
    independent-drive counts, synthetic outcomes and criterion verdicts.
  - [ ] Compare stored evidence output after normalizing only explicit run
    metadata; preserve contract fingerprints and meaningful provenance.
  - [ ] Use an existing refit fixture to verify publication/skip decisions;
    no deployed model refit is required for a behavior-preserving move.
  - [ ] Run layering, affected slow/training tiers and shared gates. Replay
    if a served caller is moved, recording row/event parity explicitly.
  - [ ] Record final owners, compatibility imports, evidence differences,
    tests and commits. Keep any substantive scientific correction separate.

## Acceptance

Validation has clear input, evaluation, metrics, criteria and evidence owners.
Known answers, eligibility accounting, independent-drive uncertainty, serialized
evidence and publication decisions remain unchanged.
