# T01 — Test-suite reliability

Opened 2026-09-23, when the new pre-push gate (`scripts/git-hooks/pre-push`)
first ran. A gate is only as trustworthy as the suite under the conditions it
runs in, and on this laptop that includes other agent sessions running their
own tests. Same conventions as the other task files: `[x]` only with the
measurement in hand.

- [x] **T01.1 `tests/test_daemon.py` fails under host load and passes alone.**
  These tests start a real daemon, including spawned children, and give it
  1–5 s of wall clock (`asyncio.wait_for(..., timeout=1.0 | 2.0 | 5.0)` for a
  published message, a task finishing or the daemon shutting down).

  | run | commit | load average | failed |
  |---|---|---|---|
  | local CI reproduction, `-n auto` (12 workers) | `bad00f1` | not recorded | `test_run_daemon_end_to_end_telemetry_published_to_ws` (TimeoutError) |
  | the same test alone, `-n0`, three times | `bad00f1` | — | 0 of 3 |
  | pre-push gate, `-n auto` | `c7756f9` | 8.9–12.9 on 12 cores | six: `wires_retention_sweeper_task`, `does_not_start_online_training_scheduler_by_default`, `wires_online_training_scheduler_when_enabled`, `end_to_end_inference_emits_anomaly`, `disable_live_inference_skips_inference_task`, `end_to_end_telemetry_published_to_ws` |
  | `tests/test_daemon.py` alone, `-n auto` | `c7756f9` | 8.9–10.8 | 0 of 68 |
  | pre-push gate, `-n auto` | `9073929` | 9.1–9.7 (1/5-min) | `tests/test_can_obd_collector.py::test_negative_pid_response_does_not_block_following_pids`, passed serially |
  | `scripts/ci_gate.sh`, `-n auto` | `80fde31` | 7.6–11.7 | `test_run_daemon_end_to_end_telemetry_published_to_ws` again, passed serially in 4.5 s (third time in one day) |
  | `scripts/ci_gate.sh`, `-n auto` | `f0b578c` | not recorded | `test_run_daemon_end_to_end_telemetry_published_to_ws` (fourth time); `tests/test_multi_pid_batching.py::test_healthy_channels_are_not_backed_off_by_a_failed_batch`, both passed serially |
  | GitHub CI (run 35905954599), `-n auto` | `ab6676b` | not recorded | `tests/test_daemon_startup_order.py::test_run_daemon_records_while_the_live_source_is_still_building` (`assert []`: no parquet yet) — T01.5 |
  | controlled load, before the fix: 9 runs beside 12 busy-loop burners (below; the first four over `test_daemon.py`, `test_can_obd_collector.py`, `test_multi_pid_batching.py`, `test_daemon_startup_order.py`, three of them with `-x`) | `145e223` | 8.2–20.7 (1-min, at end of run) | every run: `end_to_end_telemetry_published_to_ws` 9/9, `starts_retention_sweeper_and_protects_active_session` 9/9, `does_not_start_online_training_scheduler_by_default` 4/9, `test_healthy_channels_are_not_backed_off_by_a_failed_batch` 7/9, `test_loop_falls_back_when_the_ecu_refuses_batches` 6/9 (T01.6) |
  | controlled load, after the fix: 5 runs, same command | `145e223` + T01 diff | 10.9–21.4 | 0 of 194, five times |
  | `tests/test_daemon.py` alone beside the burners, after | `145e223` + T01 diff | 15.1–29.6 | 0 of 68, five times |
  | two burners per core, after | `145e223` + T01 diff | 27.5–36.6 | 0 of 194, three times |

  The pre-push hook now re-runs parallel-run failures serially once, and names
  a failure that passes serially as flaky. That keeps the gate usable. It is
  not the fix.

  Controlled load (2026-09-23, 12 cores, `stress-ng` not installed): one
  `.venv/bin/python -c "while True: pass"` per core, started 5 s before and
  killed after each run, beside
  `.venv/bin/pytest -n auto -q -rf -p no:cacheprovider tests/test_daemon.py tests/test_can_obd_collector.py tests/test_multi_pid_batching.py tests/test_daemon_startup_order.py tests/test_startup_timeline.py tests/test_replay_isolation.py`.
  - [x] Capture the failing tracebacks under controlled load: run
    `tests/test_daemon.py` with `-n auto` beside a CPU burner of one process per
    core. Record which `wait_for` expires and what the daemon was doing: which
    child was still spawning, and whether shutdown or the first publish was late.
    **Shutdown was late, every time, and always for the same reason.** All 13
    `test_daemon.py` failures in the 6 runs that kept full tracebacks (the
    other 3 kept only the summary) are the shutdown join
    `wait_for(daemon_task, timeout=5.0)` expiring (lines 873, 1112 and 2583 at
    `145e223`) while `run_daemon`'s trip-end fallback sat in
    `compute_digest_off_loop` → `wait_for(future, 900.0)`: the digest of the
    active session in a fresh `spawn` child (a new interpreter importing pandas
    and the analysis stack). The cancel logged `digest skipped for …
    (interrupted_at_shutdown)`. The first publish was never late:
    `end_to_end_telemetry_published_to_ws` had its three records before it
    failed on the join. No collector was involved (these tests pass
    `collectors=[]` or a fake).
  - [x] Replace wall-clock allowances with conditions the test controls: an
    event the daemon sets when the awaited stage is reached, a fake clock, or a
    stubbed spawn. Don't just raise the numbers: a larger timeout only moves
    the load at which it fails.
    Stubbed spawn: `tests/conftest.py::no_trip_end_digest_child` replaces
    `src.daemon.runtime.handle_trip_end_off_loop` with a recorder, applied by
    `pytestmark` in every module that starts `run_daemon` (`test_daemon.py`,
    `test_daemon_startup_order.py`, `test_startup_timeline.py`,
    `test_replay_isolation.py`); the digest child keeps its own tests in
    `tests/test_trip_end_digest.py`. Conditions: new `tests/waits.py`
    (`wait_until`, `settle`, `StopWhen`, `hang_guard_deadline`). Every
    fixed-count poll loop and sleep-then-assert-it-happened in `test_daemon.py`
    now waits for what it asserts: the sweeper's deletion, the spy being
    called, the poller's v2 swap, a second poll after the exception, a skipped
    tick, two fallback observations, the derived channels reaching the writer,
    and the startup timeline's `collector_tasks_created` /
    `first_record_stored` stages. The one number left is `HANG_GUARD_SECONDS`
    (30 s), which only turns a hang into a failure. Measured against it: the
    slowest wait, beside two burners per core (load 27–36), was `run_daemon`'s
    shutdown at 6.0 s, waiting for the dashboard app whose cold import in its
    build thread took 5.7 s; the slowest whole test was 10.9 s. That is real
    work proportional to load, so a big enough load still reaches the guard,
    about 5x above the worst measured here. No production code changed.
  - [x] Acceptance: `tests/test_daemon.py` passes 5 of 5 under `-n auto` beside
    the CPU burner. 68 passed, five runs of
    `.venv/bin/pytest tests/test_daemon.py -n auto -q -rf -p no:cacheprovider`
    beside 12 burners (1-min load 15.1–29.6 at end of run); also 194 passed,
    five runs of the six-file command above (10.9–21.4), and three runs with
    two burners per core (27.5–36.6). Before the fix, the same six-file
    command failed 9 of 9.
  - [x] Every test the gate names as "flaky under load" gets a row in the table
    above, and an item here if it is not a `test_daemon.py` test. The stamp in
    `.git/ci-gate/<sha>` records them.
    The two stamps that name any (`80fde31`, `f0b578c`) are rows above; the
    non-`test_daemon.py` tests are T01.2 (`9073929` gate), T01.3 (canary-a2's
    gate), T01.4 (`f0b578c` stamp), T01.5 (CI run 35905954599) and T01.6
    (found by the controlled-load runs).

- [x] **T01.2 `test_negative_pid_response_does_not_block_following_pids` fails under
  load.** In `tests/test_can_obd_collector.py`. It failed once in the parallel
  gate on `9073929` and passed serially in 1.8 s. Not investigated yet: find
  what it waits on in wall-clock time, and apply T01.1's remedy.
  Its stop condition ("RPM read and two requests sent") never fired: RPM holds
  the priority lane, so the one RPM read came with one request sent (probe,
  quiet machine: RPM sent and read at 0.3 ms, ENGINE_LOAD refused at 1.6 ms,
  then a Mode 03 poll the fake bus never answers, 1 s timeout). The test ended
  only at its 0.4 s deadline, so a loop that took 0.4 s to reach its first
  read failed it. Now it stops on the RPM read that *follows* the refusal
  (`StopWhen`, hang-guard deadline), with the DTC and Mode 06 polls off, and
  asserts `negative_responses == 1`, `request_timeouts == 0` and no
  ENGINE_LOAD reading. The failure did not reproduce in the 9 pre-fix burner
  runs, so the mechanism is the probe's, not a captured traceback; the test
  passed in all 8 post-fix burner runs.

- [x] **T01.3 `test_force_poll_trigger_forces_a_channel_due_immediately` fails under
  load.** In `tests/test_can_obd_collector.py`, with a 0.5 s wall-clock deadline.
  It failed in canary-a2's non-slow gate before `ab6676b` and passed 3 of 3
  serially. Same remedy as T01.1.
  Fake clock (`tests/capture_round_fakes.FakeClock`, new `_ClockedDtcBus`
  whose empty receive advances simulated time by its timeout). Three 0.1 s
  support-probe timeouts come first, so the forced read lands at 0.309 s
  simulated. On a real clock that was 0.19 s of margin before the 0.5 s
  deadline. The unforced first `DTC_STORED` read lands at 4.854 s simulated,
  so the 0.5 s window still needs the trigger. Probe: with no `request()` the
  loop does not meet the condition inside the window. It did not reproduce in
  the 9 pre-fix burner runs, and it passed in all 8 post-fix runs.

- [x] **T01.4 `test_healthy_channels_are_not_backed_off_by_a_failed_batch` fails
  under load.** In `tests/test_multi_pid_batching.py`. It failed once in the
  parallel gate on `f0b578c` and passed serially. Not investigated yet.
  Failed in 7 of the 9 pre-fix burner runs, with `a channel went silent:
  {'RPM': 8, 'SPEED': 0, 'ENGINE_LOAD': 0}`. Giving up on batching costs ten
  whole-batch timeouts of 0.05 s plus the RPM reads between them, so the first
  single SPEED read lands at 0.755 s even on a fake clock. The test gave that
  0.8 s of *wall* clock: 45 ms of scheduling delay failed it. Now it runs on
  `FakeClock` + `ScriptedEcu(answer_batches=False)` for 2.0 s simulated
  (`_BATCH_REFUSAL_WINDOW_SECONDS`, asserted below `AUTO_MISS_BACKOFF_SECONDS`
  = 30 s). Mutation check: charging every member of a failed batch a miss
  (`_update_response_state(..., emitted=False)` in the whole-batch-failure
  branch) fails it and T01.6 with `{'RPM': 0, 'SPEED': 0, 'ENGINE_LOAD': 0}`.
  0 failures in 8 post-fix burner runs.

- [x] **T01.5 `test_run_daemon_records_while_the_live_source_is_still_building`
  fails on CI.** In `tests/test_daemon_startup_order.py`. It failed on GitHub CI
  run 35905954599 (`ab6676b`) with `assert []`: no parquet file yet. It
  polled 100 × 50 ms (~5 s) for a file that the writer's 5 s
  `flush_interval_seconds` timer produces ~5.5 s after the record, so the test
  raced the timer and lost on the slower runner. Now it waits with
  `wait_until` (hang guard only), then also asserts the live source is *still*
  unbuilt when the file appears, which is the test's claim. The stand-in
  build's `release.wait(timeout=10.0)` is now the hang guard, so a slow run
  cannot release it before the test looks. The same `release` guard was fixed
  in `test_startup_timeline.py`'s `_GatedWriter`. 0 failures in 8 post-fix
  burner runs.

- [x] **T01.6 `test_loop_falls_back_when_the_ecu_refuses_batches` fails under
  load.** In `tests/test_multi_pid_batching.py`. It is not in any gate stamp.
  The controlled-load runs found it (6 of 9 pre-fix, `assert {'RPM'} >=
  {'ENGINE_LOAD', 'RPM', 'SPEED'}`). It has the same 0.8 s window as T01.4 and
  the same fix: the shared `_run_against_a_batch_refusing_ecu` on the fake
  clock. The step-down sibling `test_batch_size_steps_down_instead_of_giving_up`
  had the same 0.8 s shape but was not seen failing. It now stops once every
  asserted channel is read (`StopWhen`, hang-guard deadline). 0 failures in 8
  post-fix burner runs.

- [ ] **T01.7 `test_live_replay_live_does_not_cross_contaminate_state` fails at
  `145e223`, and it is not load.** In `tests/test_replay_isolation.py`
  (`integration` tier, so it is not in the gate). It fails as a `TimeoutError`
  at shutdown, which hides the real assertion: a scratch copy that sets
  `live_b_ready` in `finally` shows `assert live_source.buffer_size > 0` fails,
  with the buffer at 0. Suspected cause, not confirmed: the 2026-09-22
  startup-order change. Records that arrive before the live source is built are
  stored but never scored, and the test's collector never waits for the source.
  Decide whether that is a test assumption to update or a live-path gap:
  records stored but never seen by inference. Found while doing T01.1.
- [ ] **T01.8 Audit the remaining short wall-clock `deadline_monotonic` collector
  tests.** About 46 tests still end on one. They are in
  `test_can_obd_collector.py`, `test_multi_pid_batching.py`,
  `test_multi_did_batching.py`, `test_tx_gate.py`, `test_tuning_profile.py` and
  `test_d02_accounting.py`. None failed in 17 loaded runs, but the riskiest
  shape is a deadline that must cover several request timeouts before the thing
  asserted. Move those to `FakeClock` + `StopWhen`, as T01.3, T01.4 and T01.6
  were, before they surface in a gate.
