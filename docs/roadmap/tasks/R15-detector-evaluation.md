# R15 — Detector evaluation library and phased CLI

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size L. R14 precedes migration of feature consumers; loading uses R13 owners.

## Problem and target

`scripts/evaluate_detectors.py` was 3,054 lines, with a 694-line `_evaluate`.
Alongside CLI orchestration it contains score tasks/results, `DriveScorer`,
worker setup, raw-stream loading, cache identities, statistical summaries and
report rendering. Many tests import those library concepts from the script.

Create focused evaluation modules under `src/models/` for scoring primitives
that need detection/lower layers and under `src/analysis/` for corpus/label/era
orchestration needing analysis concepts. Map actual dependencies first: do not
put analysis-dependent code in layer 4. The script owns args, logging, invocation,
writing reports and exit status. Preserve one corpus evaluation protocol.

## Implementation checklist

- [ ] **R15.1 Pin the protocol and caller surface.**
  - [ ] Inventory exported helpers/types, script callers, retrain subprocess
    arguments, monkeypatch targets, worker globals and cache key components.
  - [ ] Capture CLI snapshot and a deterministic small evaluation report with
    baseline comparison, default-profile exclusion and held-out drive handling.
  - [ ] Record seeds, cadence, persistence, cross-era policy, withheld detectors,
    in-sample refusal, injection accounting and report schema.
- [ ] **R15.2 Design library phases and handoff types.**
  - [ ] Map prepared inputs, artifacts, drive selection, baseline preparation,
    scoring tasks, aggregation and report construction to explicit phase results.
  - [ ] Reuse `DriveTask`, `DriveResult`, `ScorerSpec` and `SweepSpec`; avoid a
    giant mutable context or passing `argparse.Namespace` into every helper.
  - [ ] Assign each module to models or analysis by its imports. Record the
    dependency map and preserve the deployed scoring kernel already shared.
  - [ ] Keep the coherent linear Markdown renderer; extracting its owner
    does not require fragmenting every report paragraph.
- [ ] **R15.3 Extract scoring and multiprocessing.**
  - [ ] Move `DriveScorer`, component identity and worker construction together
    with the types they serialize; leave worker entrypoints module-level.
  - [ ] Preserve deterministic seeds, thread pinning, worker memory limits,
    serial behavior and cleanup on cancellation/failure.
  - [ ] Preserve spawn/pickle compatibility; test actual worker execution,
    not only an in-process call to the worker function.
  - [ ] Preserve lazy raw stream access and exact per-drive/component outputs.
- [ ] **R15.4 Decompose `_evaluate` and extract aggregation.**
  - [ ] Separate scoped loading/artifact identity, eligible drive planning,
    baseline fitting, parity checks, task construction, scoring and aggregation.
  - [ ] Keep independent-drive denominators, default-profile exclusion and
    seen-session rules unchanged, including named skip/load failure reasons.
  - [ ] Move statistical summaries/report construction without changing JSON
    fields, regression comparison, Markdown content or exit codes.
  - [ ] Keep result writes in the CLI or an explicit publication boundary;
    library scoring must not choose a new output directory or print.
- [ ] **R15.5 Preserve transactional cache identity.**
  - [ ] Trace `evaluation_cache.code_version`: it already hashes every
    `src/**/*.py`, library versions and extra harness files. Retain that coverage;
    `_harness_files` currently supplies the script and `_cli.py` as extras.
  - [ ] Verify moving or editing extracted source changes code identity; keep
    earliest input hashes, end-of-run checks and staged-entry commit/discard.
  - [ ] Preserve `frame_key`, model/data/config identity, cadence and per-unit
    keys. Expected source-move cache invalidation is recorded, not hidden.
  - [ ] Test interruption/input changes leave no published stale score entries.
  - [ ] Update `scripts/ml_paths.txt` if extracted analysis modules would fall
    outside the previous script's training-tier coverage.
- [ ] **R15.6 Migrate and verify.**
  - [ ] Keep compatibility re-exports, move library-focused tests to owners,
    and retain CLI/subprocess integration coverage at the script path.
  - [ ] Run `test_evaluate_detectors_script.py`,
    `test_evaluate_detectors_default_profile.py`, `test_evaluation_harness.py`,
    `test_evaluation_detectors.py`, affected cache/budget tests and CLI snapshot.
  - [ ] Compare serial and spawned-worker reports on identical fixtures/seeds;
    normalize generated time and invocation text only where appropriate.
  - [ ] Compare per-sample score arrays, event times, injected outcomes,
    eligibility/skips and all meaningful report fields, not just totals.
  - [ ] Run layering, relevant slow and `ml_training` tiers and shared gates.
  - [ ] Record module ownership, CLI compatibility, expected cache invalidation,
    fixed-report differences, tests and commits.

## Acceptance

Evaluation concepts live in libraries at their correct layers. `_evaluate`
reads as phase orchestration, the CLI/report contract is intact, worker and
serial results agree, and cache identity/transaction protections still apply.
