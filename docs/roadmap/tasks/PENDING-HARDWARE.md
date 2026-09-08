# Pending hardware — the ten measurements the Pi still owes

Created 2026-09-08 when C04, C05, C06, C08, C09 and C10 were archived. Their
implementation is complete and their evidence is in the archived docs; what
remains is measurement on the vehicle, which no workstation run substitutes.

**Status: the Pi is unplugged** (operator-confirmed 2026-09-08;
`canary.local` ENOTFOUND, no route to `192.168.137.0/24`, MCP `canary` server
fails to connect). Nothing here has been attempted.

This is one checklist rather than ten scattered `[~]` boxes because it is one
session's work: plug the Pi in, run these, and the C0x set is closed. Each
item keeps its original ID, its original wording, and a pointer to the
archived task it came from.

## Before any of it

- [ ] H0 Read `GET /status.vehicle_state` and the raw freshness fields
  **before every interpretation**, and record the phase (KOEO, running,
  engine off, disconnected, replay) beside every number below. A detector
  that looks broken at `engine_off` is behaving correctly; the same reading at
  `engine_running` is a bug. This is C10.11 and it gates everything else.
  _From [C10](archive/10-sustained-validation.md). No live interpretation has
  been made yet, so no vehicle state has been read. Every synthetic report is
  labelled `hardware.adapter: synthetic` and the replay scenario `replay`._

## Sustained captures

- [ ] H1 (C10.10) Run sustained Pi scenarios covering several flush/keepalive
  and diagnostic periods. Use at least a representative 20–30 minute capture
  for final supported profiles, with enough varied operation for the intended
  analysis. Do not call an eight-second burst sustained drive validation.
  _From [C10](archive/10-sustained-validation.md). Procedure:
  `docs/sustained-validation.md` → "Pi procedure", steps 1–4. Synthetic
  scenarios are 10–60 s by construction and are not offered as sustained
  drive validation._

- [ ] H2 (C05.11) Run the same workload on the Pi, identifying hardware,
  power/thermal state, runtime, and model versions. Do not fill the missing
  Pi result with an extrapolation.
  _From [C05](archive/05-pipeline-benchmark.md). Run
  `scripts/benchmark_live_pipeline.py --profile steady --report ...` and
  record the environment next to the workstation baseline. **Compare against
  the real-artifact figures** in `docs/inference-scheduling.md`, not the
  model-less table above them — the Pi's `models_dir` holds real detector
  artifacts, and a stubbed IF understated its stage by ~8× (C08.1)._

## Per-item measurements with a workstation half already done

Each of these has a comparison already measured off-vehicle; the note says
what it showed and what the truck adds. Do not re-derive the local half.

- [ ] H3 (C04.12) On the Pi, record vehicle state and compare equivalent
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

- [ ] H4 (C06.12) On hardware, compare frame continuity and discovery
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

- [ ] H5 (C08.12) Measure Pi CPU, memory, event-loop delay, processing
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

- [ ] H6 (C10.12) Compare candidate budgets incrementally under the same
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

## Vehicle-semantics items with no workstation half

These ask what the *vehicle* does. A scripted ECU cannot answer them at all.

- [ ] H7 (C09.12) For normal monitoring, measure useful critical-channel
  coverage, probe overhead, and batching before changing the default budget.
  Reuse validated passive providers explicitly where appropriate; preserve
  source provenance and the model inputs they actually supply.
  _From [C09](archive/09-polling-plans-and-profile-capacity.md). Blocked on a
  live normal-monitoring capture. No default budget has been changed._

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
