# R06 — scripts and tools

Opened 2026-09-24 from the refactor review ([R00](../R00-refactor-README.md)).
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
- **Script paths other things call stay.** `src/control/jobs.py` launches
  `retrain.py`, `refit_behavior_models.py` and `did_sweep.py` by path; the
  `train_lstm.py` / `train_tcn.py` shims stay as stable command names (no job
  launches them -- corrected 2026-09-25).
- **Moves under `src/models`/`src/preprocessing` trigger the `ml_training`
  tier.**

---

- [x] **R06.1 Give `tools/` a shared bus-tool module.**
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
  *Both remainders done 2026-09-25.* (1) `load_widths` lives beside
  `DidSnapshot` in `src/analysis/did_scan.py`, with the record discriminator it
  reads (`RECORD_KEY`, `record_kind`); `tools/_bus_tool.py` re-exports all
  three, and `did_log`/`did_sweep` import `load_widths` from `did_scan`. (2)
  `import_uprev_log.FILENAME_TIME` is public; `watch_uprev_logs` imports it,
  and `test_no_tool_imports_another_tools_private_names` now asserts no
  offender at all (the allowance set is deleted). `test_bus_tool.py`,
  `test_did_log_tool.py`, `test_did_sweep_tool.py`, `test_did_sweep_job.py`,
  `test_did_scan.py`, `test_import_uprev_log.py`,
  `test_analyze_can_memory_log_tool.py`, `test_can_memory_log_tool.py` pass
  (128 with `test_emit_dbc`/`test_backfill_decoded`);
  `tools/watch_uprev_logs.py --help` runs.

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

- [x] **R06.3 Break up `scripts/retrain.py` (1,749 lines).**
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

  *Done 2026-09-25.*
  - Holdout pickers: `src.models.evaluation.pick_calibration_holdouts` /
    `pick_evaluation_holdouts` (+ `session_parquet_bytes`,
    `session_mode_coverage`, `session_holdout_health`, the
    `MIN_CALIBRATION_HOLDOUT_*` / `EVALUATION_HOLDOUT_DRIVES` constants and
    `nothing_left_to_train`). `sequence_bakeoff` uses them from there.
  - Staging and publish: `src/models/publish.py` (`staging_dir`,
    `artifact_name`, `write_artifact_verdict`, `publish_staged`,
    `set_aside_refused`, `ISOLATION_FOREST_SOURCE`/`SEQUENCE_SOURCE`).
  - `_cli.filter_sessions_by_boundary` + `_cli.apply_since_boundary(args,
    config, sessions)`; both copies of the `--since-boundary` block are gone.
    The helper reads the *profile-scoped* vehicle ledger, where the daemon
    records a calibration change; retrain's copy read the unscoped one (refit's
    was scoped). `test_retrain_since_boundary_excludes_older_sessions` now
    records the boundary in the profile ledger (not re-run against HEAD).
  - `main` is 85 lines over a `RetrainRun` dataclass (`args` is never written:
    `grep -c "args\.[a-z_]* = " scripts/retrain.py` → 0) and phase functions
    `_start_run`, `_select_sessions`, `_hold_out`, `_skip_disabled`,
    `_prepare_inputs` (→ `TrainingInputs`), `_stage_alert_role_outputs`,
    `_run_trainers`, `_run_benchmark_gate`, `_run_alert_budget_gate`,
    `_publish_or_refuse`, `_ingest_ledger`, `_print_report`, `_exit_code`.
    `RetrainRun.train_dir(source)` is the staging dir while the budget gate is
    pending, `served_dir(source)` the served one. `retrain._stated_reason`
    deleted. `parse_args` is 276 lines (flag definitions only).
  - R06.2 snapshot: `retrain.py`'s surface unchanged (the only regenerated
    entry is `evaluate_detectors --log-level`, R06.6).
  - Seams retargeted in `test_retrain_script.py`, `test_training_ledger.py`,
    `test_alert_budget.py`, `test_sequence_bakeoff.py`:
    `retrain.if_trainer.train` → `training_entry.train_isolation_forest`,
    `retrain.lstm_trainer.train`/`tcn_trainer.train` →
    `training_entry.train_sequence_detector` (backend first),
    `retrain._session_*` / `_pick_*` → `src.models.evaluation`'s public names,
    `evaluate_detectors.main` → `retrain.run_evaluate_detectors`,
    `_run_benchmark_gate(config, sessions, args)` → `(…, retrain._start_run(args,
    config))`. `pytest -n 4 -m "not integration and not slow"` on those four
    + `test_train_sequence_columns.py`: 91 passed; with `-m ""` 889 passed
    over 22 touched files (R06.4).
  - Real laptop retrain, HEAD archive vs HEAD + this diff (the other session's
    K-line files excluded), 9 sessions from
    `~/canary-data/sessions/nissan-xterra-2008` (2026-07-26..08-06), smoke
    config (IF 100 trees, LSTM `max_epochs: 3`, cpu), explicit
    `--if-out/--sequence-out/--ledger/--analyses-dir` in the scratchpad,
    `--no-feature-cache --no-feedback-policy --accept-thin-calibration`
    (this corpus is thin) and `--skip-benchmark`: 8m46s vs 9m44s, rc 0 both.
    Same holdouts (calibration 2026-08-04T162651, 08-06T032414, 08-06T144349;
    evaluation 07-26T223147, 07-29T205841, 07-31T145209). Both manifests: 0
    differences apart from timestamps. IF: modes idle+koeo, all 200 trees'
    node/value arrays, `offset_`, feature subsets and the scaler identical.
    LSTM: every `state_dict` tensor identical (best epoch 3, 521 windows).
    Ledger: 2 rows each, identical apart from timestamps and
    `artifact_sha256`, which differs only because the binaries embed their
    manifest's timestamps. Printed report identical. Not in the comparison:
    the benchmark gate. With the gate on, the HEAD run had not finished the
    grid after 40+ min, so both runs were stopped and repeated with
    `--skip-benchmark`. The gate's code path is covered by
    `test_run_benchmark_gate_runs_real_grid_and_discovers_prior_baseline`
    (real grid, real IF), which passes.

- [x] **R06.4 Move trainer and evaluation library code out of `scripts/`,
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

  *Done 2026-09-25.*
  - New homes: `src/models/training_entry.py` (`train_sequence_detector`,
    `build_sequence_dataset`, `train_isolation_forest`, `benchmark_held_out`,
    `SEQUENCE_BACKBONES`); `src/preprocessing/feature_pipeline.py`
    (`load_resampled_session`, and `disposition_screen_frames`, which was
    `disposition_report._scan_sessions`, imported by `retrain` and `ledger`);
    `src/models/detector_benchmark.py` (the grid, rollup, diff and
    `load_trained_sequence` behind retrain's gate). The scripts keep their old
    names as aliases/re-exports (`train_sequence.train`, `train_anomaly_model.train`,
    `detector_benchmark.run_grid` …).
  - No script imports another script except `_cli`:
    `tests/test_script_imports.py::test_no_script_imports_another_script_except_cli`
    (51 scripts; the AST scan found 17 sibling imports in 7 scripts at HEAD).
    Retrain's alert-budget gate runs `evaluate_detectors.py` as a process
    (`retrain.run_evaluate_detectors`), not an in-process `main()`. The
    `train_lstm`/`train_tcn` shims stay (stable command names; no job launches them) and execute
    `train_sequence.py` with `runpy.run_path`, which registers no module, so
    there is no second identity (a load by path, not an import).
  - `grep -rn "sys.path" tests`: only `tests/test_script_cli_snapshot.py`
    (it loads each script as `python scripts/x.py` runs it, via a scoped
    `monkeypatch.syspath_prepend`, and now drops the bare modules it loaded:
    `_forget_bare_script_modules`) and `tests/test_script_imports.py` (names
    the calls it forbids). `test_no_test_edits_sys_path` enforces it. The 22
    files that had one at HEAD now import `scripts.<name>`. Every script with a
    bare `from _cli import` now has the `except ImportError: from scripts._cli`
    fallback (18 converted), enforced by
    `test_a_bare_cli_import_falls_back_to_the_package`.
  - `pyproject` `pythonpath = ["."]` unchanged. `ml_paths.txt`: the new
    homes are under `src/models/` and `src/preprocessing/`, already listed;
    `scripts/train_lstm.py`/`train_tcn.py` added (their tests are
    `ml_training`).
  - Tests retargeted to the public seams: `test_train_sequence_columns.py`
    patches `training_entry`; `test_train_anomaly_model.py` imports
    `build_feature_frame` from `feature_pipeline`;
    `test_resample_config_construction.py` spies `disposition_screen_frames`.
    `pytest -m "" -n 4` over the 22 touched model/script test files
    (incl. `test_train_lstm.py`, `test_train_tcn.py`,
    `test_false_positive_benchmark.py`, `test_sequence_bakeoff.py`): 889 passed.

- [x] **R06.5 Operator decision: one corpus benchmark harness?** *Answered
  2026-09-25 (R00): yes — fold the baseline diff into `evaluate_detectors`
  and retire the grid. Implemented 2026-09-25, below.*
  - `detector_benchmark` is retrain's regression gate, with a documented
    blind spot for behavior-deviation features.
  - `evaluate_detectors` (A02.4) calls itself "one protocol".

  Should `detector_benchmark`'s baseline diff fold into `evaluate_detectors`
  and the older grid retire? Record the answer here. If yes, it is a
  separate **M** item after R04.5 fixes the promotion gate.

  *Done 2026-09-25.*
  - **One protocol.** `src/models/benchmark_gate.py` (new) is the regression
    view of an `evaluate_detectors` report: `gate_view` (per detector x
    realistic fault x severity recall = detected/graded, median time to
    detect; per detector false-alarm episodes per engine-hour over the
    in-era drives, computed for every *served* detector whatever its role),
    `diff_reports`, `incomparable_reason` (protocol name/version, the scored
    drives, seed/faults/injections/window/lead/grace/persistence, holdout),
    `regression_summary`, and `load_sequence_artifact` (the backend the
    artifact's manifest names). `evaluate_detectors` stamps
    `report["protocol"]` and gained `--baseline`/`--tolerance` (exit 3 on a
    regression, 2 on an unreadable baseline; a "Regression compare" section
    in the Markdown).
  - **Retired:** `scripts/detector_benchmark.py`,
    `src/models/detector_benchmark.py` and `tests/test_detector_benchmark.py`
    are deleted; `scripts/ml_paths.txt` lists `evaluate_detectors.py` in
    their place. A grid-era report is refused *by name* everywhere
    (`RetiredProtocolError`: "report is from the retired detector_benchmark
    grid (R06.5)"): the gate diff calls it incomparable, the promotion gate
    fails on it (either side), `compute_golden_divergence` returns
    `incomparable` instead of numbers.
    `synthetic_fault_eval.evaluate_detectors` (the per-frame harness the grid
    looped) stays: `test_synthetic_fault_eval.py`'s acceptance floor uses it.
  - **Retrain** (phase structure unchanged; `_run_benchmark_gate` still sits
    between the trainers and the alert-budget gate): the gate runs the
    harness in its own process (`_evaluate_on_drives(..., injections=True)`:
    every realistic fault at the harness defaults, `BENCHMARK_SEED = 42`, no
    baselines, no shift test, `--cache-dir` beside `--feature-cache`) on the
    evaluation holdout, with the IF and sequence artifacts this run wrote,
    writes `benchmark-<ts>.json` (+ `.md`), and diffs the newest comparable
    prior report. Still skips (logged) with no holdout or no IF artifact;
    still exits 1 on a regression. `--skip-benchmark`,
    `--benchmark-baseline`, `--benchmark-tolerance` unchanged.
  - **Stricter than the grid:** a cell the baseline graded and the current run
    did not (withheld detector, every window masked) is `missing` and
    **regresses** — R04.5's fail-on-missing semantics, now in the retrain diff
    too (the grid recorded `removed` and passed).
  - **Promotion gate** (`promotion_gate.py`) reads both reports through
    `gate_view`: `_check_recall` keys cells `(detector, fault, severity)`;
    `_check_fp_budget`/`_check_agreement` read false alarms per engine-hour
    (the 5.0 ceiling's unit changed from the grid's alerts per scored hour).
    R04.5's fail-on-missing is kept: `test_promotion_gate.py` (27 pass,
    fixtures rebuilt as `tests.builders.evaluation_report`) still has
    `test_gate_fails_a_candidate_whose_detector_was_withheld_where_golden_was_graded`,
    `..._with_no_recorded_false_positive_rate`,
    `..._missing_a_nominal_detector_the_golden_has`, plus
    `test_gate_refuses_a_grid_era_report_on_either_side_by_name` and
    `test_golden_divergence_names_a_grid_era_report_instead_of_numbers`.
  - **Blind spot closed.** The grid featurized with no behavior models, so an
    IF trained on `deviation:*` columns could not be scored as served. The
    harness now featurizes every frame (clean, injected slices) with the
    profile-scoped `daemon.behavior_models` (or `--behavior-models`), records
    `report["behavior_models"]` (path, sha256, B03.4 provenance per consumed
    model), and refuses (exit 2) an IF whose deviation column's model is
    absent. `test_rows_are_featurized_with_the_behavior_models`,
    `test_an_if_whose_deviation_columns_cannot_be_produced_is_refused`. (No
    artifact on disk consumes a deviation column today, so no number moves.)
  - **TCN.** The harness loaded LSTM artifacts only; it now loads through the
    registry by manifest `model_type`, so a TCN retrain keeps sequence
    coverage in the gate. Smoke: `models/_bakeoff/tcn_autoencoder` + the `na`
    IF on `2026-09-23T162918`: `sequence`/`sequence_global`/`fused` scored
    620 of 649 rows.
  - **Tests:** `tests/test_benchmark_gate.py` (new, 12 pass);
    `test_evaluate_detectors_script.py` (+3:
    `test_baseline_compare_exits_3_on_a_regression_and_names_incomparable_baselines`
    and the two above); `test_retrain_script.py`:
    `test_benchmark_gate_asks_the_harness_for_every_fault_on_a_fixed_seed`
    and `test_run_benchmark_gate_runs_the_evaluation_protocol_and_discovers_prior_baseline`
    (real harness subprocess: grid-era report passed over by name, newest
    comparable found, a baseline-graded cell the run lacks regresses), 41
    pass; `test_training_ledger.py` 35 pass; R06.2's
    `test_script_cli_snapshot.py` 83 pass with the snapshot regenerated
    deliberately (`detector_benchmark.py` removed; `evaluate_detectors`
    `--baseline`/`--tolerance`/`--behavior-models`; `--cache-dir` default
    repo-rooted) and `test_job_argv_parses_with_the_real_parser` unchanged.
    `-m ml_training` on `test_train_lstm.py`, `test_train_tcn.py`,
    `test_false_positive_benchmark.py`, `test_sequence_bakeoff.py`: 52 pass.
  - **Before/after on a real corpus** (scratchpad only): the three
    2026-09-23 NA candidates (`models/_candidates/2026-09-23-{na,na-r2,na-r3}`,
    IF only) plus two derived from `na` (thresholds set to -1e6, "flood", and
    +1e6, "blind"), on held-out `2026-09-23T{145645,150658,162918}` (one
    physical drive, 0.40 engine-h, same era as the candidates' training).
    Retrain gate, baseline → candidate, grid | evaluate_detectors:
    na→na-r2 pass|pass, na-r2→na-r3 pass|pass, na→na-r3 pass|pass (the
    deploy order); X→flood REGRESSED|REGRESSED for every X (grid: 0 → 214.6
    alerts/h; eval: 0 → 22.7 FA/engine-h and every graded cell now masked);
    na↔blind pass|pass; self-compares pass|pass. Promotion gate (golden,
    candidate): the same eligible/refused verdict in 21 of 25 pairs; the 4
    others are golden na-r2/na-r3 with candidate na/blind (difference 1).
    Of the 25 retrain-gate pairs 17 agree; the 8 that differ are the two
    families below. Differences, explained: (1) {na-r2,na-r3}→{na,blind}
    regress on the grid, not on eval: grid drift recall 1/50 → 0/50, one injection, the
    noise the grid's own tolerance comment describes (all three candidates
    grade 0/28 on the realistic faults; the IF withholds 85 % of these rows).
    (2) flood→{na,na-r2,na-r3,blind} regresses on the grid (recall 0.44-0.54
    → 0), not on
    eval: the grid counted a detector already firing on every clean row as
    catching the fault; eval grades those windows `masked`, so there is no
    recall to lose (the promotion gate refuses both ways on agreement).
    Timing (IF only): grid 540-640 s per artifact uncontended; harness 430 s cold, 70-124
    s with its frame cache warm.

- [x] **R06.6 Small de-duplications (batch them).**
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
  - Finished 2026-09-25 (with R06.3/R06.4):
    - `evaluate_detectors.discover_session_dirs` is gone; the harness asks
      `control.sessions.recorded_sessions(roots, kinds=EVALUATION_SESSION_KINDS)`.
      `recorded_sessions` gained a `kinds` argument (it refuses
      `SessionKind.REPLAY`); the harness adds `SessionKind.TUNING`, so a legacy
      `_tuning-*` recording still bridges its siblings into one drive exactly
      as the local copy did, and R04.6 still keeps its rows out of scoring.
      One difference, named: a directory with no non-empty telemetry chunk is
      no longer listed, so it drops out of the report's "skipped: no telemetry
      rows" list (it was never part of a drive). Test:
      `test_discovery_keeps_captures_drops_quarantine_and_orders_by_parsed_time`.
    - `--log-level`/`--bindings` via `_cli` in `retrain`, `train_sequence`,
      `sequence_bakeoff` and `evaluate_detectors`. The CLI snapshot moved in
      one place: `evaluate_detectors --log-level` gained the `LOG_LEVELS`
      choice set (default still `WARNING`). Retrain's surface is unchanged.
    - `retrain._stated_reason` deleted.
    - `fp_benchmark --config` via `_cli.add_common_args` (+ `load_config`).
  - Still open:
    - `tools/backfill_decoded` keeps its own `telemetry-[0-9]*` check, which
      excludes its own backfill chunks on purpose (R04.4's area).
    - `canary_companion` and `stop_recording` do not import `_cli`. They are
      deliberately light, and `_cli` pulls in pandas.
    - `tools/` cannot import `scripts/_cli`.
  - Closed 2026-09-26. Every asked de-duplication is done or routed. The
    "Still open" bullets are deliberate exclusions, not remaining work:
    - The first bullet is moot. `a09dcd4` (P02.8) retired
      `tools/backfill_decoded.py` together with its test, because decoded
      rows are now decoded on read. `grep -rn backfill_decoded src scripts
      tools tests` finds only a historical mention in
      `src/preprocessing/decode.py`'s docstring. The
      `decode_session_frame` bullet above (R04.4) went with it.
    - `canary_companion.py` and `stop_recording.py` still do not import
      `_cli`. They stay light on purpose, since `_cli` imports pandas.
    - `tools/` has no `from scripts` import, and `scripts/` is not a package
      (no `__init__.py`).

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
