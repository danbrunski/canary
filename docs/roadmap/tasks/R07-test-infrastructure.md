# R07 — test infrastructure

Opened 2026-09-24 from the refactor review ([R00](R00-refactor-README.md)).
The suite has 351 files. Two problems outrank the rest:

- **The suite writes into the repo tree.** There is a memory note about it
  and a "restore the manifests" habit, when it should be impossible.
- **The collector tests hand-roll a fake ECU per file** on the real clock,
  which is the shape T01 has been fixing one test at a time.

## Rules

- Test rewrites are behaviour-preserving. Every rewritten file passes before
  and after, and the T01 controlled-load check (one CPU burner per core,
  `-n auto`) runs after R07.3.
- A new guard that surfaces existing offenders is fixed at the offender, not
  by widening the guard's allowlist.

---

- [x] **R07.1 Stop `src/` importing from `tests/`.**
  - `src/validation/synthetic_bus.py` imports `DEFAULT_VALUES`,
    `ENGINE_ECU`, `FakeClock`, `ScriptedEcu`, `isotp_frames`,
    `support_bits` and friends from `tests.capture_round_fakes` (verified
    2026-09-24: the only `src → tests` import).
  - `tests/capture_round_fakes.py` (427 lines) imports `pytest` at module
    level.
  - `SyntheticEcu(ScriptedEcu)` backs `validation/harness.py`, which
    `scripts/sustained_validation.py` runs.

  **Do:** move `FakeClock`, `ScriptedEcu`, `isotp_frames`, `support_bits`,
  `DEFAULT_VALUES` and the constants into `src/validation/scripted_ecu.py`.
  `install_clock`, the only pytest-dependent function, stays in a test helper.
  `tests/capture_round_fakes.py` re-exports, or its 9 importers are
  repointed. **Accept:** R01.8's layering test also forbids `src → tests`,
  and `import src.validation.harness` works without pytest installed. Size
  **S**.

  **Done 2026-09-25.** `src/validation/scripted_ecu.py` (new) holds
  `FakeClock`, `ScriptedEcu`, `isotp_frames`, `support_bits`,
  `broadcast_sequence`, `DEFAULT_VALUES` and the constants;
  `synthetic_bus.py` imports it. `tests/capture_round_fakes.py` re-exports
  them and keeps `install_clock`, `group_by_stamp` and `stamp_span_seconds`,
  so its 9 importers are unchanged. Evidence:
  `tests/test_layering.py::test_src_never_imports_tests` (every package
  under `src`, `tests.*` and `pytest`) and
  `::test_validation_harness_imports_without_pytest` (a subprocess whose
  import hook refuses `pytest`, `_pytest` and `tests` imports
  `src.validation.harness`); 5/5 pass. `tests/test_sustained_validation.py` +
  `test_replay_row_accounting.py` `-m ""`: 26 passed, 1 xfailed.

- [x] **R07.2 Make test writes into the repo tree impossible.**
  - `DaemonConfig` has 19 repo-relative writable defaults:
    `data/control/{audit,jobs,trip_odometer,catalog.db,…}`, `models` and
    `data/capture_profiles.yaml`. The only guard,
    `conftest._refuse_writes_to_the_real_training_ledger`, covers one of
    them.
  - `tests/test_daemon.py:_config_yaml` says it points "every dir at
    tmp_path" but sets 4 of them. `trip_state` still resolves to the repo,
    and `build_components` builds `GpsTracker(state_path=config.daemon.trip_state)`.
  - Live evidence (2026-09-24): `data/control/trip_odometer.json` was
    rewritten at 10:22 local and `catalog.db` at 01:28. The likeliest writer
    is a test run: the `trip_state` path above, which `test_daemon.py` leaves
    at the repo default, is exactly the file that changed.
    `data/control/` is gitignored, so leaks there are invisible.
  - `data/control/training_runs.jsonl` is 420 MB and 4,252 rows. Every row's
    artifacts point at the repo's `models/`, and 1,507 of them are dated
    2026-06-10 and 1,300 dated 2026-07-27. That looks like pre-guard test
    runs; check it before trusting the ledger's history.
  - Retrain tests:
    - 14 of the 28 `retrain.main` calls in `test_retrain_script.py` omit
      `--if-out`, so output resolves to `models/<slug>/isolation_forest` in
      the repo. Only stubbed trainers keep it clean.
    - All 37 calls across the retrain tests omit `--analyses-dir` (default
      `data/analyses`).
    - 5 omit the feedback flags, so they read the operator's real
      `data/control/anomaly_feedback.jsonl`.
  - Config setup is duplicated per file:
    - 12 `_config_yaml` functions: three AST-identical, two more identical.
    - 13 `_CONFIG_YAML` constants.
    - 59 per-file config builders.
    - 87 files load `canary.example.yaml`, with 167 of those references
      using the cwd-relative string.
  - `validation/harness._SCRATCH_DAEMON_PATHS` is a hand-kept 12-entry
    version of the same rebasing, and it already omits `capture_profiles`.

  **Do:**
  - Add `src.config.rebase_state_paths(config, root) -> CanaryConfig`,
    derived from `DaemonConfig`'s `Path`-typed fields, so a new field is
    covered automatically. The harness uses it.
  - Add `tests/configs.py` with an `isolated_config(tmp_path,
    base=EXAMPLE|MINIMAL, **overrides)` fixture, replacing the
    `_config_yaml` copies.
  - Add a `retrain_argv(tmp_path, *extra)` helper that always injects
    `--if-out/--sequence-out/--ledger/--analyses-dir/--no-feedback-policy`.
  - Backstop in `conftest.py`: an `sys.addaudithook` on open-for-write and
    `os.replace/rename` that fails the test targeting `<repo>/data` or
    `<repo>/models`, plus a `pytest_sessionfinish` diff of tracked files
    there. The ledger guard becomes one case of it.

  **Accept:**
  - A full `pytest` run leaves `git status` and the mtimes under
    `data/control/` and `models/` untouched.
  - The memory note "full pytest clobbers models/ manifests + drops
    data/analyses/ files" is updated to say it is fixed.

  Size **M**.

  **Done 2026-09-25 except the slow/integration half of the acceptance run.**
  - `src.config.rebase_state_paths(config, root, *, keep=())` reads the 19
    `Path` / `Path | None` fields off `DaemonConfig` (a `None` stays `None`),
    refuses a `keep` name that is not one, and raises on any other annotation
    holding paths (`list[Path]`, ...) instead of skipping it
    (`test_path_or_none_fields_are_rebased_and_other_path_shapes_refused`,
    `test_no_daemon_field_holds_a_path_the_rebase_skips`). `validation/harness.harness_config` uses it (keeping only
    `models_dir` and the five per-vehicle inputs), so `capture_profiles` is
    now scratch too; `_SCRATCH_DAEMON_PATHS` is gone.
  - `tests/configs.py`: `isolated_config(tmp_path, "minimal"|"daemon"|"example",
    storage=, keep=, **section_overrides)`, `write_isolated_config`,
    `daemon_test_config` (conftest fixture `daemon_config`) and
    `retrain_argv(tmp_path, *extra, feedback=)`. All 12 `_config_yaml` and 13
    `_CONFIG_YAML` copies are gone (`grep -c` = 0), `test_daemon.py`,
    `test_dashboard_api.py` and `test_control_api.py` included; all 37
    `retrain.main` calls in `test_retrain_script.py`/`test_training_ledger.py`
    go through `retrain_argv`.
  - `tests/repo_guard.py` + `conftest.py`: an audit hook refuses and records
    open-for-write, rename/replace, remove, mkdir, truncate, link and
    `sqlite3.connect` under `<repo>/data` or `<repo>/models`; the autouse
    fixture fails the test even when the code swallowed the
    `PermissionError` (`retrain._ingest_ledger` does). A refusal outside any
    test phase (a module fixture's teardown) fails the run; an xdist worker
    hands it to the controller through `workeroutput`
    (`test_a_refusal_outside_any_test_phase_fails_the_run[0|2]`; the `[2]` case
    fails with the relay disabled, verified). `pytest_sessionfinish` on the
    controller diffs `(mtime, size)` of `data/control/**`, `models/**` and
    every tracked or untracked-unignored file under `data/`/`models/` (a
    child process or pyarrow). It **fails** only on changes the run alone
    could make -- `data/control/**`, or a tracked file that was unmodified at
    the start -- and **warns** on the rest (a file another session already
    had dirty, such as `kline.yaml`; new files in `data/analyses`; model
    binaries), since those may be a concurrent session's
    (`test_only_changes_the_run_alone_could_make_fail_the_end_of_run_diff`;
    a child writing `data/analyses/_r07_probe.json` exits 0 with a warning,
    one writing `data/control/` exits 1, both checked by hand). The ledger
    `JsonlStore` patch is gone; the ledger is one case
    (`test_default_cli_paths_cannot_append_to_the_real_training_ledger`).
    `ALLOWANCES` is empty.
  - Offenders it found, fixed at the test: 10 trend tests
    (`test_control_trends.py` x8, `test_fingerprint_script.py`,
    `test_label_sessions_script.py`) opened the real
    `data/control/catalog.db` -- the 01:28 `catalog.db` mtime above.
  - Evidence: `tests/test_repo_tree_isolation.py` 14 pass. Fast tier
    `pytest -m "not integration and not slow"` (2026-09-25): no guard refusal
    outside the 10 above, no end-of-run diff, `find data/control models
    -printf '%T@ %s %p'` identical before/after (37 files), `git status --
    data models` unchanged. Other failures in that run belong to concurrent
    work (K-line `plan_records` signature, `WorkbenchInputs`).
  - Memory note `project_test_suite_side_effects` updated to "fixed".
  - Ledger history (`data/control/training_runs.jsonl`, 2026-09-25): 4,252
    rows but **40 distinct run_ids**. `created_at` is the run's own creation
    time copied from its manifest; no row carries a write time, so "1,507
    rows on 06-10" is two runs (IF 798, LSTM 709) appended again and again,
    alternating IF/LSTM as one `retrain.main` ingests both manifests. 1,174
    rows carry a `detector_benchmark_report_path` under a pytest tmp dir,
    from 325 distinct `pytest-of-dan/pytest-N` sessions (N 0..1065): test
    writes for certain. The other repeats (2,679 null benchmark path, 395
    without the key) re-ingest the same manifests (1-5 distinct payloads per
    run across hundreds of rows) and cannot be attributed from the file
    alone. All rows are `succeeded` / `unpublished`, none approved or golden.
    Read the first row per run_id; the file was not rewritten.
  - `tests/test_replay_isolation.py` (integration) was broken since 7aceaab
    made the IF `evidence`: it waited on `emitted`, and evidence events count
    in `emitted_evidence` (the role gate refuses `alert` for an artifact
    without a verified alert budget, so a config override cannot make the
    stub alert). It now waits on and compares `emitted + emitted_evidence`;
    both are snapshotted and restored across a replay. Passes (`-m ""`).
  - A clean checkout has no `data/control` (gitignored), so a `mkdir` there
    was refused in CI but a no-op locally: five tests passed only in a
    working tree. The guard now refuses a mkdir (and a remove) under
    `data/`/`models/` whether or not the target exists
    (`test_guard_allows_reads_and_refuses_even_a_no_op_mkdir`), and the five
    (`test_detector_roles` x2, `test_live_sequence_ordering` x3,
    `test_role_gate::test_the_daemon_wires_the_gate_into_components`) build
    their YAML configs through the new `tests/configs.isolate` /
    `isolated_from_yaml`, which moves every writable path not already under
    `tmp_path`. Evidence: `git archive HEAD` plus every other agent's
    working-tree files except the human K-line session's, no `data/control`,
    `pytest -n 6 -m "not integration and not slow"`: 5123 passed, 7 skipped,
    6 errors, all `test_manifest_producibility.py` (needs a git checkout;
    errors at HEAD too).
  ~~**Remainder:** the `slow`, `integration` and `ml_training` tiers have not
  had a full run under the guard.~~ Done 2026-09-25, each tier whole and
  separately, working tree at `b7905fa` plus concurrent agents' uncommitted
  edits, guard armed (autouse fixture + end-of-run diff):
  - `pytest -n 4 -m slow`: 259 passed, 2 xfailed (574 s).
  - `pytest -n 4 -m integration`: 9 passed (69 s).
  - `pytest -n 4 -m ml_training`: 122 passed (231 s).
  - No guard refusal and no end-of-run diff in any of the three, so no test
    needed fixing; `find data/control models -printf '%T@ %s %p'` (61 entries)
    byte-identical before the first run and after the last, `git status --
    data models` empty throughout.

- [x] **R07.3 Build the collector tests' fake buses on `ScriptedEcu`.**
  - `test_can_obd_collector.py` defines 16 hand-rolled bus classes. Seven
    `recv` bodies are AST-identical and use a real `time.sleep`.
    `_SessionGatedBus` re-implements `ScriptedEcu(session_gated=True)` on the
    real clock.
  - `test_multi_pid_batching.py` imports `capture_round_fakes` but still
    keeps:
    - `_isotp_frames`, identical to `isotp_frames`;
    - a `_BatchingEcu` family of 6 classes whose `VALUES` duplicate
      `DEFAULT_VALUES`;
    - `_ScriptedBus`, identical to the one in `test_can_obd_probe.py`.
  - `test_d02_accounting` imports `_BatchingEcu` from that test module.
  - `_single_frame_reply` exists twice, with drift. The
    `test_can_obd_client.py` copy handles 16-bit DIDs and refuses payloads
    over 7 bytes; the `test_control_obd_request.py` copy does neither.

  **Do:** add `ScriptedEcu` options for the missing scenarios (DTC tables,
  freeze frames, per-request NRC scripts), then delete the local classes.
  Share one `single_frame_reply`. **Accept:** ~600 lines are removed, no
  collector test fake calls `time.sleep`, and the T01 load check passes.
  Size **M–L**. Do it after R07.1.

  **Done 2026-09-25 (after R02 landed, 4f68c5b).** `ScriptedEcu` options
  added: `silent` (settable mid-run, e.g. from an `on_send` hook, for key-off
  and late-wake scenarios), `max_batch_pids` + `rejected_batch_sizes`,
  per-PID NRC codes (`nrc_pids` as a mapping), `did_values`,
  `dtc_responses` (Mode 03/07/0A), `session_pending_frames` (`7F 10 78`
  before the reply), and the `messages` / `mode01_requests()` views.
  `single_frame_reply` (the `test_can_obd_client` version: 16-bit DIDs,
  refuses over 7 bytes) lives beside `isotp_frames` in
  `src/validation/scripted_ecu.py`; both copies are gone.
  - `test_can_obd_collector.py`: 12 hand-rolled buses deleted (`_FakeBus`,
    `_NegativeThenPositiveBus`, `_TieredPidBus`, `_DtcCapableBus`,
    `_FreezeFrameCapableBus`, `_SilentBus`, `_AlwaysNegativeBus`, the two
    `_Clocked*` subclasses, `_CustomPidBus`, `_SessionGatedBus`,
    `_BusyThenAnsweringSessionBus`); 23 tests now run on a `clock` fixture.
    Kept: `_StaticDiscovery` and the three `record_can_obd` fault buses
    (`_ReconnectableObdBus`, `_GoodObdBus`, `AnsweringBus`), which do not
    sleep.
  - `test_multi_pid_batching.py`: `_isotp_frames`, the six-class
    `_BatchingEcu` family and `_ScriptedBus` deleted; `_BatchingEcu.VALUES` is
    `DEFAULT_VALUES` minus the fuel trims (`BATCHING_VALUES`).
    `tests/fakes.CannedResponseBus` replaces `_ScriptedBus` here and in
    `test_can_obd_probe.py`. `test_d02_accounting.py` no longer imports a
    test module (`BATCH_REFUSAL_WINDOW_SECONDS` moved to
    `capture_round_fakes`).
  - `test_can_obd_client.py::test_discover_supported_pids_merges_multiple_responders`
    runs on `ScriptedEcu(support_responders=...)` with `time_fn=clock.monotonic`.
  - `ScriptedEcu(stamp_skew=)` offsets the frame's driver stamp from the fake
    wall clock. `test_query_with_tap_records_the_frame_arrival_and_its_clock`
    uses -10 ms and asserts `arrival.at == stamp != clock.time()`; with
    `frame_arrival` mutated to return `now` for a kernel stamp it fails
    (verified), as the HEAD version did. No other rewritten test compares a
    frame stamp to "now" (the batch/round-stamp tests compare readings with
    each other).
  - Evidence: same test counts before/after (68 + 37 functions); 204 passed
    across the six rewritten files; `git diff --stat` on them: 341 insertions,
    922 deletions (net -581). `grep time.sleep` in the six files: 0. T01 load
    check, 12 busy-loop burners on 12 cores, `pytest -n auto -m ""` over the
    six files: 3/3 green (204 passed each, 1-min load 10.3-15.9). Other
    `ScriptedEcu` users re-run: capture rounds, hybrid scheduling, discovery,
    start latency, calibration id, multi-DID batching, sustained validation:
    198 passed, 1 xfailed.
  - Not changed: `test_can_collector.py`'s receive-only buses (sleeps included;
    "looked at and fine" below) and the R02.5 `_ScriptedBus` in
    `test_can_obd_client.py`, already on a fake clock.

- [x] **R07.4 Reset process-global collection state in one autouse fixture.**
  - 12 files reset `tx_gate` and `tuning_profile` by hand, each clearing a
    different subset.
  - Only `test_tx_gate` resets `ecm_session_claims`. Only
    `test_capture_profiles` resets `bus_budget` and `capture_lifetime`. Only
    `test_capture_start_latency` un-pauses.
  - 29 files touch these modules.

  **Do:** add `src.collection.reset_process_state_for_tests()`, covering
  every module-level global in one place, and call it from an autouse
  conftest fixture. **Accept:** a test that sets each global and asserts the
  next test sees the default. Order-dependence within an xdist worker is
  gone. Size **S**.

  **Done 2026-09-25.** The function is
  `src.collection.process_state.reset_process_state_for_tests()` (a new module,
  not `src/collection/__init__.py`), calling each module's own reset:
  `tx_gate` (passive, TTL, suppressed count, ECM session claims),
  `bus_budget`, `tuning_profile` (new `_reset_for_tests()`: every module
  global back to its import-time value, `_configured` included),
  `discovery`, `queueing` (new `_reset_for_tests()`: the parse-error
  throttles and the `_UNATTRIBUTED_QUEUE_DROPS` counter), `capture_lifetime`
  and `capture_generation`. `tests/conftest.py::_reset_collection_process_state`
  runs it before and after every test. Evidence,
  `tests/test_process_state_reset.py` (4 pass):
  `test_reset_returns_every_global_to_the_start_up_value` dirties all 11
  observables, asserts each one moved, resets and compares equal; the
  `TestTheNextTestSeesTheDefault` pair fails under `--noconftest -n0`
  (verified: 1 failed, 1 passed) and passes with the fixture;
  `test_every_collection_module_with_globals_is_covered` fails when a
  `src/collection` module gains a `global` statement without joining
  `COVERED_MODULES`; `test_every_module_level_mutable_is_reset_or_justified`
  fails on any module-level mutable literal or object instance (not a known
  immutable call) that is neither in `process_state.COVERED_GLOBALS` nor in
  the test's `NOT_STATE` allowlist, each entry with its reason, and on stale
  entries. It is the test that would have caught `_UNATTRIBUTED_QUEUE_DROPS`
  (a `QueueDropCounter` instance, no `global` statement). 5 pass.
  Not covered: `kline.sessions.MODULE_SESSIONS` (its module is uncommitted
  K-line work; one `MODULE_SESSIONS.reset()` line when it lands), and,
  deliberately, `capture_profiles._SERVING_DID_CACHE` (keyed by path + mtime)
  and the `obd_scheduler.spec` `lru_cache` loaders (static catalogue files).
  The hand resets in individual files are left in place (redundant now).

- [x] **R07.5 Add `tests/builders.py` and `tests/fakes.py`; stop tests
  importing other test modules.**
  - There are 18 imports from one test module into another. Examples: `from
    tests.test_daemon import config` in two files, which pulls in all 2,803
    lines; and the chain `test_trip_speed_sources → test_trip_end_digest →
    test_digest → test_fingerprint`.
  - A fake trained IF model is copied 13 times, 9 of them in
    `test_daemon.py`. The copies disagree on whether they emit
    `calibrated_threshold`/`calibrated_anomaly`, which decides whether the
    non-emitting path is what actually gets exercised.
  - `_FakeLSTMResult` is copied 3 times, though the real type is a frozen
    dataclass (`SequenceInferenceResult`).
  - Other copies:
    - `_write_session`: 15 copies. Two are AST-identical, and a third is one
      channel off.
    - `_write_obd_session`: 3 identical copies.
    - `_frame`: 3 identical copies.
    - `_IFConfig`: 2×2.
    - `_FakeProc`: 3 copies, each equivalent to
      `subprocess.CompletedProcess`.
    - `_client_with_handler`: 3 copies that poke `DaemonClient._client`. A
      `transport=` kwarg on the constructor fixes that.

  **Do:** add shared builders (`write_obd_session`,
  `fake_if_model(label=, calibrated=)`, `lstm_result(**kw)` built on the real
  dataclass) and promote the shared fixtures to `conftest.py`. **Accept:** no
  `from tests.test_` imports remain. Size **M**.

  **Done 2026-09-25; the acceptance holds, some copies remain.** `tests/builders.py`
  (`write_obd_session`, `write_fixture_session`, `write_rich_session`,
  `digest_config`, `IFConfig`, the behavior-validation and capture-round
  builders, `sequence_training_frame`) and `tests/fakes.py`
  (`fake_if_model(label=, calibrated=)`, `StubModelManager`,
  `lstm_result(**kw)` on the real `SequenceInferenceResult`,
  `completed_process`, `daemon_client_with_handler`, `slow_digest`,
  `FakeLsmBus`, `CannedResponseBus`). `conftest.daemon_config` replaces `from
  tests.test_daemon import config`. **All 18 `from tests.test_` imports are
  gone** (`grep -rn "from tests\.test_" tests/` = 0; the last, in
  `test_d02_accounting`, went with R07.3); the `test_trip_speed_sources ->
  test_trip_end_digest -> test_digest -> test_fingerprint` chain is cut.
  Moved: the 3 identical `_write_obd_session`, the 3 identical `_frame`
  (sequence training), `_IFConfig` 2x2 (the 40-tree pair passes
  `n_estimators=40`), 2 of 3 `_FakeLSTMResult`, 2 `_FakeProc`, 2
  `_client_with_handler`, 7 fake IF models (6 in `test_daemon.py`, 1 in
  `test_replay_isolation.py`; each now says `calibrated=`). Tests of every
  touched file pass (`-m ""` where slow/ml: 418 + 96 + 153 + 116 passed).
  Not done (beyond the acceptance, for whoever is next in these files):
  - `_write_session`: 16 copies, not consolidated.
  - Fake IF models left: 3 in `test_daemon.py` (manifest-schema, inf-score and
    contribution variants), `test_alerts_emitter.py`,
    `test_isolation_forest_evaluation.py`, `test_inference_profile_gate.py`;
    `_FakeLSTMResult` in `test_alerts_emitter.py` (another session's
    uncommitted file at the start of this work).
  - `DaemonClient(transport=)` needs `src/mcp_server/client.py`; 9 test files
    still swap `client._client`.

## Looked at and fine

- `test_can_collector.py`'s 12 receive-only buses: small and
  scenario-specific.
- `tests/waits.py` and the wall-clock deadlines: T01.8.
- The conftest slow-marker loader: sound.
- `pyproject` `pythonpath = ["."]`: correct (see R06.4).
