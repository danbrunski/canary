# R18 — Memory-burst planning and accounting owner

Opened 2026-10-14. Shared rules: [R10](R10-refactor-README.md).
Size L; collection timing risk. No prerequisite. Coordinate B08/H07 and any
ongoing scheduler changes before editing the shared loop.

## Problem and target

`can_obd_loop.py` was 2,159 lines. `_CanObdLoop` owns the burst plan/decision,
slots spent, burst read timing, paced timing, session-control timing, keepalive
policy and changes to the ordinary budget clock. This is a cohesive scheduling
component inside a class already responsible for discovery, rounds, identity
reads, batching, metrics and bus IO.

Extract a component under `src/collection/`, analogous to `BatchGovernor`,
that owns memory-burst state and returns explicit scheduling decisions. The
loop retains sending/receiving, callbacks, request/round updates and lifecycle.
Keep the existing tail-of-window, back-to-back memory-read behavior exactly.

## Implementation checklist

- [ ] **R18.1 Record the current state machine.**
  - [ ] Inventory `_BurstPlan`, `_BurstDecision`, `_memory_burst`,
    `_session_budget_at`, `keepalive_policy`, `_after_burst_read`, `_slot`,
    `_spend_budget_slot`, generation reset and every consumer of their fields.
  - [ ] Map who creates, starts, charges, advances, cancels and closes a plan;
    record how ordinary reads and identity/session requests affect its clocks.
  - [ ] Record request order, issue/acquisition times, prepaid slots, budget
    clocks, window exit and keepalive from existing fake-clock burst scenarios.
  - [ ] Preserve B08.4's measured assumptions; the refactor is not a new
    optimization or a claim of improved wire throughput.
- [ ] **R18.2 Define the component boundary.**
  - [ ] Give the component ownership of the plan, slot accounting and timing
    estimates; do not leave duplicate counters in the loop.
  - [ ] Supply explicit time, budget/cadence, window facts and outstanding
    members. Return hold/issue/member decisions with clear invariants.
  - [ ] Define lifecycle hooks for plan/reset, slot spending, session/read
    observations and completion. Preserve monotonic-time units throughout.
  - [ ] Keep IO, sleeping, transmission and global profile reads out of the
    planner. Adapt loop facts into the component at one boundary.
  - [ ] Keep the API narrow; do not pass the entire loop or add a generic
    callback framework that recreates access to all its attributes.
- [ ] **R18.3 Extract without changing scheduling.**
  - [ ] Move plan/decision calculation and accounting first; wire the loop
    to one owner in a behavior-preserving commit.
  - [ ] Preserve memory reads last after other session-window members, then
    back-to-back near exit, including the deadline-too-short non-prepaid case.
  - [ ] Preserve charging each slot exactly once, including failure/NRC paths;
    ordinary requests and identity/keepalive reads shift the plan by their slots.
  - [ ] Preserve the longer-of-budget-slot/turnaround paced cost and current
    estimation update rules; no new smoothing constants or thresholds.
  - [ ] Preserve continuous-window/no-memory fallback, generation reset,
    stop/deadline handling and rearming after engine start.
  - [ ] Preserve profile filtering, discovery, forced polls, batch governors,
    round completion and scheduler metrics outside the component.
  - [ ] Retain existing loop entrypoints/exports and deliberately retarget
    fake-clock and monkeypatch bindings to the code that now reads time.
- [ ] **R18.4 Verify independently controlled transitions.**
  - [ ] Extend existing deterministic tests only for uncovered transitions:
    no plan, waiting for ordinary members, prepayment, active burst, completion
    and reset/cancellation.
  - [ ] Cover a keepalive or calibration/VIN request after planning, burst
    reads with variable turnaround, timeout/NRC, window expiry and budget change.
  - [ ] Cover generation/profile changes, passive suspension, engine-start
    rearm, continuous windows, no memory members and short deadlines.
  - [ ] Assert request ordering, timestamps, each charged slot and resulting
    budget clocks; aggregate achieved Hz alone is not evidence of parity.
  - [ ] Use existing `ScriptedEcu` and fake-clock infrastructure rather than
    wall-clock sleeps or a second approximate scheduler implementation.
- [ ] **R18.5 Integration and close-out.**
  - [ ] Run `test_memory_window_burst.py`, `test_bus_budget.py`,
    `test_hybrid_scheduling.py`, `test_capture_rounds.py`,
    `test_can_obd_collector.py` and affected batching/profile/eligibility tests.
  - [ ] Compare before/after deterministic bus traces for successful reads,
    silence/NRC and interruption; require unchanged meaningful trace fields.
  - [ ] Run layering, affected slow tests and shared gates. Replay a stored
    pipeline fixture where relevant, explicitly noting it does not exercise
    collector scheduling or demonstrate bus timing.
  - [ ] Record single ownership of each moved state field, test patch migration,
    trace comparisons and implementation commits.
  - [ ] If claiming hardware cadence or throughput improvement, record a
    separate Pi measurement; leave that claim partial until observed.

## Acceptance

One planner owns burst decisions/accounting and has no send path. The collector
loop composes it; request order, inter-read timing rules, budget charges,
keepalives and exit behavior match the original controlled traces.
