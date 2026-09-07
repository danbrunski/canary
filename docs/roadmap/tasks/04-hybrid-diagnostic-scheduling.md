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

- [ ] C04.1 Add deterministic transition tests recording session requests,
  diagnostic requests, and wake deadlines. Reproduce a 50 ms successful exit
  grace being serviced only after a 200 ms wait.
- [ ] C04.2 Define one next-wake calculation incorporating budget, eligible
  request deadline, controller transition, keepalive, stop, and recording
  deadline. Use it in the zero-eligible branch as well as the no-due branch.
- [ ] C04.3 Define hybrid cadence as an explicit cycle schedule. Preserve
  the meaning of existing saved interval fields with a schema migration or a
  newly named cycle field; do not change all historical/default profiles by
  silently reinterpreting an end-to-start interval.
- [ ] C04.4 Allocate the Mode 01 and DID work required for each cycle,
  including entry/exit request costs and required grace. Report an infeasible
  cycle instead of promising a target the selected work cannot meet.
- [ ] C04.5 Preserve progress for both services when work overruns. Skip
  missed deadlines and account for shortfall, without spinning, building a
  catch-up backlog, or starving Mode 01 with immediately reopened windows.
- [ ] C04.6 Keep fast-exit support, session IDs, grace, and measured recovery
  behavior vehicle-scoped. Retain transient/permanent refusal distinction and
  the current bounded re-probe behavior. A fallback lapse is not proof that
  Mode 01 has recovered; report actual recovery observations.
- [ ] C04.7 Handle profile switches out of continuous DID mode by completing
  the necessary session transition. Audit pause/resume, passive TX gating,
  exit failures, and reconnects; no stale controller phase may quietly prevent
  selected requests from running.
- [ ] C04.8 Expose cycle duration, service occupancy, transition latency,
  missed cycles, and acquisition separation for cross-mode tuples. Separate
  session-control traffic from channel-read throughput.

## Regression and acceptance checklist

- [ ] C04.9 Cover accepted fast exit, grace shorter/longer than the cycle,
  timeout, transient refusal, permanent refusal and recovery, no eligible
  DIDs, continuous mode, and sustained overload.
- [ ] C04.10 Verify raw CAN continues through every wait and no excluded
  service is polled merely to probe whether the ECU recovered.
- [ ] C04.11 Run controller/collector and profile tests, including an
  end-to-end fake-bus mixed capture with measured request ordering.
- [ ] C04.12 On the Pi, record vehicle state and compare equivalent captures
  using cadence, gaps, complete tuples, and transition timing. Do not claim
  4 Hz until it is achieved and persisted. Keep the profile experimental until
  its declared contract is met.
- [ ] C04.13 Acceptance: the loop wakes at the earliest relevant deadline,
  both service phases progress, and cycle targets and achieved values use the
  same definition.

## Handoff

- [ ] C04.14 Record timing-policy migration and measured hardware results,
  leaving hardware checks partial if the Pi is unavailable. Apply the shared
  validation gate.
