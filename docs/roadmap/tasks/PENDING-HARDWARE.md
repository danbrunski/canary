# Pending hardware — the ten measurements the Pi still owes

Created 2026-09-08 when C04, C05, C06, C08, C09 and C10 were archived. Their
implementation is complete and their evidence is in the archived docs; what
remains is measurement on the vehicle, which no workstation run substitutes.

**Status 2026-09-09: six of ten measured** (H0, H2, H3, H4, H5 done; H6
partial). Evidence: [`data/analyses/2026-09-09-pending-hardware.md`](../../../data/analyses/2026-09-09-pending-hardware.md).
The Pi was reachable, engine warm at idle. H1, H7, H8 and H9 need a 20–30 min
drive; the operator deferred driving on 2026-09-09, so they stay open with the
reason recorded rather than guessed at.

**The session's headline is not a checkbox.** The deployed daemon cannot hold
its 1 Hz inference tick on this hardware: `total` median 863 ms against a
1000 ms period, **48 % of ticks missed**, and event-loop stalls that cost a
concurrent `fuel-map` capture **0 of 468 complete tuples** with ~100 s gaps in
every channel. H3's and H6's numbers are bounded by that, not by the bus.

This is one checklist rather than ten scattered `[~]` boxes because it is one
session's work: plug the Pi in, run these, and the C0x set is closed. Each
item keeps its original ID, its original wording, and a pointer to the
archived task it came from.

## Before any of it

- [x] H0 Read `GET /status.vehicle_state` and the raw freshness fields
  **before every interpretation**, and record the phase (KOEO, running,
  engine off, disconnected, replay) beside every number below. A detector
  that looks broken at `engine_off` is behaving correctly; the same reading at
  `engine_running` is a bug. This is C10.11 and it gates everything else.
  _From [C10](archive/10-sustained-validation.md). No live interpretation has
  been made yet, so no vehicle state has been read. Every synthetic report is
  labelled `hardware.adapter: synthetic` and the replay scenario `replay`._
  **Measured 2026-09-09** ([evidence](../../../data/analyses/2026-09-09-pending-hardware.md)): read first and re-read per item.
  The session opened at `no_data` with `can0` ERROR-PASSIVE, 0 RX/0 TX and
  ENOBUFS on transmit — the OBD harness was unplugged, and the silent
  detectors were correct. After plugging in and starting the engine:
  `engine_running`, 1200 → 737 rpm, coolant 39 → 91 °C. Every number in the
  evidence doc carries its phase._

## Sustained captures

- [ ] H1 (C10.10) Run sustained Pi scenarios covering several flush/keepalive
  and diagnostic periods. Use at least a representative 20–30 minute capture
  for final supported profiles, with enough varied operation for the intended
  analysis. Do not call an eight-second burst sustained drive validation.
  _From [C10](archive/10-sustained-validation.md). Procedure:
  `docs/sustained-validation.md` → "Pi procedure", steps 1–4. Synthetic
  scenarios are 10–60 s by construction and are not offered as sustained
  drive validation._

- [x] H2 (C05.11) Run the same workload on the Pi, identifying hardware,
  power/thermal state, runtime, and model versions. Do not fill the missing
  Pi result with an extrapolation.
  _From [C05](archive/05-pipeline-benchmark.md). Run
  `scripts/benchmark_live_pipeline.py --profile steady --report ...` and
  record the environment next to the workstation baseline. **Compare against
  the real-artifact figures** in `docs/inference-scheduling.md`, not the
  model-less table above them — the Pi's `models_dir` holds real detector
  artifacts, and a stubbed IF understated its stage by ~8× (C08.1)._
  **Measured 2026-09-09** ([evidence](../../../data/analyses/2026-09-09-pending-hardware.md)): Pi 5 Rev 1.1, kernel
  6.18.29+rpt-rpi-2712, Python 3.13.5, git 09ba604, 327 feature columns.
  smoke tick median 122.7 ms / p95 149.4 ms; steady 127.6 / 165.8 ms; both
  `valid: true`, 0 empty ticks. One 2 s under-voltage event during the steady
  run (`get_throttled` 0x0 → 0x50000), 52 → 59 °C, clock back at 2.4 GHz.
  The stored workstation baseline was **not** used as a comparison: it names
  host `CorsairDesktop`, this workstation is `AsusLaptop` and measures 154 ms
  at the baseline's own commit._

## Per-item measurements with a workstation half already done

Each of these has a comparison already measured off-vehicle; the note says
what it showed and what the truck adds. Do not re-derive the local half.

- [x] H3 (C04.12) On the Pi, record vehicle state and compare equivalent
  captures using cadence, gaps, complete tuples, and transition timing. Do not
  claim 4 Hz until it is achieved and persisted. Keep the profile experimental
  until its declared contract is met.
  _From [C04](archive/04-hybrid-diagnostic-scheduling.md). Procedure:
  `docs/hybrid-scheduling.md`. **Already measured:** on the synthetic bus the
  cycle policy holds 0.250 s start-to-start (median and p95, 0 missed of 20,
  entry→DID 12 ms, exit→Mode 01 63 ms) and meets 4.0 Hz on every DID, while
  the interval policy misses by 26 % (2.953 / 4.0) at 0.266 bus utilisation
  with zero timeouts. **What the truck adds:** the synthetic ECU answers in a
  fixed 5 ms `sleep`; the real ECM's response time is the one number that
  decides whether the cycle plan is feasible. `tuning-hybrid-research` stays
  experimental until 4 Hz is achieved *and persisted*._
  **Measured 2026-09-09, claim denied** ([evidence](../../../data/analyses/2026-09-09-pending-hardware.md)): infeasible by plan
  on arming — 11 Mode 01 + 9 DID + 2 session requests at 90 req/s need 0.294 s
  against a 0.250 s cycle (`cycle_feasible=false`); the budget was not raised
  to hide it. Over 300 s at warm idle: `cycle_duration_s` median **0.510 s**
  (≈1.96 Hz), 590 cycles started vs **760 missed**, cross-mode separation
  0.43 s. Persisted rows: RPM 3.307 Hz / coverage 0.599, ECM_RPM 1.953 Hz /
  0.634, verdict `fail`. That is 2.26× and 2.17× the 2026-08-07 drive, and
  still not 4 Hz. **Stays experimental.**_

- [x] H4 (C06.12) On hardware, compare frame continuity and discovery
  counters around startup/recovery. Read vehicle state first; no received-byte
  count alone proves no frames were lost upstream.
  _From [C06](archive/06-diagnostic-discovery.md). Procedure:
  `docs/diagnostic-discovery.md`. **Already measured:** `discovery-recovery`
  (an adapter fault at 7 s) reconciles the sequence ledger with 0 missing on
  both sides and the push identity holds. It also surfaced finding 14 —
  discovery reports `requests_spent: 3` while the ECU counted 6 support probes
  across the reconnect, so its declared bus cost halves after one recovery;
  **left unfixed on purpose** (the fix crosses the registry's intended reset
  semantics) and worth confirming on the truck before choosing one.
  **What the truck adds:** kernel/socket receive counters, `unavailable` on
  the workstation, which are the upstream layer the item is actually about._
  **Measured 2026-09-09** ([evidence](../../../data/analyses/2026-09-09-pending-hardware.md)): discovery completes in budget —
  mode01 3 probes → 41 PIDs, mode06 6 probes → 23 MIDs, 9 requests / 0.49 s,
  responder 0x7E8, and re-walks identically after a reset. **Continuity is
  NOT clean:** over 60 s the kernel counted 110,327 frames and the daemon
  103,051 — **7,276 (6.6 %) unaccounted** with rx_dropped/rx_over/rx_missed
  and parse_errors all zero. An independent `candump` matched the kernel, so
  the loss is inside the daemon's receive path (socket-buffer overflow while
  the loop is not in `recv()`), invisible to `ip -s link`. Pre-existing; the
  metric added this session only revealed it. The counters now reach
  `/status.obd_scheduler.receive` — before that, steps 3 and 7 could not be
  executed at all, and the doc's `can_receive` pointer named a sink the
  canable path never registers._

- [x] H5 (C08.12) Measure Pi CPU, memory, event-loop delay, processing
  latency, and capture losses under the same workload before/after. Include
  1 Hz and a higher supported test cadence without changing deployed model
  defaults.
  _From [C08](archive/08-inference-scheduling.md). Procedure:
  `docs/inference-scheduling.md` → "Left for hardware"; use
  `scripts/sustained_validation.py run --inference-tick-s` (it touches no
  deployed default). **Already measured:** 1 Hz vs 2 Hz on `fuel-map` and
  `tuning-did` with the real IF — zero missed deadlines and zero record loss
  at both, +8 to +10 points of one core rather than 2× (the feature stage's
  per-tick cost is flat at 51 / 72 ms), the visible cost being loop-holding
  (`tuning-did` heartbeat p99 35 → 58 ms). **What the truck adds:** the Pi's
  CPU, memory, thermal and power behaviour, and its event-loop delay._
  **Measured 2026-09-09** ([evidence](../../../data/analyses/2026-09-09-pending-hardware.md)). Isolated harness, deployed
  defaults untouched: at 1 Hz **0 missed ticks** on both profiles (feature p95
  143 / 190 ms, lateness max ≤ 1.3 ms); at 2 Hz **6 and 12 missed** with
  lateness max 273 ms — the Pi's feature stage is 2.6–2.8× the workstation's
  and **2 Hz is where it breaks**. The live daemon is far worse, because it
  also runs the sequence detector: `total` median **863 ms** against a 1000 ms
  tick (p95 2032 ms), **306/633 ticks missed (48 %)**, loop_occupancy median
  341 ms, storage `max_write_ms` 18,475 ms, `/status` latency 0.7–5.1 s. That
  event-loop starvation — not the bus — is what produced ~100 s collection
  gaps and a concurrent `fuel-map` capture with 0/468 complete tuples._

- [~] H6 (C10.12) Compare candidate budgets incrementally under the same
  workload. Stop increasing when a requested contract fails, identify the
  constraint, and fix its source. Do not hide data loss with larger queues or
  relaxed quality thresholds.
  _From [C10](archive/10-sustained-validation.md). Procedure:
  `docs/sustained-validation.md` step 5; run
  `scripts/sustained_validation.py sweep`. **Already measured:** `fuel-map`
  over 8 rungs from 4 to 32 req/s — fails at 4 and 6, holds from 8 up, and
  every holding rung is limited by the inference feature stage, never the bus.
  Two rules are built into the sweep and should not be re-litigated: the
  baseline is the rung with the **most** headroom, and a ceiling is a failure
  **above** a rung that held. **What the truck adds:** where the floor
  actually sits against the real ECM's response time._
  **Partly measured 2026-09-09 — idle only, drive protocol still owed**
  ([evidence](../../../data/analyses/2026-09-09-pending-hardware.md)): at warm idle on `default`, requests actually sent were
  **flat at ~7.9 req/s across an 8 → 16 → 32 req/s ladder** (365 / 352 / 354
  per 45 s) with the ECM answering 97.6 % and 3–5 timeouts. The ECM's response
  time is **not** the floor; the daemon cannot issue more, for the reason H5
  names. No default was raised._

## Vehicle-semantics items with no workstation half

These ask what the *vehicle* does. A scripted ECU cannot answer them at all.

- [ ] H7 (C09.12) For normal monitoring, measure useful critical-channel
  coverage, probe overhead, and batching before changing the default budget.
  Reuse validated passive providers explicitly where appropriate; preserve
  source provenance and the model inputs they actually supply.
  _From [C09](archive/09-polling-plans-and-profile-capacity.md). Blocked on a
  live normal-monitoring capture. No default budget has been changed._
  **Still blocked 2026-09-09** — operator deferred driving. Partial evidence
  ([here](../../../data/analyses/2026-09-09-pending-hardware.md)): probe overhead is 9 requests / 0.49 s, and batching measured
  `mean_pids_per_batch` 2.02 with 65 failures, 8 desyncs, 3 size narrowings.
  No default budget changed._

- [ ] H8 (C09.14) For continuous DID profiles, identify fast dynamics and
  slow context from experiment requirements, lower context demand, and measure
  the resulting fast-channel coverage. Do not prescribe one vehicle's cadence
  to all vehicles.
  _From [C09](archive/09-polling-plans-and-profile-capacity.md). Blocked on an
  experiment-specific live capture to classify dynamics and measure released
  capacity; the schema and runtime cadence control are complete._

- [ ] H9 (C09.16) Correct capacity notes to state active transport, measured
  test duration/conditions, and actual request counts. Retain the old
  storage-loss incident as history, not a universal 24 request/s ceiling. Do
  not raise deployed rate defaults until sustained evidence supports them.
  _From [C09](archive/09-polling-plans-and-profile-capacity.md). Misleading
  DID-batching and 24 req/s claims are already corrected and no default was
  raised. C10 supplied request counts **on the synthetic bus only** (e.g.
  `tuning-did` 148 batched + 1093 single Mode 22 in ~10 s against 1613 single
  before C09), where the budget was never the constraint — so batching bought
  request count, not rate. None of that licenses a capacity note about the
  truck's transport or a raised default; H1 is what does._

## Rules that still apply

- Read `/status.vehicle_state` first, every time (H0).
- Keep captures under `_`-prefixed isolated paths; leave unrelated sessions
  intact.
- Never widen a queue or relax a quality threshold to make a contract pass —
  stop at the first failure, name the constraint, and fix its source.
- Replay validates detector behaviour, never hardware throughput.
- Mark `[x]` only with the measurement in hand, and link the artifact.
