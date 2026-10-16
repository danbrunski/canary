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

- [x] **R21.1 Inventory the actual configuration surface.**
  - [x] List public/private imports, default constants, inheritance, validators,
    path helpers, annotations, monkeypatches and serialized/pickled uses.
    Recorded at `HEAD` (24cd1a50) before any code moved, by AST over `src`,
    `scripts`, `tools`, `tests`, `hardware`, `docs`: 129 `src/` files, 17
    `scripts/`, 14 `tools/` and 204 `tests/` files import `src.config` (every
    one as `from src.config import …`; the sole module-attribute use is
    `tests/test_repo_tree_isolation.py`). 42 of the 54 names the file defined
    are imported from outside. Private names imported: `_daemon_path_fields`
    (`tests/configs.py`, `test_state_dir.py`, `test_repo_tree_isolation.py`),
    `_SequenceTrainingConfig` (`test_sequence_config_base.py`). Constants:
    `DEFAULT_BROADCAST_STALE_TIMEOUT_SECONDS` (4 files),
    `DEFAULT_PRIORITY_CHANNELS` (`collection/obd_scheduler/spec.py`),
    `REWRITE_LEAD_SECONDS`, `validate_broadcast_stale_timeout` (3),
    `DAEMON_STATE_LAYOUT` / `DAEMON_TRACKED_PATHS` (`test_state_dir.py`).
    Inheritance: every model is a `_Strict`; `LSTMAutoencoderConfig` and
    `TCNAutoencoderConfig` extend `_SequenceTrainingConfig`. 39 model classes,
    11 model validators (3 `before`), 2 field validators. Path helpers:
    `_daemon_path_fields`/`_mentions_path`/`rebase_state_paths`, introspecting
    `DaemonConfig`'s `Path` fields (25). Annotations: all fields are strings
    under `from __future__ import annotations`, resolved in the defining module.
    Monkeypatches: two `monkeypatch.setattr(config_module, "DaemonConfig", …)`
    in `test_repo_tree_isolation.py`; a sweep for `'src.config.` / `"src.config`
    patch strings in `tests`, `scripts`, `tools`, `src` found nothing else.
    Pickled uses: the live `CanaryConfig` crosses a spawn pipe to the scoring
    child (`daemon/inference_worker`), the feature process (`features/live`),
    the evaluation worker pool (`models/evaluation_workers`) and the analysis
    children; no persisted artifact holds a config class (joblib/torch payloads
    carry `manifest.to_dict()`, the feature cache pickles frames, the score
    cache pickles scorer returns) and `src` hashes no `__module__`,
    `__qualname__` or `model_json_schema()` of a config class.
  - [x] Record canonical `model_dump(mode="json")`, JSON schema and defaults
    for examples and the existing test configurations; use sanitized fixtures.
    `tests/fixtures/config_golden.json` (798 KB) from `tests/test_config_golden.py
    --write`: 13 configurations (the four `canary*.example.yaml` plus nine that
    `tests.configs` builds: minimal, daemon, three `isolated_config` bases,
    scoped and unscoped daemon test configs, single-vehicle example, scoped
    example), each as `model_dump(mode="json")`, `exclude_unset` dump and
    per-section `model_fields_set`; 39 class records (ordered fields with
    annotation, default, aliases, constraints; validators in order; private
    attributes; methods; `model_config`; defaults dump); the root JSON schema
    (validation and serialization) and a hash of each class's own schema.
    The checkout root is written `<repo>`; nothing else is normalised and no
    VIN, token or session path is in it. Regenerated in a clean `git worktree`
    at `HEAD`: byte-identical to the committed fixture.
  - [x] Record strict unknown-key rejection, retired aliases/conflicts,
    path rooting, explicitly provided fields and profile scope behavior.
    Golden sections `strict` (a typo key at each of the example's 32 mapping
    positions, plus one on each of the 39 classes), `cases` (116: `gear_ratios` retired, the
    British tire key alone and with both, driveline evidence, the retired
    `persist_decoded_confidence` with its log record and logger name, the
    broadcast floor on 11 values, the IMU/HAT-ADC/alerts/forest validators,
    the 5 timeout-vs-gap and 10 cadence-vs-window root combinations, required
    fields), `paths` (13 `state_dir` rooting cases including explicit-equal-to-
    default, explicit overlay/cards, a re-validated dump and `model_copy`; 8
    `rebase_state_paths` cases including refusals), `scope` (unscoped refusal
    text, a scoped config, `model_copy`/`deepcopy`/pickle at protocols 4 and 5,
    `rescope_like`, idempotence and the different-profile refusal).
  - [x] Capture cold import behavior: config must not pull in torch, CAN IO,
    daemon, dashboard or profile lookup as a consequence of the split.
    Golden `imports` (fresh interpreter, `import src.config`): at `HEAD` it
    loads `src`, `src.config`, `src.lsm6dsox_settings`, `src.paths` and the
    third-party `pydantic`, `pydantic_core`, `annotated_types`,
    `typing_extensions`, `typing_inspection`, `yaml`. After the split only
    `src.config.*` is added; `test_a_fresh_interpreter_loads_nothing_new_for_
    import_src_config` asserts the third-party set is no larger, nothing heavy
    (torch, numpy, pandas, can, serial, fastapi, uvicorn, duckdb, sklearn) and
    no other `src` module loads.
- [x] **R21.2 Design schema dependencies.**
  - [x] Put `_Strict`, reusable constants and foundational validation in a
    base module with no imports from the facade or root schema.
    `src/config/base.py`: `_Strict`, the broadcast-timeout default/floor/
    re-write lead and `validate_broadcast_stale_timeout` (imported by
    `storage` and `preprocessing`), `DEFAULT_PRIORITY_CHANNELS` (imported by
    the OBD scheduler). Imports `math`, `typing`, `pydantic` only.
  - [x] Assign vehicle/hardware, collection, preprocessing, model/alert and
    runtime/auxiliary schemas to cohesive domain files by actual references.
    `vehicle`, `hardware`, `collection`, `preprocessing`, `models` (models and
    alerts: detectors, fusion, roles, emission policy), `daemon` (dashboard,
    daemon and the state-path tables), `auxiliary` (the eleven small sections).
    An AST reference pass over `HEAD` found that the only cross-domain
    reference among them is `_Strict`; each section imports `base` and nothing
    else of the package.
  - [x] Preserve the shared `_SequenceTrainingConfig` inheritance and existing
    LSTM/TCN differences; do not create parallel schema copies.
    All three stay together in `models.py`; `test_sequence_config_base.py`
    green, golden `mro` and schema hashes equal, and mutation
    `tcn_detached_from_shared_base` fails 19 tests.
  - [x] Keep `CanaryConfig`, YAML loading and cross-domain validators in root
    assembly. Leaf schemas cannot import the root for annotations/convenience.
    `root.py` holds `CanaryConfig`, `from_yaml`, the `state_dir` rooting of the
    paths outside `daemon`, the broadcast-vs-session-gap and
    cadence-vs-persistence validators and the private scope state. No section
    imports it (`test_section_schemas_never_import_the_root_or_the_scoping_
    helpers`).
  - [x] Assign path rebasing/scoping helpers so their dependency is rootward
    only; retain introspection of `Path` fields rather than a hand-kept list.
    `scoping.py` imports `daemon` and `root` and nothing imports it but the
    facade. `_daemon_path_fields` still reads `DaemonConfig.model_fields`
    (golden `paths.path_fields`, 25 names; `test_repo_tree_isolation.py` still
    refuses a `list[Path]` field and rebases `Path | None`).
  - [x] Record the dependency map and old-name export list before conversion.
    The old-name list (54 names) was taken from `HEAD` by AST before moving
    code and is `OLD_NAMES` in `tests/test_config_boundaries.py`; the map is
    `DEPENDS_ON` there and in the close-out below.
- [x] **R21.3 Convert and preserve compatibility.**
  - [x] Replace `config.py` with the package in one coherent change; never
    leave both definitions as competing owners of `src.config`.
    `src/config.py` deleted and `src/config/` created in one step;
    `test_the_single_file_module_is_gone_and_the_package_owns_the_name`;
    mutation `single_file_coexists` fails it.
  - [x] Move bodies with field names/order, defaults, aliases, validator
    ordering, messages, extra-forbid behavior and schema titles preserved.
    Bodies were sliced from `git show HEAD:src/config.py` by definition. Of the
    54 top-level definitions 53 are AST-identical (`ast.dump`) and
    source-text-identical to `HEAD`; the 54th is `log`, which is
    `getLogger("src.config")` where it was `getLogger(__name__)`, so the
    logger name is unchanged. A bag-of-lines comparison finds 15 `HEAD` lines
    absent from the package (module docstring, imports, `log`, the broadcast
    intro comment, reworded to say the constants live in `base`) and nothing
    else. Golden: all 7 data sections equal.
  - [x] Re-export all existing imported names, including compatibility private
    names where required; old and new paths must refer to the same classes.
    `src/config/__init__.py` re-exports all 54 as `X as X`; it defines
    nothing (`test_the_facade_defines_nothing_…`);
    `test_the_facade_exports_the_owners_object_for_every_old_name`,
    `test_each_old_name_is_defined_in_exactly_one_module`,
    `test_old_and_new_paths_name_the_same_classes`. No `__all__`, as at `HEAD`.
  - [x] Preserve `PrivateAttr` scope state, `_scope_source`, `scoped_profile`,
    model copying and profile rescoping/hot reload.
    Golden `scope` (private attributes, `model_copy`, `model_copy(update=…)`,
    `deepcopy`, pickle 4/5, `rescope_like`, a re-validated dump is unscoped,
    `_scoped_profile` in YAML refused); `test_config_hot_reload.py`,
    `test_featurization_profile_scope.py`, `test_state_dir.py` green;
    mutations `scoped_profile_default_changed`, `scoped_profile_property_dropped`.
  - [x] Preserve `model_fields_set` distinctions: implicit runtime paths root
    under state_dir while explicit overrides retain current behavior.
    Golden `paths.rooting` and per-section `fields_set` (including the quirk
    that `calibration_dir` is not rooted by `state_dir`, and that a value
    explicitly equal to its default stays explicit); mutations
    `daemon_rooting_ignores_explicit`, `root_rooting_ignores_explicit_cards`,
    `state_layout_entry_changed` fail it.
  - [x] Preserve broadcast-timeout/session-gap and sequence-cadence/persistence
    cross-domain refusals; do not weaken them to break a dependency.
    Both stay in `root.py`; golden `cases` `root:broadcast_vs_gap:*` (5) and
    `root:sequence_cadence:*` (10, on both sides of each boundary); mutations
    `root_timeout_vs_gap_weakened`, `root_cadence_weakened`,
    `root_cadence_dropped` each fail it.
  - [x] Verify annotation resolution and any spawn/pickle compatibility for
    existing `src.config` objects; avoid unsupported blanket `__module__` rewrites.
    No `__module__` is rewritten; classes report their owner.
    `test_every_model_resolves_its_annotations_in_the_module_that_owns_it`
    (39 models complete, `get_type_hints` resolves). A pickle written at `HEAD`
    (scoped example config, 12,440 bytes) loads on the new tree, equal
    `model_dump`, `scoped_profile` and `_scope_source` intact (pickle by
    reference resolves `src.config.CanaryConfig` through the facade); a new
    pickle names `src.config.root` etc. and is 12,642 bytes (+1.6 %);
    `test_a_spawned_interpreter_unpickles_a_scoped_config`; `test_inference_
    worker.py`, `test_feature_workers.py`, `test_evaluate_detectors_script.py`
    (spawned pools) green.
- [x] **R21.4 Migrate internals and enforce direction.**
  - [x] Make config submodules import owners rather than `src.config` facade.
    External callers may keep the compatible public import path.
    Every package module imports `src.config.<owner>`; none imports
    `src.config` (`test_no_module_inside_the_package_imports_the_facade`).
    The 129 + 17 + 14 + 204 external importers are untouched.
  - [x] Sweep tests, scripts and runtime patch targets; retarget only where
    binding ownership moved, preserving original validation coverage.
    One patch target moved: `test_path_or_none_fields_are_rebased_and_other_
    path_shapes_refused` now replaces `DaemonConfig` in `src.config.scoping`,
    where `_daemon_path_fields` reads it. Proof it bites: with the old target
    (the facade) the test fails with `AssertionError: assert {'extra_log',
    'unset_log'} <= {…}` because the helper kept the real class; retargeted it
    passes, and mutation `path_or_none_not_rebased` (the `Path | None` branch
    removed) fails the file. No other test, script or tool patches a config
    name.
  - [x] Verify the existing `config: 0` layer still represents the package;
    add no upward dependencies or new layering exemptions.
    `tests/test_layering.py` green with `"config": 0` unchanged and `OWED`,
    `OWED_PAIRS` empty, `STANDING` untouched. The package imports only
    `src.paths` and `src.lsm6dsox_settings` (both layer 0)
    (`test_the_package_imports_no_higher_layer_and_nothing_heavy`).
  - [x] Add focused checks preventing leaf schema imports of root/facade or
    runtime domains, including function-level and type-only imports.
    `tests/test_config_boundaries.py` (52 tests): the `DEPENDS_ON` map, an AST
    scan of every import form (module level, function, class, `TYPE_CHECKING`,
    `try`, absolute and relative, `from src import config`, `from .. import
    config`), an external allowance (two `src` leaves, `pydantic`, `yaml`,
    the standard library), 31 planted forbidden sources each found and 7
    allowed forms not flagged. Against the real
    tree 39 mutations were applied to a scratch copy and each failed a test
    (see the close-out); the leaf-imports-root, facade-in-function,
    `TYPE_CHECKING`, `try`, relative, parent-relative, `src.daemon` and
    `numpy` forms fail `test_config_boundaries.py`.
  - [x] Update any file lists/tooling that assume config is a single `.py`
    file; inspect ML gate coverage for the now-package path.
    `README.md` and `docs/CANary-overview.md` source trees, and the
    `canary.example.yaml:256` comment (verifier finding). `docs/CANary-HAT-Spec.md:626`
    still names `src/config.py`; it sits with the HAT docs another session owns, so it
    is left as a one-token follow-up. `scripts/ml_paths.txt` never listed
    `src/config.py` and no pathspec in it matches any file this change
    touches (`git ls-files -m -d -o` against its specs is empty), so no
    `ml_training` coverage is lost or gained and the file is unchanged;
    `pyproject.toml` packaging is `src.*` (finds the sub-package); `Dockerfile`
    copies `src/`; `.gitignore` ignores nothing under `src/config/`;
    `config` had no `Path(__file__)` use (`src/paths.py`, which anchors the
    repo root, did not move); no script or tool reads a `config.py` path.
- [x] **R21.5 Verify and close out.**
  - [x] Run `test_config.py`, `test_config_hot_reload.py`,
    `test_sequence_config_base.py`, `test_state_dir.py`,
    `test_resample_config_construction.py`, profile-scoping and layering tests.
    All green in the full non-slow tier below and again with the slow ones in
    those files (`-m slow`: 37 passed across the 16 files listed in the
    close-out); plus `test_repo_tree_isolation.py`, `test_decoded_rows_never_
    stored.py`, `test_ecu_twin_contract.py`, `test_script_imports.py`.
  - [x] Compare default/example model dumps and normalized JSON schemas;
    investigate changes in defaults, aliases, required fields or validators.
    Golden `configs`, `classes`, `schemas` equal; the same fixture
    regenerated in a clean `HEAD` worktree is byte-identical to the one
    committed. 39 mutations (default, field order, `Literal`, constraint,
    docstring, base class, validator renamed, …) each fail it or the
    boundaries test.
  - [x] Check accepted/rejected retired keys, alias collisions, invalid root
    timing combinations and typo rejection against the baseline.
    Golden `strict` and `cases` as listed under R21.1; mutations
    `extra_forbid_dropped`, `retired_alias_conflict_dropped`,
    `gear_ratios_retirement_dropped`, `broadcast_floor_off_by_one`,
    `broadcast_message_changed`, `logger_name_changed`, `imu_chip_check_dropped`
    and a hand-run `hat_adc` boundary (`slow_record_hz < record_hz`) each fail.
  - [x] Verify path rebasing includes every supported field, preserves explicit
    overrides, rejects unsupported path shapes and keeps runtime data out of git.
    Golden `paths` (25 derived fields, 13 rooting and 8 rebase cases);
    `test_repo_tree_isolation.py` (`list[Path]` refused, `Path | None`
    rebased, a `keep` name that is not a `Path` field refused, absolute paths
    re-rooted) and `test_state_dir.py` green; mutations `absolute_not_rerooted`
    (9 failures), `keep_unknown_accepted`, `path_or_none_not_rebased`. After the
    full tier `git status` shows only this change's files: the conftest write
    guard saw no write into the checkout and no `models/` or `data/analyses/`
    file moved.
  - [x] Verify cold imports and relevant spawned-worker round trips; record
    any import cost without claiming a performance gain from fewer file lines.
    Cold imports as under R21.1. Import time of `src.config`, 15 runs per tree,
    cumulative `-X importtime`: `HEAD` min 91.7–93.1 ms, median 94.6–95.9 ms;
    new tree min 95.0–95.2 ms, median 99.6–101.5 ms (two alternating
    rounds, a loaded laptop): about 4 ms (4 %) slower, the cost of eleven
    files; pydantic is nearly all of either figure. No improvement is claimed.
    Spawn round trips as under R21.3.
  - [x] Run the non-slow backend suite, affected slow/training tiers as needed,
    and shared gates; replay a fixed session if root/runtime assembly changed.
    `pytest -m "not integration and not slow" -n 4`: 10133 passed, 43 skipped.
    Slow tests in the config-touching files: 37 passed, 1 failed
    (`test_detector_evaluation_golden.py::test_the_run_matches_the_pinned_one
    [full]`, a stdout log-line count 74 vs 73) which fails identically at an
    unmodified `HEAD` worktree with the same 16-file `-n 4` command and passes
    alone and beside `test_evaluate_detectors_script.py`: not this change. No
    `ml_training` run (not gated, above). `ruff check .`, `ruff format
    --check .` and `mypy src` (624 files) clean. Replay: `scripts/replay.py
    --config canary.example.yaml tests/fixtures/ground_truth/drive-slice` at
    an unmodified `HEAD` worktree and on this tree: reports byte-identical
    (`cmp`).
  - [x] Record final schemas/exports, dump/schema parity, dependency evidence,
    test results and commits. No YAML or artifact migration for a pure move.
    Close-out below. Implementation commit: the commit that archives this file.

## Acceptance

Configuration domains have clear owners behind `src.config`. Existing YAML,
model dumps, validation errors, scoped state paths and public class imports
remain compatible; foundational schemas acquire no higher-layer dependencies.

## Close-out record

Implemented 2026-10-15 on `main` over `24cd1a50`. Not committed by this run.
No YAML, model, manifest, calibration or session was written, refit, restamped
or migrated; nothing touched the Pi or a live daemon.

**Module map** (`src/config.py` was 1,740 lines; `src/config/` is 1,961 across
eleven files, the extra being module docstrings, imports and the facade's
re-export list):

| Module | Lines | Imports (package) | Owns |
|---|---|---|---|
| `base` | 63 | none | `_Strict`, broadcast timeout constants and validator, `DEFAULT_PRIORITY_CHANNELS` |
| `vehicle` | 125 | base | `VehicleParameters`, `VehicleConfig` |
| `hardware` | 291 | base | CAN bus, GPS, sensor CAN, IMU, supply voltage, HAT ADC, USB ADC, K-line, `HardwareConfig`, `HatRevisionSetting` |
| `collection` | 260 | base | `RecordArmingConfig`, `IdleShutdownConfig`, `CollectionConfig`, `log` |
| `preprocessing` | 113 | base | `OperatingModeSpec`, `PreprocessingConfig` |
| `models` | 322 | base | forest, alerts, fusion, `DetectorRole(s)`, `_SequenceTrainingConfig`, LSTM, TCN, `ModelsConfig` |
| `daemon` | 213 | base | `DashboardConfig`, `DaemonConfig`, `DAEMON_STATE_LAYOUT`, `DAEMON_TRACKED_PATHS` |
| `auxiliary` | 233 | base | trends, notifications, online training, non-volatile write, tuning readiness, profiles, auto-tag, LLM assist, plausibility, committees, twin live |
| `root` | 138 | base + the seven above | `CanaryConfig`, `from_yaml`, root-only validators, scope private state |
| `scoping` | 112 | daemon, root | `UnscopedConfigError`, `require_profile_scoped`, `_daemon_path_fields`, `rebase_state_paths` |
| `__init__` | 91 | all | the facade; defines nothing |

External imports of the package: `src.paths` and `src.lsm6dsox_settings`
(layer 0), `pydantic`, `yaml`, the standard library.

**Compatibility.** `src.config` keeps all 54 names it defined, private ones
and `log` included, as the owners' objects. What changed for a caller:
`__module__` of the 40 classes (39 models and `UnscopedConfigError`) and 5
functions names the owner (`src.config.root.CanaryConfig`), so a traceback or
`repr` shows it; the facade no longer carries the libraries the single file
imported (`yaml`, `Path`, `BaseModel`, `Field`, `ImuSettings`, `DATA_DIR`, …:
21 incidental attributes, none used by a caller; it gains the ten submodule
names as attributes); and a monkeypatch of a
name the package uses internally must target the owning module (one test
retargeted, above). Nothing else: model dumps, JSON schema (including `$defs`
titles), error text and logger name are equal.

**What invalidates, and what does not.** The laptop-only, gitignored detector
evaluation score cache keys on `code_version()`, a hash of every `src/**/*.py`
path and byte, so it rescores once, the same cost as any source edit; it
is a cache, never a source of truth. Nothing persisted that carries a stamp
changes: model manifests and artifacts hold plain dicts; the feature-frame
cache key hashes section `model_dump(mode="json")` (golden-equal);
`evidence.provenance.code_version` hashes only the analysis modules it is
given, none of which is config; no code hashes a config class path or a
schema. The pickle of a config differs by 202 bytes (+1.6 %).

**Evidence differences.** None. Golden equal for all seven data sections,
regenerated in a clean `HEAD` worktree byte-identical to the committed one;
`scripts/replay.py` report byte-identical.

**Tests.** New: `tests/test_config_golden.py` (10; fixture
`tests/fixtures/config_golden.json`, regenerated only with `--write`),
`tests/test_config_boundaries.py` (52). Changed: `tests/test_repo_tree_
isolation.py` (one patch target). Mutations (scratch copy, bytecode off), each
caught: default changed; field order; `Literal` widened; constraint widened;
`extra="forbid"` dropped; retired-alias conflict check dropped; `gear_ratios`
retirement dropped; broadcast message changed; broadcast floor off by one;
timeout-vs-gap weakened; cadence weakened; cadence check dropped; state-dir
rooting ignores explicit; cards path ignores explicit; layout entry changed;
`Path | None` not rebased; absolute path not re-rooted; unknown `keep`
accepted; scoped-profile default changed; `scoped_profile` property dropped;
unscoped refusal inverted; TCN detached from the shared base; logger name
changed; IMU chip check dropped; HAT-ADC rate check flipped; docstring
changed; validator renamed; `numpy` imported; leaf imports root (module level,
in a function, under `TYPE_CHECKING`); leaf imports scoping in `try`; facade
imported in a function, relatively, and through `from .. import config`;
`src.daemon` imported lazily; facade drops a name; `src/config.py` beside the
package. 36 fail the assertion meant for them; three (HAT-ADC flipped, the two
module-level imports) leave a package that cannot be imported.
