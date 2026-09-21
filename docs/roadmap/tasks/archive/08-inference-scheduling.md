# C08 — Isolate expensive inference and keep a stable sample clock

Priority: medium. Dependencies: repaired C05 benchmark; C10 supplies the full
pipeline workload. Coordinate event-loop measurements with C07.

## Problem and code entry points

`run_live_inference()` in [inference.py](../../../../src/daemon/inference.py) invokes
`source.tick()` and detector processing synchronously on the event loop, then
waits a full configured interval. Actual tick separation is processing time
plus that interval. Raising the resampling rate makes the discrepancy more
important and can interfere with collection queues.

[live.py](../../../../src/preprocessing/live.py) reconstructs and featurizes the
rolling buffer each tick. That may be acceptable at 1 Hz; no current benchmark
establishes its full Pi cost. State mutation, model swaps, broadcasts, and
writer operations mean wrapping the entire loop in a thread is not a correct
ownership design.

## Implementation checklist

- [x] C08.1 Measure feature construction, each detector, result publication,
  and total tick occupancy with the repaired benchmark. Add loop scheduling
  delay and missed-deadline metrics separate from model scoring latency.
  _metrics done 2026-09-07: `InferenceTimingRecorder` (bounded per-stage occupancy: feature/publish/behavior/IF/sequence/compute/apply/total, plus lateness and missed-by-reason kept separate) exposed at `/status.detectors.inference`._
  _measured 2026-09-08 with a real IF artifact under the C10 full-pipeline workload (`scripts/sustained_validation.py run --models-dir`, added for this): `isolation_forest` 44.09 ms median / 52.07 ms p95 on `fuel-map` against the stub's 4.57 / 5.82 — an **8× understatement** by the model-less table. Timed directly, the sklearn forward pass is only ~4.7 ms of it; the rest is per-tick pandas on a one-row frame. Loop occupancy (43 ms) stays below tick total (81 ms), so C08's worker isolation holds. Table, breakdown, caveats and reproduction in `docs/inference-scheduling.md` ("Per-detector occupancy with real artifacts"). The sequence detector stays unmeasured — no torch artifact exists off the Pi — and the Pi numbers remain C08.12._
- [x] C08.2 Schedule ticks from an absolute monotonic deadline. Timestamp the
  actual observation snapshot, record lateness, and advance deadlines without
  adding processing duration to every period.
  _done: `TickScheduler` deadlines `start + k·interval` on the monotonic clock; observation stamped with actual wall time; lateness recorded; processing never extends the period (100 ticks @ 100 ms with 40 ms work: drift 4013 ms → ~2 ms real clock, 0 fake clock)._
- [x] C08.3 Define overload behavior: coalesce/skip missed scheduled ticks and
  report them instead of generating back-to-back catch-up rows. Preserve real
  elapsed time for derivatives, persistence, sequence masks, and gap flags.
  _done: overrun skips to the next future deadline, counted as `missed_ticks[overrun]`; 3.5-period stall → exactly 3 missed, next tick at deadline 4; skipped ticks appear as real time gaps to `LiveFeatureSource`, sequence buffer and persistence._
- [x] C08.4 Define ownership before offloading. Snapshot inputs and the
  selected model generation on the loop, execute computational work in one
  owned worker, and apply results on the loop. Alternatively use a single
  actor owning observation and computation; record the choice and measured
  reason. Do not concurrently mutate `LiveFeatureSource` from two threads.
  _done: snapshot on the loop (`LiveFeatureSource` touched only there), pure compute (IF score / sequence forward pass) in one owned `ThreadPoolExecutor(max_workers=1)`, apply on the loop; behavior detector stays on the loop; ownership table in `docs/inference-scheduling.md`._
- [x] C08.5 Keep broadcasts, asyncio primitives, control transitions, writer
  calls, and alert emission in their appropriate owning context. Reject stale
  results from a previous capture/replay/model generation when applying them
  would cross a state boundary.
  _done: emits/broadcasts/writer/health/idle-shutdown all on the loop; frozen `GenerationToken` (replay id, capture id, serving model versions) checked at apply → `stale_results_discarded`; hot-swap and replay-transition tests discard exactly once._
- [x] C08.6 Bound pending computation. A slow detector must not create an
  unbounded executor backlog. Measure GIL contention and worker suitability;
  do not claim that thread offload guarantees CPU parallelism.
  _done: at most one in-flight future; busy worker → `missed_ticks[compute_busy]`, never queued; GIL measured (busy-loop vs sleep worker) and documented as "offload ≠ CPU parallelism"._
- [x] C08.7 If profiling justifies incremental feature computation, reuse
  shared feature definitions and prove offline/live equivalence across gaps,
  mode boundaries, rolling-window eviction, and model updates. Otherwise retain
  the shared recomputation path and record that optimization as unnecessary.
  _done: profiling shows the feature stage is ~13 ms median, 1.3–4.3% of the 1 Hz period → shared recomputation retained, incremental features recorded as unnecessary._
- [x] C08.8 Keep collection cadence and model cadence separate. Increasing
  acquisition to 15–20 Hz does not imply IF/sequence models should run at that
  rate. Any new model cadence requires corresponding train/serve validation.
  _done: model cadence stays `tick_seconds or 1/resample_hz`; no deployed defaults changed; separation documented in `docs/inference-scheduling.md` and `docs/daemon-architecture.md`._

## Regression and acceptance checklist

- [x] C08.9 Use a fake clock and variable computation durations to verify
  stable deadlines, reported overruns, no catch-up bursts, and prompt stop.
  _done: fake-clock tests for grid stability, overrun reporting, no catch-up, prompt stop (`tests/test_inference_scheduling.py`, 24 tests, 3× `-n auto` clean)._
- [x] C08.10 Test concurrent observation, model hot swap, pause/resume,
  replay transition, worker failure, and shutdown. Assert each accepted result
  applies once to the correct generation.
  _done: concurrent observe, hot swap, replay transition, worker failure, shutdown (no leaked thread), exactly-once ordered apply; no pause/resume hook exists in the daemon (noted in the doc)._
- [x] C08.11 Replay a known-good stored session to compare feature values,
  gap flags, operating modes, sequence inputs, and emitted results. Read
  `vehicle_state`; do not interpret live engine-off silence as regression.
  _done: slow parity test on `tests/fixtures/ground_truth/drive-slice` — identical feature rows, `__gap` flags, operating modes, value columns and operating point between the synchronous path and the scheduled pipeline._
- **C08.12 — moved to [`PENDING-HARDWARE.md`](PENDING-HARDWARE.md) as H5.** Measure Pi CPU, memory, event-loop delay, processing latency, and capture losses under the same workload before/after. Include 1 Hz and a higher supported test cadence without changing deployed model defaults. Not a checkbox here: it is a measurement on the vehicle, and this document's implementation is complete without it.
- [x] C08.13 Acceptance: processing time does not accumulate as cadence drift,
  shared state has one owner, and computational overload remains observable
  without corrupting measurement chronology or filling an unbounded queue.
  _done: drift no longer accumulates; `LiveFeatureSource` has one owner; overload observable via `missed_by_reason{overrun,compute_busy}` with wall-clock chronology intact and a one-future bound._

## Handoff

- [x] C08.14 Record ownership, overrun semantics, replay parity, and measured
  benefit. Apply the shared validation gate and keep hardware checks open
  when only workstation evidence exists.
  _done: `docs/inference-scheduling.md` (ownership, overrun semantics, replay parity, measured benefit, GIL note, hardware left open) + `docs/daemon-architecture.md` rows updated. Gate green._
