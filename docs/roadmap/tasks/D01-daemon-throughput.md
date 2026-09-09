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

## Open — needs the Pi

- [ ] D01.3 **Measure D01.1 on the truck.** The change is unvalidated on
  hardware: the Pi went offline before it could be deployed. Set
  `models.out_of_process_scoring: true` in `/etc/canary/canary.yaml` (it ships
  off), confirm the daemon log shows `inference worker started (pid …)`, then
  compare against the recorded baseline at the same vehicle state and
  profile — `detectors.inference` (`missed_ticks`, `missed_by_reason`,
  `lateness`, `stages`), `health.system`, and a capture's
  `capture_quality.json` coverage/`max_gap_s`. The specific predictions to
  falsify:
  - `missed_by_reason.compute_busy` (was 135) should fall sharply.
  - The ~100 s collection gaps should disappear; per-channel `coverage`
    should approach 1.0 on a stationary capture.
  - `missed_by_reason.overrun` (was 177) should **not** improve much — see
    D01.4.
  - `/status` latency should return to milliseconds (was 0.7–5.1 s).

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
  buffer — invisible to `ip -s link`. **Re-measure after D01.3 first:** this
  may be partly the same GIL starvation, since the collector thread was not
  being scheduled. Whatever remains needs a dedicated reader thread that owns
  `recv()` and fans out to the tap and the response matcher. Not a bigger
  buffer — that only delays the overflow.

- [ ] D01.6 Storage write stalls. `max_write_ms` reached **18,475 ms** with
  the queue at 7,778 / 10,000. Likely the same contention; confirm against
  D01.3 before treating it as its own defect. Do not widen the queue.

- [ ] D01.7 Decide the sequence detector's place on this hardware. It is the
  largest stage (378 ms median, above the 332 ms feature build and 4× the
  IF's 92 ms). If D01.3 and D01.4 leave the tick over budget, the honest
  options are a longer sequence cadence or dropping the backbone on the Pi —
  a measured decision, taken after the process split, not before.

## Rules

- The bus is not the constraint. No rate default goes up against this doc.
- Never widen a queue or relax a threshold to make a contract pass; name the
  constraint and fix its source.
- Read `/status.vehicle_state` before interpreting any number, and record the
  phase beside it.
