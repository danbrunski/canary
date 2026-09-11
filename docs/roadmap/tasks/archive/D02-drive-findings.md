# D02 — what the 2026-09-10 drive found

Opened 2026-09-10 from the first sustained drive on the truck. Measurements
and raw evidence:
[`data/analyses/2026-09-10-drive-h1-h7-h9.md`](../../../../data/analyses/2026-09-10-drive-h1-h7-h9.md)
(24 min 58 s at `engine_running`, `default` profile, counters read live).

D01 is about the Pi's capacity to run the daemon and its tick is now inside
budget. **This set is about what the daemon does with that capacity** —
counters that don't reconcile, capacity left switched off, loss the socket
fix won't reach, and a detector that scored nothing for a whole drive. Each
item below was checked against the code before it was written down; where an
obvious-looking fix was already considered and rejected, the rejection is
recorded so it isn't re-derived.

**Not in scope, deliberately.** Retiring never-answering PIDs from the plan:
`_miss_backoff_seconds` documents why they are backed off exponentially but
never dropped (a PID gated behind a warm engine still deserves a periodic
probe). Leave that alone. D01.9 / D01.4 / D01.5 / D01.8 / D01.10 stay in D01.

Every number here was measured **without** D01.8 deployed and with the
Isolation Forest blind (D02.5); neither changes any item's premise.

## Rules

- Read `/status.vehicle_state` before interpreting anything (H0).
- Counters must reconcile: `sent == responses + timeouts + <named bucket>`.
  A silent remainder is a defect, whatever caused it.
- No bus-budget raise, no queue widening, no threshold relaxing.
- A measured width or constraint goes in `data/dids.yaml` with a
  `*_source`, never in a comment.
- Mark `[x]` only with evidence; `[~]` names what's left.

## Data integrity

- [x] D02.1 **Reconcile per-member counters on a failed batch.**
  `_issue_batched_request` increments every member's `requests_sent` before
  the request goes out, then on a whole-batch failure (`result.readings is
  None`) returns early without recording a miss for any member — no
  `timeouts`, no `_update_response_state`, no backoff. The remainder is a
  silent bucket. Measured over the drive (101 batch failures):

  | channel | sent | responses | timeouts | unaccounted |
  |---|---:|---:|---:|---:|
  | `CATALYST_TEMP_B1S2` | 60 | 0 | 6 | 54 |
  | `CATALYST_TEMP_B2S2` | 60 | 0 | 6 | 54 |
  | `AMBIANT_AIR_TEMP` | 60 | 0 | 6 | 54 |

  Peers that time out singly sit at 6 / 0 / 6 and are correctly backed off.
  Two consequences: a channel that always rides in a failing batch keeps its
  slot forever, and per-request `response_rate` under-reports.

  Whether a *batch-level* failure should back off every member is a real
  design question — the ECU may be refusing the batch, not the PID — so do
  not simply call `_update_response_state` in the early return and move on.
  Decide, then make the counters agree with the decision: either count the
  member miss (and back off), or add an explicit `batch_failed` per-member
  counter so `sent == responses + timeouts + batch_failed` holds and the
  status payload can show it. Add the reconciliation as an invariant the
  status builder asserts, and a test that fails a batch and checks the sum.
  Gate: targeted collector + scheduler tests, then the whole-repo gate.
  **Done 2026-09-10.** Decision taken: the no-penalty rule stays — a
  batch-level failure is no evidence about any member, and the docstring
  already argued that. So the *counters* were made to agree with it instead.
  `_PollRequestState.batch_failures` records the swallowed send,
  `_request_accounting_error` publishes `requests_sent - (responses +
  timeouts + batch_failures)` per request in `/status`, and `response_rate`
  is now taken over sends there is evidence about rather than all sends, so a
  healthy PID riding failing batches no longer reads as dead. Five tests in
  `tests/test_d02_accounting.py`, including an end-to-end run whose published
  payload must reconcile for every request.

- [ ] D02.2 **Measure the MCP2515 controller overflow separately from the
  socket loss.** `can0 rx_over_errors` rose by **121** across the drive and
  by 14 in a 65 s idle window while `rx_dropped` stayed flat. The MCP2515
  has two hardware RX buffers; an overflow there is the SPI driver not
  draining the chip in time — a loss *underneath* the socket-level ~4.2 %
  that D01.5 addresses, and one a reader thread cannot touch. Do not fold
  the two into one number.

  Steps, on the Pi: record the overlay in force (`/boot/firmware/config.txt`
  `dtoverlay=mcp2515-*`, its `oscillator`/`interrupt`/`spimaxfrequency`
  params) and the bus rate; read `rx_over_errors` over a fixed window at
  warm idle and again driving; then try, one at a time and each measured
  against the same window, the SPI clock and the driver's IRQ handling. Also
  fix the overlay declaring a second controller that fails to probe every
  boot (`mcp251x spi0.0: Probe failed, err=110`). Record every rung as
  `data/analyses/…`; the deliverable is an overflow rate per rung and which
  knob moved it, not a raised buffer. If nothing moves it, say so — the
  answer may be a controller with a real FIFO, and that is a hardware
  decision for the operator, not a code change.
  _Blocked-overnight: needs the Pi and the truck._
  **Config audit done 2026-09-11 on the bench; the measurement still needs the
  truck** (a bench Pi has no bus traffic, so there is nothing to overflow —
  `can0` sits ERROR-PASSIVE at 0 RX/0 TX with no vehicle attached, which is
  expected and is the same reading H0 caught on 2026-09-09).

  In force on this Pi:

  ```
  dtparam=spi=on
  dtoverlay=mcp2515-can1,oscillator=16000000,interrupt=25
  dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=23
  ```

  Two candidates, both to be tried one at a time and each measured against the
  same window:

  1. **No `spimaxfrequency` is set**, so the overlay's 10 MHz default applies.
     That is the bus speed the driver drains the MCP2515's two RX buffers
     over, and it is the most direct lever on a controller overflow. 20 MHz is
     the conventional next rung.
  2. **A second controller is declared that does not exist.** `mcp2515-can1`
     probes `spi0.0` and fails every boot (`MCP251x didn't enter in conf mode
     after reset`, `err=110`); only `spi0.1` initialises. Removing that line
     costs nothing and stops a real failure being normal background noise.

  Checked and **not** a defect: the overlay declares a 16 MHz oscillator while
  `ip -d link` reports `clock 8000000`. That is correct — the MCP2515's CAN
  system clock is the oscillator halved — and it should not be "fixed".

  Neither change is applied: both need a reboot, and the ranking between them
  is only decidable with bus traffic._

- [x] D02.3 **Surface and bound the child-scorer fallback.** The parent
  waits `RESULT_TIMEOUT_SECONDS = 30.0` for the child, then scores
  in-process — and that in-process fallback has no bound of its own. At
  engine-off on 2026-09-10 the tick recorded `compute_wait` max
  **63,454 ms** with 63 `overrun` misses, while `compute_failures` stayed
  at **0**: the worker's own `_failures` counter is not the one `/status`
  shows, so a minute-long stall is invisible except in a max. Three parts:
  (a) publish worker fallbacks (timeouts, broken pipe, restarts) in the
  inference stats and `/status`; (b) bound the in-process fallback the same
  way the child is bounded — a tick that cannot score within its period is
  discarded and counted, not waited on; (c) revisit 30 s: on a 1 s tick it
  is thirty consecutive `compute_busy` misses before the fallback even
  starts. Whether the 63 s was the child, the fallback, or the box itself
  stalling on UPS power at 2 % SOC is unknown — (a) is what would have told
  us. Gate: unit tests for each path, the `run_live_inference` end-to-end
  tests still completing ticks, then the whole-repo gate.
  **(a) and (c) done 2026-09-10; (b) refused with a reason.**
  (a) `InferenceWorker` counts fallbacks by reason (`timeout`, `pipe_error`,
  `worker_down`, `bad_reply`, `worker_error`); they reach
  `/status.detectors.inference.worker_fallbacks`. A new `slow_computes`
  counts ticks whose compute outran the period — the count behind
  `compute_busy`, which was previously visible only as a `compute_wait` max.
  (c) the flat 30 s is gone: the steady-state wait is `3 x` the tick period
  floored at 2 s, with the 30 s allowance kept **only** until the child's
  first answer, because it loads artifacts on that tick.
  (b) **not implementable as written.** Bounding the in-process fallback
  means preempting a running Python thread inside sklearn/torch, which
  CPython cannot do safely. The scheduler is already bounded — it refuses to
  start a tick while one is in flight and counts `compute_busy` — so what was
  actually missing was naming the overrun, which `slow_computes` now does.
  Re-open only with a design that does not require thread cancellation.
  **Closed 2026-09-11 rather than left `[~]` forever.** Two of the three parts
  shipped and the third is a refusal with a reason, not outstanding work —
  carrying it as partial indefinitely would hide that a decision was taken.
  D01.9 also removed most of what (b) was reaching for: the scheduler no
  longer stalls behind a slow compute, it pipelines past it, so an unbounded
  in-process fallback costs a queued tick rather than a missed deadline.

## Capacity left switched off

- [x] D02.4 **Record the missing Mode 22 response widths.** Multi-DID
  batching is enabled but did nothing all drive — `batching.mode22.max_dids
  = 1`, **0 batched requests** — because a batch cannot be parsed without
  every member's payload width and only three DIDs (`0x1101`, `0x1208`,
  `0x1219`) carry `response_length` in `data/dids.yaml`. The four polled at
  0.02 Hz (`0x1225`, `0x1226`, `0x120F`, `0x1210`) do not. Their widths are
  in stored session payloads already: `source == 'obd'`/raw responses from
  any session that polled them single-DID. Measure each from at least two
  sessions, write `response_length` + `response_length_source` per entry,
  and confirm `resolved_plan.raw_frames_per_second.estimate_complete`
  flips to true and `batch_max.mode22` rises. Do not guess a width from the
  decode's `byte_length` — the response may carry more bytes than the
  binding reads. This is the cheapest path to the released capacity **H8**
  asks about, and it is a prerequisite for H8's drive being informative.
  Gate: `tests/test_analysis_ground_truth.py` unchanged, plan tests, then
  the whole-repo gate.
  **Done 2026-09-10.** All four measure **2 bytes**, unambiguously: 497 to
  42,600 stored payloads each across 32-55 local sessions, no other width
  seen once. The `byte_length`-vs-response-width ambiguity the task warned
  about is resolved rather than assumed — `_decode_response` stores
  `response.payload` unmodified for a DID, so the stored bytes *are* the full
  post-header response. **Known-answer check first:** the same method
  reproduced the three already-pinned widths exactly (0x1101 → 1, 0x1208 → 2,
  0x1219 → 2) before any novel width was trusted. Written to `data/dids.yaml`
  with `response_length_source` naming sessions, counts and method;
  `estimate_complete` now holds for every DID the `default` profile polls.
  **Batching is still off, deliberately:** `obd_did_batch_max_dids` defaults
  to 1 per install and the widths were its precondition, not its switch.
  Turning it on is an operator decision that belongs with H8's drive.
  **Independently re-confirmed 2026-09-10 on the H8 drive**
  ([evidence](../../../../data/analyses/2026-09-10-h8-continuous-did.md)): a
  different session on a different daemon revision reproduced **every**
  recorded width exactly, 15,7xx payloads each, no other width seen once.
  Five more were measured and recorded the same way (0x1110, 0x1135, 0x1139,
  0x113A, 0x1163, all 1 byte), so **every DID in both the `default` and
  `tuning-did` profiles now has a provenance-backed width** and multi-DID
  batching is unblocked for both. Still switched off — enabling it during the
  drive that measured baseline DID capacity would have confounded it.

- [x] D02.4a **Deduplicate `unknown_response_width_channels`.**
  `polling_plan.py` builds it as a list and the drive's payload repeated
  `MONITOR_RESULTS` more than twenty times (one per Mode 06 MID). Any
  capacity estimate or coverage fraction built from its length is inflated.
  Make it a sorted, unique list; one test. Fold into D02.4's commit if
  convenient.
  **Done 2026-09-10** — sorted `set` in `polling_plan.to_dict`, one test.

## The detector that scored nothing

- [~] D02.5 **Trace why the Isolation Forest was blind for the whole drive.**
  `missing_feature_rate` read **1.0** in every snapshot and every operating
  mode (`warmup`, `idle`, `idle_transient`, `city`, `highway`) against
  `isolation_forest_2026-07-27T045036.161167Z`. Not one expected feature
  was present at serve, so the drive produced no IF detection evidence and
  the 73 ms `isolation_forest` stage in D01's table is the cost of scoring
  an all-NaN vector. This is **not** the version-mismatch case
  (`/status` carries no `model_version_mismatches` on this build; manifests
  load with `rejected: 0`). Put the artifact's declared feature list beside
  the columns the live builder actually produces for session
  `2026-09-10T142711` and name the drift — renamed columns, a spec
  regeneration, a scope/profile mismatch, or the builder never seeing the
  ring buffer. Then decide: retrain against the current spec, or fix the
  builder — and add the check that would have refused to serve at
  `missing_feature_rate == 1.0` for more than N ticks instead of scoring
  quietly. That refusal is the "make it impossible" half; the trace is the
  fix. _Needs the session parquet — blocked until the Pi is reachable._

- [x] D02.6 **Measure the anomaly-event rate before touching a threshold.**
  With the IF contributing nothing, the behaviour and sequence detectors
  emitted **76 events in 24 minutes (3.1 / min)** with 1598 more suppressed
  as duplicates and 107 by persistence. That is high for an ordinary drive,
  but "high" is not a finding. Replay `2026-09-10T142711` and a known-good
  session from the training set through the live pipeline
  (`start_replay`, isolated `_replay-*` outputs) and compare per-detector
  event rates by operating mode. If the sequence detector dominates, this
  becomes D01.7's input; if the behaviour detector does, it is a
  bank-balance / residual question. Either way the output is a rate table,
  not a threshold change. _Replay can run off-vehicle once the session is
  pulled._
  **Measured 2026-09-11 on the bench** ([evidence](../../../../data/analyses/2026-09-11-d026-replay-event-rates.md)).
  104 events over 1,910 s = **3.27/min**, against **3.1/min** live — the
  replay reproduces the live rate, so the instrument is sound and the live
  figure was not a driving artifact. **The sequence detector dominates:**
  `lstm_autoencoder` 62 (1.95/min, 60 %), `behavior` 30, `fused` 8,
  `misfire` 4, `isolation_forest` **0**. Per the task text that makes this
  D01.7's input. The two main detectors fire in **complementary regimes** —
  the sequence model 45 of 62 while driving, the behavior overlay 25 of 30
  while stationary — which is not one detector flooding.
  **No threshold was changed**, and deliberately: whether 3.27/min is *too
  high* needs a known-good comparison, and the Pi's retention has swept every
  session the models were trained on. Per-mode rates normalised by time were
  also not obtainable (the replay output does not persist `operating_mode` as
  a channel), and raw per-mode counts without the denominator are the
  aggregate comparison this repo refuses. Characterised and attributed, not
  yet judged._

## Found while closing D02.6

- [ ] D02.9 **Replay grows without bound and OOM-killed the daemon.**
  A 1x replay of a 25-minute drive took the daemon to **7.7 GB resident on an
  8 GB Pi** and the kernel killed it 32 minutes in
  ([evidence](../../../../data/analyses/2026-09-11-d026-replay-event-rates.md))
  (`Out of memory: Killed process 1217 (python) total-vm:15756560kB
  anon-rss:7744720kB`). systemd restarted it cleanly and it has been healthy
  since at 469 MB, so only the tail of the replay was lost.

  The bound worth starting from: the replay wrote **5,860,000 rows in 586
  chunks** while covering **32 %** of a source session that contains
  **4,544,560 rows in 511 chunks** total. It produced more rows than its input
  holds, over a third of the span. Something is amplifying rows and memory
  grows with it; **do not guess the mechanism** — find it, then decide whether
  the fix is at the emit path or the writer.

  This matters beyond tidiness: `start_replay` is the sanctioned way to
  evaluate detectors without the truck (see AGENTS.md), and on this hardware a
  long replay takes the daemon down. Until it is fixed, a replay longer than
  ~30 minutes should be treated as unsafe on the Pi.

  **Pre-existing, not a D01.9 regression** — the Pi ran `3c2ae25` throughout;
  D01.9 was committed on the laptop and never deployed.

- [ ] D02.10 **Get a known-good baseline session back onto the Pi.**
  D02.6 could characterise the event rate but not judge it, because retention
  has swept every session the deployed models were trained on. Push one
  (e.g. `2026-07-26T234833`) from `~/canary-data` and pin it so retention
  cannot take it, then re-run D02.6's replay against it and compare
  per-detector rates. Without this, "3.27 events/min" has no reference.

## Housekeeping the drive exposed

- [x] D02.7 **Bring the Pi to a committed revision.** Its tree is `09ba604`
  plus hand-copied D01.1 files (`inference_worker.py` untracked;
  `inference.py`, `obd_scheduler.py`, `config.py`, one doc modified) and
  two untracked benchmark JSONs. Every measurement since 2026-09-09 has
  been taken against a state git cannot name. `git pull` to a revision that
  contains D01.1 and D01.8, restart, confirm `inference worker started` in
  the journal, and record the sha in the next evidence doc. Copy the two
  benchmark JSONs back to the laptop first.
  **Done 2026-09-10.** All five hand-copied source files were verified
  byte-identical to commit `9d3f55a` before anything was touched, so nothing
  was lost; the two benchmark JSONs were copied to the laptop first. Reverted
  with explicit pathspecs only, never tree-wide. The Pi had no DNS, so the
  revision was delivered by `git bundle` rather than `git pull`. It now sits
  at **`4c84ac3` with a clean tree**, and the H8 drive is the first
  measurement on this truck taken against a revision git can name._

- [x] D02.8 **Finish H1's continuity pass.** The drive closed H1 on the
  operator's stated bar, but the per-channel gap analysis over the session
  parquet was never run because the link dropped. Pull
  `2026-09-10T142711`, run the gap/complete-tuple analysis the
  sustained-validation procedure names, and append the result to the
  evidence doc. Expect it to confirm the counters; if it does not, that is
  the finding.
  **Done 2026-09-11 on the bench**
  ([evidence](../../../../data/analyses/2026-09-10-drive-h1-h7-h9.md), addendum).
  It confirmed the counters. The fast tier held 0.95–0.97 Hz with a 1.00 s
  median gap, a 3.67 s worst gap and **no gap over 5 s on any channel**;
  **96.3 %** of grid seconds carried the complete six-channel model input and
  **every incomplete second was isolated** — longest run 1 s, no holes.
  Broadcast CAN ran 2,439,597 frames at 1,628/s with zero gaps over 1 s. The
  slow-tier and DID gaps are the profile's own cadences, not loss. The
  contrast that matters: the 2026-09-09 session this procedure was written
  for had ~100 s gaps in every channel._

## Status 2026-09-10

Implemented on the laptop, gate green (`ruff`, `ruff format`, `mypy`,
3549 tests passing). **Not deployed — the Pi is off; push and deploy later.**

| item | state |
|---|---|
| D02.1 counters reconcile | done |
| D02.4 / D02.4a DID widths | done |
| D02.3 worker fallbacks | partial — (b) refused, see the item |
| D02.5 IF blindness | root cause found + alarm added; retrain owed |
| D02.7 Pi on a committed revision | done 2026-09-10 — Pi is at `4c84ac3`, clean |
| D02.8 H1 continuity | done 2026-09-11 — no holes, 96.3 % complete tuples |
| D02.6 anomaly rate | done 2026-09-11 — sequence detector dominates |
| D02.2 | still needs the truck |

The headline is D02.5. The Isolation Forest went blind because two broadcast
bindings were **corrected** out of `data/bindings.yaml` on 2026-08-04/05,
three months after the deployed artifact was trained on them — one of those
corrections being the `ECM_CALC_LOAD_B` retraction that AGENTS.md's "never
compare aggregates" rule exists to prevent. Fixing a bad binding silently
blinded the detector that depended on it, and nothing connected the two.

## Order

D02.1 and D02.4 (+4a) first — both are small, both are verifiable from data
already on the laptop, and neither needs the truck. Then D02.7 so the next
measurement has a sha. D02.5 and D02.8 the moment the Pi is back. D02.2 is
its own Pi session. D02.3 and D02.6 are not urgent and should not pre-empt
D01.9.
