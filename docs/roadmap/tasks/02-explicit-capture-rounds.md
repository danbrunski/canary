# C02 — Make capture rounds explicit and preserve acquisition clocks

Priority: high. Dependencies: none for the scheduler regression; coordinate
the persisted timing contract with C01 and tuple accounting with C03.

## Problem and code entry points

`run_can_obd_loop()` in
[can_obd_collector.py](../../../src/collection/can_obd_collector.py) holds
`round_wall_start` until it runs out of due or eligible work. Under sustained
demand that may never happen. A synthetic two-channel capture asking for
10 Hz per channel with an 8 request/s budget, batching disabled and no priority
channels, produced eight readings spanning 875 ms with one logical timestamp.

`acquired_at` currently records a userspace clock read after response handling,
not necessarily the kernel frame-arrival time. Both its precision and its
fallback behavior need explicit semantics.

Other entry points: [obd_scheduler.py](../../../src/collection/obd_scheduler.py),
[obd_collector.py](../../../src/collection/obd_collector.py),
[telemetry.py](../../../src/telemetry.py),
[parquet_writer.py](../../../src/storage/parquet_writer.py), and
[channels.py](../../../src/analysis/channels.py).

## Implementation checklist

- [ ] C02.1 Reproduce the overloaded-round failure with a deterministic fake
  clock and bus. Include a batch failure/fallback variant so correctness does
  not depend on batching being available.
- [ ] C02.2 Introduce explicit round state: capture generation, round ID,
  scheduled start, deadline, selected members due in this round, attempted
  members, and successful observations. End a round on membership completion
  or deadline, even while later work is due.
- [ ] C02.3 Define mixed-rate membership. Fast channels belong to every fast
  round; context channels join only when their cadence requires it. A channel
  must not be attempted repeatedly in one round simply because it is overdue.
- [ ] C02.4 Schedule rounds on monotonic deadlines. Mark missed/incomplete
  rounds explicitly and skip obsolete deadlines rather than issuing a burst
  of synthetic catch-up samples. Keep request budgeting separate from desired
  sampling rate.
- [ ] C02.5 Select same-round Mode 01 companions together, bounded by ECU and
  transport capability. Prioritize the required tuple's completion without
  violating configured rates or starving other due members. Record actual
  cross-batch acquisition skew.
- [ ] C02.6 Carry acquisition time and round identity separately through
  `ObdReading`, `TelemetryRecord`, Parquet, replay, and public readers. Prefer
  acquisition time for measurement chronology. Keep any legacy presentation
  stamp explicitly labeled and provide versioned compatibility for old files.
- [ ] C02.7 Preserve received-frame timing through response reassembly.
  Declare whether a multiframe response uses first-frame or completion time;
  retain a response interval when needed. Do not describe a shared response
  timestamp as proof that the ECU sampled all sensors atomically.
- [ ] C02.8 Reset round state on profile changes, stop/start, pause/resume,
  reconnect, and replay transitions. Tag or finish in-flight requests against
  the generation in which they began so they cannot complete a new capture's
  tuple.
- [ ] C02.9 Sweep timestamp consumers: live freshness, status, storage session
  rotation, preprocessing pivot/resampling, derived rows, rate summaries,
  tuning reports, and replay. Preserve readable historic sessions without
  claiming that missing acquisition evidence can be reconstructed exactly.

## Regression and acceptance checklist

- [ ] C02.10 Verify bounded round duration under overload, timeouts, permanent
  refusals, partial batches, and single-PID fallback. The 875 ms counterexample
  must produce distinct rounds or explicitly incomplete rounds.
- [ ] C02.11 Verify six-PID batches plus additional members share a round ID
  while retaining distinct response times. Test 4 Hz fast / 1 Hz context
  scheduling and continuously due 15 Hz DID work.
- [ ] C02.12 Round-trip metadata through the real writer and readers, not
  only mocked records. Test no repeated-round timestamp can collapse distinct
  acquired measurements in analysis.
- [ ] C02.13 Extend [test_multi_pid_batching.py](../../../tests/test_multi_pid_batching.py),
  [test_can_obd_collector.py](../../../tests/test_can_obd_collector.py), and
  [test_tuning_frame.py](../../../tests/test_tuning_frame.py). Check preprocessing
  rate-of-change and gap handling against persisted data.
- [ ] C02.14 Acceptance: each successful reading belongs to one capture/round
  generation, every round terminates, and round identity never substitutes for
  measured temporal coherence.

## Handoff

- [ ] C02.15 Document the timestamp/round schema, legacy-reader behavior, and
  the maximum-skew evidence C03 can consume. Apply the shared validation gate.
