# R21 — Configuration schemas behind compatible imports

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size M–L; lower priority with a broad import surface. Land after overlapping
R13–R20 moves, so one schema migration does not collide with many caller edits.

## Problem and target

`src/config.py` was 1,740 lines, mostly strict Pydantic schemas and validators.
The review counted 112 importing source modules. Its size is less urgent than
the IO and orchestration issues, but vehicle/hardware, collection, preprocessing,
models/alerts, runtime and root validation have useful domain boundaries.

Convert the module into a `src/config/` package with a compatible `__init__.py`
facade. Keep common strict-model/validation primitives and independent domain
schemas below root `CanaryConfig` assembly. Root-only cross-domain validation
stays with the root schema; config remains layer 0 and never imports runtime
or vehicle-profile implementations from higher layers.

## Implementation checklist

- [ ] **R21.1 Inventory the actual configuration surface.**
  - [ ] List public/private imports, default constants, inheritance, validators,
    path helpers, annotations, monkeypatches and serialized/pickled uses.
  - [ ] Record canonical `model_dump(mode="json")`, JSON schema and defaults
    for examples and the existing test configurations; use sanitized fixtures.
  - [ ] Record strict unknown-key rejection, retired aliases/conflicts,
    path rooting, explicitly provided fields and profile scope behavior.
  - [ ] Capture cold import behavior: config must not pull in torch, CAN IO,
    daemon, dashboard or profile lookup as a consequence of the split.
- [ ] **R21.2 Design schema dependencies.**
  - [ ] Put `_Strict`, reusable constants and foundational validation in a
    base module with no imports from the facade or root schema.
  - [ ] Assign vehicle/hardware, collection, preprocessing, model/alert and
    runtime/auxiliary schemas to cohesive domain files by actual references.
  - [ ] Preserve the shared `_SequenceTrainingConfig` inheritance and existing
    LSTM/TCN differences; do not create parallel schema copies.
  - [ ] Keep `CanaryConfig`, YAML loading and cross-domain validators in root
    assembly. Leaf schemas cannot import the root for annotations/convenience.
  - [ ] Assign path rebasing/scoping helpers so their dependency is rootward
    only; retain introspection of `Path` fields rather than a hand-kept list.
  - [ ] Record the dependency map and old-name export list before conversion.
- [ ] **R21.3 Convert and preserve compatibility.**
  - [ ] Replace `config.py` with the package in one coherent change; never
    leave both definitions as competing owners of `src.config`.
  - [ ] Move bodies with field names/order, defaults, aliases, validator
    ordering, messages, extra-forbid behavior and schema titles preserved.
  - [ ] Re-export all existing imported names, including compatibility private
    names where required; old and new paths must refer to the same classes.
  - [ ] Preserve `PrivateAttr` scope state, `_scope_source`, `scoped_profile`,
    model copying and profile rescoping/hot reload.
  - [ ] Preserve `model_fields_set` distinctions: implicit runtime paths root
    under state_dir while explicit overrides retain current behavior.
  - [ ] Preserve broadcast-timeout/session-gap and sequence-cadence/persistence
    cross-domain refusals; do not weaken them to break a dependency.
  - [ ] Verify annotation resolution and any spawn/pickle compatibility for
    existing `src.config` objects; avoid unsupported blanket `__module__` rewrites.
- [ ] **R21.4 Migrate internals and enforce direction.**
  - [ ] Make config submodules import owners rather than `src.config` facade.
    External callers may keep the compatible public import path.
  - [ ] Sweep tests, scripts and runtime patch targets; retarget only where
    binding ownership moved, preserving original validation coverage.
  - [ ] Verify the existing `config: 0` layer still represents the package;
    add no upward dependencies or new layering exemptions.
  - [ ] Add focused checks preventing leaf schema imports of root/facade or
    runtime domains, including function-level and type-only imports.
  - [ ] Update any file lists/tooling that assume config is a single `.py` file;
    inspect ML gate coverage for the now-package path.
- [ ] **R21.5 Verify and close out.**
  - [ ] Run `test_config.py`, `test_config_hot_reload.py`,
    `test_sequence_config_base.py`, `test_state_dir.py`,
    `test_resample_config_construction.py`, profile-scoping and layering tests.
  - [ ] Compare default/example model dumps and normalized JSON schemas;
    investigate changes in defaults, aliases, required fields or validators.
  - [ ] Check accepted/rejected retired keys, alias collisions, invalid root
    timing combinations and typo rejection against the baseline.
  - [ ] Verify path rebasing includes every supported field, preserves explicit
    overrides, rejects unsupported path shapes and keeps runtime data out of git.
  - [ ] Verify cold imports and relevant spawned-worker round trips; record
    any import cost without claiming a performance gain from fewer file lines.
  - [ ] Run the non-slow backend suite, affected slow/training tiers as needed,
    and shared gates; replay a fixed session if root/runtime assembly changed.
  - [ ] Record final schemas/exports, dump/schema parity, dependency evidence,
    test results and commits. No YAML or artifact migration for a pure move.

## Acceptance

Configuration domains have clear owners behind `src.config`. Existing YAML,
model dumps, validation errors, scoped state paths and public class imports
remain compatible; foundational schemas acquire no higher-layer dependencies.
