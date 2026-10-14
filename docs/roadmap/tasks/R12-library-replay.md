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

- [ ] **R12.1 Inventory the public and test surface.**
  - [ ] Find imports of `scripts.replay`, `replay_sessions`, `ReplayFrames`
    and `ReplayResult`, including job runners and monkeypatches.
  - [ ] Record the CLI snapshot, arguments/defaults, error handling and JSON
    report fields using existing script and replay tests.
  - [ ] Capture fixed-fixture intermediate frames and optional IF scores;
    cover absent model artifacts and behavior-model loading.
- [ ] **R12.2 Move the library operation.**
  - [ ] Extract result types, `replay_sessions` and the pure helpers it needs.
    Leave `parse_args`, `main`, printing and CLI configuration in the script.
  - [ ] Preserve scoped behavior-model paths, explicit `did_map=None` for
    this current undecoded path, value selection and numeric-column handling.
  - [ ] Keep the current resampling and shared featurization path; do not
    change decoding or add detectors as part of the move.
  - [ ] Keep imports at analysis layer or below and avoid library stdout.
  - [ ] Retain direct script re-exports for existing programmatic callers;
    ensure the old and new names refer to the same types/functions.
- [ ] **R12.3 Migrate consumers and patch points.**
  - [ ] Make `replay_window` import the owning library module.
  - [ ] Migrate library-focused tests and tools; keep CLI tests exercising
    script behavior rather than merely importing the new function.
  - [ ] Retarget monkeypatches to the binding actually used by the consumer;
    verify the patches still affect replay execution.
  - [ ] Sweep `src` for remaining script imports, including function-level ones.
- [ ] **R12.4 Enforce the boundary.**
  - [ ] Add an AST-based architectural check rejecting static imports of
    `scripts` from `src`, including lazy and `TYPE_CHECKING` imports.
  - [ ] Verify the checker detects a forbidden nested import in a temporary
    source fixture; do not add a standing allowance for this dependency.
  - [ ] Keep CLI entrypoints free to import library code in the other direction.
- [ ] **R12.5 Verify compatibility and close out.**
  - [ ] Run `test_replay_script.py`, `test_replay_window.py`,
    `test_replay_window_job.py`, `test_resample_config_construction.py`,
    `test_script_cli_snapshot.py`, layering and the new boundary test.
  - [ ] Compare long, wide, resampled, operating, feature and scored frames:
    rows, column order, dtypes, timestamps, missing masks and numerical values.
  - [ ] Compare report fields and CLI exit behavior. Normalize only declared
    nondeterministic metadata, not meaningful counts or warnings.
  - [ ] Check `scripts/ml_paths.txt` coverage for the extracted implementation
    and run relevant slow/training tests when required by the shared rules.
  - [ ] Record commits, caller migration, fixture differences and test results.

## Acceptance

No `src` module imports a script. The same replay result is available through
the library and existing CLI, with unchanged arguments, reports and optional
artifact behavior. The script is an entrypoint, not the implementation owner.
