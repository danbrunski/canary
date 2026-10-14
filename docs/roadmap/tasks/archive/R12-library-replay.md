# R12 — Replay as library code

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size S–M. No prerequisite; coordinate feature imports with R14 if it overlaps.

## Problem and target

`src.analysis.replay_window.replay_window` lazily imports `replay_sessions`
from `scripts/replay.py`. That script owns `ReplayFrames`, `ReplayResult`,
preprocessing/scoring orchestration, report helpers and CLI parsing. A library
call therefore depends on an executable module and its CLI import fallback.

Move programmatic replay into `src/analysis/replay.py`; the script imports it
and handles arguments, console output and exit codes. Keep `replay_window`
as a consumer. Offline replay and daemon runtime replay remain distinct APIs.

## Implementation checklist

- [x] **R12.1 Inventory the public and test surface.**
  - [x] Find imports of `scripts.replay`, `replay_sessions`, `ReplayFrames`
    and `ReplayResult`, including job runners and monkeypatches.
    `rg "scripts\.replay|scripts/replay|replay_sessions|ReplayFrames|ReplayResult"`
    over `src scripts tools tests`: one `src` caller (`replay_window`, a
    function-level import), tests `test_replay_script`, `test_replay_window`
    (patched `scripts.replay.replay_sessions`), `test_featurization_profile_scope`
    (patches `script.replay_sessions`, read by `script.main`),
    `test_resample_config_construction` (spied `replay.resample_wide`,
    called `replay._resample_numeric`). No tool or job runner imports the script
    (`tools/replay_window.py` goes through `src.analysis.replay_window`); no
    pickle or serialized type names the classes. Docs mentions are historical.
  - [x] Record the CLI snapshot, arguments/defaults, error handling and JSON
    report fields using existing script and replay tests.
    `tests/fixtures/script_cli_snapshot.json` `replay.py` entry is unchanged
    (`test_script_cli_snapshot.py` green, no regeneration). Error handling:
    `main` maps `FileNotFoundError`/`KeyError`/`ValueError` to `error: ...` on
    stderr, exit 1; argparse errors exit 2. Report fields are the 16 keys of
    `_report` (pinned by `test_replay_sessions_builds_preprocessing_report`).
  - [x] Capture fixed-fixture intermediate frames and optional IF scores;
    cover absent model artifacts and behavior-model loading.
    Scratchpad `r12_baseline.py capture` on the unmodified script: 9 library
    cases (OBD, OBD + IF artifact, absent scoped behavior-models file,
    behavior models by explicit path and by `config.daemon.behavior_models`
    default, with and without IF, CAN-only, two sessions, `value_bool`), two
    refusals (no sessions, missing dir), 7 CLI subprocess runs (stdout JSON, exit
    codes incl. missing session, absent `--model`, argparse rejects).
    Frames long/wide/resampled/segmented/features/scored kept as DataFrames.
- [x] **R12.2 Move the library operation.**
  - [x] Extract result types, `replay_sessions` and the pure helpers it needs.
    Leave `parse_args`, `main`, printing and CLI configuration in the script.
    New `src/analysis/replay.py` holds `ReplayFrames`, `ReplayResult`,
    `replay_sessions`, `_resample_numeric`, `_report`, `_scoring_report` and the
    column/count helpers, moved verbatim. `scripts/replay.py` keeps
    `parse_args`, `main` and the `_cli` import fallback.
    `test_script_is_an_entrypoint_not_the_implementation`.
  - [x] Preserve scoped behavior-model paths, explicit `did_map=None` for
    this current undecoded path, value selection and numeric-column handling.
    Bodies unchanged; baseline cases `maf_bm_config_default` (scoped
    `config.daemon.behavior_models`), `maf_bm_explicit`, `obd_default_bm_absent`,
    `value_bool`, `can_only` all 0 diffs.
  - [x] Keep the current resampling and shared featurization path; do not
    change decoding or add detectors as part of the move.
    Still `_resample_numeric` -> `resample_wide(resample_config_for(config))` and
    `featurize_wide_frame(..., did_map=None)`; `test_replay_library_uses_the_recipe`.
  - [x] Keep imports at analysis layer or below and avoid library stdout.
    `src/analysis/replay.py` imports behavior, columns, config, features, models,
    preprocessing, timefmt (all below layer 5), no `print`.
    `test_layering.py` green (`OWED`/`OWED_PAIRS` empty).
  - [x] Retain direct script re-exports for existing programmatic callers;
    ensure the old and new names refer to the same types/functions.
    `from src.analysis.replay import ReplayFrames as ReplayFrames` etc.;
    `test_script_reexports_are_the_library_objects`. Private helpers are not
    re-exported (no caller outside the one retargeted test).
- [x] **R12.3 Migrate consumers and patch points.**
  - [x] Make `replay_window` import the owning library module.
    Top-level `from src.analysis.replay import replay_sessions` (the lazy
    import existed only to reach a script).
  - [x] Migrate library-focused tests and tools; keep CLI tests exercising
    script behavior rather than merely importing the new function.
    `test_replay_script.py` library tests now import `src.analysis.replay`;
    `main` tests stay on the script and gained
    `test_replay_main_runs_the_library_and_prints_its_report` and
    `test_replay_main_forwards_value_and_model_to_the_library` (arguments,
    printed JSON, exit code). No tool needed migrating.
  - [x] Retarget monkeypatches to the binding actually used by the consumer;
    verify the patches still affect replay execution.
    `test_replay_window.py` patches `src.analysis.replay_window.replay_sessions`
    (a stale `scripts.replay` patch would let the real function run on a
    nonexistent session with a `None` config and raise, so the tests would fail rather than
    silently pass); the `resample_wide` spy now targets `src.analysis.replay`
    (renamed `test_replay_library_uses_the_recipe`);
    `test_featurization_profile_scope` still patches the script binding, which
    `main` reads. All green.
  - [x] Sweep `src` for remaining script imports, including function-level ones.
    `test_src_never_imports_scripts` walks every `src` file (AST, nested and
    `TYPE_CHECKING` included): 0 offenders. Run against `HEAD:replay_window.py`
    it reports `analysis/replay_window.py -> scripts.replay`.
- [x] **R12.4 Enforce the boundary.**
  - [x] Add an AST-based architectural check rejecting static imports of
    `scripts` from `src`, including lazy and `TYPE_CHECKING` imports.
    `tests/test_layering.py::test_src_never_imports_scripts` (reuses the
    layering scanner, so it sees function bodies, `try`, `TYPE_CHECKING`).
  - [x] Verify the checker detects a forbidden nested import in a temporary
    source fixture; do not add a standing allowance for this dependency.
    `test_scripts_import_scanner_flags_nested_and_type_checking_imports`
    (function-level `from scripts.replay import`, `TYPE_CHECKING`
    `import scripts.retrain`, `try: from scripts import replay`; look-alikes
    `src.scripts_helper` and `scriptsy` ignored). No allowance table.
  - [x] Keep CLI entrypoints free to import library code in the other direction.
    `scripts/replay.py` imports `src.analysis.replay`; the check scans `src` only.
- [x] **R12.5 Verify compatibility and close out.**
  - [x] Run `test_replay_script.py`, `test_replay_window.py`,
    `test_replay_window_job.py`, `test_resample_config_construction.py`,
    `test_script_cli_snapshot.py`, layering and the new boundary test.
    `pytest -n 4` on those plus `test_featurization_profile_scope.py`: 159 passed;
    `-m slow` on test_replay_script/test_replay_window/test_resample_config_construction/
    test_layering: 5 passed. Also `test_mcp_replay`, `test_replay_behavior_compare`,
    `test_mcp_server`: 28 passed.
  - [x] Compare long, wide, resampled, operating, feature and scored frames:
    rows, column order, dtypes, timestamps, missing masks and numerical values.
    `r12_baseline.py compare` against both `src.analysis.replay` and
    `scripts.replay` (re-exports): 72 items compared, 0 diffs
    (`assert_frame_equal(check_exact=True, check_dtype=True)`, column order,
    `isna()` masks; shapes e.g. features 80x27 plain, 40x76 with behavior
    models, scored 80x34 / 40x83). Capture re-run against the unmodified script
    was also 0 diffs, so the fixture is deterministic.
  - [x] Compare report fields and CLI exit behavior. Normalize only declared
    nondeterministic metadata, not meaningful counts or warnings.
    Report JSON strings equal for all 9 cases; CLI subprocess stdout, exit code
    (0/0/0/1/1/2/2) and stderr tail identical. Nothing normalized except the
    fixture directory in stderr paths (stable path, so none was needed in practice).
  - [x] Check `scripts/ml_paths.txt` coverage for the extracted implementation
    and run relevant slow/training tests when required by the shared rules.
    Neither `scripts/replay.py` nor `src/analysis/` is listed in `ml_paths.txt`
    and no `ml_training` test touches replay, so the extraction removes nothing
    from the training tier and `push.sh` will not select it for this change.
    Relevant `slow` tests run (above). Nothing added to `ml_paths.txt`.
  - [x] Record commits, caller migration, fixture differences and test results.
    Caller migration, fixture differences (none) and test results are in the
    close-out below. Commit: `refactor(replay): library replay no longer
    imports a CLI (R12)` on `main`, the commit that archives this file.

## Acceptance

No `src` module imports a script. The same replay result is available through
the library and existing CLI, with unchanged arguments, reports and optional
artifact behavior. The script is an entrypoint, not the implementation owner.

## Close-out record (2026-10-14)

Library: `src/analysis/replay.py` (`ReplayFrames`, `ReplayResult`,
`replay_sessions`; `scripts/replay.py` re-exports these three). Boundary test:
`tests/test_layering.py::test_src_never_imports_scripts`.

Migrated callers:

| Caller | Before | After |
|---|---|---|
| `src/analysis/replay_window.py` | function-level `from scripts.replay import replay_sessions` | top-level `from src.analysis.replay import replay_sessions` |
| `tests/test_replay_script.py` | `from scripts.replay import main, replay_sessions` | `main` from the script, `replay_sessions` from the library |
| `tests/test_replay_window.py` | patched `scripts.replay.replay_sessions` | patches `src.analysis.replay_window.replay_sessions` |
| `tests/test_resample_config_construction.py` | spied `scripts.replay.resample_wide` | spies `src.analysis.replay.resample_wide` |
| `tests/test_featurization_profile_scope.py` | patched `script.replay_sessions` | unchanged (CLI test; `main` reads the script binding) |

Fixture differences: none (72 items, 0 diffs; frames exact, dtypes, column
order, NaN masks; reports and CLI output byte-identical).

Behaviour: unchanged. `ReplayFrames`/`ReplayResult` now report
`__module__ == "src.analysis.replay"` (no pickle or serialized reference named
the old path). `replay_window` imports the replay module at import time rather
than on first call; the cost is the same packages it already imported
(`src.models.isolation_forest`, `src.config`) plus `src.features`/`src.preprocessing`.
