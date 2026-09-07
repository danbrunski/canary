# C08 — Isolate expensive inference and keep a stable sample clock

Priority: medium. Dependencies: repaired C05 benchmark; C10 supplies the full
pipeline workload. Coordinate event-loop measurements with C07.

## Problem and code entry points

`run_live_inference()` in [inference.py](../../../src/daemon/inference.py) invokes
`source.tick()` and detector processing synchronously on the event loop, then
waits a full configured interval. Actual tick separation is processing time
plus that interval. Raising the resampling rate makes the discrepancy more
important and can interfere with collection queues.

[live.py](../../../src/preprocessing/live.py) reconstructs and featurizes the
rolling buffer each tick. That may be acceptable at 1 Hz; no current benchmark
establishes its full Pi cost. State mutation, model swaps, broadcasts, and
writer operations mean wrapping the entire loop in a thread is not a correct
ownership design.

## Implementation checklist

- [ ] C08.1 Measure feature construction, each detector, result publication,
  and total tick occupancy with the repaired benchmark. Add loop scheduling
  delay and missed-deadline metrics separate from model scoring latency.
- [ ] C08.2 Schedule ticks from an absolute monotonic deadline. Timestamp the
  actual observation snapshot, record lateness, and advance deadlines without
  adding processing duration to every period.
- [ ] C08.3 Define overload behavior: coalesce/skip missed scheduled ticks and
  report them instead of generating back-to-back catch-up rows. Preserve real
  elapsed time for derivatives, persistence, sequence masks, and gap flags.
- [ ] C08.4 Define ownership before offloading. Snapshot inputs and the
  selected model generation on the loop, execute computational work in one
  owned worker, and apply results on the loop. Alternatively use a single
  actor owning observation and computation; record the choice and measured
  reason. Do not concurrently mutate `LiveFeatureSource` from two threads.
- [ ] C08.5 Keep broadcasts, asyncio primitives, control transitions, writer
  calls, and alert emission in their appropriate owning context. Reject stale
  results from a previous capture/replay/model generation when applying them
  would cross a state boundary.
- [ ] C08.6 Bound pending computation. A slow detector must not create an
  unbounded executor backlog. Measure GIL contention and worker suitability;
  do not claim that thread offload guarantees CPU parallelism.
- [ ] C08.7 If profiling justifies incremental feature computation, reuse
  shared feature definitions and prove offline/live equivalence across gaps,
  mode boundaries, rolling-window eviction, and model updates. Otherwise retain
  the shared recomputation path and record that optimization as unnecessary.
- [ ] C08.8 Keep collection cadence and model cadence separate. Increasing
  acquisition to 15–20 Hz does not imply IF/sequence models should run at that
  rate. Any new model cadence requires corresponding train/serve validation.

## Regression and acceptance checklist

- [ ] C08.9 Use a fake clock and variable computation durations to verify
  stable deadlines, reported overruns, no catch-up bursts, and prompt stop.
- [ ] C08.10 Test concurrent observation, model hot swap, pause/resume,
  replay transition, worker failure, and shutdown. Assert each accepted result
  applies once to the correct generation.
- [ ] C08.11 Replay a known-good stored session to compare feature values,
  gap flags, operating modes, sequence inputs, and emitted results. Read
  `vehicle_state`; do not interpret live engine-off silence as regression.
- [ ] C08.12 Measure Pi CPU, memory, event-loop delay, processing latency,
  and capture losses under the same workload before/after. Include 1 Hz and
  a higher supported test cadence without changing deployed model defaults.
- [ ] C08.13 Acceptance: processing time does not accumulate as cadence drift,
  shared state has one owner, and computational overload remains observable
  without corrupting measurement chronology or filling an unbounded queue.

## Handoff

- [ ] C08.14 Record ownership, overrun semantics, replay parity, and measured
  benefit. Apply the shared validation gate and keep hardware checks open
  when only workstation evidence exists.
