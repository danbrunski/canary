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

- [x] **R20.1 Capture the validation contract.**
  - [x] Inventory exports, re-exported split functions, imports from refit,
    reports, inventory, timing lookup and tests, including private patch targets.
    At `HEAD` (`7988dc28`) the module defined 80 names plus `log`, listed 58 in
    `__all__` and re-exported 12 `splits` names (R05.13). Importers: `refit`,
    `timing_lookup`, `behavior_inventory`, `lambda_delivery` (function-level),
    `scripts/evaluate_behavior_models.py`, `inventory_behavior_inputs.py`,
    `refit_behavior_models.py`, `inventory_cold_starts.py` (function-level),
    `tools/timing_lookup_coverage.py`, and 13 test files. Patch targets: one,
    `monkeypatch.setattr(behavior_validation, "read_model_reads", ...)` in
    `test_behavior_axis_pairing.py`; no string-path patch, no private name
    imported anywhere, no pickled type (`grep pickle` over the callers). The
    `HEAD` logger `log` was never used. The map is pinned as `OWNERS` and
    `SPLIT_NAMES` in `tests/test_behavior_validation_boundaries.py`.
  - [x] Pin current validation documents and evidence fixtures, their schemas,
    required fields, taxonomy refusals, provenance and criteria versions.
    `tests/test_behavior_validation_golden.py::...[documents]`: the profile's
    `behavior_validation.yaml` dump, `BehaviorValidationDocument` and
    `ValidationEvidence` JSON schemas, taxonomy problems (example config, retired,
    unapproved), `ProvenanceResolution`/`RegimeAxis`/extra-field refusals,
    `EventPolicy.from_config`, `ValidationContext.for_run`, `EVALUATOR_VERSION`
    and every constant, `mode_labels_predate_fix`, `file_identity`/`file_sha256`/
    `decoder_identity`/`session_content_identity` over fixed bytes, `__all__`.
  - [x] Capture known-answer outputs for eligible/off-map/stale/missing-input
    rows, per-read axis pairing and independent-drive evidence.
    Golden `[rows]` (eligible, `off_map`, `input_stale`, `input_missing`,
    `actual_missing`, profile-stamped tuning and unlisted capture pieces, the
    unstamped refusal, shaped offsets, microsecond clocks), `[per_read]` (paired,
    `unpaired`, `clock_unsupported`, `not_captured`, `no_grid_context`, an
    equidistant tie, all 11 `AxisPairingRefusedError` reasons), `[corpus]` (five
    drives: per-drive metrics, drive bootstrap, qualifying-drive counts, a fixed
    3-train/1-cal/2-test split through `build_evidence`), `[known_answers]` (the
    committed historical/corrected slices under the 2026-09-22 artifact: rows,
    metrics, regimes, synthetic and transient outcomes, 16 model/slice pairs).
    Captured from an untouched `HEAD` worktree (`--write` there); 6 passed there
    and 6 pass on the split tree.
  - [x] Read archived R05.13/B02 decisions; retain `splits.Drive` distinct
    from `PhysicalDrive` and avoid repeating already completed split work.
    `splits.py` is unmodified; the facade still re-exports its 12 names;
    `test_split_drives_stay_distinct_from_physical_drives`.
- [x] **R20.2 Assign owners and dependency direction.**
  - [x] Put validation document types and file/provenance helpers in a leaf
    analysis module; reuse existing hash helpers rather than cloning them.
    `behavior_validation_schema` (imports no family module); file hashes still
    go through `src.artifact_hash.sha256_file_or_none`, bodies verbatim.
  - [x] Put profile exclusion and per-read/axis pairing in an input owner,
    preserving timestamp basis and acquisition separation contracts.
    `behavior_validation_inputs` (leaf); `build_evaluation_frame` and
    `evaluation_decoded_signals` in `behavior_validation_frame` (imports inputs
    only), so the pairing owner does not pull in the preprocessing chain.
  - [x] Put `RowEvaluation`, grading and event projection in an evaluation
    owner; use the existing serving eligibility and persistence implementations.
    `behavior_validation_evaluation`: `RowEvaluation`, `evaluate_rows`,
    `axis_separation_summary`, `EventPolicy`, events, the band-excess persistence
    fit; still `behavior_eligibility.eligibility_reasons` and
    `models.fusion.PersistenceGate`/`BandExcessAccumulator`.
  - [x] Put physical metrics, drive bootstrap, baselines and sensitivity in
    owners downstream of row evaluation. Criteria consume those results.
    `behavior_validation_metrics` (schema, evaluation),
    `behavior_validation_sensitivity` (evaluation, metrics),
    `behavior_validation_criteria` (no family import: it reads `FoldMetrics` /
    `SyntheticSensitivity` / `TransientControlResult` from `behavior_models`).
  - [x] Make evidence construction the orchestration boundary, importing
    schemas, evaluation, metrics and criteria in one direction.
    `behavior_validation_evidence` (`ValidationContext`, `build_evidence`,
    `carry_over_flags`) imports the six others; nothing of the family imports it
    (`test_evidence_orchestration_is_imported_only_by_its_consumers`).
  - [x] Record the final module map before moving bodies; every owner stays
    at analysis layer or uses existing lower-layer shared functionality.
    Map in the close-out; enforced as `DEPENDS_ON`. All eight are
    `src/analysis/*`; `tests/test_layering.py` green (no new package).
- [x] **R20.3 Extract incrementally.**
  - [x] Move schemas/input preparation first, then evaluation/events, metrics,
    sensitivity/criteria and evidence, maintaining direct facade exports.
    Bodies were extracted by script from `HEAD` line ranges, owner by owner in
    that order: all 80 definitions are AST-identical to `HEAD`'s (80 of 80, 0
    differ). The facade is `name as name` re-exports plus `log` and the
    unchanged `__all__` (`test_the_facade_exports_the_owners_object`,
    `test_the_facade_defines_nothing_but_its_logger`).
  - [x] Preserve missing/fresh/eligible/evaluated denominators and refusal
    distinctions; ineligible rows must not become healthy measurements.
    Golden `[rows]`/`[known_answers]` arrays (`present`, `fresh`, `reasons`,
    `evaluated`, `eligible`) and fold denominators; mutation
    `eligible_denominator` (drop `off_map` from `eligible`) fails `rows`,
    `known_answers`.
  - [x] Preserve axis-pairing skew and basis refusals, sample reuse rules and
    actual/expected engineering units.
    Golden `[per_read]` (per-row separations, policy column, reuse: each axis
    read paired once); mutations `pairable_basis` (accept `round`) and
    `pairing_tie_break` (`later`) fail `per_read`.
  - [x] Preserve event persistence/dedupe, band-excess accumulation, reset
    gaps and per-read versus per-grid evaluation behavior.
    Golden event times (`band_excess_events`, `model_events_*`, 70 each),
    fitted persistence limit; mutations `dedupe` (30 -> 29 s) and
    `persistence_fit` fail `corpus`.
  - [x] Preserve bootstrap units/seeds, split/leak checks, baseline fitting
    only on permitted folds, and synthetic detection versus observed fault recall.
    Golden seeds 0/3/11 bootstrap intervals, baselines, synthetic windows;
    mutations `bootstrap_reps`, `synthetic_windows` fail. `splits.py` untouched;
    `test_behavior_validation.py` leak/fold tests green.
  - [x] Preserve passed/failed/insufficient-evidence criteria and publication
    behavior; no new threshold, tolerance or fallback criterion.
    Golden `criteria` (9 cases: passed, failed, insufficient) and the
    `fit_recipe` outcomes; mutations `criteria_coverage`, `taxonomy_downgrade`
    fail `corpus`. `apply_criteria` is AST-identical.
  - [x] Preserve schemas and hashes of meaningful evidence fields; moving
    a class must not require rewriting stored validation documents.
    Every schema type stayed put or moved verbatim; the JSON schemas in the
    golden are identical; `test_behavior_validation_known_answers.py` reads the
    committed `behavior_models_2026-09-22.yaml` unchanged; mutation
    `evaluator_version` fails `documents`, `corpus`.
- [x] **R20.4 Sweep consumers and enforce boundaries.**
  - [x] Migrate internal imports to owners, including refit and evaluation CLI,
    and retain existing split re-exports for compatible callers.
    The 9 non-test importers import owners (split names from `splits`);
    `test_no_library_module_script_or_tool_imports_the_facade` scans `src`,
    `scripts`, `tools`. Tests stay on the facade as compatible callers.
  - [x] Retarget monkeypatches to execution owners and check alias identity.
    `test_a_recipe_without_axis_pairing_is_graded_on_the_grid_unchanged` now
    patches `behavior_validation_inputs.read_model_reads`. Proof it bites: with
    `graded_frame` mutated to read on the grid path, the retargeted test fails
    with its own `AssertionError: a grid-graded recipe read its sessions`; the
    same test patching the facade fails with `FileNotFoundError` from the real
    reader (the facade patch is never called). Alias identity:
    `test_the_facade_exports_the_owners_object` (`is`, 80 + 12 names).
  - [x] Add focused checks against importing the facade from implementations
    or importing evidence orchestration into leaf schema/evaluation modules.
    `tests/test_behavior_validation_boundaries.py` (31 tests): `DEPENDS_ON`,
    consumer/`scripts`/`tools`/upward edges, AST scan with function-level,
    `TYPE_CHECKING`, `try` and relative forms (19 scanner cases), a planted
    evaluation -> evidence edge and a planted facade importer both found.
  - [x] Inspect `scripts/ml_paths.txt` so previously covered moved logic keeps
    its coverage; do not infer a new training run merely from the split.
    No `ml_paths.txt` entry matched `behavior_validation.py` at `HEAD`, and
    `git ls-files -m -o -- <ml_paths>` lists none of this change's files: no
    coverage to keep, no `ml_training` run, no edit.
- [x] **R20.5 Verify and close out.**
  - [x] Run `test_behavior_validation.py`,
    `test_behavior_validation_known_answers.py`, `test_behavior_axis_pairing.py`,
    `test_behavior_regimes.py`, `test_behavior_serving_contracts.py`,
    `test_refit.py`, affected lifecycle and evaluation-script tests.
    47 files (every test importing the module, refit, the scripts, timing
    lookup, lambda delivery, plus layering/CLI snapshot/script imports), slow
    tests included: 1808 passed, 6 skipped.
  - [x] Compare fixed row arrays, refusal reasons, event times, physical metrics,
    independent-drive counts, synthetic outcomes and criterion verdicts.
    The golden (6 scenarios, captured at `HEAD`); 12 of 12 mutations fail it.
  - [x] Compare stored evidence output after normalizing only explicit run
    metadata; preserve contract fingerprints and meaningful provenance.
    Golden evidence dumps drop only `evaluated_at` and `fitted_at`
    (`contract_fingerprint`, `decoder_identity`, `provenance` compared).
    `scripts/evaluate_behavior_models.py --mode historical` over two fixture
    sessions at `HEAD` and on the tree: report JSON identical (154 leaves) after
    dropping `generated_at`/`dirty_paths` and the checkout prefix; exported row
    parquet byte-identical; stdout identical but timestamps. `--mode corrected`
    refuses identically on both (example-config taxonomy).
  - [x] Use an existing refit fixture to verify publication/skip decisions;
    no deployed model refit is required for a behavior-preserving move.
    Golden `fit`/`broad_band` (`tests.builders.behavior_corpus` through
    `fit_recipe`: skipped, reason, candidate and evidence); `test_refit.py`,
    `test_refit_behavior_models_script.py`, `test_refit_event_gating.py` green.
  - [x] Run layering, affected slow/training tiers and shared gates. Replay
    if a served caller is moved, recording row/event parity explicitly.
    `ruff check .`, `ruff format --check .`, `mypy src` (613 files) clean;
    `test_layering.py` green; slow tests in the affected files ran; no
    `ml_training` path changed. No served caller moved: nothing under
    `src/daemon`, `src/behavior` or `src/mcp_server` imports the family, and
    `lambda_delivery`'s lazy `LeakedDriveError` import now names `splits`, the
    same object. No replay needed.
  - [x] Record final owners, compatibility imports, evidence differences,
    tests and commits. Keep any substantive scientific correction separate.
    Close-out below; no scientific correction made. Implementation commit: the
    commit that archives this file.

## Acceptance

Validation has clear input, evaluation, metrics, criteria and evidence owners.
Known answers, eligibility accounting, independent-drive uncertainty, serialized
evidence and publication decisions remain unchanged.

## Close-out record

Implemented 2026-10-15 on `main` over `7988dc28`. Not committed by this run. No
artifact was trained, refit, restamped or deployed; nothing touched the Pi or a
live daemon.

**Module map** (`behavior_validation.py` was 2,204 lines; it is now a 260-line
facade, mostly its docstring and re-exports):

| Module | Lines | Imports (family) | Owns |
|---|---|---|---|
| `behavior_validation_schema` | 244 | none | document types, loader, `EVALUATOR_VERSION`, mode-label cut-off, file/decoder/session identity |
| `behavior_validation_inputs` | 475 | none | profile stamps and refusals, per-read pairing, `SessionReads`, `graded_frame` |
| `behavior_validation_frame` | 120 | inputs | `build_evaluation_frame`, `evaluation_decoded_signals` |
| `behavior_validation_evaluation` | 485 | schema, inputs | `RowEvaluation`, `evaluate_rows`, `EventPolicy`, events, persistence fit |
| `behavior_validation_metrics` | 367 | schema, evaluation | fold metrics, baselines, envelope, exposure, bootstrap, breakdowns, regimes |
| `behavior_validation_sensitivity` | 195 | evaluation, metrics | synthetic faults, transient controls, windows |
| `behavior_validation_criteria` | 147 | none | `apply_criteria` |
| `behavior_validation_evidence` | 244 | all six above | `ValidationContext`, `build_evidence`, `carry_over_flags` |

**Compatibility.** `src.analysis.behavior_validation` keeps every name it
defined (80, private ones included) and the 12 `splits` re-exports as the
owners' objects, the same 58-name `__all__`, and `log` (same logger name, still
unused). Library code, scripts and tools import owners. What changed for a
caller: `__module__` of the 80 definitions (and the class path in tracebacks)
names the owner; the facade no longer carries the libraries it used to import
(`np`, `pd`, `yaml`, `serving_spec`, `PersistenceGate`, ...); a monkeypatch on
the facade no longer reaches the code (one test retargeted, above).

**Evidence differences.** None. The golden and both CLI runs are identical
apart from run metadata (`evaluated_at`, `fitted_at`, `generated_at`,
`dirty_paths`, the checkout path). Stored documents need no rewrite.

**Tests.** New: `tests/test_behavior_validation_golden.py` (6, fixture
`tests/fixtures/behavior_validation_golden.json`, 494 KB, regenerated only with
`--write`), `tests/test_behavior_validation_boundaries.py` (31). Golden
mutations, each caught: event dedupe, eligible denominator, bootstrap reps,
envelope minimum rows, pairable basis, pairing tie-break, evaluator version,
synthetic windows, coverage criterion, taxonomy failure prefix, profile stamp,
persistence limit.
