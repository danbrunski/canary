# C10 — Validate sustained collection from bus receipt through analysis

Priority: required evidence for performance claims. Dependencies: C05 before
baseline timing; repeat final scenarios after the relevant C01–C09 changes.

## Objective

Establish useful, persisted collection capacity under the actual daemon load.
Short dedicated-bus probes, workstation microbenchmarks, and configured rate
targets are different measurements. This task joins acquisition, storage,
tuple quality, and inference observations without conflating them.

Entry points: [collector](../../../src/collection/can_obd_collector.py),
[fanout and sinks](../../../src/daemon/drain.py),
[writer](../../../src/storage/parquet_writer.py),
[health](../../../src/models/health.py),
[inference](../../../src/daemon/inference.py), and existing benchmark/replay tools.

## Harness and report checklist

- [x] C10.1 Build a repeatable synthetic-bus workload with known frame
  sequence numbers, response latency, supported channels, partial responses,
  diagnostic session behavior, and configurable arrival bursts. Drive the
  actual collector and fanout/storage path, not just `LiveFeatureSource.tick()`.
  _done: `src/validation/synthetic_bus.py` — numbered frames on `0x6A0` (bound nowhere in `data/bindings.yaml`), scripted response latency, supported/silent/refused channels, session gating, configurable bursts; driven through `collect_can_obd_from_config` → `ThreadHandoff` → `drain_queue_with_broadcast` → `ParquetTelemetryWriter`, not `LiveFeatureSource.tick()`._
- [x] C10.2 Support runs with the normal decode, dashboard subscriber, model,
  derived-channel, and storage work enabled. Declare disabled components in
  each report so a lightweight run cannot be mistaken for full-daemon capacity.
  _done: decode, dashboard subscriber, live feature source, DTC/monitor sinks, derived channels, storage and the inference loop all run; `provenance.components` and `provenance.models` declare what is enabled, stubbed (IF) or absent (sequence detector, GPS, ELM327) in every report._
- [x] C10.3 Record revision, dependency/runtime versions, hardware/adapter,
  vehicle/ECU identity, profile snapshot, model versions, power/thermal state,
  workload, warmup, run duration, and timestamp basis in a versioned artifact.
  _done: `schema_version: 1` JSON per scenario with git sha/describe/worktree, Python/platform, pinned dependency versions, adapter, vehicle slug, profile snapshot, model status, power/thermal, workload, warmup, declared vs measured duration and per-row-kind timestamp basis (`src/validation/report.py`)._
- [x] C10.4 Record service requests and responses, batch occupancy/fallback,
  session transitions, per-channel interarrival median/p95/p99/max, coverage
  gaps, complete tuple count/skew, and achieved versus requested cadence.
  _done: `evidence.transport` — requests/responses by service from the ECU's own log, batch occupancy and single/batched counts on both sides, session transitions and keep-alives, per-channel interarrival median/p95/p99/max with achieved vs requested cadence, coverage gaps, complete tuple count and skew._
- [x] C10.5 Record collector/ingest/storage losses, queue high-water marks,
  available kernel/socket receive counters, and persisted raw/decoded row
  totals. Reconcile one-to-many decoded expansion separately from raw input
  sequence completeness.
  _done: `evidence.losses` proves `collector_pushed == persisted_raw_rows + buffer_dropped + queue_dropped + storage_dropped` and the C06 tap identity in every scenario; kernel counters reported as `unavailable` by name; `evidence.decoded_expansion` reconciles one-to-many decode separately from raw input. Queue high-water marks were the one gap — a saturated sink read `queue_depth: 0` after the stall cleared — fixed here by `telemetry_sinks.<sink>.queue_high_water` (finding 9); the committed before/after JSON predates it, its test coverage does not._
- [x] C10.6 Record event-loop delay, feature and detector latency, missed
  inference deadlines, writer/flush latency, CPU, memory, and storage growth.
  Report unavailable counters explicitly. Use bounded instrumentation without
  per-frame log spam.
  _done: `evidence.timing` — event-loop heartbeat overshoot (bounded reservoir), the `InferenceTimingRecorder` snapshot (lateness, missed-by-reason, per-stage p95), writer flush latency, storage sink write stats, `getrusage` CPU/RSS, storage growth, wall-clock steps. Unavailable counters (`kernel_socket_counters`, `power_thermal`) say so by name. No per-frame logging._
- [x] C10.7 Finalize output through the real writer and reopen Parquet.
  Compute C03 persisted quality and run C01 known-answer comparisons. Scheduler
  success counters alone cannot establish that data reached disk intact.
  _done: output goes through `AsyncParquetTelemetryWriter` and `finalize_capture`, and every number is read back from reopened Parquet. `evidence.quality` carries the C03 sidecar as written plus a `with_adapter_ledger` recompute; `evidence.known_answer` runs `compare_channels` on `can:SYNTH_RPM ~ obd:RPM` (1:1), the deliberately wrong-scale `SYNTH_RPM_HALF` (slope 2, differs as expected) and `did:ECM_RPM ~ can:SYNTH_RPM`; refusals are reported as refusals._

## Scenario checklist

| Scenario | Required evidence |
|---|---|
| Normal monitoring with discovery | Critical-input gaps, probe cost, fairness, startup/retry raw-frame continuity |
| Fuel-map | Complete required fueling tuples, actual skew, mixed-rate context, batch occupancy |
| Continuous tuning DID | Requested DID cadence, raw frame continuity, per-DID context weights, session continuity |
| Fuel DID research | Actual single/batched request demand, measured width/capability, persisted tuple yield |
| Hybrid Mode 01/22 | Both services progress, entry/exit delays, cycle deadlines, cross-mode pairing limits |
| Passive / legacy ELM327 companion | Receive-loop responsiveness, raw capture completeness, no sniffer sends |
| Fault and transition cases | Silent/unsupported channel, delayed response, storage stall, pause, profile change, reconnect, final flush |

- [x] C10.8 Run synthetic scenarios before and after optimization with the
  same workload and configuration. Locate the limiting stage before changing
  budgets. Include overload to prove accounting, not to establish an acceptable
  data-loss rate.
  _done: same harness, same workload, run from a read-only worktree of `2257b53` (pre-C09) and at HEAD; fields the older tree lacks are dropped and listed in `provenance.*_unsupported_by_this_tree`. `evidence.limiting_stage` names the constraint per scenario before any budget moved, and both overloads produce the same named failures on both sides. Procedure and diff in `docs/sustained-validation.md`; no budget or threshold was relaxed to pass._
- [x] C10.9 Replay a committed known-good session through the supported
  isolated replay path to validate feature/detector behavior. Label replay
  results; they do not measure physical CAN transport or ECU service capacity.
  _done: `replay-drive-slice` replays `tests/fixtures/ground_truth/drive-slice` at 2× through `replay_session` → the real fanout → the bindings decoder → the inference loop into `_replay-validation-*`; 49/49 ticks at 0.5 s, and the C01 pairs re-read from the replayed Parquet still agree 1:1. Labelled `replay` everywhere — it measures no CAN transport or ECU capacity._
- [~] C10.10 Run sustained Pi scenarios covering several flush/keepalive and
  diagnostic periods. Use at least a representative 20–30 minute capture for
  final supported profiles, with enough varied operation for the intended
  analysis. Do not call an eight-second burst sustained drive validation.
  _pending hardware 2026-09-08: Pi unreachable (`canary.local` ENOTFOUND, MCP server failed to connect). Synthetic scenarios are 10–60 s by construction and are not offered as sustained drive validation. Procedure in `docs/sustained-validation.md` (“Pi procedure”, steps 1–4): 20–30 minute captures per supported profile covering several flush intervals, DID keep-alives and hundreds of hybrid cycles._
- [~] C10.11 Read `/status.vehicle_state` and raw freshness fields before
  every live interpretation. Record phase-specific vehicle state; distinguish
  KOEO, running, engine off, disconnected, and replay results.
  _pending hardware 2026-09-08: no live interpretation was made, so no vehicle state was read. Every synthetic report is labelled `hardware.adapter: synthetic` and the replay scenario is labelled `replay`; the Pi procedure requires `/status.vehicle_state` and the raw freshness fields per phase (KOEO, running, engine off) beside each number._
- [~] C10.12 Compare candidate budgets incrementally under the same workload.
  Stop increasing when a requested contract fails, identify the constraint,
  and fix its source. Do not hide data loss with larger queues or relaxed
  quality thresholds. No automatic runtime kill-switch is required.
  _partial: done on the synthetic bus — `evidence.limiting_stage` names the constraint for each mode (storage sink under the wedged writer, request pacing under rate overload, the inference feature stage otherwise) and both overloads keep their named failures rather than being widened away. The incremental budget comparison on the truck is pending hardware; procedure in `docs/sustained-validation.md` step 5._
- [x] C10.13 Keep capture artifacts under `_`-prefixed isolated paths and
  leave unrelated sessions intact. If hardware is unavailable, finish local
  work and mark Pi evidence pending rather than substituting extrapolation.
  _done: every capture landed under `data/_validation/{before,after}/<scenario>/_capture-*` and `_replay-validation-*`; the root is now gitignored and no historic session was touched. Hardware evidence is marked pending above rather than extrapolated from the workstation._

## Acceptance and completion checklist

- [x] C10.14 Supported profile claims are backed by persisted required-channel
  and tuple coverage, acquisition-skew limits, and no unexplained recording
  loss during the declared active interval. Intentionally unsupported or
  overloaded cases produce the expected named failure.
  _done for the synthetic bus: per-profile persisted coverage, tuple counts and skew, and the C03 verdict (plus the adapter-ledger recompute a Pi would judge with) are in `evidence.quality`. Every intentionally unsupported or overloaded scenario produced its expected named failure — `headline.expected_failures_met` is true in all eleven. **No claim in `data/nissan-xterra-2008/capture_profiles.yaml` is established by this**; those need C10.10._
- [x] C10.15 Synthetic raw-frame sequence accounting reconciles exactly;
  hardware loss evidence includes the available kernel and application levels,
  with any unobservable layer called out. No undocumented “zero drops” claim.
  _done: `evidence.sequence_reconciliation` accounts for every adapter-delivered sequence number, `reconciled` (0 missing) in every non-overload scenario and `reconciled_with_counted_loss` (3506 missing ≤ 3742 counted) under the storage stall; `UNEXPLAINED_LOSS` nowhere. Kernel counters are `unavailable` on this host and say so — no undocumented “zero drops” claim anywhere._
- [x] C10.16 Inference remains within the configured cadence or reports missed
  deadlines accurately, and known-answer replay/analysis checks still pass.
  _done: 0 missed deadlines in every non-overloaded scenario, and under the storage stall the 4 misses are reported by reason (`overrun`) rather than hidden — the coupling behind them is finding 8. The known-answer replay and the C01 ground-truth pairs still pass._
- [x] C10.17 Save reports under `data/analyses/` with links to isolated raw
  artifacts where available. State what improved, which resource limited each
  mode, and which claims remain unverified.
  _done: `data/analyses/2026-09-07-sustained-validation.md` with per-scenario JSON under `data/analyses/sustained-validation/{before,after}/`, linking the isolated raw artifacts under `data/_validation/` (local, gitignored). It states what improved before → after, which resource limited each mode, and closes with an explicit list of what remains unverified (all hardware)._
- [x] C10.18 Update capacity evidence in profiles and the overlapping roadmap
  validation checkboxes only after measured results support them. Apply the
  shared implementation validation gate before committing explicit paths.
  _done: this pass ticks only the workstation-backed items and leaves every hardware half pending. No profile capacity claim, no `multi-pid-polling.md` sustained-rate checkbox and no rate default was changed on synthetic evidence; C04.12, C05.11, C06.12, C08.12 and C09.12/.14/.16 keep their `[~]` hardware notes. Whole-repo gate green before commit._
