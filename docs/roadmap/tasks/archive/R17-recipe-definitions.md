# R17 — Recipe definitions independent of fitting

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size M. No prerequisite; land before R20 changes overlapping refit imports.

## Problem and target

`refit.py` owns recipe dataclasses, catalog defaults, builders, shared recipes
and fitting. The vehicle recipe module imports `Recipe`, `Surface2DRecipe`,
`TOTAL_TRIM_STEADY_RATES` and private builders from refit. `default_recipes`
then loads those vehicle recipe modules. Definition depends on execution and
execution depends on definition.

Create a recipe contract/builder module under `analysis`, plus a shared catalog
under `analysis/recipes/`. Both shared and vehicle catalogs import contracts;
refit consumes catalogs. Retain the public `refit` recipe exports and
`default_recipes(profile)` signature. Recipes remain typed Python.

## Implementation checklist

- [x] **R17.1 Capture the catalog contract.**
  - [x] Inventory types, catalog/building helpers, recipe constants, imports
    and monkeypatches across refit, inventory scripts, validation and profile modules.
    `rg` over `src scripts tools tests`: importers of `refit` were
    `behavior_input_inventory` (`RPM_COLUMN`, `Recipe`, `recipe_serving_spec`),
    `recipes/__init__` (type only), `recipes/nissan_xterra_2008` (`Recipe`,
    `Surface2DRecipe`, `TOTAL_TRIM_STEADY_RATES`, `_sigma`, `_surface_1d`,
    `_surface_2d`), four scripts and 16 test files. Monkeypatches of
    recipe names: none (`test_profile_recipes` patches `importlib.util.find_spec`
    and `sys.modules`; `test_refit_behavior_models_script` patches the script's
    own `refit_all`). No `behavior_validation` or other `src` module imported
    recipe types from refit.
  - [x] Record shared/profile recipe order, names and contract fingerprints
    from `tests/fixtures/recipe_catalog_golden.json`.
    Golden test green before the move (72 passed with the refit files) and
    unmodified fixture green after. Fixed-input baseline (`r17_baseline.py`,
    scratchpad): 24 Xterra / 18 shared / 18 unknown-profile recipes, each with
    name, type, fingerprint, `asdict` repr, serving-spec repr and fit columns;
    captured at HEAD, compared after: 0 diffs.
  - [x] Pin per-recipe overrides versus family defaults, serving specs,
    persistence configuration and profile-shadow refusal.
    Existing: `test_a_minimum_a_recipe_declares_wins_over_the_family_default`,
    `test_a_profile_recipe_may_not_shadow_a_shared_one`. New:
    `test_every_recipes_serving_spec_is_unchanged` against
    `tests/fixtures/recipe_serving_specs_golden.json` (24 specs captured at
    HEAD before any edit), `test_event_persistence_configuration_is_unchanged`
    (STFT x3 and the AFR bank balance persisted, LTFT x3 not).
- [x] **R17.2 Extract recipe contracts and builders.**
  - [x] Move `_RecipeBase`, surface recipe types and `Recipe` into the leaf
    contract owner, keeping defaults, validation and field order unchanged.
    `src/analysis/recipe_contract.py`; bodies copied by line range, golden
    `asdict` reprs (field order and defaults) unchanged.
  - [x] Move definition-side constants and builders with their dependencies;
    give catalog-facing builders intentional public names.
    `src/analysis/recipe_builders.py`: `CATALOG_1D/2D_DEFAULTS`,
    `WARMUP_1D_FIELDS`, `RPM_LOAD_2D_FIELDS`, `TOTAL_TRIM_STEADY_RATES`,
    `surface_1d_recipe`, `surface_2d_recipe`, `warmup_1d_recipe`,
    `rpm_load_2d_recipe`, `sigma_deviation`. Catalog-only constants
    (`WARM_IDLE_*`, `BANK_BALANCE_MODES`, `STFT_BANK_BALANCE_PERSISTENCE`,
    `TOTAL_TRIM_UNIT`) moved to `recipes/shared.py` with the recipes that use them.
    `WARMUP_CONTEXT_*` and the 4.6 strategy functions stay in refit: they decide
    how fitting treats a corpus, not what a recipe is.
  - [x] Preserve old builder aliases where callers still use them. Do not
    rewrite `**fields` behavior or default precedence during the extraction.
    `refit._surface_1d/_surface_2d/_warmup_1d/_rpm_load_2d/_sigma/_recipe_columns`
    are direct assignments; builder bodies unchanged
    (`test_refit_keeps_the_private_builder_aliases`).
  - [x] Decide where serving-spec/fingerprint helpers belong by their inputs;
    move definition-only helpers if needed, keep fitting-dependent ones in refit.
    `recipe_serving_spec`, `recipe_contract_fingerprint`, `recipe_columns` take
    only a recipe, so they moved to `recipe_contract`. `legacy_reproduction_spec`
    takes a fitted model and artifact history, so it stays in refit.
  - [x] Ensure the contract module imports neither refit nor a catalog loader.
    `test_definition_modules_never_import_the_fitting_engine`,
    `test_the_contract_and_builders_import_no_catalog`, and subprocess check
    `test_the_contract_and_builders_load_no_catalog`.
- [x] **R17.3 Move catalog construction.**
  - [x] Move `shared_recipes` and its family construction to the shared catalog.
    `src/analysis/recipes/shared.py` (bodies copied by line range; names
    switched to the public builders).
  - [x] Migrate the Xterra module from refit/private imports to the builder API.
    Preserve DID-specific facts in that vehicle module.
    `recipes/nissan_xterra_2008.py` imports `recipe_builders` and
    `recipe_contract`; its DID facts, constants and persistence are untouched.
  - [x] Point `recipes/__init__.py` type annotations at the recipe contract,
    preserving profile-module resolution and unknown-profile behavior.
    `TYPE_CHECKING` import is now `recipe_contract.Recipe`. Added
    `NON_PROFILE_MODULES = {catalog, shared}`: a profile whose slug maps to one
    of the package's catalog modules gets `[]`, exactly as an unknown profile did,
    instead of a `TypeError` from importing it as a vehicle module
    (`test_a_profile_cannot_name_a_catalog_module`).
  - [x] Keep `default_recipes`' required profile argument, shared-then-profile
    order, unknown-profile fallback and refusal of duplicate names.
    `recipes/catalog.py::default_recipes` (verbatim body, module-level import of
    `profile_recipes`); `test_profile_recipes.py` 6 tests unchanged in substance.
  - [x] Keep refit's old names as direct aliases; do not duplicate dataclasses
    or eagerly import vehicle modules into the contract owner.
    `test_refit_names_are_the_owners_objects` (19 names, `is` identity),
    `test_the_recipe_types_are_defined_once`; vehicle modules load only through
    `profile_recipes` at call time.
- [x] **R17.4 Sweep and enforce ownership.**
  - [x] Migrate inventory, tools and internal refit/validation type imports
    to the owning module; audit old-path tests and monkeypatches.
    Migrated: `behavior_input_inventory`, `scripts/inventory_behavior_inputs`,
    `scripts/evaluate_behavior_models`, `scripts/refit_behavior_models`
    (`Recipe`, `default_recipes`), `tests/builders.py`,
    `tests/test_recipe_catalog_golden.py` (public builders instead of
    `refit._surface_*`), `tests/test_profile_recipes.py`. Left on the refit
    facade on purpose: `scripts/inventory_cold_starts` (`WARMUP_CONTEXT_*`,
    `choose_warmup_confounding_strategy`, which refit still owns) and the other
    behavior tests, which exercise the engine's public surface. No monkeypatch
    targeted a moved name. All four scripts still `--help` cleanly.
  - [x] Add a focused architecture test forbidding contract/catalog modules
    from importing the fitting engine, including type-only and local imports.
    `tests/test_recipe_boundaries.py` (AST scan; top-level, function, `TYPE_CHECKING`,
    `try`, `from src.analysis import refit` and the relative forms
    `from ..refit import X`, `from .. import refit`, `from . import shared`,
    resolved against the file's package, proven by
    `test_the_scanner_finds_every_form_of_the_forbidden_import`; the HEAD copy
    of the Xterra module is reported as `-> src.analysis.refit`).
  - [x] Verify profile modules load independently of a fitting invocation
    and the definition/execution cycle is removed.
    `test_loading_a_definition_never_loads_the_fitting_engine` (6 subprocess
    cases incl. the Xterra module and `profile_recipes(XTERRA)`: `src.analysis.refit`
    absent from `sys.modules`).
  - [x] Inspect `scripts/ml_paths.txt`; preserve gating for moved code where
    the previous path was covered, without broadening tiers gratuitously.
    Neither `src/analysis/` nor `refit` is listed; no `ml_training` test imports
    refit or recipes. Nothing removed from the training tier, nothing added.
- [x] **R17.5 Verify and close out.**
  - [x] Run `test_recipe_catalog_golden.py`, `test_profile_recipes.py`,
    `test_refit.py`, `test_refit_event_gating.py`,
    `test_refit_behavior_models_script.py` and affected serving-contract tests.
    `pytest -n 4` on those plus 16 more refit/recipe-importing files
    (serving contracts, lifecycle, validation, axis pairing, trim contracts,
    bank balance persistence, evaluate script, canaryctl, dashboard API,
    trainability, CLI snapshot) and the new boundary test: 517 passed.
  - [x] Require identical names, order, fields, serving specs and fingerprints;
    do not regenerate the golden fixture to accept an accidental change.
    `recipe_catalog_golden.json` not modified (`git status`); golden test and the
    serving-spec pin green.
  - [x] Run a fixed-input fit/dry-run where existing tests provide fixtures;
    compare skip/publication decisions and model contract fields.
    `r17_baseline.py`: seeded 3000-row, 5-session frame through `refit_all` over
    the 24-recipe Xterra catalog: 10 fitted, 14 skipped with reasons, every
    skip reason, every published `BehaviorModels` field (only `evaluated_at`
    scrubbed) identical before and after: 0 diffs. Existing fit tests
    (`test_refit`, `test_behavior_lifecycle`, `test_behavior_validation`) green.
  - [x] Run layering and affected slow tests/shared gates. No new refit,
    artifact restamp or Pi publication is necessary for a pure extraction.
    `tests/test_layering.py` 9 passed (no `OWED` entries); `-m slow` on 12
    affected files: 28 passed. `ruff check .`, `ruff format --check .`,
    `mypy src`: clean. No refit, restamp or publication was run.
  - [x] Record module map, compatibility aliases, catalog evidence and commits.
    Implementation commit: the commit that archives this file. Close-out below.

## Acceptance

Vehicle/shared recipes depend on definitions, never on the fitting engine.
The engine consumes the same ordered recipes; golden fingerprints and
artifact-serving contracts do not change.

## Close-out record (2026-10-14)

Module map:

| Module | Owns |
|---|---|
| `src/analysis/recipe_contract.py` | `_RecipeBase`, `Surface1DRecipe`, `Surface2DRecipe`, `Recipe`, `RPM_COLUMN`, `recipe_columns`, `recipe_serving_spec`, `recipe_contract_fingerprint` (leaf) |
| `src/analysis/recipe_builders.py` | family defaults and fields, `TOTAL_TRIM_STEADY_RATES`, `surface_1d_recipe`, `surface_2d_recipe`, `warmup_1d_recipe`, `rpm_load_2d_recipe`, `sigma_deviation` |
| `src/analysis/recipes/shared.py` | `shared_recipes`, warm-idle / bank-balance / total-trim catalog constants |
| `src/analysis/recipes/catalog.py` | `default_recipes(profile)` |
| `src/analysis/recipes/nissan_xterra_2008.py` | the Xterra's DID surfaces (now on the builder API) |
| `src/analysis/refit.py` | fitting, publication, `legacy_reproduction_spec`, warmup-confounding strategy; 2048 to 1348 lines |

Compatibility: every moved public name remains importable from
`src.analysis.refit` as the owner's object (19 names plus the six private
builder aliases), asserted by `tests/test_recipe_boundaries.py`.

Importers of `src.analysis.refit` before the move: `behavior_input_inventory`,
`recipes/__init__` (type), `recipes/nissan_xterra_2008`, four scripts, 16 test
files. After: the first three and three scripts import owners; the rest use the
facade deliberately.

Fixture differences: none (catalog golden untouched; 24 serving specs identical;
24-recipe fixed-input refit identical, 10 fitted / 14 skipped).

Behaviour: unchanged, with one hardening: a profile slug equal to `catalog` or
`shared` returns no vehicle recipes (it would otherwise have imported the
package's own catalog module as a vehicle). Recipe dataclasses now report
`__module__ == "src.analysis.recipe_contract"`; no persisted or pickled
reference named the old path. `docs/daemon-architecture.md` now points
"add a recipe" at the new homes.

Also changed, not literally asked for:
- `refit.__all__` gained `CATALOG_1D_DEFAULTS`, `CATALOG_2D_DEFAULTS`,
  `WARMUP_1D_FIELDS`, `RPM_LOAD_2D_FIELDS`, `TOTAL_TRIM_STEADY_RATES`,
  `TOTAL_TRIM_UNIT`, `BANK_BALANCE_MODES`, `STFT_BANK_BALANCE_PERSISTENCE`,
  `WARM_IDLE_STEADY_RATES`, `WARM_IDLE_LOCAL_RANGES` (compatibility re-exports).
- `refit._RecipeBase` and the pass-through names `CLOSED_LOOP_FUEL_STATES`,
  `STFT_BANK_BALANCE_SMOOTHING_SECONDS`, `VE_AMBIENT_CHANNEL`,
  `VE_MANIFOLD_CHANNEL` and `_BANK_BALANCE_NOTES` are no longer attributes of
  refit; nothing imports them from there.
- The scanner in `tests/test_recipe_boundaries.py` resolves relative imports, and
  `test_every_recipes_module_is_a_vehicle_or_declared_not_one` requires every
  `recipes/*.py` to define `recipes()` or be in `NON_PROFILE_MODULES`.
