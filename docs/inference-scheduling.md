# Live-inference scheduling and isolation (C08)

Roadmap task C08 (`docs/roadmap/tasks/08-inference-scheduling.md`). Code:
`src/daemon/inference_scheduling.py` (deadline scheduler, generation token),
`src/daemon/inference_metrics.py` (bounded timing recorder, `/status`
snapshot), `src/daemon/inference.py` (`LiveInferencePipeline`, detector
`prepare`/`compute`/`apply` split). Tests: `tests/test_inference_scheduling.py`.

## The defect this replaces

`run_live_inference()` used to do `source.tick(now)` and all detector work
synchronously on the event loop, then `await wait_for(stop, timeout=interval)`.
So the real period was `processing + interval`, and processing time
accumulated as cadence drift. Reproduced on the workstation
(`tests/test_inference_scheduling.py::test_processing_time_does_not_accumulate_as_drift`
under a fake clock; the real-clock numbers below from a scratch run of the
same two loops):

| loop | 100 ticks @ 100 ms with 40 ms busy processing | median period | drift at tick 100 |
|---|---|---:|---:|
| pre-C08 `process(); wait(interval)` | reference implementation | 140.5 ms | **4018 ms** (40 %) |
| `TickScheduler` (absolute deadlines) | this change | 100.18 ms | **0.29 ms** |

Loop lateness (actual start − scheduled deadline) with the scheduler on the
same run: median 1.27 ms, p95 2.24 ms, max 2.73 ms; zero missed deadlines.

## Clock semantics (C08.2 / C08.3)

* Deadlines are `start + k * interval` on `time.monotonic()`. The scheduler
  sleeps *to* the deadline (interruptibly, so stop is prompt), never *for* an
  interval after processing.
* Every tick reads the wall clock (`datetime.now(UTC)`) when it starts; that
  `now` is what `LiveFeatureSource.tick(now)` stamps the row with and derives
  gap flags / Δt from. Monotonic and wall clocks are never mixed in stored
  data. Lateness is recorded separately from every processing stage.
* **Overrun:** when the loop returns to the scheduler and one or more
  deadlines have already passed (by more than 1 µs), those deadlines are
  skipped, counted as `missed_ticks` (reason `overrun`), and the next tick
  runs at the next *future* deadline. A 3.5-period stall yields exactly three
  missed deadlines and one tick at the fourth — never a burst of back-to-back
  catch-up rows. Because the tick still observes real `now`, a skipped period
  shows as a genuine gap (`__gap` flags, GRU-D Δt, persistence timing), not a
  synthesized fill. Processing that lands exactly on the deadline is on time.

## Ownership (C08.4 / C08.5) — snapshot on the loop, compute in one worker, apply on the loop

Decision implemented: the "snapshot → owned worker → apply" option, not a
single actor. `LiveFeatureSource` has one owner (the loop) and is never
touched from the worker thread (`test_forward_pass_runs_in_worker_while_source_stays_on_loop`
asserts `source.tick` only ever runs on the main thread while the forward
pass runs on the `canary-inference` thread).

| phase | thread | what runs |
|---|---|---|
| **snapshot** (`LiveInferencePipeline._snapshot`) | loop | `source.tick(now)` (feature construction), derived-channel broadcast + Parquet write, autotag hook, segmenter-fallback health edge, vehicle-state gate, idle-shutdown observe, `GenerationToken` read, `IsolationForestDetector.prepare` (pin `loaded`), `SequenceDetector.prepare` (version bookkeeping and engine hot-swap/rebuild), `BehaviorDetector.prepare` (`_observe_behavior`: every surface graded off `latest_operating_point`, live overlay updated) |
| **compute** (`_compute_tick`) | worker | `_if_compute`: schema align, `IsolationForestInferenceEngine.observe` (sklearn), threshold/scale lookup, contributor ranking. `_sequence_observe`: schema align, window append, torch forward pass. Pure — returns data and its own stage durations |
| **apply** (`_apply`) | loop | generation check → `_if_apply` (health, `/ws/scores`, persistence gate, IF emit), `SequenceDetector.apply` (`/ws/scores`, gate, LSTM emit), `BehaviorDetector.apply` (gate, `/ws/scores`, behavior emits), fusion + fused emit, timing bookkeeping |

What was **not** offloaded, and why: the behavior detector reads loop-owned
source state and its surfaces are tiny interpolations (measured 0.006 ms
median, see below), so it has no worker half. The sequence engine's window
buffer is detector-owned and only advanced from the worker, one job at a
time; the engine object is only rebuilt on the loop, in `prepare`, which the
pipeline reaches only when no job is in flight — so no two threads ever hold
it. `_score_and_emit`, `_sequence_observe` and `_observe_behavior` keep their
signatures; `_score_and_emit` is now literally `_if_apply(_if_compute(...))`.

**Generation token** (`GenerationToken`): replay id (if running), active
capture id, and `(manager, serving_version)` for every model manager, read on
the loop at snapshot and re-read at apply. A mismatch discards the result and
bumps `stale_results_discarded`; the next tick scores under the new
generation. Tested for a model hot-swap and a replay start mid-compute. There
is no pause/resume hook in `src/daemon/` or `src/control/` today (grep), so
that case is covered by the replay transition; if one is added it should be
folded into the token.

## Bounded computation (C08.6)

Exactly one `ThreadPoolExecutor(max_workers=1)` is owned per
`run_live_inference` task, and at most one compute job is in flight. If a job
is still running when the next deadline arrives, that tick is skipped
(`missed_by_reason.compute_busy`) — never queued. A worker exception is
logged, counted (`compute_failures`), and the loop continues. On stop the
in-flight job is awaited for up to `max(interval, 1 s)`, then the executor is
shut down; the shutdown test asserts no `canary-inference` thread survives.
If a forward pass outlives that bound the executor is released without
waiting and a warning names it — a thread cannot be cancelled mid-pass.

A `compute_busy` skip skips the whole tick, including `source.tick()`, so the ring buffer receives no row for that period. That is the same effective cadence loss the pre-C08 loop suffered as drift, now counted and reported rather than silently accumulated; the next tick's row carries a real time gap.

### GIL note — thread offload is not CPU parallelism

Measured with the pipeline at a 50 ms cadence and a fake IF whose `score`
takes 30 ms either as a pure-Python busy loop (holds the GIL) or as
`time.sleep` (releases it), with a 5 ms "ping" coroutine on the loop
measuring its own wake-up overshoot:

| worker behaviour | tick lateness median / p95 | loop ping overshoot median / p95 / max | ticks |
|---|---|---|---|
| busy-loop (GIL held) | 0.61 / 1.06 ms | **4.50 / 5.28 / 10.33 ms** | 80/80, 0 missed |
| sleep (GIL released) | 0.65 / 1.03 ms | **0.14 / 3.52 / 5.39 ms** | 80/80, 0 missed |

Read: the scheduler's own deadlines are kept either way (the sleep to the
next deadline is long compared to the GIL switch interval), but every other
loop task is delayed by ~one GIL switch interval (5 ms) whenever the worker
holds the GIL. Real sklearn/torch forward passes release the GIL inside their
native kernels for most of their runtime, but the pandas/numpy glue around
them does not. So the offload buys loop *responsiveness during the native
parts* and keeps `LiveFeatureSource` mutation single-owner; it does not make a
CPU-bound Python stage free. Whether the Pi 5's four cores turn that into a
real win is a C08.12 hardware measurement, not a claim made here.

## Feature-stage profiling — incremental features not warranted (C08.7)

C05's feature-only microbenchmark (`data/analyses/2026-09-06-benchmark-live-pipeline-baseline.md`)
measured `LiveFeatureSource.tick` at 39.2 ms median / 43.2 ms p95 on the
workstation with 16 OBD channels + 20 CAN frames/tick over a full 181-row
buffer. The new recorder run through the pipeline (16 OBD channels, no CAN,
200 ticks, no model, full 181-row buffer) gives, per stage:

| stage | median | p95 | max |
|---|---:|---:|---:|
| `feature` (`source.tick`) | 12.815 ms | 14.134 ms | 58.578 ms |
| `behavior` | 0.006 ms | 0.015 ms | 0.033 ms |
| `publish_derived` | 0.022 ms | 0.039 ms | 0.055 ms |
| `apply` | 0.004 ms | 0.007 ms | 0.024 ms |
| `loop_occupancy` (snapshot + apply) | 12.997 ms | 14.333 ms | 58.789 ms |

The feature stage is the dominant loop-side cost (>98 % of occupancy), but at
the deployed 1 Hz model cadence that is 1.3–4.3 % of the period on the
workstation. It is on the loop by design (single owner of the ring buffer),
and with the deadline grid it no longer drifts the clock. Incremental feature
computation would require proving offline/live equivalence across gaps, mode
boundaries, buffer eviction and model updates for every rolling feature —
**recorded as unnecessary at the current cadence**; the shared
`featurize_wide_frame` recompute path is retained. Revisit only if the Pi
measurement (C08.12) shows the feature stage approaching the period, and then
with the parity test below extended to the incremental path.

## Collection cadence vs model cadence (C08.8)

`tick_seconds` is the model cadence. It defaults to
`1 / preprocessing.resample_hz` (1 Hz deployed) and is independent of any
collector's acquisition rate: raising OBD polling or CAN capture to 15–20 Hz
changes what `observe()` sees between ticks, not how often the detectors
score. The IF and sequence models were trained on 1 Hz resampled features; a
different model cadence needs matching train/serve validation before it can
be deployed. No deployed default changed in this task. `run_live_inference`'s
`tick_seconds=` override remains for tests and experiments.

## Replay parity (C08.11)

`test_replay_parity_between_synchronous_path_and_scheduled_pipeline` (marked
`slow`, ~4.5 s) feeds the first 90 s of the committed ground-truth drive slice
(`tests/fixtures/ground_truth/drive-slice`, recorded timestamps preserved,
`data/bindings.yaml` decode) into two `LiveFeatureSource`s, ticking one with
the pre-C08 synchronous `source.tick(now)` and the other through
`LiveInferencePipeline.run_tick` with an injected wall clock, at identical tick
times. Asserted identical per tick: the full feature row
(`assert_frame_equal`), every `__gap` flag, `operating_mode`, all value
columns (the sequence-engine inputs), and `latest_operating_point`. Passed.

`/status.vehicle_state` note for anyone repeating this live: engine-off
silence yields `row is None` ticks and no scoring — that is the vehicle-state
gate, not a regression.

## Observability (C08.1 / C08.12)

`components.inference_timing` (an `InferenceTimingRecorder`, loop-owned,
bounded to the last 300 samples per stage) is exposed on `/status` under
`detectors.inference`:

```
interval_seconds, ticks_scheduled, ticks_completed,
missed_ticks, missed_by_reason{overrun, compute_busy},
stale_results_discarded, compute_failures,
lateness{count, median_ms, p95_ms, max_ms, last_ms},
stages{feature, publish_derived, behavior, snapshot, isolation_forest,
       sequence, compute, compute_wait, apply, loop_occupancy, total}
```

`compute` is the worker's own wall time; `compute_wait` is what the loop saw
from submit to result (the difference is thread hand-off + GIL contention).
`health.inference_latency_ms` (the IF forward pass only) is unchanged and
deliberately separate. CPU %, memory and CAN counters stay in `health.system`
/ `health.can0` from `HealthMonitor`.

## Left for hardware (C08.12 — open)

Not attempted here (Pi unreachable, offline task): Pi CPU/memory, event-loop
delay, processing latency and capture-loss counts before/after under the
same workload, at 1 Hz and at a higher test cadence. Everything needed is now
in-process: read `/status.detectors.inference` and `/status.health` during a
replay (`start_replay`) of a known-good session on the Pi, once with the
pre-C08 build and once with this one. Do not change the deployed
`resample_hz` for that measurement; use `tick_seconds` on a test instance.
