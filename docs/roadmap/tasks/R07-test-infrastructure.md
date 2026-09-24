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

- [ ] **R07.1 Stop `src/` importing from `tests/`.**
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

- [ ] **R07.2 Make test writes into the repo tree impossible.**
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

- [ ] **R07.3 Build the collector tests' fake buses on `ScriptedEcu`.**
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

- [ ] **R07.4 Reset process-global collection state in one autouse fixture.**
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

- [ ] **R07.5 Add `tests/builders.py` and `tests/fakes.py`; stop tests
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

## Looked at and fine

- `test_can_collector.py`'s 12 receive-only buses: small and
  scenario-specific.
- `tests/waits.py` and the wall-clock deadlines: T01.8.
- The conftest slow-marker loader: sound.
- `pyproject` `pythonpath = ["."]`: correct (see R06.4).
