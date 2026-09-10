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

## Open

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

- [ ] D01.8 Batch the per-column rolling in the feature builder.
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

- [ ] D01.6 Storage write stalls. `max_write_ms` reached **18,475 ms** with
  the queue at 7,778 / 10,000. Likely the same contention; confirm against
  D01.3 before treating it as its own defect. Do not widen the queue.

- [ ] D01.7 Decide the sequence detector's place on this hardware. If D01.9,
  D01.4 and D01.8 leave the tick over budget, the honest options are a longer
  sequence cadence or dropping the backbone on the Pi — a measured decision,
  taken after those land, not before.

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
