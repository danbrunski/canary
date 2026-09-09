# Hybrid Mode 01 / Mode 22 scheduling

Roadmap task C04 (`docs/roadmap/tasks/archive/04-hybrid-diagnostic-scheduling.md`).
This is the contract for how the Canable collector alternates standard OBD
(Mode 01 and its siblings) with session-gated Mode 22 windows on an ECU where
the two are mutually exclusive, what a *cycle* is, when a cycle is declared
infeasible, and what the scheduler reports about it. Round semantics are in
`docs/capture-rounds.md`; the vehicle's session facts are in
`data/dids.yaml` (`access`).

## The defects this replaces

Both were reproduced on the fake clock + session-gated `ScriptedEcu`
(`tests/capture_round_fakes.py`, `tests/test_hybrid_scheduling.py`) with two
Mode 01 PIDs, two DIDs, a 32 req/s budget, 5 ms ECU latency and the Xterra's
`10 81` fast exit with a 50 ms grace:

1. **Waits ignored the controller's deadlines.** `run_can_obd_loop`'s
   zero-eligible branch drained the bus for a flat 200 ms. After an accepted
   exit the 50 ms grace had long expired when the loop looked again, so
   every exit was followed by Mode 01 **0.206 s** later; a budget-blocked exit
   trailed the last DID by the same 0.206 s.
2. **Window cadence was end-to-start.** `DidWindowController._close_window()`
   scheduled the next window `window_interval_seconds` after the previous
   one *closed*. With the sweep, exit, grace and the two 200 ms holes on top,
   the hybrid profile's 0.25 s interval measured as a **0.948 s cycle
   (1.05 Hz)** — not the 4 Hz it named. The 2026-08-07 drive's rates were
   evidence of that code, not of the ECU.

After the fix, the same scenario runs at **0.250 s per cycle** exactly (14
cycles in 3.5 s, zero missed), exit → Mode 01 request **0.055 s** (grace +
reply latency), last DID → exit one budget slot (0.031 s).

Two further defects surfaced while writing the tests and are fixed with them:

- Under a profile with one `target_rate_hz`, Mode 01 and DID rounds share a
  period, and the C02 round grid held a DID sweep back by up to a round after
  its window opened (entry at 0.75 s, first DID at 0.96 s). A controller
  phase change now re-anchors the round schedule.
- A paused capture (or a profile that dropped its DIDs) kept re-opening
  windows: entry/exit pairs every 0.5 s with nothing to read. Window entry is
  now gated on a DID being *selected right now*, while an owed exit is still
  sent.

## One wake calculation

`src/collection/hybrid_cycle.next_wake_at(now, *, budget_at, next_due,
transition_at, keepalive_at, deadline)` returns a `WakeDecision(at, reason)`
— the earliest candidate strictly after `now`, else the bounded idle tick
(`MAX_IDLE_TICK_SECONDS`, 0.2 s). Candidates in the past are dropped: they
mean "act next tick", and returning them would spin. Every wait in the loop
goes through it (`_wake_for`): the passive branch, the no-request branch, the
zero-eligible branch, the budget wait and the "no round to open" wait.
`tests/test_hybrid_scheduling.py::test_every_wait_in_the_loop_goes_through_next_wake_at`
asserts every `_drain_frames_until` target is an instant it chose.

`next_due` in the zero-eligible branch is the earliest un-suppression of a
backed-off eligible request (`_next_unsuppressed_at`), so a lifted backoff is
serviced when it lifts. The budget slot counts as a deadline there only when
the controller has an exit, entry or keep-alive waiting on it
(`session_work_pending`).

## Timing-policy migration

| field | meaning | where |
|---|---|---|
| `window_interval_seconds` / profile `did_interval_seconds` / config `obd_did_window_interval_seconds` | **end-to-start**: seconds from a window closing to the next opening. Historic meaning, unchanged. Not a cycle period. | `DidSessionSpec`, `CaptureProfile`, `CollectionConfig` |
| `cycle_seconds` / profile `did_cycle_seconds` / config `obd_did_cycle_seconds` | **start-to-start**: period of one Mode 01 + Mode 22 cycle. Windows open at `anchor + k * cycle` on monotonic deadlines; sweep, exit, grace and the Mode 01 phase all sit inside it. | same three places, new |

Precedence: profile `did_cycle_seconds` → config `obd_did_cycle_seconds` →
profile `did_interval_seconds` → config interval. When a cycle is set the
interval is ignored. `None` (every existing saved profile and the config
default) keeps the interval policy byte-for-byte; nothing reinterprets a saved
interval as a cycle. `did_cycle_seconds` is rejected with `did_continuous`
(a held session has no cycle).

The one explicit migration: `data/nissan-xterra-2008/capture_profiles.yaml`
`tuning-hybrid-research` gains `did_cycle_seconds: 0.25` beside its
retained `did_interval_seconds: 0.25`. The profile stays `experimental` until
C04.12 measures it on the truck.

A **cycle starts when the session-entry request goes out** and ends when the
next one does. The target (`cycle_seconds`) and the achieved
`cycle_duration_s` use this one definition. An entry may trail its boundary by
the request in flight at that instant plus one budget slot (the loop does not
pre-empt), never lead it.

## Feasibility rule

Before each window the loop computes a `CyclePlan` (`plan_cycle`) from the
work currently selected: Mode 01-side channels (PIDs, DTC/service polls,
custom PIDs) → requests after batching, unsuppressed DIDs, session requests
(entry + exit with a fast exit, entry alone without) and the grace (the exit
grace, or the full `lapse_seconds` without a fast exit), each request costed
at the budget pacing floor `1 / effective budget`:

```
required = (mode01_requests + did_requests + session_requests) / budget_rps + grace
feasible = required <= cycle_seconds
```

Example, the hybrid profile: 8 PIDs batched 6 → 2, 9 DIDs, 2 session, 90 req/s,
50 ms grace → 0.194 s ≤ 0.25 s, feasible. The same work on a 0.1 s cycle is
short by 0.094 s. An infeasible plan is reported as
`did_profile.cycle.cycle_feasible: false` with `plan.shortfall_seconds`, and
logged **once** per distinct plan; the target is never promised. ECU latency
and timeouts are not in the plan (`worst_case_seconds` shows one timeout for
scale); they make cycles *longer*, which the accounting reports as missed
cycles rather than the plan hiding.

## Overrun

- Boundaries that cannot be met are **skipped**, counted in `cycles_missed`,
  and the next window opens at the next future boundary — no catch-up burst,
  never two windows inside one cycle.
- **Mode 01 share:** after the grace the next window may not open before
  `plan.mode01_share_seconds` (= Mode 01 requests at budget pace) has passed,
  so windows cannot reopen back-to-back and starve Mode 01. Measured under a
  100 ms ECU on a 0.25 s cycle: 0.7–0.8 s per cycle, two boundaries missed
  each, both Mode 01 channels still served every cycle.
- `mode01_shortfall` = planned Mode 01 channels minus channels attempted in
  the cycle; `did_shortfall` = DIDs still pending when the window closed
  (deadline or lost session). Both are cumulative for the generation.
- Nothing spins: every wait is a bounded `recv`; the tests assert no gap
  between `recv` calls exceeds one 50 ms slice across a whole run.

## Vehicle scope and recovery

`DidSessionSpec` is resolved from config + `data/dids.yaml` `access`:
`session`, `exit_session`, `keepalive_seconds` as before, and now
`exit_grace_seconds` (Xterra: 0.05 — Mode 01 answered on the *next request*
after `10 81`, 2026-08-04). No shared code carries a vehicle's grace or session
ids.

The transient/permanent refusal distinction and the bounded re-probe are
unchanged (`FAST_EXIT_REFUSALS_BEFORE_DISABLE`, `FAST_EXIT_RETRY_*`). While the
fast exit is disabled no `10 81` is sent to find out whether it recovered;
during a lapse no Mode 01 is sent to find out whether the blackout ended;
backed-off DIDs are not probed by opening windows for them.

**A lapse is not recovery.** `mode01_recovery_pending` is set when a window
closes (exit sent, or lapse started) and cleared only by an *observed*
successful Mode 01 response; `mode01_recovered_age_s` reports how long ago
that was and is `null` until it happens. The fake ECU reproduces the measured
Xterra behaviour (blackout outliving the S3 lapse) and the test holds the
pending flag through 30 s of miss backoff until Mode 01 actually answers.

## Stale phases (C04.7)

| event | what happens now |
|---|---|
| profile switch out of continuous DID mode (or to any profile without DIDs) | `controller.release()` closes the window; the exit is sent and the grace honoured before Mode 01 is polled |
| capture generation change (profile, pause/resume, passive toggle, replay) | `on_generation_change`: release, cycle accounting reset, next window due *now* — a DID profile armed mid-drive opens its first window within a slot, not up to a config interval later (the 26.56 s first-DID delay of 2026-08-07) |
| paused | owed exit sent, no window opened, raw CAN received; resume opens the next window immediately |
| passive TX gating lifted | the held window is released, exit first, then both services |
| exit failure | transient: this window lapses, next window retries; permanent ×2: lapse path until the re-probe |
| (re)connect | a vehicle with a verified `exit_session` is returned to the default session by the first request, so a previous loop's held session cannot black out Mode 01 |

Under a capture profile the controller is driven whenever the profile owns
DIDs **or** the controller is mid-transition (`_controller_engaged`), so no
phase is ever polled through.

## Metrics (`/status.obd_scheduler`)

`did_profile.cycle`:

| key | meaning |
|---|---|
| `timing_policy` | `"cycle"` or `"interval"` |
| `cycle_seconds` | target start-to-start period; `null` on the interval policy |
| `cycle_feasible`, `plan` | the current `CyclePlan` (`required_seconds`, `shortfall_seconds`, per-service request counts, `mode01_share_seconds`, `worst_case_seconds`) |
| `cycles_started`, `cycles_missed` | this generation |
| `mode01_shortfall`, `did_shortfall` | per-service shortfall, cumulative |
| `cycle_duration_s` | `{n, median, p95, max}` over the last `CYCLE_HISTORY` (240) cycles |
| `occupancy_s` | last completed cycle's bus time per service: `mode01`, `mode22`, `session` |
| `transition_latency_s.exit_to_mode01` | exit request → first Mode 01 *response*, summary |
| `transition_latency_s.entry_to_did` | entry request → first DID *response*, summary |
| `cross_mode_separation_s` | per cycle, the widest gap between a Mode 01 and a Mode 22 `acquired_at` — the honest bound on any cross-mode pairing inside that cycle |
| `mode01_recovery_pending`, `mode01_recovered_age_s` | see above |

Top-level `session_control` separates UDS 0x10 traffic from channel-read
throughput: `sent`, `responses`, `timeouts`, `negative_responses`, `seconds`,
`channel_requests_sent` (= `requests_sent` − session control) and
`channel_response_rate`. `requests_sent` itself still counts every frame on
the wire; the shared `request_timeouts` / `negative_responses` still include
session failures so nothing becomes less visible.

The dashboard type is `HybridCycleStatus` in `types/control.ts`; no panel
renders it yet.

## What was measured (fake clock)

| scenario | result |
|---|---|
| 0.25 s interval, pre-C04 | 0.948 s cycle; exit→Mode 01 0.206 s; DID→exit 0.206 s |
| 0.25 s interval, now | 0.291 s cycle (= interval + sweep, end-to-start preserved); exit→Mode 01 0.055 s |
| 0.25 s cycle | 0.250 s cycle on the grid, 0 missed, both services every cycle |
| 0.1 s cycle (needs 0.2375 s) | reported infeasible, shortfall 0.1375 s, one warning, still progresses |
| 0.25 s cycle, 100 ms ECU | 0.75 s median, ≥ 2 missed per cycle, Mode 01 both channels every cycle |
| grace 0.4 s > cycle | infeasible, every other boundary missed, both services progress |
| busy / silent exit | one lapse, retried next window, `fast_exit_supported` stays true |
| `subFunctionNotSupported` ×2 | exit disabled 30 s with no probe, re-probe accepted, recovery observed |
| no fast exit, ECU blackout 1 s | `mode01_recovery_pending` true through 30 s of backoff until Mode 01 answers |
| silent DIDs | one window, no further entries, Mode 01 at rate, idle not counted as missed |
| continuous profile | session held, keep-alive every 0.5 s (+≤ 1 slot), wake reason `keepalive` |
| end-to-end hybrid profile, batching | per cycle `entry, did, did, exit, mode01(batch)`; wakes at the grace deadline and the boundary; no `idle` wake |

## Hardware procedure (C04.12 — pending, orchestrator/operator)

Not attempted here. On the Pi, with the daemon on this commit:

1. `GET /status` — record `vehicle_state` (`engine_running` or `ignition_only`
   both work; KOEO is fine for timing). Note `obd_scheduler.did_profile.cycle`
   at rest: `timing_policy`, `cycle_feasible`, `plan`.
2. Arm `tuning-hybrid-research` (`did_cycle_seconds: 0.25`). Watch for the
   one-time `hybrid cycle ... is infeasible` warning in the daemon log; if it
   fires, the plan is the answer — do not raise the budget to make it go away
   without `capacity_evidence`.
3. Run ≥ 5 minutes. Every 30 s snapshot `did_profile.cycle` and
   `session_control`: `cycle_duration_s` (median/p95/max), `cycles_missed`,
   `mode01_shortfall`, `did_shortfall`, `occupancy_s`,
   `transition_latency_s`, `cross_mode_separation_s`, `mode01_recovered_age_s`.
4. Stop the capture. Compare with the 2026-08-07 drive
   (`_capture-tuning-hybrid-research-2026-08-07T170918`: Mode 01 1.46 Hz,
   DIDs 0.90 Hz, 0.341 s median cross-mode separation) using
   `achieved_hz_capture`, `capture_max_gap_seconds`, `rounds` and the round
   skew evidence (`round_skew`), not by re-deriving rates from counts.
5. Claim 4 Hz only if `cycle_duration_s.median` ≈ 0.25 with `cycles_missed`
   near zero **and** the persisted rows show it; record the numbers in this
   file and only then drop `experimental` from the profile. Keep it
   experimental otherwise and record what was achieved.


## Measured on the truck (C04.12, 2026-09-09)

Phase `engine_running`, warm idle (coolant 91 °C), 300 s,
`_capture-tuning-hybrid-research-2026-09-09T190901`. Full evidence:
[`data/analyses/2026-09-09-pending-hardware.md`](../data/analyses/2026-09-09-pending-hardware.md).

**The 4 Hz claim is denied and the profile stays `experimental`.**

The cycle is infeasible *by plan*, reported on arming before any timing was
taken: 11 Mode 01 + 9 DID + 2 session requests at 90 req/s plus 0.05 s grace
need **0.294 s** against a 0.250 s cycle — short by 0.044 s,
`cycle_feasible=false`. Per the procedure the budget was not raised.

| quantity | measured | needed for the claim |
|---|---:|---|
| `cycle_duration_s` median | **0.510 s** (≈1.96 Hz) | ≈ 0.25 s |
| `cycle_duration_s` p95 / max | 0.76 / 2.09 s | – |
| cycles started / missed | 590 / **760** | missed ≈ 0 |
| `mode01_shortfall` / `did_shortfall` | 1,966 / 0 | – |
| cross-mode separation median | 0.43 s | – |

Persisted rows (`capture_quality.json`, verdict `fail`) — RPM 3.307 Hz at
coverage 0.599 (max gap 45.1 s), ECM_RPM 1.953 Hz at 0.634 (max gap 26.9 s).
Against the 2026-08-07 drive (Mode 01 1.46 Hz, DIDs 0.90 Hz, 0.341 s
separation) that is **2.26× and 2.17×** — a real gain, still short of 4 Hz.

The binding constraint on the *achieved* side is not the ECM: it answered
97.6 % of requests. It is the daemon's inference loop holding the event loop
(C08.12 below / H5), which also produced the 27–60 s coverage gaps.
