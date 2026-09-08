# Sustained collection validation — bus receipt through analysis (C10)

Roadmap task C10 (`docs/roadmap/tasks/archive/10-sustained-validation.md`). This is
the handoff record for the synthetic-bus harness under `src/validation/`,
the before/after evidence it produced for the C09 polling-plan work, the
defects it surfaced on the way, and the exact Pi procedure that is still
pending. Measured results: `data/analyses/2026-09-07-sustained-validation.md`
(per-scenario JSON under `data/analyses/sustained-validation/{before,after}/`).

**Nothing here was measured on the truck.** Every number is a workstation
number against a scripted ECU; the hardware items (C10.10–C10.12) are written
down below and were not attempted.

## What the harness runs (C10.1 / C10.2)

`src/validation/harness.py::run_scenario` builds the daemon the way
`run_daemon` does and drives it on the **real clock**:

| stage | what runs | real or declared |
|---|---|---|
| adapter / ECU | `SyntheticEcu` (`src/validation/synthetic_bus.py`), the C02/C04/C06 `ScriptedEcu` on a `RealClock` whose `advance` sleeps | **synthetic** — every report says `hardware.adapter: synthetic` |
| collector | `collect_can_obd_from_config` → `run_can_obd_loop` in its thread, the shipped `client_factory` path, `ThreadHandoff`, reconnect policy | real |
| passive sniffer | `collect_can_from_config` → `CanReader` thread | real |
| fanout and sinks | `drain_queue_with_broadcast`: storage (with `FrameDecoder` decode), dashboard broadcaster (a subscriber counts and drops), live feature source, DTC and monitor sinks | real |
| writer | `ParquetTelemetryWriter` behind `AsyncParquetTelemetryWriter`, `enter_capture_mode` manifests, atomic fsync'd chunks | real (`InstrumentedParquetWriter` only times `flush` and can inject a stall) |
| capture lifecycle | `record_capture_start` epoch, `bus_budget.set_override` + `tuning_profile.activate` in the control route's order, `finalize_capture` (boundary → drain → flush → judge) | real |
| inference | `run_live_inference` on its deadline grid, `LiveFeatureSource` with the synthetic bindings, behavior detector, derived-channel persistence | real; **IF is a declared stub** (`models.isolation_forest.status: stub`) because only manifests are checked in under `models/`; the sequence detector is declared `absent` |
| kernel counters | `can0.*` | `unavailable` on this host; the synthetic ledger's `adapter.rx_dropped` stands in, and the report carries both verdicts |
| power / thermal | — | `unavailable (workstation)` |
| GPS, ELM327 | — | not exercised; the ELM327 companion needs the ELM327-emulator serial integration tier |

**The compromise, stated.** A real-time run was chosen over a fake-clock
collector feeding a real fanout because the handoff, the fanout batching,
the storage sink's batch/flush cadence and the finalize drain only mean
anything with the real thread-to-loop boundary in place. The price is that
every timing figure carries workstation scheduler jitter, the ECU's latency is
a `sleep`, and — measured while building this — **this WSL2 host's wall clock
steps by ~0.9 s every couple of minutes** (NTP correction). A step inside a
run re-orders every wall-stamped row across it, which showed up as a ±135 rpm
"disagreement" between two copies of the same signal 3 ms apart. The harness
samples `time.time() - time.monotonic()` at 100 Hz, records steps
(`timing.wall_clock_steps`), marks such a run `valid: false`, and
`run_scenario_clean` reruns it (bounded). No report in `after/` or
`before/` is from a stepped run unless its `valid` flag says so.

**Running the scenario tests.** The `slow` tests in
`tests/test_sustained_validation.py` drive whole scenarios on the real clock,
so they need a host that is not otherwise busy. Observed 2026-09-08: a
`pytest -n auto` run competing with a second scenario run took 476 s instead
of 198 s and failed `test_overloaded_storage_yields_persistence_loss_never_pass`
and an unrelated real-clock batching test; both passed serially and the
uncontended full run was green. Treat a failure in these as a regression only
after re-running it on an idle host (`-n0`) — the same convention the repo
already applies to `TuningFeedback.test.ts`.

### The synthetic bus

- Numbered broadcast frames on `0x6A0` (an id bound nowhere in
  `data/bindings.yaml` / `data/dbc`): payload `seq(4) rpm_raw(2) speed(1) 0`,
  at a steady rate plus periodic bursts. The ledger of *delivered* sequence
  numbers is shared across bus instances, so a reconnect keeps numbering;
  frames queued in a faulted adapter are counted as `adapter.rx_dropped`,
  frames generated ahead but never due before stop as `pending_at_stop`
  (not a loss).
- One `SignalModel` serves the same rpm as Mode 01 `0x0C` (quarter-rpm
  exact), as bytes 4–5 of the broadcast (`SYNTH_RPM`, scale 0.25) and as DID
  `0x1219` (`ECM_RPM`, scale 1). `SYNTH_RPM_HALF` binds the same bytes at a
  deliberately wrong scale so the known-answer check has a known-different
  pair (C10.7).
- Mode 01 payload widths are asserted against the registry at construction;
  Mode 22 answers use the catalogue's `response_length` (batched requests
  are answered per DID, silent DIDs omitted, session gating inherited).

## Report schema (C10.3)

One JSON per scenario run, `schema_version: 1`, `report: sustained_validation`,
`label`, `scenario`, `git_rev`, `provenance` (git sha/describe/worktree,
Python and platform, `importlib.metadata` versions of pyarrow/pandas/numpy/
python-can/scikit-learn/torch/pydantic, hostname and cpu count, adapter,
vehicle profile slug and make/model/year, profile snapshot, fields the tree
did not accept, collector spec, model status, enabled/declared components,
power/thermal, workload, warmup, declared and measured durations, timestamp
basis per row kind, session dirs), `evidence` (below) and a `headline`.
`summarize_markdown` diffs two directories field by field.

## Evidence blocks (C10.4–C10.7)

- `sequence_reconciliation` — every adapter-delivered sequence number on
  disk exactly once, else listed as missing; `reconciled_with_counted_loss`
  only when `missing <= counted drops`, `UNEXPLAINED_LOSS` otherwise.
- `losses` — the push identity
  `collector_pushed == persisted_raw_rows + buffer_dropped + queue_dropped + storage_dropped`
  and, without a reconnect, `collector_pushed - readings_emitted ==
  adapter_delivered_frames` (the C06 tap invariant); queue high-water marks
  per sink; kernel counters `unavailable`; dashboard subscriber drops kept
  apart (freshest wins by design).
- `decoded_expansion` — `decoded:*` rows reconciled as `persisted frames ×
  persisted signals`, never mixed into raw counts.
- `transport` — requests on the wire by service (from the ECU's own log),
  frames by responder, ECU-side batch/single/session counts, the scheduler's
  final snapshot (rounds, session control, discovery, batching, resolved
  plan) and the last snapshot *before* the stop boundary (`did_profile`,
  because the boundary resets cycle accounting), per-channel interarrival
  median/p95/p99/max with achieved vs requested cadence, generation events.
- `quality` — the stop-time C03 sidecar as `finalize_capture` wrote it, and
  `with_adapter_ledger`: the same recompute with `can0.*` replaced by the
  synthetic adapter's `rx_dropped`. On this host the C03 verdict can never be
  `pass` (`unknown_loss_counters`); the adapter-ledger verdict is what a
  Pi run with kernel counters would judge.
- `known_answer` — `compare_channels` under an explicit `PairingPolicy`:
  `can:SYNTH_RPM ~ obd:RPM` and `did:ECM_RPM ~ can:SYNTH_RPM` must agree
  1:1 (slope within 3 %, `difference_bias` within tolerance);
  `can:SYNTH_RPM_HALF ~ obd:RPM` must *not* (expected slope 2); the replay
  uses the C01 ground-truth pairs. Refusals (`too few pairs`, `no
  observations`) are reported as refusals, not as passes.
- `timing` — event-loop heartbeat overshoot (10 ms sleep, bounded
  reservoir), the `InferenceTimingRecorder` snapshot (lateness, missed
  ticks by reason, per-stage p95), writer flush latency, storage-sink
  write stats, `resource.getrusage` CPU/RSS, storage growth, wall-clock
  steps. No per-frame logging anywhere.
- `limiting_stage` — the first stage that lost data, else the first that
  missed a deadline, else the rate contract's cause (budget pacing vs ECU
  time), else the loop occupancy the feature stage imposes, else
  `none_reached` with the budget headroom.

## Scenarios (`src/validation/scenarios.py`)

| scenario | task table row | required evidence produced |
|---|---|---|
| `normal-monitoring` | Normal monitoring with discovery | discovery probe count/time, per-tier fairness, startup continuity |
| `fuel-map` | Fuel-map | complete tuples from rows, skew, mixed-rate context, Mode 01 batch occupancy |
| `tuning-did` | Continuous tuning DID | per-DID cadence incl. `mode22_rate_weights`, session keep-alives/lapses, raw continuity |
| `fuel-did-research` | Fuel DID research | single vs batched Mode 22 demand, known widths, persisted tuple yield |
| `hybrid-mode01-mode22` | Hybrid Mode 01/22 | both services at rate, cycle plan/feasibility, transition latency, cross-mode pairing bound |
| `passive-sniffer` | Passive / legacy ELM327 companion | receive-loop responsiveness, completeness, `sent == []`; ELM327 declared not exercised |
| `discovery-recovery` | Normal monitoring with discovery | raw-frame continuity and discovery counters across an adapter fault + reconnect (C06.12) |
| `fault-channels` | Fault cases | silent PID → `unanswered_input`, refused PID omitted from batches, 60 ms ECU |
| `transitions` | Transition cases | pause window, profile change (ledger snapshot kept), adapter fault → reconnect, final flush |
| `overload-storage-stall` | Overload | counted loss at the storage sink, `persistence_loss`, identities intact |
| `overload-rate` | Overload | starvation named (`rate_shortfall` or `unanswered_input`), priority-lane starvation |
| `replay-drive-slice` | Replay (C10.9) | feature/detector behaviour on the committed slice; C01 pairs re-read from the replayed Parquet |

Every armed scenario warms up for `ARMED_WARMUP_S` (5 s) before the
capture is armed — see finding 2 below for why.

## Before / after procedure (C10.8)

"Before" is `2257b53` (pre-C09), "after" is `HEAD`. The same harness files
run in both trees; fields a tree does not know are dropped and listed in
`provenance.*_unsupported_by_this_tree` (before: `mode22_rate_weights`,
`did_batch_max_dids`; and no `resolved_polling_plan` in the manifest).

```
SCRATCH=<session scratch>
git worktree add --detach $SCRATCH/wt-before 2257b53
ln -s $PWD/.venv $SCRATCH/wt-before/.venv
cp src/validation/*.py $SCRATCH/wt-before/src/validation/   # after mkdir -p
cp scripts/sustained_validation.py $SCRATCH/wt-before/scripts/
( cd $SCRATCH/wt-before && .venv/bin/python scripts/sustained_validation.py run \
    --label before --out $PWD/data/analyses/sustained-validation/before \
    --sessions-root $PWD/data/_validation --scratch $SCRATCH/before-scratch )
.venv/bin/python scripts/sustained_validation.py run --label after \
    --out data/analyses/sustained-validation/after --sessions-root data/_validation
.venv/bin/python scripts/sustained_validation.py summarize \
    --before data/analyses/sustained-validation/before \
    --after data/analyses/sustained-validation/after
git worktree remove --force $SCRATCH/wt-before
```

Raw sessions land under `data/_validation/<label>/<scenario>/` (a
`_`-prefixed root; Parquet is gitignored). Nothing was committed there.

## Incremental budget comparison (C10.12)

`scripts/sustained_validation.py sweep` walks candidate budgets under **one
fixed workload** — only `bus_budget_override` changes between rungs; members,
target rate, duration, ECU, queue sizes and quality thresholds are carried
through unchanged (`spec_at_budget`, asserted by test). Nothing is ever
retried with a looser setting.

Two rules make the answer trustworthy, both learned by getting them wrong
first:

- **The baseline is the rung with the most headroom, not the least.** The
  first version anchored on the lowest budget and stopped at its opening step,
  because `acquisition_gap` fires at every budget (finding 3). Anchoring low
  would also fold genuine starvation into the baseline the moment the ladder
  started below demand, after which no ceiling could ever be reported. A
  failure seen at maximum headroom is not about pacing; that is the only rung
  where that statement is sound.
- **A ceiling is a failure *above* a rung that held.** A failure below every
  holding rung is a floor — the budget is under the workload's demand. The
  sweep runs every rung (the ladder is explicit and bounded) and reports both
  edges rather than stopping at the first failure, which would report the
  floor as though it were the ceiling.

Measured 2026-09-08, `fuel-map` (16 channels, up to 4 Hz, ~41 req/s demand),
8 rungs from 4 to 32 req/s, full markdown and JSON in
[`sustained-validation/sweep/`](../data/analyses/sustained-validation/sweep/):

| budget req/s | contract | limiting stage | worst achieved/requested | named failures |
|---:|---|---|---:|---|
| 4 | **failed** | `bus_budget_request_pacing` | 0.985 (13 of 16 channels ever seen) | `unanswered_input` |
| 6 | **failed** | `bus_budget_request_pacing` | 0.250 | `rate_shortfall` |
| 8 | held | `inference_feature_stage_on_loop` | 0.998 | — |
| 12–32 | held | `inference_feature_stage_on_loop` | 1.000 | — |

**The minimum sufficient budget for this workload is 8 req/s, and above it the
bus is never the constraint** — every holding rung is limited by the inference
feature stage on the loop (finding 6), not by pacing or storage. The deployed
`fuel-map` override of 20 req/s therefore carries ~2.5× headroom *against this
workload on this bus*. The ladder found no ceiling: past the workload's own
request demand, more budget cannot fail, so a ceiling would have to be sought
by raising demand — which would no longer be the same workload, and is not
what this item asks for.

None of that is a statement about the truck. The synthetic ECU answers in a
fixed `sleep`; the ECM's real response time is the thing that decides where
the floor actually sits, and it is unmeasured. **No default was changed.**

## Model-cadence comparison (C08.12, workstation half)

`scripts/sustained_validation.py run --inference-tick-s` overrides the model
cadence for one run without touching any deployed default. Measured
2026-09-08 with the **real IF artifact** (see C08.1), same workload each side:

| scenario | cadence | ticks | missed | lateness p95 | feature p95 | heartbeat p99 | CPU (1 core) | RSS | storage drops | seq missing |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `fuel-map` | 1 Hz | 16/16 | 0 | 1.0 ms | 51.1 ms | 32.9 ms | 16.1 % | 764 MB | 0 | 0 |
| `fuel-map` | 2 Hz | 31/31 | 0 | 1.7 ms | 51.3 ms | 41.4 ms | 24.4 % | 762 MB | 0 | 0 |
| `tuning-did` | 1 Hz | 16/16 | 0 | 0.7 ms | 71.2 ms | 35.2 ms | 27.4 % | 807 MB | 0 | 0 |
| `tuning-did` | 2 Hz | 31/31 | 0 | 1.9 ms | 72.7 ms | 57.7 ms | 37.2 % | 803 MB | 0 | 0 |

Doubling the cadence costs **+8 to +10 points of one core**, not 2×: the
feature stage's per-tick cost is flat (51 / 72 ms at both cadences) and it is
the buffer work, not the tick rate, that dominates. No deadline was missed at
either cadence, no record was lost, and RSS is unchanged (the torch import
dominates it). The visible cost is loop-holding: `tuning-did`'s heartbeat p99
goes 35 → 58 ms at 2 Hz.

CPU is process-wide and includes the harness itself. **This is the workstation
half of C08.12** — the Pi's CPU, thermal and power behaviour, and its
event-loop delay under the same workload, are what that item still holds for.

## Diagnostic policy comparison (C04.12, workstation half)

`hybrid-interval-policy` is `hybrid-mode01-mode22` with `did_cycle_seconds`
dropped and nothing else changed, which selects the older end-anchored
interval policy (`capture_profiles` resolves cycle → interval → collection
default). Both run the identical workload:

| policy | worst DID achieved / requested | quality failures | limiting stage | bus utilisation | timeouts |
|---|---:|---|---|---:|---:|
| **cycle** (`did_cycle_seconds: 0.25`) | 4.000 / 4.0 Hz | `acquisition_gap` | `inference_feature_stage_on_loop` | — | 0 |
| **interval** (`did_interval_seconds: 0.25`) | 2.953 / 4.0 Hz | **`rate_shortfall`** | `scheduling_policy_idle_bus` | 0.266 | 0 |

The cycle policy meets the requested 4 Hz on every DID; the interval policy
misses it by **26 %** — with the bus only 27 % used and every request
answered. Neither the budget nor the ECU was the constraint: the end-anchored
schedule simply did not ask often enough. That is the C04 change, measured
against its predecessor under one workload.

**It is not the 4 Hz claim.** `tuning-hybrid-research` stays experimental
until 4 Hz is achieved *and persisted* on the truck, against the real ECM's
response time — the synthetic ECU answers in a fixed 5 ms `sleep`, which is
the one number that decides whether the cycle plan is feasible there.

## Discovery across startup and recovery (C06.12, workstation half)

`discovery-recovery` is `normal-monitoring` with an adapter fault at 7 s and
nothing else changed, so the reconnect policy brings the bus back mid-run and
discovery's counters can be read across a recovery rather than only across a
clean startup:

| | startup (`normal-monitoring`) | recovery (`discovery-recovery`) |
|---|---:|---:|
| reconnects | 0 | 1 |
| sequence reconciliation | reconciled, **0 missing** | reconciled, **0 missing** |
| push identity | holds | holds |
| discovery `requests_spent` / `seconds_spent` | 3 / 0.166 s | 3 / 0.166 s |
| discovery `supported_count`, `complete` | 18, true | 18, true |
| **support probes counted by the ECU** | 3 | **6** |
| discovery `cache_version` | 2 | 4 |
| deferred diagnostics visible | 0 | 8 |

**Continuity holds across the reconnect** — every adapter-delivered sequence
number is on disk exactly once on both sides, which is the thing the item
says a received-byte count cannot establish. (The C06 tap identity is
deliberately not evaluated after a reconnect, so the sequence ledger is what
carries the claim.)

The counters do not. See finding 14.

## Findings the harness surfaced

These are defects or facts found while building the harness, each with its
reproduction. Only finding 9 was fixed here; the rest are candidates for a
source fix in their owning task.

1. **Host wall-clock steps (environment).** WSL2 steps `time.time()` by
   ~0.9 s every 1–2 min. Every persisted stamp is wall time, so a step
   inside a capture re-orders rows across it and breaks acquisition-time
   pairing (`compare_channels` read slope 1.01 / r² 0.89 on two copies of
   one signal). Reproduced with a 25 s offset sampler. Handled by detection
   and rerun in the harness; on the Pi `chrony` slews, but the same check
   (`timing.wall_clock_steps`) should be read before trusting any pairing.
2. **Arm-time deferral of a continuous-DID window.** `_initial_request_states`
   staggers first due times at the *default* 8 req/s spacing (31 requests →
   up to 3.9 s after loop start). A continuous-DID profile armed before its
   DIDs' first due instant finds `DidWindowController.session_control_due`
   with no due DID and reschedules the window `max(now + interval,
   next_due)` = **60 s** later (the default `obd_did_window_interval_seconds`),
   although the DIDs become due milliseconds later. Reproduced on the fake
   clock: armed at 1.5 s → zero DID requests for the whole 5 s run, window
   at 59.8 s. The same path is reached after a **reconnect** during a
   continuous capture (fresh stagger, fresh controller with
   `_next_window_at = start + 60`). Owner: C04/C09. Suggested fix: in the
   empty-window branch use `next_due` when it is inside the interval, and
   treat a generation change or loop start under a continuous profile as
   "window due at the first DID due time".
3. **Members keep the previous profile's due time across activation.** A
   Mode 01 channel polled at the default `medium` tier (1 Hz × 1/3 = 3 s
   interval) keeps that `next_due_at` when a 4 Hz profile activates, so its
   first capture observation is up to 3 s late and C03 names
   `acquisition_gap` (`COMMANDED_EQUIV_RATIO`: `missing_initial_s: 2.4`).
   Nothing in the generation-change path re-dues requests whose rate rose.
   Owner: C02/C09.
4. **The required-tuple preference does not resolve DID names.**
   `tuning_profile.required_tuple()` returns profile names (`ECM_RPM`) while
   DID requests are `DID_1219`, so `_preferred_members` never matches a DID
   tuple and the tuple's members are served in due order. In
   `fuel-did-research` the four-member tuple spans 29 ms p95 against a
   25 ms `max_tuple_skew_s` (158 of 197 rounds `skew_rejected`) for the
   first 8 s, then 9 ms once the due-order phase drifts the tuple to the
   front. `mode22()` maps through the aliases; `required_tuple()` should
   too. Owner: C02.5/C09.
5. **Only three provenance-backed Mode 22 widths.** `data/dids.yaml` records
   `response_length` for `0x1101`, `0x1208`, `0x1219` only, so the
   fuel-DID tuple needs three requests (two of its members cannot batch)
   and cannot meet a 25 ms skew bound at 160 req/s. Measuring the widths of
   `0x1209` and `0x123F` would let the whole tuple batch into two requests.
6. **The feature stage holds the loop for ~40–60 ms per tick.** Heartbeat
   p99 ≈ 30 ms at 1 Hz on this workstation, no data loss, no missed
   deadlines — the C08 design choice, now with the loop-side number beside
   the collector's counters.
7. **`default` is not a capture.** The daemon never writes a manifest or a
   resolved plan for normal collection; the harness writes a plan-less
   sidecar so the recompute path can judge it, and the C03 threshold for a
   `1/15 Hz` slow-tier channel then defaults to 2 s (`acquisition_gap` on
   `BAROMETRIC_PRESSURE`). Informational.
8. **Derived-channel persistence couples the inference loop to the writer
   lock.** Under `overload-storage-stall` the loop's `publish_derived` stage
   took 2.53 s (median 8.9 ms) while the injected writer stall held the
   lock, `loop_occupancy` reached 2.59 s, the heartbeat reached 97 ms and
   the inference loop missed four deadlines — `missed_by_reason:
   {"overrun": 4}`. C08 isolated model *compute* off the loop
   (`compute_wait` p95 8.9 ms here), but writing derived channels still runs
   on it, so a wedged writer becomes missed inference deadlines. The
   accounting is correct — the loss is counted at the storage sink and the
   overrun is named — so this is a coupling to fix, not a measurement
   failure. Owner: C08.
9. **A saturated sink queue reported depth 0 (fixed).** `telemetry_sinks`
   carried `queue_depth` — the depth at the last observation — and no peak.
   In `overload-storage-stall` the storage sink dropped 3,742 records behind
   a 256-deep queue and still reported `queue_depth: 0`, because by the time
   anything read the field the stall had cleared; only `dropped_records`
   said loss had happened, and nothing said the queue was the place it
   happened. `HealthMonitor.observe_telemetry_queue` now keeps a monotonic
   `queue_high_water` beside the depth, and the fanout already observes
   every sink's queue after each batch — i.e. while a wedged consumer is
   still blocked — so the mark is taken during the stall, not after it. It
   reaches `/status.telemetry_sinks.<sink>.queue_high_water` and the C10
   `losses.queue_high_water` evidence block. Covered by
   `tests/test_health.py::test_sink_queue_high_water_survives_the_drain_that_empties_it`
   and asserted end-to-end in the storage-stall scenario test. **The
   before/after tables above predate the fix** (their JSON has no
   `queue_high_water` key); a re-run at HEAD carries it.

10. **A stub detector understates the stage it stands in for by ~8×.**
   Only manifests are committed under `models/`, so every workstation run
   here installed the declared IF stub and reported the `isolation_forest`
   stage at 4.4–4.6 ms median. With a real artifact of the deployed shape
   (200 estimators, 84 features, trained from the drive-slice fixture) the
   same stage is 44 ms median / 52 ms p95 on `fuel-map`. `harness_config`
   and `scripts/sustained_validation.py run` now take `--models-dir` so the
   measurement is reproducible; the numbers, the breakdown (the sklearn
   forward pass is ~4.7 ms of the 44) and the caveats are in
   `docs/inference-scheduling.md` (C08.1). Nothing was optimised — at 1 Hz
   the tick is 8–11 % of the period with zero missed deadlines.

11. **Starvation was reported as an unsupported channel (fixed).** At
   4 req/s against ~41 req/s of demand the scheduler never got three of
   sixteen `fuel-map` channels onto the wire at all — zero timeouts, only 13
   channels observed — and `limiting_stage` called it
   `ecu_unsupported_channel`. On the truck that reads as "the ECM does not
   serve this PID" about a PID it serves fine one rung up, and it is the
   ECU-binding mistake this project has retracted most often. An unsupported
   channel answers with a negative response or times out; a starved one does
   neither. The classifier now requires headroom before blaming the ECU: a
   saturated budget with zero timeouts is reported as
   `bus_budget_request_pacing` with a note saying why. Found by the C10.12
   sweep, which is the only thing that runs a budget low enough to see it.

12. **Starvation has two names, and the overload scenario sat on the
   boundary.** A channel answering too slowly yields `rate_shortfall`; one
   that never reaches the wire yields `unanswered_input`. The C10.12 sweep
   located the boundary for `fuel-map` between 4 and 6 req/s — and
   `overload-rate`'s budget is 4, so which name appeared depended on host
   timing, making both its regression test and its own
   `expected_failures_met` flag flaky. `ScenarioSpec` now has
   `expected_failures_any` (at least one of these must appear) beside
   `expected_failures` (all of these must), and the scenario declares both
   names. The test asserts the invariant instead: starvation is named and the
   constraint is pacing.

13. **A missed rate on an idle bus was blamed on the ECU (fixed).** The
   interval-policy run missed its requested rate at 0.266 bus utilisation
   with zero timeouts, and `limiting_stage` reported `ecu_response_time` —
   the ECM answering everything it was asked, promptly, being named as the
   constraint for a schedule that did not ask. `limiting_stage` now reports
   `scheduling_policy_idle_bus` when a rate shortfall coincides with an idle
   bus and no timeouts. Same shape as finding 11: the classifier reached for
   a vehicle-side explanation of a scheduler-side fact.

14. **Discovery's own counters understate its bus cost across a recovery.**
   Startup and recovery both report `requests_spent: 3` and
   `seconds_spent: 0.166` for a completed Mode 01 bitmap walk — but the ECU
   counted **6** support probes in the recovery run, and `cache_version` went
   2 → 4. The registry is rebuilt on reconnect (deliberate: it is vehicle-keyed
   and resettable), and its counters are scoped to the current generation, so
   the declared cost of discovery is per-walk while the actual cost on the wire
   is per-walk × generations. Anything budgeting for discovery from
   `requests_spent` — a live budget calculation on the truck, say — would
   halve it after one reconnect. **Not fixed here**: making the counters
   cumulative crosses the C06 registry's deliberate reset semantics, and
   guessing at that is worse than naming it. The reproduction is
   `discovery-recovery` against `normal-monitoring`; the two numbers to compare
   are `scheduler_last_attempt.discovery.requests_spent` and
   `service_accounting.ecu.support_probes`.

## Replay (C10.9)

`replay-drive-slice` replays `tests/fixtures/ground_truth/drive-slice` at
2× through `src/collection/replay.py::replay_session` → the same fanout →
the real bindings decoder → `LiveFeatureSource` → the inference loop at a
0.5 s model tick, into a `_replay-validation-*` dir. Evidence: rows
persisted by source, inference ticks/lateness/stages, IF stub calls,
dashboard subscriber counts, and the C01 pairs `can:ECM_RPM ~ obd:RPM` and
`can:WHEEL_SPEED_6F7_A ~ obd:SPEED` re-read from the replayed Parquet at
250 ms. It is labelled replay: it measures nothing about CAN transport or
ECU service capacity.

## Pi procedure (C10.10–C10.12 — pending, operator)

Not attempted here. On the Pi with the daemon on this commit:

1. `GET /status` — record `vehicle_state` and the raw freshness fields
   **before every interpretation**; record `health.can0` (`rx_packets`,
   `rx_dropped`, `rx_over_errors`), `detectors.inference`,
   `telemetry_sinks`, `obd_scheduler.discovery`. Repeat per phase (KOEO,
   running, engine off) and keep the phase beside each number.
2. For each supported profile (`fuel-map`, `tuning-did`,
   `fuel-did-research`, `tuning-hybrid-research`), arm through
   `POST /control/capture/start`, drive 20–30 minutes with varied
   operation (idle, cruise, pulls) covering several flush intervals, DID
   keep-alives and — for the hybrid profile — hundreds of cycles. Stop
   through the route so `finalize_capture` judges it.
3. Read `capture_quality.json`: the verdict must be `pass` **with**
   `losses.recording.counters.can0.*` `verified`; `tuples.complete`,
   `skew`, per-channel `coverage` and `cadence` are the supported-profile
   claim. Compare `frames_seen` against `can0.rx_packets` over the same
   window — equal counts *and* zero `rx_dropped`/`rx_over_errors` deltas
   are the continuity evidence; a byte count alone is not.
4. Read `detectors.inference`: `missed_ticks` must be zero or explained;
   `lateness.p95_ms` and `stages.feature.p95_ms` are the Pi numbers C05/C08
   left pending. Read `health.system` (CPU %, temperature) and
   `health.power` (throttled flags) alongside.
5. Budgets (C10.12): raise `bus_budget_override` one step at a time under
   the same drive protocol; stop at the first capture whose sidecar names a
   failed contract (`rate_shortfall`, `tuple_skew`, `persistence_loss`) and
   identify the stage from the counters — never widen a queue or relax a
   threshold to make it pass.
6. Do not call an eight-second burst a sustained validation. Keep every
   capture under its `_capture-*` dir; leave unrelated sessions alone.

## Roadmap checkboxes this evidence bears on (C10.18)

Not ticked here. Workstation evidence could support, once the orchestrator
accepts it: the synthetic-bus parts of C04.12 (cycle timing on the fake bus
is already there), C06.12's tap continuity (sequence accounting at every
scenario), C08.12's event-loop and inference timing (workstation only).
Needing the Pi: every profile capacity claim in
`data/nissan-xterra-2008/capture_profiles.yaml`, `multi-pid-polling.md`
4.1–4.4 (sustained rate, batched vs single, timing parity), C05.11, the
hardware halves of C04.12 / C06.12 / C08.12, and C10.10–C10.12 themselves.

## Unverified claims

Everything that involves the truck: real SocketCAN stamp skew, the ECM's
actual response times and batched widths, kernel receive drops, Pi CPU and
thermal behaviour, chrony's clock discipline, and any rate a profile names.
The synthetic ECU answers in a fixed `sleep`; the numbers above bound the
daemon's software path, not the vehicle.
