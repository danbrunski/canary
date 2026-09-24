# Live-inference scheduling and isolation (C08)

Roadmap task C08 (`docs/roadmap/tasks/archive/08-inference-scheduling.md`). Code:
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

## Ownership (C08.4 / C08.5 / D01.4) — loop-owned state, process-built features

`LiveFeatureSource` has one mutable-state owner: the event loop. D01.4 splits
its tick into `prepare_tick` (snapshot and bounded-ring append), the pure
`build_prepared_live_tick` transform, and `apply_built_tick` (operating point
and trip-integral state). Production runs the pure transform in one persistent
spawned process; the synchronous `tick` composition remains the exact debug,
test and parity reference. The request carries the config, behavior-model
snapshot and source generation for that tick, so hot reload is coherent and a
result that finishes across a replay reset is discarded.

| phase | thread | what runs |
|---|---|---|
| **prepare** (`LiveFeatureSource.prepare_tick`) | loop | Stamp the newest observation snapshot, derive gaps, append the bounded ring and copy its frame |
| **feature build** (`build_prepared_live_tick`) | spawned process by default | Exact shared `featurize_wide_frame` transform; returns only the newest feature row, operating point and segmenter-fallback verdict |
| **snapshot continuation** (`LiveInferencePipeline._snapshot`) | loop | Apply the feature result, derived-channel broadcast + Parquet write, autotag hook, segmenter-fallback health edge, vehicle-state gate, idle-shutdown observe, `GenerationToken` read, detector prepares and behavior residuals |
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
`run_live_inference` task, and at most one scoring job is in flight. If a job
is still running when the next deadline arrives, the new snapshot is retained
in a depth-one newest-wins queue; displacing an older pending snapshot counts
`missed_by_reason.compute_busy`. "Still running" is judged *after* the
snapshot, not before it: a job that finishes while the next tick's features
are built has already drained the queue, and a tick queued on the earlier
answer was then overtaken by the tick after it, so its older row reached the
sequence window late and restarted it (2026-09-24: the r2 LSTM scored no
window for a whole drive). Dispatch also refuses any tick older than the last
one dispatched, counted as `compute_busy`. A worker exception is
logged, counted (`compute_failures`), and the loop continues. On stop the
in-flight job is awaited for up to `max(interval, 1 s)`, then the executor is
shut down; the shutdown test asserts no `canary-inference` thread survives.
If a forward pass outlives that bound the executor is released without
waiting and a warning names it — a thread cannot be cancelled mid-pass.

Feature construction has its own persistent
`ProcessPoolExecutor(max_workers=1)`. A process failure or timeout disables
that executor and recomputes the affected tick in a thread, preserving state
progression while keeping the failure non-fatal. Normal shutdown joins both
owned executors.

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

## Per-detector occupancy with real artifacts (C08.1)

The table above was taken **without models** — it is the loop-side cost of
feature construction alone. Only manifests are committed under `models/`
(binaries travel by `scp`), so the C10 harness installs a declared IF stub by
default and the sequence detector is `absent`; every stub-based figure
understates detector occupancy.

Measured 2026-09-08 with a **real IF artifact** — same hyperparameters as the
deployed manifest (`n_estimators: 200`, contamination 0.01, 84 feature
columns), trained from `tests/fixtures/ground_truth/drive-slice` into a
scratch directory and loaded via
`scripts/sustained_validation.py run --models-dir <dir>`:

| stage | scenario | median | p95 |
|---|---|---:|---:|
| `isolation_forest` (worker) | `fuel-map` | 44.09 ms | 52.07 ms |
| `isolation_forest` (worker) | `replay-drive-slice` | 33.11 ms | 38.62 ms |
| `compute` (worker total) | `fuel-map` | 44.09 ms | 52.07 ms |
| `compute_wait` (loop awaits the worker) | `fuel-map` | 44.48 ms | 52.32 ms |
| `loop_occupancy` (snapshot + apply) | `fuel-map` | 43.38 ms | 55.81 ms |
| `total` (tick wall) | `fuel-map` | 81.00 ms | 110.89 ms |

The same two scenarios with the declared stub (`c10-stub`, a constant score;
`data/analyses/sustained-validation/after/`) put the `isolation_forest` stage
at 4.57 ms median / 5.82 ms p95 (`fuel-map`) and 4.41 / 5.92 ms
(`replay-drive-slice`) — so **a stub-based stage table understates IF
occupancy by roughly 8×**, which is why C08.1 held for a real artifact.
(The stub's few ms are the harness's own schema work, not a model.)

Where the 44 ms goes, timed directly rather than by subtraction:

| step | median |
|---|---:|
| `IsolationForestInferenceEngine.__init__` | 0.00 ms |
| `TrainedIsolationForest.score` (1-row frame) | 20.15 ms |
| — of which sklearn `score_samples` (200 trees, 84 features, 1 row) | 4.7 ms |
| `IsolationForestInferenceEngine.observe` | 20.79 ms |

**The sklearn forward pass is ~11 % of the stage.** The rest is per-tick
pandas work on a one-row frame — `reconcile_feature_frame_to_spec` plus the
DataFrame construction, scaling and per-mode selection inside
`TrainedIsolationForest.score`, which is a batch-shaped API called once per
row. At the deployed 1 Hz cadence the whole tick is 8–11 % of the period and
zero deadlines were missed, so **nothing is changed here**; this is the stage
to attack first if the model cadence rises, and it is a loop-*wait*, not loop
occupancy — `loop_occupancy` (43 ms) stays below `total` (81 ms) because C08
already moved the forward pass into the worker.

Caveats, stated: WSL2 workstation, not the Pi (C08.12 still holds the Pi
numbers). The artifact was trained on a 7-minute slice, so its per-mode
`max_samples_` is 97 against the deployed 256 — deeper trees on the Pi's
model make the sklearn 4.7 ms somewhat larger, and none of the surrounding
pandas cost depends on it. The synthetic stream is missing trained feature
columns, so the run was in `blind_modes` and skipped
`_rank_feature_contributors`; a healthy stream does that work too, making
these figures a **lower** bound on the stage. The sequence detector remains
`absent` — no torch artifact exists locally, and its occupancy is unmeasured.

Reproduce:

```
.venv/bin/python scripts/train_anomaly_model.py \
    tests/fixtures/ground_truth/drive-slice --config <cfg with models_dir=$SCRATCH/models>
.venv/bin/python scripts/sustained_validation.py run --label real-models \
    --out $SCRATCH/reports --sessions-root data/_validation \
    --models-dir $SCRATCH/models --scenario fuel-map --scenario replay-drive-slice
```

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
stages{feature, feature_wait, publish_derived, behavior, snapshot, isolation_forest,
       sequence, compute, compute_wait, apply, loop_occupancy, total}
```

`compute` is the worker's own wall time; `compute_wait` is what the loop saw
from submit to result (the difference is thread hand-off + GIL contention).
`health.inference_latency_ms` (the IF forward pass only) is unchanged and
deliberately separate. CPU %, memory and CAN counters stay in `health.system`
/ `health.can0` from `HealthMonitor`.

## Left for hardware (C08.12 — open)

**Measured 2026-09-09** — see
[`data/analyses/2026-09-09-pending-hardware.md`](../data/analyses/2026-09-09-pending-hardware.md) for the full tables.
Isolated harness (`--inference-tick-s`, no deployed default touched): at 1 Hz
**zero missed ticks** on `fuel-map` and `tuning-did` (feature p95 143 / 190 ms,
lateness max ≤ 1.3 ms, zero record loss); at 2 Hz **6 and 12 missed ticks**
with lateness max 273 ms, `limiting_stage` moving from
`inference_feature_stage_on_loop` to `inference_loop`. The Pi's feature stage
is 2.6–2.8× the workstation's 51 / 72 ms, and **2 Hz is not a supported
cadence on this hardware**.

The *deployed* daemon is worse still, because it also runs the sequence
detector against a real ~3,000 frame/s bus: `total` median **863 ms** against
a 1000 ms tick (p95 2,032 ms, max 51 s), **306 of 633 ticks missed (48 %)**,
`loop_occupancy` median 341 ms, storage `max_write_ms` 18,475 ms and `/status`
latency 0.7–5.1 s. That starvation stalls collection for ~100 s at a time: a
concurrent `fuel-map` capture scored 0 of 468 complete tuples with coverage
0.03–0.34, while its in-sample rate was a healthy 3.957 Hz. Fixing that is a
new task, not part of C08.

Originally left open here (now answered above): Pi CPU/memory, event-loop
delay, processing latency and capture-loss counts before/after under the
same workload, at 1 Hz and at a higher test cadence. Everything needed is now
in-process: read `/status.detectors.inference` and `/status.health` during a
replay (`start_replay`) of a known-good session on the Pi, once with the
pre-C08 build and once with this one. Do not change the deployed
`resample_hz` for that measurement; use `tick_seconds` on a test instance.


## Out-of-process scoring (2026-09-09)

C08 moved the IF and sequence forward passes off the event loop into a worker
*thread*. The Pi measurement above shows why that was not enough: **the GIL
makes "off the loop" and "off the CPU" different things.** The daemon missed
48 % of its 1 Hz ticks while `load_1m` was 1.52 on four cores and CPU sat at
24 % — two and a half cores idle. One Python process is bound to roughly one
core, so a slow forward pass starved the collector thread and the HTTP server
sharing that process. That is why collection stopped for ~100 s at a stretch
with **zero** dropped records: nothing overflowed, the collector thread simply
never got scheduled.

`src/daemon/inference_worker.py` runs the two forward passes in a child
process. Only the one-row feature frame and two version strings cross the
pipe; the models are loaded child-side from the same artifact paths the parent
watches, and a hot-swap propagates as a version mismatch rather than a
shipped artifact. Measured IPC cost: **0.16 ms per tick** steady-state, and
~2 s once at startup while the child loads its artifacts.

**Exactly one persistent worker, never a pool.** The sequence engine owns a
rolling window advanced only by a scoring pass, so the window is state; a
second worker would score against a different history. A restarted worker
resumes with an empty window, which refills — `_sequence_observe` already
returns `None` until it is full.

Every side effect stays in the parent. The child touches no broadcaster,
emitter, persistence gate or storage; it returns the same `_TickResult` the
in-process path returned and `_apply` publishes it on the loop as before.
`_IFComputed` carries `model_version` instead of the engine for this reason —
sending a model back every tick to read one string would have cost more than
the scoring it offloads.

Failure is never fatal. A worker that cannot start, dies, breaks its pipe or
answers late returns `None` and the tick is scored in-process exactly as
before. It is a performance knob, never a correctness one.

`models.out_of_process_scoring` ships **off**. The child rebuilds its models
from the config's artifact paths, so it only ever sees models that came from
disk: a parent holding an injected or in-memory model would have the child
score against something else and say so nowhere. Turning it on by default made
every `run_live_inference` end-to-end test complete zero ticks — the parent's
stub models were invisible to the child, so tick 0 blocked and every later
tick skipped as `compute_busy`. Deployments whose models come from disk opt
in.

### What feature-process isolation does *not* fix

The child still recomputes the bounded rolling frame. D01.4 removes that
Python/pandas work from event-loop occupancy; it does not reduce total feature
CPU or the cost of copying the bounded snapshot into the child. Hardware
validation therefore compares both `feature_wait` (elapsed transform/IPC) and
`loop_occupancy` (parent work), plus collection rate and HTTP latency. A later
incremental feature engine is justified only if worker CPU becomes the measured
constraint; it must preserve the shared train/serve transform contract.
