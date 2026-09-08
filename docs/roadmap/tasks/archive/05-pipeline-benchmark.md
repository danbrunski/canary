# C05 — Make the live-pipeline benchmark measure real work

Priority: high prerequisite for performance claims. Dependencies: none.

## Problem and code entry points

[benchmark_live_pipeline.py](../../../../scripts/benchmark_live_pipeline.py) observes
records at `float(i)` Unix seconds, then ticks at a 2026 datetime. Freshness
checks in [live.py](../../../../src/preprocessing/live.py) reject every observation.
A 30-tick run reported about 27,600 Hz with buffer size zero. Warmup and
measurement also restart the same timestamp sequence.

A separate consistent-clock smoke check produced actual rows at approximately
11 ms median on the workstation. It had a different workload and is not a
replacement capacity measurement or a Pi estimate.

## Implementation checklist

- [x] C05.1 Generate observations and ticks from the same timestamp epoch,
  advance continuously through warmup and measurement, and advance by the
  configured resampling period rather than assuming one second.
  _done 2026-09-06: `SCENARIO_EPOCH` + `tick_time()`; one monotonic clock across warmup+measure, ticks advance by `1/resample_hz`._
- [x] C05.2 Verify each measured tick produced the expected feature result.
  Count empty/stale ticks separately and fail the benchmark's validity check
  when the intended workload was not exercised. Assert the warm buffer is
  populated before reporting steady-state timing.
  _done: per-tick `completed` flag; `validity_reasons()` rejects empty warm buffer, zero completed ticks, and any partially-empty run; timings cover completed ticks only; exit code 2 when invalid._
- [x] C05.3 Honor channel-count and CAN-frame-count arguments exactly. The
  current finite channel list and unique-ID selection are not arbitrary
  requested counts. Validate supported ranges or generate a documented mix;
  repeated frames should follow actual per-ID cadence, not one frame per ID.
  _done: `--channels` validated against the 27 supported PIDs; `--can-frames-per-tick` honoured exactly, IDs round-robin with per-ID cadence spread across the tick period (documented even mix; bindings carry no per-ID rate data)._
- [x] C05.4 Use physically meaningful values or a recorded workload so mode
  classification and downstream feature work are exercised intentionally.
  Record active channels, bindings, model/config versions, and operating modes.
  _done: deterministic synthetic drive cycle with physically valid values; report records channels, bindings/config hashes, model manifests, operating-mode counts. Recorded-workload replay deferred to C10 (stated in the script docstring)._
- [x] C05.5 Separate timings for observation/decode, feature construction,
  detector scoring, and total processing. Preserve a clearly labeled
  feature-only microbenchmark; implement the full pipeline harness in C10.
  _done: observe / tick / total stage timings; script labelled feature-only microbenchmark; detector scoring untimed (C10)._
- [x] C05.6 Report median/p95/p99/max, completed and rejected ticks, buffer
  size, host/runtime identity, warmup, sample count, resample rate, and workload
  counts in a machine-readable report alongside human-readable output.
  _done: `--report` JSON with median/p95/p99/max, completed/empty ticks, buffer sizes, host/git identity, warmup, counts, resample rate, validity + reasons; stdout mirrors it._
- [x] C05.7 Remove the unmeasured fixed workstation-to-Pi multiplier. Label
  inverse tick latency as a microbenchmark estimate that excludes untimed
  components, not a demonstrated sustainable daemon collection rate.
  _done: ×3 Pi multiplier removed from the script and from `docs/daemon-architecture.md`; `1/p95` labelled a feature-stage estimate, not a collection rate._
- [x] C05.8 Make benchmark runs deterministic where possible and keep output
  artifacts isolated from the training corpus. Provide a short smoke workload
  and a longer steady-state workload.
  _done: `--seed`, default output under gitignored `data/_benchmarks/`, `--profile smoke|steady`._

## Regression and acceptance checklist

- [x] C05.9 Add tests for consistent timestamps, no warmup rewind, exact
  requested workload counts, populated buffers, non-1 Hz cadence, and explicit
  rejection of an all-stale or zero-output run.
  _done: `tests/test_benchmark_live_pipeline.py` (13 tests) incl. all-stale rejection via the documented test-only epoch-offset knob and partial-empty rejection._
- [x] C05.10 Run the repaired benchmark on the workstation and save the
  configuration and raw summary. Verify the feature transformation executes;
  elapsed duration alone is not sufficient evidence.
  _done: `data/analyses/2026-09-06-benchmark-live-pipeline-baseline.md` (+ smoke/steady JSON); feature rows verified > 0 (60 and 600 rows × 312 columns)._
- **C05.11 — moved to [`PENDING-HARDWARE.md`](../PENDING-HARDWARE.md) as H2.** Run the same workload on the Pi when available, identifying hardware, power/thermal state, runtime, and model versions. Do not fill the missing Pi result with an extrapolation. Not a checkbox here: it is a measurement on the vehicle, and this document's implementation is complete without it.
- [x] C05.12 Acceptance: a reported valid feature benchmark has actual
  feature rows, a warmed buffer, monotonic scenario time, and truthful workload
  counts. The previous zero-row 27,600 Hz result must be impossible to accept.
  _done: verifier reproduced the 1-of-60-completed case → `valid: false`, no Hz quoted; zero-row run exits 2._

## Handoff

- [x] C05.13 Link the baseline report from C07, C08, and C10 and record
  exactly which pipeline stages it times. Apply the shared validation gate.
  _done: baseline linked from `docs/daemon-architecture.md`; stages timed = `observe()` + `tick()` (feature construction) only; C07/C08/C10 link the same report as they land._
