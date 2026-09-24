# R06 — scripts and tools

Opened 2026-09-24 from the refactor review ([R00](R00-refactor-README.md)).
The July refactor gave `scripts/` a shared preamble (`scripts/_cli.py`).
`tools/` never got one. The bus tools written since, for memory channels,
DID logging and sweeps and batch probing, copy each other and import each
other's private names. Two scripts, `retrain.py` and `train_sequence.py`,
have grown into library code with a CLI attached.

## Rules

- **Scripts and tools stay thin wrappers over `src/`.** Library logic found
  in a script moves to `src/`.
- **A transmitting tool cannot skip the passive check.** The JobController
  runs `did_sweep`, which must keep skipping it deliberately, and says so.
- **`src/control/jobs.py` pins script paths.** The `train_lstm.py` /
  `train_tcn.py` shims stay.
- **Moves under `src/models`/`src/preprocessing` trigger the `ml_training`
  tier.**

---

- [ ] **R06.1 Give `tools/` a shared bus-tool module.**
  - No shared module exists. `tools/did_log.py` serves as one:
    `can_memory_log`, `batch_probe` and `verify_memory_channels` import its
    private `_passive_problem`, `_resolve_session`, `_resolve_token` and
    `http_passive_checker`.
  - Copies:
    - `_open_client` is AST-identical in `batch_probe.py`,
      `can_memory_log.py` and `verify_memory_channels.py`. There are near
      copies in `probe_local_identifier.py` (a renamed kwarg) and
      `did_log._open_transport`. In total 7 tools hand-build
      `CanObdClient(… response_id_min=request_id + 8 …)`; R02.5 moves the
      pinning into the client.
    - `_resolve_session` is identical in `did_log.py` and `did_sweep.py`.
    - `did_sweep._width_lookup` is a subset of `did_log.load_widths`.
    - The ~8-line passive-gate refusal block appears 4 times.
  - Drift:
    - `--daemon-url` defaults to `localhost` in `verify_memory_channels` and
      `127.0.0.1` elsewhere.
    - `batch_probe` hard-codes `check_seconds=30.0`.
    - The JSONL record discriminator is `"kind"` in `did_log` and `"type"`
      in `can_memory_log`. `started_at` is an epoch float in one and an ISO
      string in the other.
    - `probe_local_identifier.py` and `scan_constant_dids.py` open the bus
      with **no passive check at all**.

  **Do:** create `tools/_bus_tool.py`. The HTTP half may go to
  `src/control/passive_client.py`. It holds:
  - `add_bus_tool_args(parser)`: config, header, session, read timeout,
    daemon URL, no-daemon, passive-check seconds;
  - `open_client(args, passive: PassiveVerdict)`, which takes the verdict so
    that a transmitting tool cannot open the bus without one;
  - `resolve_session`, `resolve_token`, `http_passive_checker` and
    `require_passive(args)`.

  Move `load_widths` next to `DidSnapshot` in `src/analysis/did_scan.py`.
  Settle one JSONL record shape.

  **Accept:** failing tests first: `probe_local_identifier` and
  `scan_constant_dids` refuse while the daemon is transmitting. No tool
  imports another tool's private names. Size **M**.

- [ ] **R06.2 Pin every script's CLI surface before refactoring one.** This
  is the safety net for R06.3 and R06.4.
  - No test runs the JobController's retrain / refit / did_sweep argv through
    the real parsers.
  - Flag drift:
    - `--accept-alert-budget` uses `type=_stated_reason`.
    - `--accept-thin-calibration` (retrain, train_sequence, sequence_bakeoff)
      has no validator.
    - The IF path checks `if calibration_override:`, so a whitespace-only
      reason passes the A04.4 refusal. The sequence path strips it first.

  **Do:**
  - Add `tests/test_script_cli_snapshot.py`, in the style of the MCP
    snapshot, pinning each script's argparse surface: options, defaults,
    types, required.
  - Add a job-argv → parser round-trip test for every `jobs.py` kind.
  - Add `_cli.add_override_reason_arg`, used by every `--accept-*` flag.

  **Accept:** a failing test first: `--accept-thin-calibration "  "` is
  refused. Size **S**.

- [ ] **R06.3 Break up `scripts/retrain.py` (1,749 lines).**
  - `main` is 527 lines and `parse_args` 288.
  - `main` runs about 10 phases in sequence: sessions, holdout, frames,
    quarantine, feedback, trainers, benchmark gate, alert-budget gate,
    publish, ledger, exit code.
  - It carries run state by mutating `args`: `if_out`, `sequence_out`,
    `skip_if`, `skip_sequence`, `holdout_split`, `seq_model_type`,
    `sequence_model`.
  - Library logic lives in the script:
    - the holdout pickers `_pick_calibration_holdouts` (113 lines) and
      `_pick_evaluation_holdouts`; `sequence_bakeoff.py` reuses the first as
      `retrain._pick_calibration_holdouts`;
    - the staging, verdict, publish and refuse code (`_staging_dir` …
      `_set_aside_refused`), whose reader is `src/models/role_gate.py`.
  - `_filter_sessions_by_boundary` is imported privately from
    `refit_behavior_models`, and the whole `--since-boundary` block is
    duplicated in both scripts.
  - The tests work around the missing seams: `_cli.discover_sessions` is
    patched 40 times, `if_trainer.train` 31, `lstm_trainer.train` 28,
    `build_feature_frame` 28, and `_session_*` 20.

  **Do:**
  - Move the holdout selection into `src/models/evaluation.py`, next to
    `holdout_by_drive`.
  - Move staging and publish into `src/models/publish.py`.
  - Move `filter_sessions_by_boundary` and an `apply_since_boundary(args,
    config, sessions)` helper into `_cli.py`.
  - Rewrite `main` as phase functions over a `RetrainRun` dataclass that
    stops `args` being mutated.

  **Accept:**
  - R06.2's snapshot is unchanged.
  - `test_retrain_script.py` passes. Where it patched a private seam,
    retarget it to the new public one and say so.
  - A real laptop retrain on a small corpus gives the same staged artifacts
    and ledger row, apart from timestamps.

  Retrain is the deploy path. Size **L**.

- [ ] **R06.4 Move trainer and evaluation library code out of `scripts/`,
  and end sibling imports between scripts.**
  - `train_sequence.train` (308 lines) is training-contract logic: it drops
    parked rows, chooses validation drives, fits the scaler, drops unobserved
    and suppressed columns, and resolves the feature set. Its own comment
    calls it "the one entry every sequence retrain goes through".
  - Other library code with a CLI attached: `train_anomaly_model.train`
    (127 lines), and `detector_benchmark.load_resampled_session`, which
    `evaluate_detectors` and `retrain` both use.
  - Bare sibling imports between scripts:
    - `retrain` imports 7 scripts, and `sequence_bakeoff` imports 4.
    - `train_lstm`/`train_tcn` import the private
      `_resolve_training_sessions`.
    - `fp_benchmark` imports `scripts.train_lstm`.
    - `evaluate_detectors.py` does a `try: from _cli … except ImportError:
      from scripts._cli` dance.
  - Tests import the same file under two module identities: bare
    `evaluate_detectors`/`_cli`/`retrain` in 21 files that use
    `sys.path.insert`, and `scripts.evaluate_detectors`/`scripts._cli` in
    others. A test that patches one identity silently misses code bound to
    the other.

  **Do:**
  - Create `src/models/training_entry.py` (`train_sequence_detector`,
    `train_isolation_forest`) and
    `src/preprocessing/feature_pipeline.load_resampled_session`.
  - Scripts import only `src` and `_cli`.
  - Delete the 21 test `sys.path` hacks.
  - Keep `pyproject`'s `pythonpath = ["."]` and don't add `scripts` to it,
    which would spread the dual-identity trap.

  **Accept:**
  - `grep -rn "sys.path" tests` is empty or each hit is justified here.
  - No script imports another script except `_cli`.
  - `ml_paths.txt` covers the new `src` homes.

  Size **L**.

- [ ] **R06.5 Operator decision: one corpus benchmark harness?**
  - `detector_benchmark` is retrain's regression gate, with a documented
    blind spot for behavior-deviation features.
  - `evaluate_detectors` (A02.4) calls itself "one protocol".

  Should `detector_benchmark`'s baseline diff fold into `evaluate_detectors`
  and the older grid retire? Record the answer here. If yes, it is a
  separate **M** item after R04.5 fixes the promotion gate.

- [ ] **R06.6 Small de-duplications (batch them).**
  - `scripts/replay.py:_format_timestamp` is identical to
    `src.timefmt.format_timestamp`.
  - The sha256 copies: see R01.6.
  - `tools/backfill_decoded.decode_session_frame`: see R04.4.
  - The session sort key `session_started_at(p) or datetime.min` and
    `evaluate_detectors._spread_in_time`'s `TypeError`: see R01.1.
  - "Has telemetry" is hand-rolled in 6 scripts, with drift (`st_size > 0` vs
    any chunk). `evaluate_detectors.discover_session_dirs` defines a third
    "recorded drives, captures included" discovery question locally. Add
    `loader.has_telemetry` and a named
    `control.sessions.recorded_sessions(roots)`, and keep it distinct from
    both "which may train?" and "which is live?".
  - `--log-level` is defined in 26 scripts/tools and `--bindings` in 19.
    Add `_cli.add_log_level_arg` and `add_bindings_arg`, defaulting to
    R01.2's constant.
  - `scripts/fp_benchmark.py` still hand-rolls `--config` (the July
    leftover).

  Size **S**.

## Looked at and fine

- `scripts/canaryctl.py`'s urllib client: stdlib-only on purpose.
- `scripts/record.py` `main` (271 lines): mostly signal, pidfile and
  disk-space orchestration. Its spec drift is R02.7.
- `scripts/ups_shutdown_watch.py`: single-file by design (July).
- `evaluate_detectors.render_markdown` (198 lines): linear report
  rendering.
- `scripts/benchmark_live_pipeline.py`: self-contained, and its docstring
  justifies the shape.
- `scripts/canary_companion.py`: the `CANARY_CONFIG` env default is a genuine
  divergence (July).
