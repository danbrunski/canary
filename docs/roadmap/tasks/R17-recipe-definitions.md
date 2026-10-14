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

- [ ] **R17.1 Capture the catalog contract.**
  - [ ] Inventory types, catalog/building helpers, recipe constants, imports
    and monkeypatches across refit, inventory scripts, validation and profile modules.
  - [ ] Record shared/profile recipe order, names and contract fingerprints
    from `tests/fixtures/recipe_catalog_golden.json`.
  - [ ] Pin per-recipe overrides versus family defaults, serving specs,
    persistence configuration and profile-shadow refusal.
- [ ] **R17.2 Extract recipe contracts and builders.**
  - [ ] Move `_RecipeBase`, surface recipe types and `Recipe` into the leaf
    contract owner, keeping defaults, validation and field order unchanged.
  - [ ] Move definition-side constants and builders with their dependencies;
    give catalog-facing builders intentional public names.
  - [ ] Preserve old builder aliases where callers still use them. Do not
    rewrite `**fields` behavior or default precedence during the extraction.
  - [ ] Decide where serving-spec/fingerprint helpers belong by their inputs;
    move definition-only helpers if needed, keep fitting-dependent ones in refit.
  - [ ] Ensure the contract module imports neither refit nor a catalog loader.
- [ ] **R17.3 Move catalog construction.**
  - [ ] Move `shared_recipes` and its family construction to the shared catalog.
  - [ ] Migrate the Xterra module from refit/private imports to the builder API.
    Preserve DID-specific facts in that vehicle module.
  - [ ] Point `recipes/__init__.py` type annotations at the recipe contract,
    preserving profile-module resolution and unknown-profile behavior.
  - [ ] Keep `default_recipes`' required profile argument, shared-then-profile
    order, unknown-profile fallback and refusal of duplicate names.
  - [ ] Keep refit's old names as direct aliases; do not duplicate dataclasses
    or eagerly import vehicle modules into the contract owner.
- [ ] **R17.4 Sweep and enforce ownership.**
  - [ ] Migrate inventory, tools and internal refit/validation type imports
    to the owning module; audit old-path tests and monkeypatches.
  - [ ] Add a focused architecture test forbidding contract/catalog modules
    from importing the fitting engine, including type-only and local imports.
  - [ ] Verify profile modules load independently of a fitting invocation
    and the definition/execution cycle is removed.
  - [ ] Inspect `scripts/ml_paths.txt`; preserve gating for moved code where
    the previous path was covered, without broadening tiers gratuitously.
- [ ] **R17.5 Verify and close out.**
  - [ ] Run `test_recipe_catalog_golden.py`, `test_profile_recipes.py`,
    `test_refit.py`, `test_refit_event_gating.py`,
    `test_refit_behavior_models_script.py` and affected serving-contract tests.
  - [ ] Require identical names, order, fields, serving specs and fingerprints;
    do not regenerate the golden fixture to accept an accidental change.
  - [ ] Run a fixed-input fit/dry-run where existing tests provide fixtures;
    compare skip/publication decisions and model contract fields.
  - [ ] Run layering and affected slow tests/shared gates. No new refit,
    artifact restamp or Pi publication is necessary for a pure extraction.
  - [ ] Record module map, compatibility aliases, catalog evidence and commits.

## Acceptance

Vehicle/shared recipes depend on definitions, never on the fitting engine.
The engine consumes the same ordered recipes; golden fingerprints and
artifact-serving contracts do not change.
