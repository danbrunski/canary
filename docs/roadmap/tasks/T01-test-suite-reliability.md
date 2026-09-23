# T01 — Test-suite reliability

Opened 2026-09-23, when the new pre-push gate (`scripts/git-hooks/pre-push`)
first ran. A gate is only as trustworthy as the suite under the conditions it
runs in, and on this laptop that includes other agent sessions running their
own tests. Same conventions as the other task files: `[x]` only with the
measurement in hand.

- [ ] **T01.1 `tests/test_daemon.py` fails under host load and passes alone.**
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

  The pre-push hook now re-runs parallel-run failures serially once, and names
  a failure that passes serially as flaky. That keeps the gate usable. It is
  not the fix.
  - [ ] Capture the failing tracebacks under controlled load: run
    `tests/test_daemon.py` with `-n auto` beside a CPU burner of one process per
    core. Record which `wait_for` expires and what the daemon was doing: which
    child was still spawning, and whether shutdown or the first publish was late.
  - [ ] Replace wall-clock allowances with conditions the test controls: an
    event the daemon sets when the awaited stage is reached, a fake clock, or a
    stubbed spawn. Don't just raise the numbers: a larger timeout only moves
    the load at which it fails.
  - [ ] Acceptance: `tests/test_daemon.py` passes 5 of 5 under `-n auto` beside
    the CPU burner.
  - [ ] Every test the gate names as "flaky under load" gets a row in the table
    above, and an item here if it is not a `test_daemon.py` test. The stamp in
    `.git/ci-gate/<sha>` records them.

- [ ] **T01.2 `test_negative_pid_response_does_not_block_following_pids` fails under
  load.** In `tests/test_can_obd_collector.py`. It failed once in the parallel
  gate on `9073929` and passed serially in 1.8 s. Not investigated yet: find
  what it waits on in wall-clock time, and apply T01.1's remedy.
