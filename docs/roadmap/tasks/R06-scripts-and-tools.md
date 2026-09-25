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

- [~] **R06.1 Give `tools/` a shared bus-tool module.**
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

  *Done 2026-09-25, except the two remainders below.* `tools/_bus_tool.py`
  (`add_bus_tool_args`, `require_passive`, `open_client(args, passive)`,
  `resolve_session`, `parse_header`, `load_widths`, `header_record` /
  `record_kind`) and `src/control/passive_client.py` (`PassiveStatus`,
  `http_passive_checker`, `passive_problem`, `resolve_token`,
  `DEFAULT_DAEMON_URL`). `open_client` raises `TypeError` without a
  `PassiveVerdict`, and a verdict cannot be built by hand, only by
  `require_passive` or `PassiveVerdict.job_controller()`, which `did_sweep`
  alone uses, with a comment saying why. All 8 bus tools open the bus through
  it: the 7 above plus `can_obd_probe`, which also transmitted unchecked. The
  JSONL shape is `"kind"` + epoch `started_at` (`can_memory_log` moved off
  `"type"`/ISO; `analyze_can_memory_log` reads both). One `--daemon-url`
  default (127.0.0.1); `batch_probe`'s hard-coded 30 s is now
  `--passive-check-seconds` (default 30). Failing first:
  `tests/test_bus_tool.py::test_probe_local_identifier_refuses_while_daemon_transmits`
  and `::test_scan_constant_dids_refuses_while_daemon_transmits[--canable|--port]`
  failed (3 failed) against the old tools, then pass. Structure tests:
  `test_no_tool_constructs_a_client_itself`,
  `test_only_the_job_controller_worker_skips_the_passive_check`,
  `test_no_tool_imports_another_tools_private_names`. Remainders, both in R05's
  area: (1) `load_widths` lives in `tools/_bus_tool.py`, not beside
  `DidSnapshot` in `src/analysis/did_scan.py`; (2)
  `watch_uprev_logs` still imports `tools.import_uprev_log._FILENAME_TIME`,
  pinned as the one allowed entry in the test. Make it public in
  `import_uprev_log.py` and delete the entry.

  *Verifier follow-up, 2026-09-25:*
  - **Responder window.** `open_client` pins a physical header's response
    window to `request + 8` (`_bus_tool.response_window`), as HEAD did;
    0x7DF keeps the default 0x7E8-0x7EF window. Without that, `--header 740`
    (answered on 0x748) was silently deaf.
    `test_open_client_hears_a_module_answering_outside_the_obd_window` runs
    on a virtual bus with a module replying on 0x748. It fails with the
    unpinned window and passes pinned.
  - **Renamed flag.** `probe_local_identifier` and `can_obd_probe` renamed
    `--timeout` to `--read-timeout`; `--timeout` still works as a deprecated
    alias (`add_bus_tool_args(timeout_alias=True)`, with two tests).
  - **Docs.** `docs/abs-wheel-speed-and-did-gap-protocol.md` now says to arm
    passive mode before `scan_constant_dids`, or pass `--no-daemon` on a bench.
  - **Known gap.** `did_sweep` skips the passive check when run *by hand*
    too: it always uses the `job_controller` verdict, because the job argv
    carries no daemon URL or token.

- [x] **R06.2 Pin every script's CLI surface before refactoring one.** This
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

  *Done 2026-09-25.* `tests/test_script_cli_snapshot.py` +
  `tests/fixtures/script_cli_snapshot.json` pin 48 scripts' surfaces: options,
  dest, action, default, type, nargs, choices, required, subcommands. Two
  scripts are hand-parsed and listed in `NO_ARGPARSE`. Defaults read from the
  environment are cleared, and `--device` is pinned as `<machine>`.
  Regenerate with `CANARY_UPDATE_CLI_SNAPSHOT=1`. `test_job_argv_parses_with_the_real_parser`
  runs 13 cases covering all 9 `ALLOWED_JOB_KINDS` through the real parsers,
  and `test_every_job_kind_has_round_trip_params` keeps that list complete.
  `_cli.add_override_reason_arg` / `override_reason` back all 4 `--accept-*`
  flags. `test_every_accept_flag_is_listed` catches a new one. Failing first:
  `test_accept_flag_refuses_a_blank_reason` failed for `retrain`,
  `train_sequence` and `sequence_bakeoff` `--accept-thin-calibration` with
  `""`, `"  "` and `"\t\n"`, and now passes. 84 passed. Not done here: the
  library-level `if calibration_override:` in
  `src/models/isolation_forest.py` (R04's area) still passes a whitespace
  reason from a non-CLI caller. `retrain._stated_reason` is now unused;
  delete it in R06.3.

- [ ] **R06.3 Break up `scripts/retrain.py` (1,749 lines).** *Deferred until R04 lands.*
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

- [ ] *Deferred until R04 lands.* **R06.4 Move trainer and evaluation library code out of `scripts/`,
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

- [~] **R06.5 Operator decision: one corpus benchmark harness?** *Awaiting the
  operator's answer (2026-09-25); nothing implemented.*
  - `detector_benchmark` is retrain's regression gate, with a documented
    blind spot for behavior-deviation features.
  - `evaluate_detectors` (A02.4) calls itself "one protocol".

  Should `detector_benchmark`'s baseline diff fold into `evaluate_detectors`
  and the older grid retire? Record the answer here. If yes, it is a
  separate **M** item after R04.5 fixes the promotion gate.

- [~] **R06.6 Small de-duplications (batch them).**
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

  *Partial 2026-09-25.*
  - Done:
    - `replay._format_timestamp` is deleted; the script uses
      `src.timefmt.format_timestamp`.
    - sha256 was already done in R01.6: `grep hashlib.sha256 scripts tools`
      shows only non-file uses.
    - The session sort key was already done in R01.1: `evaluate_detectors`
      sorts with `session_sort_key`, including `_spread_in_time`.
    - `_cli.add_log_level_arg` (one `LOG_LEVELS` choice set, default kept per
      script) and `_cli.add_bindings_arg` (default `src.paths.BINDINGS_YAML`)
      now serve 19 scripts for `--log-level` and 9 for `--bindings`,
      including `detector_benchmark` and `train_anomaly_model`. The CLI
      snapshot moved in only two places: `dataset_health` and
      `export_dataset` `--log-level` gained the choice set.
    - `src.preprocessing.loader.has_telemetry` is the one "has data"
      predicate: a non-empty `telemetry-*.parquet` chunk. It is used by
      `discover_sessions`, `control.sessions.latest_session`, and the
      hand-rolled checks in `catalog`, `validate_command_pairing`,
      `inventory_behavior_inputs` and `refit_behavior_models`. The last three
      of those accepted empty chunks before.
    - `control.sessions.recorded_sessions(roots)` is the third question, "every
      recording", with captures. It uses `RECORDED_SESSION_KINDS`, keeps the
      first root for a name seen twice, and orders by `session_sort_key`.
    - `refit_behavior_models._discover_behavior_capture_sessions` sorted by name; it now uses
      `session_sort_key`.
    - Tests: `tests/test_recorded_sessions.py`, 4 passed.
  - Remaining:
    - `evaluate_detectors.discover_session_dirs` should become
      `recorded_sessions` once R04 releases the file. Note that its local
      copy includes `_tuning-*` and does not check for data.
    - `tools/backfill_decoded` has its own `telemetry-[0-9]*` check, which
      excludes its own backfill chunks on purpose. The file is dirty from
      another session, and `decode_session_frame` is R04.4.
    - `fp_benchmark --config` is R04's area.
    - `--log-level`/`--bindings` in `retrain`, `train_sequence` and
      `sequence_bakeoff` are blocked on R06.3; edits to those files are
      limited to the `--accept-*` lines until then.
    - `--log-level`/`--bindings` in `evaluate_detectors` are blocked on R04,
      which is editing the file.
    - `canary_companion` and `stop_recording` do not import `_cli`. They are
      deliberately light, and `_cli` pulls in pandas.
    - `tools/` cannot import `scripts/_cli`.

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
