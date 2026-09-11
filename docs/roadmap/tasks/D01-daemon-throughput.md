# D01 — daemon throughput on the Pi

Opened 2026-09-09 from the pending-hardware session. Measurements and their
raw evidence: [`data/analyses/2026-09-09-pending-hardware.md`](../../../data/analyses/2026-09-09-pending-hardware.md).

The vehicle measurements (H0–H6) found one problem behind most of the others:
**the daemon is a single Python process, GIL-bound to roughly one core, doing
more work per tick than a tick has time for.** On the Pi at `engine_running`
it missed **306 of 633** 1 Hz inference ticks while `load_1m` was 1.52 on four
cores and CPU sat at 24 % — two and a half cores idle. That starvation is what
stalled collection for ~100 s at a stretch and left a concurrent `fuel-map`
capture with **0 of 468** complete tuples.

The bus is not implicated anywhere: the ECM answered 97.6 % of requests and
throughput was flat at ~7.9 req/s across an 8 → 16 → 32 req/s budget ladder.
**Do not raise a rate default against any item here.**

## Done

- [x] D01.1 Score IF + sequence in a child process
  (`src/daemon/inference_worker.py`, `models.out_of_process_scoring`,
  default **off** — see below). One persistent worker, never a pool — the sequence engine's
  rolling window is state. Only the one-row frame and two version strings
  cross the pipe; measured IPC cost **0.16 ms/tick** steady, ~2–7 s once at
  startup while the child loads artifacts. Falls back to in-process scoring
  when the child cannot start, dies, breaks its pipe, or answers late.
  Design and rationale: `docs/inference-scheduling.md` → "Out-of-process
  scoring".

  **Ships off.** The child rebuilds its models from the config's artifact
  paths, so it only sees models that came from disk; a parent holding an
  injected or in-memory model would have the child score against something
  else and report nothing wrong. Enabling it by default made every
  `run_live_inference` end-to-end test complete zero ticks for exactly that
  reason. Deployments whose models come from disk opt in — which is D01.3's
  job, not a default.

- [x] D01.2 Make `_IFComputed` pure data — it carried the
  `IsolationForestInferenceEngine` and was used only for `.model_version`,
  which cannot cross a pipe every tick. Prerequisite for D01.1.

- [x] D01.3 **Measured on the truck 2026-09-09.** Deployed, enabled
  (`models.out_of_process_scoring: true` in `/etc/canary/canary.yaml`), worker
  confirmed up (`inference worker started (pid 1517)`, child process present).
  Before: `default` profile at warm idle, worker off, 5 min. After: same
  profile and build with the worker on, over an ~13 min drive (40–91 km/h,
  up to 3700 rpm). Raw samples:
  `data/analyses/2026-09-09-pending-hardware/drive-d01/`.

  | | before (worker off, idle) | after (worker on, driving) |
  |---|---|---|
  | ticks missed | 52 / 437 (11.90 %) | 50 / 599 (8.35 %) |
  | — `compute_busy` | 51 | 48 |
  | — `overrun` | 1 | 2 |
  | `compute` median | 423.2 ms | **349.8 ms (−17 %)** |
  | `total` median | 759.9 ms | 696.5 ms (−8 %) |
  | `feature` median | 307.3 ms | 311.2 ms (unchanged) |
  | `loop_occupancy` median | 314.0 ms | 317.4 ms (unchanged) |

  **Verdict: real but partial, and one prediction failed.**

  - *Held:* `feature` and `loop_occupancy` did not move. They are still on
    the loop, exactly as predicted — D01.4 owns them.
  - *Held:* `compute` fell 17 %. The child has its own core and no longer
    contends for the parent's GIL. This is the change doing what it should.
  - **Failed:** `compute_busy` was predicted to "fall sharply". It did not —
    48 of 50 misses are still `compute_busy`. The reason is in `run_tick`:
    while a compute is in flight it counts the miss and **returns before
    taking the next snapshot**, so the parent sits idle while the child
    works. Moving scoring to another process does not overlap it with the
    next tick's feature build, because the pipeline forbids that overlap.
    Necessary, not sufficient — see D01.9.

  Caveat on the miss-rate row: the before window was at idle and the after
  window was driving, so 11.90 % → 8.35 % is not a like-for-like comparison.
  The stage timings are the trustworthy part of this table.

  Not re-measured: the ~100 s collection gaps and `/status` latency. Those
  were observed under a concurrent capture with a much longer-lived daemon;
  neither this before nor this after window reproduced those conditions.

## Where the tick budget actually goes (measured, 2026-09-09)

Against a 1000 ms tick, on the Pi, with the worker enabled and driving:
`feature` 311 ms (on the loop) + `compute` 350 ms (in the child) ≈ 696 ms
`total`, with p95 above the period — which is what produces the residual
skips. The three levers, in order of measured value:

1. **D01.9 pipelining** — run those two concurrently instead of in series.
   Costs nothing but scheduler care; would take the critical path from
   ~696 ms to ~max(311, 350) ≈ 350 ms.
2. **D01.8 batched rolling** — 5.4× on the rolling stage, bit-identical.
3. **D01.4 feature off the loop** — the structural fix, and the largest
   change.

None of them is the bus: the ECM answered 97.6 % and throughput was flat
across a 4× budget ladder.

## The plan (2026-09-09)

Sequenced by measured value per unit of risk. Every number here was measured
this session; every phase ends with a gate that can fail.

**Target, stated up front:** 1 Hz inference with `missed_ticks` ≈ 0 over a
20–30 minute drive, `/status` under 100 ms, and a `default`-profile capture
whose per-channel `coverage` ≈ 1.0. That final drive doubles as H1 evidence,
closing two docs with one session.

### Phase 1 — D01.8: batch the per-column pandas calls ✅ DONE

**Result: workstation feature tick 155 ms → 51 ms (3.0×)**, smoke and steady
both (`steady` median 54.3 ms, was ~155). Bit-identical output, verified two
ways: an exact old-vs-new comparison over three real resampled sessions
(420×182 up to 263×497) across five `FeatureConfig` variants, and a permanent
regression test (`tests/test_features_batched_parity.py`, 25 cases) that keeps
the verbatim per-column implementation as its reference and compares with
`array_equal(..., equal_nan=True)` — not `allclose`, because this feeds
trained models. Ground-truth suite green.

Two of the three planned sites landed; the third was measured and rejected:

- ✅ `features.py` rolling `mean`/`std` → `windowed.mean()` / `windowed.std()`.
- ✅ `features.py` ROC → one frame-wide `shift` and one `div(axis=0)`.
- ❌ `feature_pipeline.py` `reconcile_feature_frame_to_spec` — **reverted.**
  It costs 54.6 ms/call on a *one-row* 327-column frame, so it looked like an
  easy win, but batching it made it **slower (62.7 ms)**. Profiling showed why:
  the cost is block-manager churn (`iset`, `_iset_split_block`, `blocks.delete`
  — 6,540 writes per 20 calls), and a bulk `out[cols] = frame` assignment
  splits blocks harder than the loop did. Fixing it properly means building
  the output frame once instead of mutating it, which is a real restructure of
  the function that produces model input. Deferred: it runs in the child
  process now, so its ~54 ms is ~25 % of `compute` and does not block the
  loop. Tracked as D01.10.

*Original plan text follows.*

### Phase 1 (as planned) — batch the per-column pandas calls

*Smallest risk, no scheduler changes, workstation-verifiable.* Three
same-shaped loops in the feature path make ~135+ single-column pandas calls
per tick where 2–3 whole-frame calls compute the same thing:

- `features.py` rolling `mean`/`std`: per-column → `windowed.mean()` /
  `windowed.std()`. Measured 21.97 → 4.10 ms (5.4×), bit-identical.
- `features.py` ROC: per-column `shift`/subtract/divide → one
  `ordered[columns].shift(n_rows)` and frame-wide arithmetic.
- `feature_pipeline.py` missing-column fill: per-column `out[col] = fill`
  (the "highly fragmented" warning) → build once, `pd.concat`.

Gate: `np.allclose(..., equal_nan=True)` old-vs-new on real session data,
the ground-truth suite, full pytest, and **operator sign-off** — featurization
feeds the trained IF and sequence models and parity is non-negotiable.
Success metric: the workstation benchmark tick median (219 ms at last
profiling) re-measured; the actual number recorded, not assumed.

### Phase 2 — D01.9: pipeline `run_tick` — **DEFERRED pending Phase 3**

Not implemented, on purpose. Phase 1 returned 3.0× on the feature stage
rather than the modest gain it was scoped for, which changes the arithmetic:
projecting the workstation ratio onto the Pi's measured 311 ms feature stage
gives ~105 ms, so the serial path becomes ~105 + 350 ≈ **455 ms against a
1000 ms tick** — most of the `compute_busy` pressure may already be gone.

Pipelining also carries the sharpest risk in this doc. `SequenceDetector.
prepare` rebuilds the engine on the loop and documents its invariant
explicitly: *"never while a worker pass over the old one could still be
running (the pipeline only snapshots when no compute job is in flight)"*.
Overlapping snapshot with compute breaks that invariant for the in-process
fallback path, so pipelining has to be worker-only, with the fallback
excluded — extra machinery that is worth building only if the measurement
says it is needed.

**Decision rule for Phase 3:** if `missed_by_reason.compute_busy` is near
zero and `total` p95 is comfortably under 1000 ms with Phase 1 deployed,
close D01.9 as unnecessary. If `compute_busy` is still material, build it
worker-only. Do not build it blind — that is how the last prediction in this
doc failed.

### Phase 2 (as planned) — pipeline `run_tick`

*The missing half of D01.1, and the single largest latency win.* Today a
tick that lands while a compute is in flight is skipped **before its
snapshot**, so parent and child alternate instead of overlapping. Change:
take tick N+1's snapshot while tick N scores in the child, holding at most
one pending request; apply results strictly in tick order (the pipe is FIFO
and the generation token already guards staleness). The sequence window's
ordering is preserved because the child still scores one tick at a time.

Expected: critical path drops from feature+compute in series (~696 ms) to
max(feature, compute) (~350 ms); `compute_busy` misses — 48 of 50 today —
approach zero at 1 Hz. Gate: ordering/generation unit tests, full pytest,
then the Pi comparison below.

### Phase 3 — remeasure on the truck — ✅ **DONE 2026-09-10**

Ran the D01.3 protocol over a 24-minute drive with D01.1 deployed and
**D01.8 not deployed** ([evidence](../../../data/analyses/2026-09-10-drive-h1-h7-h9.md)). That is the post-D01.1 /
pre-batching baseline; the `features.py` half is still owed.

| stage | median | p95 | 2026-09-09 |
|---|---:|---:|---:|
| `total` | 659.7 ms | 862.0 ms | 863 ms |
| `feature` | 296.9 ms | 396.1 ms | – |
| `compute` | 328.8 ms | 477.3 ms | – |
| `sequence` | 251.5 ms | 394.8 ms | – |
| `isolation_forest` | 73.0 ms | 95.6 ms | – |
| `loop_occupancy` | 301.9 ms | 431.4 ms | 341 ms |

Ticks missed fell from **306 / 633 (48 %)** to **31 / 1448 (2.14 %)**, every
one of them `compute_busy` and none an `overrun`; tick lateness p95 2.52 ms.
**The tick is inside its 1000 ms budget on this hardware.** Storage's worst
write fell from 18,475 ms to 952.3 ms with the queue never above 2003 / 10,000
and nothing dropped.

Caveat on `isolation_forest`: `missing_feature_rate` read **1.0** in every
operating mode for the whole drive, so that 73.0 ms is time spent scoring an
all-missing vector and is not a representative cost. `sequence` at 251.5 ms
dominates `compute` regardless, so the conclusions above stand. The blind IF is
its own defect, recorded in the evidence doc.

**Completed 2026-09-10.** `features.py` deployed and the Pi brought to a
committed revision (`4c84ac3`, clean tree — D02.7). Post-D01.8 on hardware:

| stage | pre-D01.8 drive | post-D01.8 idle | post-D01.8 under `tuning-did` |
|---|---:|---:|---:|
| `feature` | 296.9 ms | 164.6 ms | — |
| `loop_occupancy` | 301.9 ms | 169.2 ms | — |
| `total` | 659.7 ms | 439.0 ms | 320 ms |

Phase 3 is closed. The event loop is now free ~83 % of each second against
~70 % before, and the collector took up the slack: 116.8 req/s sustained on
the H8 drive against a ~7.9 req/s idle ceiling that three earlier sessions
had read as a hardware limit.

Cheapest first check needs no vehicle at all: `scripts/benchmark_live_pipeline.py
--profile steady` on the Pi. Today's baseline is **smoke 122.7 ms / steady
127.6 ms**; the workstation went 155 → 51 ms on the same change, so a
comparable drop should show without the engine running.

### Phase 3 (as planned) — remeasure on the truck

Same protocol as D01.3 (5-min windows, `default` profile, phase recorded).
This is also where two deferred questions get their post-fix baseline:

- D01.5: is the 6.6 % receive loss still there once the loop isn't starved?
- D01.6: do the 18.5 s storage stalls survive?

Use the UPS keep-alive (`set_ups_keepalive`) so the ignition doesn't end
the window again.

### Phase 4 — what the numbers say, not before

- Loss persists → D01.5 dedicated receive thread.
- Stalls persist → D01.6 storage investigation.
- Tick still over budget → D01.4 (feature build off the loop — the big
  structural change, deliberately last because Phases 1–2 may make it
  unnecessary), and only then D01.7 (sequence cadence/backbone decision).

**What the 2026-09-10 numbers said** ([evidence](../../../data/analyses/2026-09-10-drive-h1-h7-h9.md)):

- **Loss persists → D01.5 is warranted.** A 65 s warm-idle comparison put the
  kernel at 120,599 frames and the daemon at 115,480: **4.24 % unaccounted**,
  with `rx_dropped` flat — the same invisible-to-`ip -s link` signature as the
  6.6 %. The two reads are not atomic and the daemon's window is the wider, so
  4.2 % is a floor. Some of the original 6.6 % was GIL starvation and is gone.
  D01.5 also gained a **second, distinct cause**: `can0 rx_over_errors` rose by
  121 during the drive while `rx_dropped` stayed flat — MCP2515 controller
  overflow (two hardware RX buffers, SPI not draining them), a driver-level
  loss underneath the socket-level one. Do not fold them into one number.
- **Stalls did not persist → D01.6 closes.** 18,475 ms → 952.3 ms, queue high
  water 7778 → 2003 of 10,000, 0 dropped. It was contention from the starved
  loop, as D01.6 suspected, not a defect of its own. Nothing was widened.
- **Tick is under budget → D01.4 and D01.7 are not forced.** The feature build
  still holds the event loop for 301.9 ms median, which remains the argument
  for D01.4, but it is no longer a deadline argument.
- **D01.9 is the highest-value item left**: all 31 misses were `compute_busy`,
  which is exactly the serialisation it removes.

### Non-goals, determined this session

- **Rust port: no.** Profiled the real feature pipeline end to end: our own
  code is **2.1 %** of runtime, numpy 3.9 %, pandas **70.8 %** — at
  **139,000 pandas calls per tick**. The cost is dispatch overhead inside a
  library, not arithmetic in our code; there is no hot kernel to rewrite,
  and the heavy detectors are already native (torch/ATen, sklearn Cython).
  If Phase 1 is not enough, the escalation is **polars** (Rust underneath,
  GIL-releasing, consumed as a dependency) — not a PyO3 extension and a
  cross-compile toolchain in a solo-maintained deploy.
- No bus-budget raises, no queue widening, no threshold relaxing.

## Open

- [ ] D01.9 **Pipeline `run_tick`** — the missing half of D01.1. Today the
  scheduler bounds in-flight work to one tick and returns *before* the
  snapshot when a compute is outstanding, so parent and child never overlap
  and `compute_busy` dominates whatever process the scoring runs in. Allow
  tick N+1's snapshot while tick N's compute is in flight, so the ~311 ms
  feature build and the ~350 ms score run concurrently instead of summing to
  ~696 ms. The generation-token machinery that makes a late result safe to
  discard already exists (`_apply` checks it). Watch the sequence engine's
  window ordering: results must still be applied in tick order.

- [ ] D01.4 Get the feature build off the event loop. `source.tick()` runs
  in phase 1 on the loop at **332 ms median / 22 s max**, which is
  `loop_occupancy` almost entirely and the source of the `overrun` misses
  D01.1 does not address. It cannot simply move to a thread — it is
  Python-level pandas (hence the `DataFrame is highly fragmented` warning)
  and holds the GIL. Moving it means moving the ring buffer that `observe()`
  feeds, and the behavior detector that reads
  `source.latest_operating_point`, into the child as well. Design it before
  building it; this is the larger half of the problem.

- [ ] D01.5 Always-draining CAN receive path. The daemon loses **6.6 %** of
  broadcast frames at its socket (kernel counted 110,327 in 60 s, the daemon
  103,051, every kernel drop counter zero; an independent `candump` matched
  the kernel). `_drain_frames_until` only calls `recv()` between other work,
  so frames arriving during request/response handling overflow the socket
  buffer — invisible to `ip -s link`. Still to re-measure now D01.3 has
  landed: some of it may have been GIL starvation of the collector thread.
  Whatever remains needs a dedicated reader thread that owns `recv()` and
  fans out to the tap and the response matcher. Not a bigger buffer — that
  only delays the overflow.
  **Re-measured 2026-09-10 ([evidence](../../../data/analyses/2026-09-10-drive-h1-h7-h9.md)): loss persists at
  ≥ 4.24 %** (kernel 120,599 vs daemon 115,480 over 65 s at warm idle,
  `rx_dropped` flat; non-atomic reads bias the figure low). Reduced from 6.6 %
  but not gone, so the dedicated reader thread is still the fix. **Second
  cause found:** `rx_over_errors` +121 across the drive with `rx_dropped` flat
  is MCP2515 controller overflow, a driver/SPI-level loss distinct from the
  socket overflow this task describes. Measure and fix them separately.

- [x] D01.8 Batch the per-column rolling in the feature builder.
  `src/preprocessing/features.py` builds one rolling object over all columns
  and then loops `windowed[col].mean()` / `.std()` **per column** — ~135
  pandas calls per tick, which is what the profile's 5.5 M `isinstance`,
  49 k `Series.__init__` and 12 k rolling `_validate` calls actually are.
  `windowed.mean()` computes every column at once. Measured on a
  representative 181×67 frame: **21.97 ms → 4.10 ms (5.4×)** with
  bit-identical output (`np.allclose(..., equal_nan=True)` across every
  column). Cheapest large win available, and it is pure pandas — no new
  language or dependency. **Requires operator sign-off and the ground-truth
  suite**: featurization feeds trained IF and sequence models, and
  preprocessing parity is a non-negotiable, so "bit-identical" gets proven
  on the real chain before it ships.
  **Done — shipped `7dfad2c`, deployed 2026-09-10, measured on the truck**
  ([evidence](../../../data/analyses/2026-09-10-h8-continuous-did.md)).
  Parity proven two ways before it shipped (see Phase 1 above): an exact
  old-vs-new comparison over three real resampled sessions across five
  `FeatureConfig` variants, plus a permanent 25-case regression test that
  keeps the per-column implementation as its reference and compares with
  `array_equal(..., equal_nan=True)`. On the Pi the feature stage went
  **296.9 ms → 164.6 ms** and the tick total **659.7 ms → 439 ms** at idle;
  under the heavier `tuning-did` load the tick held a **320 ms** median.
  The released headroom reached the collector, which is the point: sustained
  request rate went from ~7.9 req/s to **116.8 req/s** (H8).

- [x] D01.6 Storage write stalls. `max_write_ms` reached **18,475 ms** with
  the queue at 7,778 / 10,000. Likely the same contention; confirm against
  D01.3 before treating it as its own defect. Do not widen the queue.
  **Resolved 2026-09-10** ([evidence](../../../data/analyses/2026-09-10-drive-h1-h7-h9.md)): it was the same contention.
  Over a 24-minute drive with D01.1 deployed, `max_write_ms` was **952.3 ms**,
  the queue peaked at 2003 / 10,000 and 0 records were dropped. No queue was
  widened and no threshold relaxed.

- [ ] D01.10 Rebuild `reconcile_feature_frame_to_spec` instead of mutating it.
  54.6 ms per call on a one-row 327-column frame, every tick in the serving
  path. The naive batching is *slower* (62.7 ms) — measured, reverted — because
  the cost is block-manager churn, not conversion: `iset` /
  `_iset_split_block` / `blocks.delete` dominate, and a bulk column assignment
  splits blocks harder than the per-column loop. The fix is to construct the
  aligned output frame once from a dict of finished Series rather than
  inserting into and mutating `out`. Moderate risk — this function produces
  the model input — for ~25 % of `compute`, which no longer blocks the event
  loop. Do it after Phase 2, if `compute` is still the binding constraint.

- [x] D01.7 Decide the sequence detector's place on this hardware. If D01.9,
  D01.4 and D01.8 leave the tick over budget, the honest options are a longer
  sequence cadence or dropping the backbone on the Pi — a measured decision,
  taken after those land, not before.
  **Decided 2026-09-11: the sequence detector stays, at 1 Hz, unchanged.**
  Its trigger condition never fired. D01.8 alone brought the tick to a
  **439 ms** median at idle and **320 ms** under the far heavier `tuning-did`
  load, both inside the 1000 ms period and both **with the sequence detector
  running** — so neither a longer cadence nor dropping the backbone is
  justified, and doing either would be paying a detection cost for headroom
  that already exists.

  Two measurements decided it rather than one. The budget question is answered
  above. The *worth* question is answered by D02.6
  ([evidence](../../../data/analyses/2026-09-11-d026-replay-event-rates.md)):
  the sequence model produces **60 % of all anomaly events** (1.95/min of
  3.27/min), concentrated while driving — 45 of its 62 events in `city` and
  `highway` — where the behavior overlay contributes almost nothing. With the
  Isolation Forest structurally blind, it is currently the **only** detector
  covering the driving regimes at all. Dropping it would leave the truck
  unmonitored exactly where it is being driven.

  Revisit only if a future measurement puts the tick back over budget *and*
  the sequence model's share of useful events has fallen. Its cost on the Pi
  is ~250 ms of a ~440 ms tick, so it remains the largest single stage and
  the first place to look if that happens._

## Not a defect — ruled out 2026-09-09

The Pi powered off three times during this session, twice mid-measurement.
Every one was the UPS watchdog behaving **correctly**: the vehicle was
switched off, the X1202 saw `running on battery`, waited its 300 s grace and
called `systemctl poweroff` (e.g. `on battery for 300s, SoC=94.0%`). An
earlier draft of this doc recorded it as a power fault that dropped the Pi
while driving — that was wrong, and is retracted; the operator had ended the
drive and shut the car off each time.

The one real annoyance is unchanged and is not a CANary bug: **the Pi does
not boot itself when power returns**, so an unattended rig stays down until
someone presses start. Worth a look at the bootloader's `WAKE_ON_GPIO` /
`POWER_OFF_ON_HALT` settings or the UPS board, separately from this doc.
There is also a keep-alive (`/run/canary/ups_keepalive`, and the
`set_ups_keepalive` control) that holds off the shutdown — use it when a
measurement has to outlive the ignition.

## Rules

- The bus is not the constraint. No rate default goes up against this doc.
- Never widen a queue or relax a threshold to make a contract pass; name the
  constraint and fix its source.
- Read `/status.vehicle_state` before interpreting any number, and record the
  phase beside it.
