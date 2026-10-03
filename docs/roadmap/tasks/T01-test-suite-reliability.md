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
  | `scripts/ci_gate.sh`, `-n auto` | `3f2845d` | 18.9–23.0 (1/5-min, at the serial re-run) | `tests/test_sustained_validation.py::test_fuel_map_persists_tuples_and_known_answers_hold`, passed serially in 39 s — T01.9 |
  | `scripts/ci_gate.sh`, `-n auto` | `9d3fa4b` | 30.1 (1-min, at the serial re-run) | same test again, passed serially in 25 s — T01.9 |
  | `scripts/ci_gate.sh` vitest (full dashboard suite) | `92b5374` | not recorded | `src/panels/SessionsPanel.test.ts` › hosts SessionsCard and ReplayCard side by side; failed again in a full local `pnpm vitest run`, passed 3 of 3 alone — T01.10 |
  | `scripts/ci_gate.sh`, `-n auto` | `dda8835` | not recorded | `tests/test_daemon.py::test_run_live_inference_sequence_detector_emits_on_full_window`, passed serially in 7.5 s (a `test_daemon.py` test: T01.1's family, after its fix) |
| `scripts/ci_gate.sh`, `-n auto` | `95eca7c` | 15.67 (1-min, at the serial re-run) | same test again, passed serially in 4.9 s; the commit touched only `tests/test_layering.py` and docs — T01.13 |
| R09.2 verifier, `-m slow`, beside the R09.3 implementer | `5f6717b` | 9.77 (5-min) | `tests/test_daemon.py::test_run_daemon_end_to_end_inference_emits_anomaly`, 32.7 s against a normal 5–6 s; 8 of 9 alone |
| R09.3 implementer, `-n 4` | `5f6717b` + R09.3 diff | not recorded | the same test again, passed alone and in the slow run |
| R09.4 implementer, inner loop, beside a 31-min replay | `c9d11ab` + R09.4 diff | not recorded | `tests/test_daemon.py::test_run_live_inference_scores_during_replay_despite_engine_off_reading` (30 s hang-guard `CancelledError`), passed alone in 5.9 s |
| R09.5 verifier, inner loop | `1df5e66` | not recorded | `tests/test_can_collector.py::test_collect_can_counts_malformed_frames_without_aborting` (`assert 3 == 4`), 3 of 3 alone — T01.14 |

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

- [x] **T01.7 `test_live_replay_live_does_not_cross_contaminate_state` fails at
  `145e223`, and it is not load.** In `tests/test_replay_isolation.py`
  (`integration` tier, so it is not in the gate). It fails as a `TimeoutError`
  at shutdown, which hides the real assertion: a scratch copy that sets
  `live_b_ready` in `finally` shows `assert live_source.buffer_size > 0` fails,
  with the buffer at 0. Suspected cause, not confirmed: the 2026-09-22
  startup-order change. Records that arrive before the live source is built are
  stored but never scored, and the test's collector never waits for the source.
  Decide whether that is a test assumption to update or a live-path gap:
  records stored but never seen by inference. Found while doing T01.1.
  - [x] **Cause: the test's assumption was stale.** Probe at `6ca6d85` (quiet
    laptop, timeline stages): `collector_tasks_created` 1.55 s,
    `live_source_ready` 2.05 s. The collector's 15 fixed records ended 0.46 s
    after the daemon task started, 0.15 s before the source existed, so no
    record was ever observed and `buffer_size` stayed 0. The live sink joins
    the fanout only when the source resolves, which is S01's intended order.
  - [x] **The test now waits for conditions and fails on the real assertion.**
    Each live phase feeds records, stamped when sent, until the state it
    asserts is reached (`wait_until`: buffer filled, gate active, emitter
    fired). Live A's records are drained before the boundary
    (`health_monitor.wait_for_queues_idle(("ingest", "live"))`). Every
    collector wait also ends on `stop`, and the shutdown join is `settle`.
    "Pre-replay" emitter state is now captured inside
    `emitter.enter_replay_mode` by a spy. A snapshot taken before `start()` raced
    a live tick that could emit while `start()` awaited the writer's switch.
    That failed 2 of 5 loaded runs as `assert 2 == 1`, a live emission that is
    correctly kept. Mutation checks: skipping `live_source.reset()` in
    `ReplayController._reset_in_memory_state` fails with `assert 5 == 0` in
    3.7 s, and skipping the counter restore in `exit_replay_mode` fails with
    `assert 0 == 1`. Neither hangs. Loaded: 10 of 10 alone (`-n 4`, 12
    burners, load 6.0–15.3) and 5 of 5 alongside the six T01.8 files
    (`-n auto`, load 15.3–22.3). No production code changed.
  - [x] **Live-path gap: real, measured, and accepted.** *Operator,
    2026-09-23: losing the first 0.5–1 s after a daemon start is fine; no fix.*
    Records that arrive before the live source is built are stored but never
    observed by it. On the Pi on 2026-09-23 (`/status.startup` after the 17:46Z
    restart at `0d4b32b`: `first_record_stored` 5.078 s, `live_source_ready`
    5.598 s) that was 5.08 s → 5.6 s, a 0.52 s
    gap. The first 0.52 s of 18 stored sessions from 2026-09-18/19 held
    764–1,263 records across 35–67 channels. `LiveFeatureSource` keeps one
    current value per channel, and its ring buffer holds *tick* rows stamped
    `now`, not records. So no replay of held records could score those
    0.52 s: nothing ticks before the source exists. The only loss is the last
    value of a channel sampled only in the gap. In 12 of the 18 sessions that
    was 1–15 channels (slow/medium-tier Mode 01, GPS, DIDs). Their next
    sample came 1.5–15 s later (DIDs about 61 s, one outlier 197 s), and
    until then they read as gaps. Those sessions start at rollover or arming,
    not always at a daemon start, so the cadence figure is what transfers.
    A fix would hold the newest record per `(source, channel)` in the fanout
    while the source builds, then feed those records to the live sink when it
    joins: about 40 lines on the drain's hot path. It would buy a few seconds
    of slow-channel coverage after a daemon start, which rarely coincides
    with driving. Left open for that call; the drain docstring already states
    the behaviour.
- [x] **T01.8 Audit the remaining short wall-clock `deadline_monotonic` collector
  tests.** About 46 tests still end on one. They are in
  `test_can_obd_collector.py`, `test_multi_pid_batching.py`,
  `test_multi_did_batching.py`, `test_tx_gate.py`, `test_tuning_profile.py` and
  `test_d02_accounting.py`. None failed in 17 loaded runs, but the riskiest
  shape is a deadline that must cover several request timeouts before the thing
  asserted. Move those to `FakeClock` + `StopWhen`, as T01.3, T01.4 and T01.6
  were, before they surface in a gate.
  Measured, not guessed: a scratch plugin wrapped every `run_can_obd_loop` call
  in the six files (quiet laptop, `-n0`). For each call it recorded the
  wall-clock budget, whether the loop ended on its stop condition or on the
  deadline, the first read of each channel, and the first send of each
  service/PID. The *margin* is the budget divided by the time at which the
  asserted state was reached. The rule: convert when the margin is under 10x,
  or when the claim compares or covers the work done inside a wall-clock
  window. T01.2–T01.6 failed at margins of 1.06–1.6x.
  - [x] **Converted to `FakeClock`** (the fakes gained an optional `clock`:
    `_BatchingEcu` and subclasses, `_SchedulerDidEcu`/`_SleepyDidEcu`, new
    `_ClockedFreezeFrameBus`. A receive with nothing pending costs its timeout
    in simulated time, as in `ScriptedEcu`):
    `test_batch_silence_from_a_sleeping_ecu_does_not_narrow` (0.41 s needed of
    1.2 s, 14 timeouts first), `test_an_awake_ecu_that_ignores_batches_still_disables_batching`
    (0.35 of 1.0, 9 whole-batch timeouts), `test_an_ecu_that_refuses_batches_with_an_nrc_narrows_regardless`
    (0.13 of 0.6, 4 timeouts), `test_a_pid_whose_measured_width_contradicts_the_registry_leaves_batches`
    ×2 (0.08 of 1.0, 3 timeouts) and `test_a_desync_followed_by_silence_does_not_storm`
    (shares `_run_capture`), `test_batching_beats_single_pid_on_round_trips`
    (compared two wall-clock-bounded runs), `test_rejected_did_batches_fall_back_without_backing_off_members`
    (0.31 of 0.5), `test_did_batch_silence_from_a_sleeping_ecu_does_not_disable_batching`
    (0.44 of 0.8, 9 timeouts), `test_freeze_frame_trigger_is_consumed_once_then_reads_go_dormant`
    (sweep behind a 0.1 s timeout; its claim is about the rest of the second).
    Also `test_accounting_reconciles_over_a_whole_run_with_failing_batches`: the
    single-PID recovery it claims to reconcile came at 0.73 s of 0.8 s, so a
    loaded run silently skipped it. It now runs the 2.0 s simulated
    `_BATCH_REFUSAL_WINDOW_SECONDS` and asserts the recovery happened.
  - [x] **Converted to `hang_guard_deadline()`** (they already stop on their
    condition): `test_scheduler_polls_fast_tier_more_often_than_slow_tier`
    (0.25 of 1.0, 4 timeouts), `test_scheduler_metrics_report_rates_and_backoff_state`
    (0.056 of 0.4), `test_custom_pid_response_decodes_through_evaluator_into_custom_channel`
    (0.20 of 1.0, behind a 0.2 s timeout).
  - [x] **Left as safe, with the reason:**
    `test_query_with_tap_*` ×4 and D02's `test_failed_batch_names_every_send_it_swallowed` /
    `test_failed_batch_does_not_back_off_its_members`: one request whose own
    timeout is shorter than the deadline, with an instant answer or an
    assertion about the frame sent. `test_force_poll_trigger_ignores_unknown_channel_names`:
    asserts only that the loop returns. `test_mode06_discovery_retries_after_successful_obd_contact`
    (stops at 0.14 of 2.0), `test_dtc_poll_reads_codes_and_mil_with_engine_off_koeo`
    (0.026 of 2.0), `test_run_loop_emits_can_frames_pid_readings_and_did_readings`
    (0.007 of 1.0), `test_did_session_windows_read_gated_dids_and_resume_mode_01`
    and `test_failed_session_entry_skips_window_and_keeps_mode_01_alive` (0.35
    of 5.0), `test_did_requests_stay_quiet_outside_windows` (0.11 of 2.0,
    negative assertion): all stop on their condition, at 14x or more.
    `test_dtc_poll_skips_cleanly_when_bus_never_answers`: the Mode 03 send it
    needs is at 0.02 of 0.5, and the rest is lower bounds.
    `test_loop_batches_and_emits_one_reading_per_channel`,
    `test_every_pid_in_a_batch_lands_on_one_timestamp`,
    `test_a_capture_round_puts_every_channel_on_one_row`,
    `test_batch_readings_share_receipt_time_and_round_but_not_a_claim_of_atomicity`,
    `test_a_capture_round_with_baro_walks_every_member` ×3: the asserted state
    is reached in under 3 ms with no timeout before it, and the per-reading
    properties do not depend on how many rounds ran.
    `test_active_scheduler_batches_known_width_dids_with_full_provenance`
    (4.6 ms of 0.2 s), `test_known_answer_decode_matches_single_and_batched_scheduler_paths`
    (12.8 ms of 0.2 s, no timeout first), `test_physical_did_batch_rejects_a_different_ecu_responder`
    (about 0.01 of 0.15, then a negative assertion). tx_gate's three loop tests
    and `test_tuning_mode_does_not_defeat_passive_mode`: negative assertions,
    or the first send at 0.3 ms. `test_poll_loop_only_requests_tuning_channels_when_active`
    and `test_poll_loop_polls_everything_when_inactive`: the last channel they
    need is sent at about 0.02 of 0.6 s.
  - [x] Mutation checks on the conversions. Counting every silent batch
    failure toward narrowing (`if not counts:` → `if False:`) fails both
    sleeping-ECU tests. A `FreezeFrameTrigger.take()` that never clears fails
    the dormancy test.
  - [x] Acceptance: 5 of 5 under `-n auto` beside 12 busy-loop burners (one
    per core, killed by a trap and checked gone afterwards): 194 passed each
    run of the six files (1-min load 8.6–20.3 at end of run), and 195 passed
    each run with `tests/test_replay_isolation.py` added (15.3–22.3).

- [x] **T01.9 `test_fuel_map_persists_tuples_and_known_answers_hold` fails under
  load and passes alone.** Named "flaky under load" by the `3f2845d` gate
  (2026-09-23; the commit touched no daemon, collector or sustained-validation
  code): failed in the 8.5-minute parallel run, passed serially in 39 s with the
  host at load 18.9–23.0. It runs the `fuel-map` sustained scenario end to end
  with inference on (`_run(scenario("fuel-map"), tmp_path, inference=True)`), so
  it is the same shape as T01.1: real work with a wall-clock budget somewhere
  inside. The failing traceback was not kept (the gate prints only the summary
  line). Reproduce beside the per-core burners, record which wait expires, and
  replace it with a condition, as T01.1–T01.6 did.
  **No wait expired. The host clock stepped.** Beside 12 burners the test
  passed alone (2 of 2, 25 s each), and so did four concurrent copies (8 of 8).
  An instrumented `_heartbeat` found the cause. Each `time.time()` read sat
  between two `time.monotonic()` reads under 0.3 ms apart, and still this WSL2
  host stepped `CLOCK_REALTIME` forward +2.93 s every 32.25 s under load
  (`steps.py`, 150 s: five steps). Every process saw each step at the same
  monotonic instant, so neither the GIL nor a descheduled thread explains it.
  `run_scenario_clean` marks a stepped run invalid and reruns it, three
  attempts at most. A 17–35 s attempt beside a 32 s period can lose all three.
  One loaded run did exactly that: 118 s, 3 attempts, a +2.93 s step in the
  last, `invalid_reasons: ["host_wall_clock_step"]`. That is the test's first
  assertion, `evidence["valid"] is True`. Its other margins held under load:
  known-answer bias 0.03–0.42 rpm against a 15 rpm tolerance, slope within
  0.0007 of 1, 40–105 complete tuples against >10, and every scheduled tick
  completed.
  Fix: new `tests/conftest.py::step_free_wall_clock`. `time.time()` becomes
  the setup-time wall clock advanced by `time.monotonic()`, and so does live
  inference's `src.daemon.inference._utc_now`, which judges record freshness.
  It applies to all of `tests/test_sustained_validation.py` via `pytestmark`.
  The T01.9 test now also asserts `attempts_used == 1`. The step path, no
  longer reachable from those runs, is tested by injection:
  `test_heartbeat_records_an_injected_wall_clock_step` (+0.5 s recorded once)
  and `test_a_run_that_saw_a_wall_clock_step_is_rerun_then_reported`. Two
  mutations were run. A 1.0 s step threshold fails the first ("hung: the
  heartbeat seeing the step"), and `attempts=1` fails the second
  (`assert 1 == 2`). Acceptance: 12 of 12 passed with `attempts_used == 1`,
  as three rounds of four concurrent `pytest -n0` copies beside 12 burners
  (load 16.2–17.9, 26–35 s each). A logger in the same rounds recorded 3, 2
  and 3 host steps, so every copy ran across at least one step. The whole
  module passed, 21 of 21 (`-n 4 -m ""`, 51 s). No production code changed.

- [x] **T01.10 `SessionsPanel.test.ts` fails in the full vitest run and passes
  alone — and the gate has no retry for vitest.** Blocked the `92b5374` push
  (2026-09-23), a commit whose only dashboard change was two tag labels in
  `LoggingPage.tsx`; failed again in a full local run (1 of 357), passed 3 of 3
  run alone. The Python side of `scripts/ci_gate.sh` re-runs parallel-run
  failures serially and names them flaky; the vitest side does not, so a
  dashboard flake blocks a push outright. Two fixes: find what the test waits
  on by wall clock under load (the T01.1 approach), and give the gate the same
  serial re-run for vitest.
  **What it waited on: the text, when the next step needs the picker value.**
  The session list reaches the DOM in one commit. ReplayCard's picker gets its
  value two passive-effect passes later: SessionsPanel's selection effect,
  then ReplayCard's follow effect. A probe `MutationObserver` saw the three
  `<option>`s with `value=""` first. React's scheduler works in 5 ms slices
  timed by `performance.now()`. When a busy host uses up a slice between
  those passes, `findAllByText` resolves first: testing-library's post-wait
  drain is a single `setTimeout(0)`. The test then read `picker.value` (the
  failing test), or clicked Start (the other two), where an empty picker
  throws "Select a session first." Three full `pnpm vitest run`s beside 12
  burners did not hit it by chance, and neither did 300 render loops. A
  `performance.now()` that advances 10 ms per read, so every slice is spent,
  hit it every time: `value=""` in 50 of 50 renders, and 3 of the file's 4
  tests failed. Fix, test only (`SessionsPanel.tsx` unchanged): the file now
  keeps that clock skew in `beforeEach` as a standing load reproduction.
  `pickerHolds(id)` waits for the picker value before reading it or clicking
  Start. 4 of 4 pass under the skew. The full suite passed beside 12
  burners: 428 of 428, load 19.3.
  **Gate:** `scripts/ci_gate.sh` now re-runs the files that fail in
  `vitest related` once, with `--no-file-parallelism`. Failing again blocks the
  push. Passing prints "FLAKY UNDER LOAD" and adds them to the stamp's
  `flaky-under-load` list. Tested by extracting the block with a scratch test
  file: fails-once-then-passes gave FLAKY UNDER LOAD, exit 0 and the path in
  `.flaky`; always-fails gave "failed again serially" and exit 1. The file
  parser was checked on the ANSI-coloured and collection-error `FAIL` forms.
  Two other files failed in the loaded full-suite runs before this fix, in R08's
  area and not investigated here: `useControlResource.test.ts` (3 tests, 1 run;
  the file was being edited at the time) and `TuningContextBar.test.tsx`
  (1 test, 2 of 3 runs).

- [x] **T01.11 `tests/test_passive_restart.py::test_a_passive_arm_survives_a_restart_and_nothing_is_sent_first`
  fails under full-suite load and passes alone.** Seen 2026-09-25 in the
  isolated fast-tier run of the R05.16 commit (`-n 6`, 5875 other tests
  passing); passed 3 of 3 alone (`-n0`) and beside `test_daemon.py` +
  `test_tx_gate.py` (`-n 6`). The test came in with D03.30 (`96edfcf`) and
  drives a real restart. Reproduce beside the per-core burners (T01.1's
  method), record which wait expires, and replace it with a condition the test
  controls.
  **The wait that expired was "first collector send" (the 30 s hang guard),
  and the daemon was already dead: the two runs shared one storage root.**
  Caught on a quiet host, no burners, `-n0`, on the 3rd of a loop of runs
  (an earlier 30.9 s failure, traceback not kept, had the same duration): the second `run_daemon` raised
  `FileExistsError` in `make_session_dir` for `storage/2026-09-25T233019`,
  the first run's session directory. Session directories are named to the
  second and created `exist_ok=False` (collisions loud on purpose,
  `test_make_session_dir_refuses_collision`), so whenever the restart fell in
  the same wall-clock second as the first start, the second daemon died
  before any collector existed and `_first_status_read` waited out the guard.
  Load is incidental: it only moves where the second boundary falls. Not
  D03.30's restore ordering: `restore_passive_state` still runs synchronously
  as `run_daemon`'s first statement, before the writer and every collector,
  and the captured log shows `passive mode RESTORED ... 1799 s of its 1800 s
  TTL left` before the crash. No production code changed. The sibling
  `test_a_disarm_before_the_restart_is_not_resurrected` had the same race and
  had not been seen yet. Deterministic reproduction: a scratch pytest plugin
  pinning `src.storage.parquet_writer.datetime.now` to one instant. At `HEAD`
  both restart tests fail (the survive test after 31.2 s on "hung: first
  collector send", the disarm test on `FileExistsError`). Fix, test only:
  `_restarted(config, run)` gives each run its own storage root and keeps the
  shared control-state directory, where the passive file lives. So the test
  now also pins that restore does not depend on the session directory.
  `_run_once` now races the probe against the daemon task, so a daemon that
  dies first fails the test at once with its own traceback, not after 30 s.
  Mutation (`_restarted` returning `config` unchanged, clock pinned): both
  fail on `FileExistsError` in 3.9 s total. Fixed, clock pinned: 3 of 3 pass.
  Acceptance: the file passed 10 of 10 copies beside 12 burners (two rounds
  of five concurrent `pytest -n0` copies, 11–13 s each, 1-min load 4.8 and
  8.4 at the end of the short runs), and 15 of 15 sequential quiet runs,
  where the collision had hit 1 in 3 before.

- [x] **T01.12 `tests/test_inference_scheduling.py::test_run_live_inference_records_timing_and_shuts_down`
  fails under CI load and passes alone.** Seen 2026-09-27 in CI run
  36311185614 (`34f9886`, which touched no inference code):
  `assert snap.ticks_scheduled >= 3` got 2. Passed 5 of 5 alone (`-n0`). The
  test slept a fixed 0.4 s after starting `run_live_inference` at a 50 ms
  cadence, so the scheduler's startup (executor, first feature build) shared
  the window, and a slow start left room for only two ticks. The test measured
  the runner's startup speed, not the scheduler. Fix, test only: wait until
  three ticks are scheduled (10 s deadline, failing at once with the task if
  the pipeline dies first), then stop. The later assertions are unchanged.
  Acceptance: 10 of 10 copies (two rounds of five concurrent `pytest -n0`)
  beside 12 CPU burners, and the whole file 28 of 28.

- [x] **T01.13 `tests/test_daemon.py::test_run_live_inference_sequence_detector_emits_on_full_window`
  fails under gate load and passes alone.**
  - **Seen twice in the parallel push gate:** at `dda8835`, and again on
    2026-09-27 at `95eca7c` (load average 15.67, with another session running).
    The second commit touched no `src/` code. It passed serially both times,
    in 7.5 s and then 4.9 s.
  - **Not T01.12's shape.** The test's only wait is
    `settle(sub.get())`, the 30 s `HANG_GUARD_SECONDS`, not a short
    wall-clock bound. So the failure may be an assertion (the first message
    on the anomalies broadcaster not being `lstm_autoencoder`) rather than a
    timeout.
  - **No traceback survives.** `scripts/ci_gate.sh` writes the pytest log
    inside its throwaway worktree, which is deleted afterwards.

  **Do:**
  1. Capture the failure by running the file beside one CPU burner per
     core, as in T01.1's controlled-load recipe.
  2. Fix the test, not the daemon, unless the traceback shows a daemon bug.
  3. Consider having `ci_gate.sh` copy the log out of the worktree when a
     test fails, so the next flake keeps its traceback.

  **Acceptance:** 10 of 10 copies under the controlled load, as in T01.12.

  *Fixed 2026-10-03, test only.* The failure was the hang, not an assertion.
  Beside 12 burners (load average 13.8), 7 of 20 copies failed after ~37 s
  with `TimeoutError` in `settle(sub.get())`. The passing copies took 9–10 s.
  - **Cause.** The test fed the live source a single `obd:RPM` record and
    then waited. Scoring needs `vehicle_state` `engine_running`, which needs
    OBD fresher than `FRESHNESS_THRESHOLD_SECONDS` (10 s,
    `src/vehicle_state.py`). A loaded startup (the captured log shows
    "overran 14 deadline(s)" first) could take the first scoreable tick past
    10 s. The vehicle then read `engine_off`, every tick counted
    `skipped_vehicle_state`, nothing fed RPM again, and the test waited out
    the 30 s guard.
  - **Mutation.** The old one-record shape, stamped 11 s old, never emits on
    a quiet machine (`TimeoutError` at an 8 s cap).
  - **Fix.** A feeder task keeps observing RPM every 0.2 s until `stop`, as a
    running engine does. It is awaited with the inference task. The
    assertions are unchanged.
  - **Acceptance.** 20 of 20 copies (four rounds of five concurrent
    `pytest -n0`) beside 12 burners, load average 12.2. The whole
    `tests/test_daemon.py` passed 69 of 69.
  - **Gate log (Do 3).** `scripts/ci_gate.sh` now copies a failed pytest or
    vitest log out of the worktree to `.git/ci-gate/failed-logs/` before the
    serial re-run, keeps the newest 20, and prints the path. The next flake
    keeps its traceback.

- [x] **T01.14 `tests/test_can_collector.py::test_collect_can_counts_malformed_frames_without_aborting`
  races its own stop condition.**
  - **Seen** 2026-09-27 by the R09.5 verifier in an inner-loop run at
    `1df5e66`, which touched nothing in `can_collector`. The failure was
    `assert 3 == 4` on `parse_errors`. It passed 3 of 3 alone.
  - **Diagnosis (verifier, not yet confirmed under controlled load).** The
    test sets `stop` once the queue holds 8 frames, which can happen before
    the collector reads the 4th malformed frame. The assertion then counts
    three.
  - **Do:** wait for `stats.parse_errors == 4`, or for all 12 bus calls to be
    consumed, before setting `stop`, with a deadline in the style of
    `tests/waits.py`. Fix the test only.
  - **Acceptance:** 10 of 10 copies beside 12 CPU burners, as in T01.12.

  *Fixed 2026-10-03, test only.* The diagnosis is confirmed. `FlakyBus`
  serves good frames on calls 1, 2, 4, 5, 7, 8, 10 and 11 and raises on 3, 6,
  9 and 12. So the queue reaches 8 at call 11's push, and `CanReader` checks
  `stop` before call 12. If the reader loses the CPU between that push and
  that check, the loop sees 8 frames, sets `stop`, and the 4th malformed
  frame is never read. Under plain load the window is too narrow to hit: the
  old test passed 40 of 40 beside 12 burners. Deterministic reproduction: a
  scratch fixture wrapping `ThreadHandoff.push` to sleep 0.2 s after the 8th
  push. The old condition then fails with exactly `assert (8, 3, 8) == (8, 4,
  8)`, and the new one passes. The test now waits with `wait_until(bus.calls >
  bus.total_calls)`: the reader has asked past the script, so all 12 scripted
  results were consumed. It then `settle`s the task. Acceptance: 10 of 10
  copies (two rounds of five concurrent `pytest -n0`) beside 12 burners, load
  average 9.4. The whole file passed 29 of 29 under the same load.
