# C05 — Make the live-pipeline benchmark measure real work

Priority: high prerequisite for performance claims. Dependencies: none.

## Problem and code entry points

[benchmark_live_pipeline.py](../../../scripts/benchmark_live_pipeline.py) observes
records at `float(i)` Unix seconds, then ticks at a 2026 datetime. Freshness
checks in [live.py](../../../src/preprocessing/live.py) reject every observation.
A 30-tick run reported about 27,600 Hz with buffer size zero. Warmup and
measurement also restart the same timestamp sequence.

A separate consistent-clock smoke check produced actual rows at approximately
11 ms median on the workstation. It had a different workload and is not a
replacement capacity measurement or a Pi estimate.

## Implementation checklist

- [ ] C05.1 Generate observations and ticks from the same timestamp epoch,
  advance continuously through warmup and measurement, and advance by the
  configured resampling period rather than assuming one second.
- [ ] C05.2 Verify each measured tick produced the expected feature result.
  Count empty/stale ticks separately and fail the benchmark's validity check
  when the intended workload was not exercised. Assert the warm buffer is
  populated before reporting steady-state timing.
- [ ] C05.3 Honor channel-count and CAN-frame-count arguments exactly. The
  current finite channel list and unique-ID selection are not arbitrary
  requested counts. Validate supported ranges or generate a documented mix;
  repeated frames should follow actual per-ID cadence, not one frame per ID.
- [ ] C05.4 Use physically meaningful values or a recorded workload so mode
  classification and downstream feature work are exercised intentionally.
  Record active channels, bindings, model/config versions, and operating modes.
- [ ] C05.5 Separate timings for observation/decode, feature construction,
  detector scoring, and total processing. Preserve a clearly labeled
  feature-only microbenchmark; implement the full pipeline harness in C10.
- [ ] C05.6 Report median/p95/p99/max, completed and rejected ticks, buffer
  size, host/runtime identity, warmup, sample count, resample rate, and workload
  counts in a machine-readable report alongside human-readable output.
- [ ] C05.7 Remove the unmeasured fixed workstation-to-Pi multiplier. Label
  inverse tick latency as a microbenchmark estimate that excludes untimed
  components, not a demonstrated sustainable daemon collection rate.
- [ ] C05.8 Make benchmark runs deterministic where possible and keep output
  artifacts isolated from the training corpus. Provide a short smoke workload
  and a longer steady-state workload.

## Regression and acceptance checklist

- [ ] C05.9 Add tests for consistent timestamps, no warmup rewind, exact
  requested workload counts, populated buffers, non-1 Hz cadence, and explicit
  rejection of an all-stale or zero-output run.
- [ ] C05.10 Run the repaired benchmark on the workstation and save the
  configuration and raw summary. Verify the feature transformation executes;
  elapsed duration alone is not sufficient evidence.
- [ ] C05.11 Run the same workload on the Pi when available, identifying
  hardware, power/thermal state, runtime, and model versions. Do not fill the
  missing Pi result with an extrapolation.
- [ ] C05.12 Acceptance: a reported valid feature benchmark has actual
  feature rows, a warmed buffer, monotonic scenario time, and truthful workload
  counts. The previous zero-row 27,600 Hz result must be impossible to accept.

## Handoff

- [ ] C05.13 Link the baseline report from C07, C08, and C10 and record
  exactly which pipeline stages it times. Apply the shared validation gate.
