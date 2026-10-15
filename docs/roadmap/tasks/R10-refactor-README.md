# R10 — Refactor review and implementation order, 2026-10-14

This set implements the October 14 codebase review. It covers the nine main
findings and both secondary candidates, behavior validation and configuration.
The review inventoried source size, inspected dependency graphs and repeated
helpers, and read the earlier refactor decisions. These are structural findings;
they do not establish a runtime bug in every affected module.

`tests/test_layering.py` passed all nine tests during the review. Its unit is a
top-level package or root module, so same-package cycles and imports from
`src` into `scripts` need their own checks. Dependency findings include
function-level and `TYPE_CHECKING` imports; a cycle is not itself proof of an
import-time exception. Deliberately lazy backend registries are not targets.

## Task index

| Task | Outcome | Size / risk | Prerequisite |
|---|---|---|---|
| [R11](archive/R11-explicit-time-lookup.md) | Shared previous, nearest and bracket lookup with explicit semantics | S–M / numerical | none |
| [R12](archive/R12-library-replay.md) | Library replay no longer imports a CLI | S–M / offline scoring | none |
| [R13](archive/R13-preprocessing-boundaries.md) | Readers, transforms and orchestration have one dependency direction | L / data ordering and memory | none |
| [R14](R14-feature-pipeline.md) | Feature assembly separated into cohesive modules | L / training and serving | R13 before loading moves |
| [R15](R15-detector-evaluation.md) | Library scoring and a phased evaluation CLI | L / scoring, cache and spawn | R14 before feature caller migration |
| [R16](archive/R16-dyno-modules.md) | Separate dyno contracts, calibration, power, comparison and persistence | M–L / numerical | none |
| [R17](archive/R17-recipe-definitions.md) | Recipe definitions independent of the fitting engine | M / artifact contracts | before R20 refit caller migration |
| [R18](R18-memory-burst-scheduler.md) | Burst planning and accounting have one owner | L / collection timing | none |
| [R19](R19-dashboard-page-modules.md) | Logs and Analysis pages composed from focused views | M / UI behavior | none |
| [R20](R20-behavior-validation.md) | Validation schema, pairing, evaluation, metrics and evidence separated | L / evidence and publication | R17; coordinate with R15 |
| [R21](R21-config-schemas.md) | Configuration schemas organized behind compatible imports | M–L / broad import surface | last, after overlapping moves |

Size is a relative planning estimate, not a promised duration. The two final
items are lower priority than the first nine. Counts in the individual files
are review snapshots; locate code by symbol, not historical line number.

## Suggested implementation order

1. R11 and R12: small boundaries with clear contracts and immediate value.
2. R13: establish reader ownership before moving feature loading.
3. R17 and R16: independent recipe and dyno boundaries.
4. R14, then R15: feature responsibilities followed by evaluation consumers.
5. R18 and R19: collector scheduling and dashboard view decomposition.
6. R20, then R21: lower-priority broad splits after their callers settle.

This order does not modify [OVERNIGHT-QUEUE.md](../OVERNIGHT-QUEUE.md).
Writing these plans is not evidence that any implementation has landed.

## Shared implementation and acceptance rules

- Keep behavior unchanged unless a separately named defect has its own
  reproducer, expected output, and deliberate behavior-change commit. Do not
  use a file split to change thresholds, defaults, pairing, model selection,
  request ordering, or evidence denominators.
- Capture an import/caller inventory with `rg`, including tests, tools,
  monkeypatch targets, lazy imports and pickled types. Keep old public paths
  as direct re-exports during migration. Internal modules import owning
  modules, never their compatibility facade.
- Record a fixed-input baseline before numerical or stateful moves. Compare
  ordering, dtypes, NaN masks, units, refusals, state transitions and counts,
  not just aggregate scores. Use the existing ground-truth fixtures for
  scientific claims; signal comparisons use `compare_channels`.
- Keep new submodules within existing packages and layers. A genuinely new
  top-level package must be placed in `tests/test_layering.py`. No upward
  imports or new circular facades; `OWED` and `OWED_PAIRS` stay empty.
- Use focused architecture tests for the named boundaries. Include imports
  inside functions and `TYPE_CHECKING`. A test must fail for an actual forbidden
  dependency; do not add a blanket acyclicity rule across intentional registries.
- Preserve scoped vehicle data, runtime state paths and underscore-directory
  isolation. No runtime writer begins writing to a tracked catalog or checkout.
- Verify serve-path changes with a fixed-session replay, recording row and
  event differences. Collection timing additionally needs deterministic bus
  and clock tests: replay cannot establish wire cadence. Check `vehicle_state`
  before interpreting any live result; record replay state explicitly.
- Each document has a focused test list. Use `.venv/bin/python` and
  `.venv/bin/pytest`; while iterating run affected files. Before committing,
  run `scripts/check.sh --fast` and the relevant tests. Run affected `slow`
  tests and the `ml_training` tier when model/preprocessing work requires it.
  Preserve skip-without-torch behavior; do not add torch to GitHub CI.
- Consult `scripts/ml_paths.txt` when moving code. An extraction must not
  silently remove a previously gated implementation from the training tier.
- Dashboard implementation runs typecheck, formatting, lint and related
  vitest tests; while CI is off, full lint, vitest and e2e are also required.
  Fix new lint violations and prune obsolete suppressions; never suppress all.
- Work directly on `main`, use explicit commit pathspecs, and push through
  `scripts/push.sh`. CI is disabled as of this review: the local push gate is
  the verdict; do not wait for a GitHub run. Record flakes in T01.
- These refactors do not require deployment to claim desk-side completion.
  If an item claims Pi throughput, memory or cadence improvement, keep that
  claim partial until measured. Use `scripts/restart_canary.sh` for any later
  restart, and do analysis on the laptop while the Pi collects.
- Mark `[x]` only with evidence in the task's close-out section. `[~]` names
  exactly what remains. Archive a task only when every required item is done.

## Decisions carried forward

Read [R00](archive/R00-refactor-README.md) and the affected archived task before
changing its boundary. In particular:

- `isolation_forest.py` is not a whole-file split target.
- Recipe catalogs remain typed Python and vehicle-specific recipes stay scoped.
- One shared feature transform continues to serve both batch and live paths.
- The API retains one function per route and hand-written MCP tools.
- Transport/decoder differences, distinct torch dependency errors, and the
  lazy sequence backend registry are not gratuitous deduplication targets.
- `splits.Drive` and `PhysicalDrive` represent different units; do not unify
  them as part of R20.
- Pure moves do not trigger a retrain, refit, recipe restamp or calibration write.

## Close-out record

Each task records implementation commits, focused and tier test results,
compatibility/import evidence, fixed-fixture differences, and any measured
limitations. The checkboxes in R11–R21 are the source of truth; this index
does not duplicate their status.
