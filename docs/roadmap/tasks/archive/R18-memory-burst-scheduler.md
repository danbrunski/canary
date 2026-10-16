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

- [x] **R18.1 Record the current state machine.** (Close-out: "State machine
  at the move".)
  - [x] Inventory `_BurstPlan`, `_BurstDecision`, `_memory_burst`,
    `_session_budget_at`, `keepalive_policy`, `_after_burst_read`, `_slot`,
    `_spend_budget_slot`, generation reset and every consumer of their fields.
    Recorded below; outside `can_obd_loop.py` the only consumers were
    `test_memory_window_burst.py` (patches `_CanObdLoop._memory_burst`) and
    `test_bus_budget.py` (source counts of `bus_budget.effective(self.budget_rps)`
    and `self._spend_budget_slot()`), both still satisfied.
  - [x] Map who creates, starts, charges, advances, cancels and closes a plan;
    record how ordinary reads and identity/session requests affect its clocks.
    Lifecycle table below.
  - [x] Record request order, issue/acquisition times, prepaid slots, budget
    clocks, window exit and keepalive from existing fake-clock burst scenarios.
    `tests/test_memory_burst_traces.py`: 30 `ScriptedEcu`/`FakeClock`
    runs (the `test_memory_window_burst` setups plus the transitions
    R18.4 names), each trace = every request (time, bytes), every reading
    (channel, acquisition, stamp), each tick's burst decision and each tick's
    state (plan fields, slots spent, budget clock, three turnarounds). Digests
    pinned in `tests/fixtures/memory_burst_traces.json` from a worktree of
    38bb1b69 (collection code identical to fda3c7b7) before any `src` edit.
  - [x] Preserve B08.4's measured assumptions; the refactor is not a new
    optimization or a claim of improved wire throughput. No constant,
    threshold or rule changed; the B08.4 docstrings moved verbatim into
    `src/collection/memory_burst.py`; no throughput claim is made.
- [x] **R18.2 Define the component boundary.**
  - [x] Give the component ownership of the plan, slot accounting and timing
    estimates; do not leave duplicate counters in the loop.
    `MemoryBurstPlanner` owns plan, `next_budget_at`, `slots_spent`,
    memory/paced/session turnarounds and the memory index set; the loop reads
    the clock through a read-only `next_budget_at` property.
    `test_the_loop_keeps_no_copy_of_the_moved_state` (AST over `self.<attr>`)
    fails on a reintroduced `self._session_seconds` (shown).
  - [x] Supply explicit time, budget/cadence, window facts and outstanding
    members. Return hold/issue/member decisions with clear invariants.
    `decide(now, slot, BurstWindow | None, eligible)`; `BurstWindow(members,
    others_owed, deadline)`; `BurstDecision(members, outside, hold_until,
    issue_now, paced_close_at)` with its invariants in the docstring.
  - [x] Define lifecycle hooks for plan/reset, slot spending, session/read
    observations and completion. Preserve monotonic-time units throughout.
    `decide` (plan/start/reset/complete), `spend_slot(now, slot)`,
    `on_session_control(seconds)`, `after_read(seconds, slot)`,
    `session_budget_at`, `keepalive_policy`; every time is caller-supplied
    `time.monotonic()` seconds.
  - [x] Keep IO, sleeping, transmission and global profile reads out of the
    planner. Adapt loop facts into the component at one boundary.
    `test_the_planner_imports_nothing_that_could_send_sleep_or_read_a_profile`
    (AST scan incl. function-level, `TYPE_CHECKING` and relative imports;
    fails on an added `from . import tuning_profile` inside a function, shown)
    and `test_the_planner_never_touches_a_bus_or_a_clock`. The loop's
    `_memory_burst` is the one adapter (controller phase, continuous flag,
    owed members, deadline, `close_as_paced`).
  - [x] Keep the API narrow; do not pass the entire loop or add a generic
    callback framework that recreates access to all its attributes.
    `test_the_planner_api_takes_facts_not_the_loop`: every public parameter is
    a float/int/bool/frozenset, `BurstWindow` or the existing `RequestFilter`.
- [x] **R18.3 Extract without changing scheduling.**
  - [x] Move plan/decision calculation and accounting first; wire the loop
    to one owner in a behavior-preserving commit. One change: new
    `src/collection/memory_burst.py`, `can_obd_loop.py` 2,159 -> 1,964 lines.
  - [x] Preserve memory reads last after other session-window members, then
    back to back near exit, including the deadline-too-short non-prepaid case.
    `test_memory_window_burst.py` 10/10 (incl. the slow 90 req/s parity test);
    traces `paced`, `short_deadline` identical.
  - [x] Preserve charging each slot exactly once, including failure/NRC paths;
    ordinary requests and identity/keepalive reads shift the plan by their slots.
    Traces `nrc`, `silence`, `keepalive_in_wait`, `calibration_id_in_wait`
    identical incl. per-tick `slots_spent`/clock;
    `test_each_burst_read_is_charged_once_and_restores_the_clock`,
    `test_a_prepaid_plan_holds_then_charges_every_read_at_once`.
  - [x] Preserve the longer-of-budget-slot/turnaround paced cost and current
    estimation update rules; no new smoothing constants or thresholds.
    Traces `high_budget*` (200 req/s, reads straddling the 5 ms slot)
    identical; `test_the_next_plan_is_sized_from_the_last_burst`.
  - [x] Preserve continuous-window/no-memory fallback, generation reset,
    stop/deadline handling and rearming after engine start. Traces
    `continuous`, `no_memory`, `generation_change`, `stop_mid_burst`,
    `deadline_mid_burst`, `window_expiry_mid_burst`, `engine_start_rearm`
    identical.
  - [x] Preserve profile filtering, discovery, forced polls, batch governors,
    round completion and scheduler metrics outside the component. None of
    them moved; 730 tests across the collector files below pass.
  - [x] Retain existing loop entrypoints/exports and deliberately retarget
    fake-clock and monkeypatch bindings to the code that now reads time.
    `run_can_obd_loop`, `can_obd_collector` re-exports and the
    `_CanObdLoop` methods `_memory_burst`, `_session_budget_at`,
    `keepalive_policy`, `_after_burst_read`, `_slot`, `_spend_budget_slot`
    remain (`test_the_loop_entrypoints_are_unchanged`). The planner reads no
    clock, so `install_clock` needs no new target: every `time.monotonic()`
    stays in `can_obd_loop`. The paced-reference patch
    (`_CanObdLoop._memory_burst -> None`) still bites:
    `test_the_paced_reference_patch_still_turns_the_burst_off` (paced gaps
    >= one slot, unpatched < slot/4).
- [x] **R18.4 Verify independently controlled transitions.**
  - [x] Extend existing deterministic tests only for uncovered transitions:
    no plan, waiting for ordinary members, prepayment, active burst, completion
    and reset/cancellation. `tests/test_memory_burst_planner.py`
    (`test_no_window_or_nothing_owed_is_no_plan_and_drops_one`,
    `test_memory_waits_for_the_other_window_members`,
    `test_a_prepaid_plan_holds_then_charges_every_read_at_once`,
    `test_each_burst_read_is_charged_once_and_restores_the_clock`).
  - [x] Cover a keepalive or calibration/VIN request after planning, burst
    reads with variable turnaround, timeout/NRC, window expiry and budget change.
    Trace scenarios `keepalive_in_wait`, `keepalive_near_burst`,
    `high_budget_keepalive_{40..80}ms`, `high_budget_varied_keepalive_{40..80}ms`,
    `calibration_id_in_wait`,
    `variable_turnaround`, `silence`, `nrc`, `window_expiry_mid_burst`,
    `budget_change`; planner tests for keep-alive policy, session budget and
    budget change.
  - [x] Cover generation/profile changes, passive suspension, engine-start
    rearm, continuous windows, no memory members and short deadlines. Trace
    scenarios `generation_change`, `passive`, `engine_start_rearm` (start at
    40 s, past the 30 s engine-off hold; memory resumes at +40.385 s),
    `continuous`, `no_memory`, `short_deadline`;
    `test_a_deadline_inside_the_wait_reads_at_once_charged_per_read`.
  - [x] Assert request ordering, timestamps, each charged slot and resulting
    budget clocks; aggregate achieved Hz alone is not evidence of parity.
    The trace digests cover every request's time and bytes and every tick's
    `slots_spent` and `next_budget_at`; the planner tests assert each charge
    and clock value. No rate or count-over-time assertion is used as parity.
  - [x] Use existing `ScriptedEcu` and fake-clock infrastructure rather than
    wall-clock sleeps or a second approximate scheduler implementation. Both
    new files use `tests.capture_round_fakes` (`ScriptedEcu`, `FakeClock`,
    `install_clock`); the planner tests use explicit floats.
- [x] **R18.5 Integration and close-out.**
  - [x] Run `test_memory_window_burst.py`, `test_bus_budget.py`,
    `test_hybrid_scheduling.py`, `test_capture_rounds.py`,
    `test_can_obd_collector.py` and affected batching/profile/eligibility tests.
    730 passed (list in close-out), plus 113 in six further files touching the
    client/loop/replay.
  - [x] Compare before/after deterministic bus traces for successful reads,
    silence/NRC and interruption; require unchanged meaningful trace fields.
    All 30 full trace dumps (29 scenarios + `deadline_mid_burst`) byte-identical
    between the 38bb1b69 worktree and the refactor; digests pinned as a test.
  - [x] Run layering, affected slow tests and shared gates. Replay a stored
    pipeline fixture where relevant, explicitly noting it does not exercise
    collector scheduling or demonstrate bus timing. `test_layering.py` green;
    the slow 90 req/s parity test ran; ruff/format/mypy green.
    `test_replay_event_diff.py` and `test_replay_row_accounting.py` (stored
    fixtures) pass; replay re-emits stored readings and never runs
    `_CanObdLoop`, so it neither exercises this scheduling nor shows bus timing.
  - [x] Record single ownership of each moved state field, test patch migration,
    trace comparisons and implementation commits. Close-out below.
    Implementation commit: the commit that archives this file.
  - [x] If claiming hardware cadence or throughput improvement, record a
    separate Pi measurement; leave that claim partial until observed. No such
    claim is made: the refactor is behaviour-preserving and the traces show
    identical wire order and timing on the fake clock. Nothing was deployed.

## Acceptance

One planner owns burst decisions/accounting and has no send path. The collector
loop composes it; request order, inter-read timing rules, budget charges,
keepalives and exit behavior match the original controlled traces.

## Close-out record (2026-10-15)

Module map:

| Module | Owns |
|---|---|
| `src/collection/memory_burst.py` (new, 366 lines) | `BurstPlan`, `BurstWindow`, `BurstDecision`, `MemoryBurstPlanner`: the plan, the budget clock (`next_budget_at`), `slots_spent`, memory-read / paced-read / session-control turnarounds, the memory index set, keep-alive policy and session budget |
| `src/collection/can_obd_loop.py` (2,159 -> 1,964 lines) | sending/receiving, the controller, rounds, discovery, batching, metrics; `_memory_burst` adapts window facts into the planner and applies `close_as_paced`; `_slot()` stays the one `bus_budget.effective` read |

Imports: `memory_burst` imports only `dataclasses` and
`src.collection.obd_scheduler.requests` (the owning module, not the
`obd_scheduler` facade); `can_obd_loop` imports `memory_burst`. Same package,
no new layer.

### State machine at the move (R18.1)

| Field (before) | Written by | Read by | Owner now |
|---|---|---|---|
| `_burst` (`_BurstPlan`) | `_memory_burst` (create, start, reset), `_after_burst_read` (`reads_done`) | `_session_budget_at`, `keepalive_policy`, `_after_burst_read` | `MemoryBurstPlanner._plan` |
| `slots_spent` | `_spend_budget_slot` | plan `spent_at_plan`, `start`, `release` | `MemoryBurstPlanner._slots_spent` |
| `next_budget_at` | `__init__`, `_spend_budget_slot`, prepaid start, each prepaid read | VIN, calibration id, discovery wait, `_ensure_round`, `_tick` budget waits, four `_wake_for` calls, `_session_budget_at`, plan `first_slot` | `MemoryBurstPlanner._next_budget_at` (loop: read-only property) |
| `_memory_read_seconds`, `_paced_read_seconds`, `_burst_reads_s` | `_after_burst_read`; list reset per plan | plan sizing, `close_as_paced` value, keep-alive lead | planner |
| `_session_seconds` | `_tick_controller` after a session request | plan sizing / close / lead fallback | planner (`on_session_control`) |
| `memory_indices` | `__init__` | `_memory_burst` | planner |

Lifecycle: **created** by `_memory_burst` once no other window member is owed
(`first_slot = max(clock, now)`, `per_read = max(slot, paced mean or read
mean)`, prepaid when the paced end falls before the window deadline);
**held** while `now < start`; **started** on the first tick at or after the
start, a prepaid plan then **charging** every read at once (clock = release)
and handing `close_as_paced(release - per_read + read turnaround)` to the
controller; **advanced** by each burst read (`reads_done + 1`, clock restored to
release when prepaid); **cancelled** whenever no window is open, the window is
continuous, the plan has no memory, nothing is owed, or another member is owed
again; **closed** when its members are drained. Generation change has no
explicit reset: the controller completes the transition, the phase leaves
`window` and the next `_memory_burst` drops the plan. Every other request
(ordinary read, batch, discovery probe, calibration id, VIN, session control)
spends one slot through `_spend_budget_slot`; spent since the plan, each moves
`start` and `release` out by one slot (`BurstPlan.others`).

### Trace comparison

`tests/test_memory_burst_traces.py`, scenarios and HEAD counts (requests /
readings / decision changes / state changes): paced 53/88/25/61,
keepalive_in_wait 53/76/29/61, keepalive_near_burst 53/88/25/61, silence
(0x23 unseen, S3 3 s) 87/135, nrc (2nd address `7F 23 31`), variable_turnaround,
short_deadline, window_expiry_mid_burst, continuous 16/14, no_memory 31/54,
end_to_start_interval, budget_change (20 req/s at the 7th read, cleared at the
16th), generation_change, passive (2 s at the 4th read), stop_mid_burst,
deadline_mid_burst, calibration_id_in_wait, engine_start_rearm 144/222,
high_budget (reads straddling the 5 ms slot), high_budget_varied (reads all
longer than it) and each of those with keep-alives of 40-80 ms. The full JSON
dumps of all 30 runs were byte-identical before and after.

Mutation checks against the 30 trace tests (each reverted): `per_read`
without the paced mean -> 6 fail; `slots_spent += 2` -> all fail; no clock
restore after a prepaid read -> 27 fail; `session_budget_at` without
`min(clock, now)` -> 2 fail; mid-burst keep-alive without the past-release
rule -> 5 fail; loop not calling `close_as_paced` -> 1 fails
(`end_to_start_interval`); loop not calling `_after_burst_read` -> 28 fail.
At 8 req/s alone (every read shorter than the slot) the `per_read` and
`session_budget_at` mutations passed; the 200 req/s scenarios close that.
Measured blind spot: the keep-alive lead without its session-turnaround term
passes every trace (the lead's `burst_s` alone already covers the keep-alive
in these setups); `test_keepalive_policy_while_waiting_and_mid_burst` fails on
it instead.

### Tests

Focused: `test_memory_burst_planner.py` (17), `test_memory_burst_traces.py`
(30). Collector set, 730 passed: those two plus `test_memory_window_burst`,
`test_bus_budget`, `test_hybrid_scheduling`, `test_capture_rounds`,
`test_can_obd_collector`, `test_multi_pid_batching`, `test_multi_did_batching`,
`test_tuning_profile`, `test_profile_edit`, `test_session_window_predicate`,
`test_memory_channels`, `test_diagnostic_discovery`,
`test_default_profile_b08_context`, `test_default_profile_core_rate`,
`test_memory_rate_weights`, `test_ecu_calibration_id`,
`test_capture_start_latency`, `test_d02_accounting`, `test_tx_gate`,
`test_vehicle_power`, `test_queueing`, `test_capture_profiles`,
`test_capture_profile_preview_parity`, `test_layering`. Further 113 passed:
`test_control_obd_request`, `test_can_obd_client`, `test_replay_event_diff`,
`test_repo_tree_isolation`, `test_sustained_validation`,
`test_replay_row_accounting`. No path in `scripts/ml_paths.txt` changed.

### Behaviour

Scheduling unchanged (traces above). Visible to code, not to the bus:

- `_CanObdLoop` lost `memory_indices`, `slots_spent`, `_burst`,
  `_memory_read_seconds`, `_paced_read_seconds`, `_burst_reads_s` and
  `_session_seconds`, gained `burst` (the planner); `next_budget_at` is a
  read-only property. Nothing outside the loop read or wrote them.
- `can_obd_loop._BurstPlan` / `_BurstDecision` are gone (now
  `memory_burst.BurstPlan` / `BurstDecision`, `__module__`
  `src.collection.memory_burst`); `BurstDecision` gained `paced_close_at`.
  `can_obd_loop.dataclasses` is no longer a module attribute. Nothing
  imported any of them; `can_obd_loop` has no `__all__`, `can_obd_collector`'s
  is unchanged; no logger added or renamed.
- `_after_burst_read` reads the budget slot once per read where it read it
  twice; `_session_budget_at`, `keepalive_policy` and `_memory_burst` read
  `time.monotonic()`, the slot, the keep-alive due time and the window
  deadline eagerly instead of after their early returns. All are pure reads;
  on the fake clock the traces are identical.
- The planner has no send path, no clock, no profile read.

