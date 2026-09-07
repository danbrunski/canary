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

- [ ] C10.1 Build a repeatable synthetic-bus workload with known frame
  sequence numbers, response latency, supported channels, partial responses,
  diagnostic session behavior, and configurable arrival bursts. Drive the
  actual collector and fanout/storage path, not just `LiveFeatureSource.tick()`.
- [ ] C10.2 Support runs with the normal decode, dashboard subscriber, model,
  derived-channel, and storage work enabled. Declare disabled components in
  each report so a lightweight run cannot be mistaken for full-daemon capacity.
- [ ] C10.3 Record revision, dependency/runtime versions, hardware/adapter,
  vehicle/ECU identity, profile snapshot, model versions, power/thermal state,
  workload, warmup, run duration, and timestamp basis in a versioned artifact.
- [ ] C10.4 Record service requests and responses, batch occupancy/fallback,
  session transitions, per-channel interarrival median/p95/p99/max, coverage
  gaps, complete tuple count/skew, and achieved versus requested cadence.
- [ ] C10.5 Record collector/ingest/storage losses, queue high-water marks,
  available kernel/socket receive counters, and persisted raw/decoded row
  totals. Reconcile one-to-many decoded expansion separately from raw input
  sequence completeness.
- [ ] C10.6 Record event-loop delay, feature and detector latency, missed
  inference deadlines, writer/flush latency, CPU, memory, and storage growth.
  Report unavailable counters explicitly. Use bounded instrumentation without
  per-frame log spam.
- [ ] C10.7 Finalize output through the real writer and reopen Parquet.
  Compute C03 persisted quality and run C01 known-answer comparisons. Scheduler
  success counters alone cannot establish that data reached disk intact.

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

- [ ] C10.8 Run synthetic scenarios before and after optimization with the
  same workload and configuration. Locate the limiting stage before changing
  budgets. Include overload to prove accounting, not to establish an acceptable
  data-loss rate.
- [ ] C10.9 Replay a committed known-good session through the supported
  isolated replay path to validate feature/detector behavior. Label replay
  results; they do not measure physical CAN transport or ECU service capacity.
- [ ] C10.10 Run sustained Pi scenarios covering several flush/keepalive and
  diagnostic periods. Use at least a representative 20–30 minute capture for
  final supported profiles, with enough varied operation for the intended
  analysis. Do not call an eight-second burst sustained drive validation.
- [ ] C10.11 Read `/status.vehicle_state` and raw freshness fields before
  every live interpretation. Record phase-specific vehicle state; distinguish
  KOEO, running, engine off, disconnected, and replay results.
- [ ] C10.12 Compare candidate budgets incrementally under the same workload.
  Stop increasing when a requested contract fails, identify the constraint,
  and fix its source. Do not hide data loss with larger queues or relaxed
  quality thresholds. No automatic runtime kill-switch is required.
- [ ] C10.13 Keep capture artifacts under `_`-prefixed isolated paths and
  leave unrelated sessions intact. If hardware is unavailable, finish local
  work and mark Pi evidence pending rather than substituting extrapolation.

## Acceptance and completion checklist

- [ ] C10.14 Supported profile claims are backed by persisted required-channel
  and tuple coverage, acquisition-skew limits, and no unexplained recording
  loss during the declared active interval. Intentionally unsupported or
  overloaded cases produce the expected named failure.
- [ ] C10.15 Synthetic raw-frame sequence accounting reconciles exactly;
  hardware loss evidence includes the available kernel and application levels,
  with any unobservable layer called out. No undocumented “zero drops” claim.
- [ ] C10.16 Inference remains within the configured cadence or reports missed
  deadlines accurately, and known-answer replay/analysis checks still pass.
- [ ] C10.17 Save reports under `data/analyses/` with links to isolated raw
  artifacts where available. State what improved, which resource limited each
  mode, and which claims remain unverified.
- [ ] C10.18 Update capacity evidence in profiles and the overlapping roadmap
  validation checkboxes only after measured results support them. Apply the
  shared implementation validation gate before committing explicit paths.
