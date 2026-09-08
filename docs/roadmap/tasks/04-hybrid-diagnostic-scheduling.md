# C04 — Schedule hybrid diagnostic transitions against real deadlines

Priority: medium, high for mixed Mode 01/22 capture. Dependencies: coordinate
with C02 round state and C06 diagnostic-work eligibility.

## Problem and code entry points

In [can_obd_collector.py](../../../src/collection/can_obd_collector.py), the
`active_request_count == 0` branch drains frames for 200 ms without honoring
the next controller transition. A successful session exit defaults to a
50 ms grace in `DidSessionSpec`, so this branch adds avoidable waiting.

`DidWindowController._close_window()` in
[obd_scheduler.py](../../../src/collection/obd_scheduler.py) schedules the next
window from the end of the current window. A 0.25-second interval therefore
does not describe a complete 4 Hz hybrid cycle: sweep duration and transition
costs extend the period. Historical hybrid rates are evidence of a previous
run, not a verified result of the current code.

## Implementation checklist

- [x] C04.1 Add deterministic transition tests recording session requests,
  diagnostic requests, and wake deadlines. Reproduce a 50 ms successful exit
  grace being serviced only after a 200 ms wait.
  _done 2026-09-07: `tests/test_hybrid_scheduling.py` records session requests, DID requests and wake deadlines on the fake clock; the 50 ms exit grace was serviced after 0.206 s (flat 200 ms drain) — now 0.055 s._
- [x] C04.2 Define one next-wake calculation incorporating budget, eligible
  request deadline, controller transition, keepalive, stop, and recording
  deadline. Use it in the zero-eligible branch as well as the no-due branch.
  _done: `src/collection/hybrid_cycle.py::next_wake_at` (budget, next due, controller transition, keepalive, deadline; bounded by 0.2 s) drives every drain wait in the loop via `_wake_for`, including the zero-eligible and no-due branches; test asserts every wait target is a `next_wake_at` instant._
- [x] C04.3 Define hybrid cadence as an explicit cycle schedule. Preserve
  the meaning of existing saved interval fields with a schema migration or a
  newly named cycle field; do not change all historical/default profiles by
  silently reinterpreting an end-to-start interval.
  _done: new `DidSessionSpec.cycle_seconds` / profile `did_cycle_seconds` / config `obd_did_cycle_seconds` = start-to-start cycle on monotonic deadlines; `window_interval_seconds` keeps its end-to-start meaning (0.25 s interval → 0.291 s cycle, unchanged for existing profiles); only the Xterra `tuning-hybrid-research` profile was migrated explicitly and stays experimental._
- [x] C04.4 Allocate the Mode 01 and DID work required for each cycle,
  including entry/exit request costs and required grace. Report an infeasible
  cycle instead of promising a target the selected work cannot meet.
  _done: `plan_cycle()` costs Mode 01 + DID + entry/exit + grace at budget pace; infeasible cycles report `feasible=False` with `shortfall_seconds` (0.1 s cycle on a 5 ms ECU short 0.1375 s), logged once._
- [x] C04.5 Preserve progress for both services when work overruns. Skip
  missed deadlines and account for shortfall, without spinning, building a
  catch-up backlog, or starving Mode 01 with immediately reopened windows.
  _done: missed cycle boundaries skipped and counted (`cycles_missed`), per-service shortfalls accounted, Mode 01 share reserved after the grace; overload test shows both services progress every cycle with no catch-up burst after a 5 s clock jump._
- [x] C04.6 Keep fast-exit support, session IDs, grace, and measured recovery
  behavior vehicle-scoped. Retain transient/permanent refusal distinction and
  the current bounded re-probe behavior. A fallback lapse is not proof that
  Mode 01 has recovered; report actual recovery observations.
  _done: `DidAccess.exit_grace_seconds` in the vehicle DID catalog (Xterra 0.05 from the 2026-08-04 measurement); transient/permanent refusal and bounded re-probe retained; `mode01_recovered_at` set only on an observed Mode 01 response (lapse ≠ recovery test)._
- [x] C04.7 Handle profile switches out of continuous DID mode by completing
  the necessary session transition. Audit pause/resume, passive TX gating,
  exit failures, and reconnects; no stale controller phase may quietly prevent
  selected requests from running.
  _done: profile switch out of continuous DID mode sends the exit and Mode 01 resumes; pause/resume, passive lift, exit failure and reconnect (`on_generation_change`, exit-on-connect) each tested — no stale phase blocks selected requests._
- [x] C04.8 Expose cycle duration, service occupancy, transition latency,
  missed cycles, and acquisition separation for cross-mode tuples. Separate
  session-control traffic from channel-read throughput.
  _done: `did_profile.cycle` in scheduler metrics — target, achieved duration summary, per-service occupancy, transition latency, missed cycles, cross-mode acquisition separation; `session_control` counters separate from channel reads._

## Regression and acceptance checklist

- [x] C04.9 Cover accepted fast exit, grace shorter/longer than the cycle,
  timeout, transient refusal, permanent refusal and recovery, no eligible
  DIDs, continuous mode, and sustained overload.
  _done: accepted fast exit, grace shorter/longer than the cycle, timeout, transient refusal, permanent refusal + recovery, no eligible DIDs, continuous mode, sustained overload (26 tests, 3× `-n auto` clean)._
- [x] C04.10 Verify raw CAN continues through every wait and no excluded
  service is polled merely to probe whether the ECU recovered.
  _done: `recv` continuity asserted (≤ 50 ms gap) through every wait; no `10 xx` while disabled, no Mode 01 during a lapse, no excluded service polled to probe recovery._
- [x] C04.11 Run controller/collector and profile tests, including an
  end-to-end fake-bus mixed capture with measured request ordering.
  _done: controller/collector/profile tests plus an end-to-end fake-bus mixed capture asserting per-cycle request order (entry, DIDs, exit, Mode 01) and wake reasons._
- [~] C04.12 On the Pi, record vehicle state and compare equivalent captures
  using cadence, gaps, complete tuples, and transition timing. Do not claim
  4 Hz until it is achieved and persisted. Keep the profile experimental until
  its declared contract is met.
  _pending hardware 2026-09-08: Pi still unreachable. Procedure in `docs/hybrid-scheduling.md` (read `vehicle_state`, compare cadence, gaps, complete tuples and transition timing between the interval and cycle policies); `tuning-hybrid-research` stays experimental until 4 Hz is achieved and persisted._
  _software half measured on the synthetic bus (C10, `hybrid-mode01-mode22`): cycle duration 0.250 s median and p95, 0 missed of 20, entry→DID 12 ms and exit→Mode 01 63 ms median, both services progressing, and the honest cross-mode pairing bound (153 ms median separation → ±43 rpm at a 470 rpm/s slew)._
  _policy comparison done 2026-09-08: `hybrid-interval-policy` is the same scenario with `did_cycle_seconds` dropped and nothing else changed, which selects the older end-anchored interval policy. Under the identical workload the cycle policy meets 4.0 Hz on every DID; the interval policy misses it by 26 % (2.953 / 4.0) with the bus 27 % used and zero timeouts — neither the budget nor the ECU, but the schedule. That comparison also surfaced and fixed finding 13 (`limiting_stage` blamed `ecu_response_time` for it). Table in `docs/sustained-validation.md` ("Diagnostic policy comparison")._
  _the 4 Hz claim itself is untouched by all of this: the synthetic ECU answers in a fixed 5 ms sleep, and the real ECM's response time is what decides whether the cycle plan is feasible on the truck._
- [x] C04.13 Acceptance: the loop wakes at the earliest relevant deadline,
  both service phases progress, and cycle targets and achieved values use the
  same definition.
  _done: loop wakes at the earliest relevant deadline; both phases progress; target and achieved cycle share the entry-to-entry definition (0.250 s median, 0 missed on the fake bus)._

## Handoff

- [x] C04.14 Record timing-policy migration and measured hardware results,
  leaving hardware checks partial if the Pi is unavailable. Apply the shared
  validation gate.
  _done: `docs/hybrid-scheduling.md` (migration table, wake calculation, feasibility, overrun, stale-phase table, metrics, measurements, hardware procedure) + `docs/runbooks.md` pointer. Gate green._
