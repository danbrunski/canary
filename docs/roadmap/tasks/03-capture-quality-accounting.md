# C03 — Evaluate complete captures with attributable losses and real tuples

Priority: high. Dependencies: C01 temporal pairing and C02 explicit rounds for
final joint-coverage accounting. Counter/lifetime fixes can start immediately.

## Problem and code entry points

`CanObdLoopStats.capture_rates()` in
[obd_scheduler.py](../../../src/collection/obd_scheduler.py) evaluates at most
60 seconds. The stop route persists that snapshot as the capture verdict.
A local reproduction with samples only during seconds 240–300 of a
five-minute capture reported 60 observed seconds, a 0.25 s maximum gap, and
`pass`.

[health.py](../../../src/models/health.py) passes cumulative storage drops to
[capture_quality.py](../../../src/collection/capture_quality.py), which describes
them as losses during this capture. The same evaluator marks the minimum of
per-channel rates as exact tuple coverage if round timestamps are enabled.
Neither a shared stamp nor a median cadence establishes complete tuple yield.

Other consumers: [control routes](../../../src/dashboard/routes/control.py),
[tuning_capture.py](../../../src/mcp_server/write_tools/tuning_capture.py),
[capture_manifest.py](../../../src/control/capture_manifest.py), and
[tuning_digest.py](../../../src/analysis/tuning_digest.py).

## Implementation checklist

- [ ] C03.1 Add the five-minute counterexample, two captures separated by an
  earlier storage loss, and equal channel rates with disjoint successful
  rounds as regression tests.
- [ ] C03.2 Separate trailing live statistics from capture-lifetime
  statistics in names and schema. Maintain first/last observation, total
  successful observations, missing initial/final intervals, maximum gap,
  and bounded cadence-distribution summaries for each capture member.
- [ ] C03.3 Keep cadence, coverage, and yield separate. Median interarrival
  time measures cadence; observed/eligible duration and long gaps measure
  coverage; persisted complete tuples measure joint yield. Never substitute
  count divided by total wall time for cadence.
- [ ] C03.4 Define pause accounting explicitly. Retain total elapsed and
  intended-active durations, record pause windows, and exclude intentional
  pauses only from metrics whose contract says so. Do not silently exclude
  engine-off gaps from a capture that promised continuous acquisition.
- [ ] C03.5 Snapshot loss-counter epochs at capture start and reconcile
  them at stop. Cover collector-to-ingest losses, storage queue/write losses,
  and available kernel/socket receive losses. Mark unknown or reset epochs as
  unverified instead of manufacturing zero loss. Keep dashboard-only drops
  separate from recording loss.
- [ ] C03.6 Connect `QueueDropCounter` to attributable health accounting;
  collector drops currently logged outside the storage sink must not vanish
  from the capture verdict. Distinguish raw records from derived expansions.
- [ ] C03.7 Remove `tuple_bound_is_exact` inference from `round_timestamps`.
  Count actual tuples satisfying member availability and declared acquisition
  skew, with no observation reuse. Keep any rate bound explicitly a bound.
  Expose scheduled, attempted, complete, persisted, and incomplete counts.
- [ ] C03.8 Make capture finalization establish a generation boundary, drain
  or reconcile that generation's queued/in-flight records, flush its writer,
  and then finalize persisted quality. Avoid writing a passing sidecar before
  the final records' persistence outcome is known.
- [ ] C03.9 Version quality sidecars and expose identical semantics in HTTP,
  MCP, dashboard, and digest. Historic sidecars containing only a trailing
  snapshot must retain their limited scope. Recompute persisted coverage on
  demand where possible; missing historical loss counters remain unknown.
- [ ] C03.10 Replace automatic “raise budget”/“lower rate” advice with the
  observed failing constraint: unanswered input, rate shortfall, acquisition
  gap, tuple skew, or persistence loss. An old drop incident is not proof of
  the current limiting resource.

## Regression and acceptance checklist

- [ ] C03.11 Test missing starts, mid-capture stalls older than 60 seconds,
  missing tails, pauses, profile switches, reconnects, and clock resets.
- [ ] C03.12 Test pre-capture losses do not fail a clean later capture;
  capture-local losses fail even if the final minute is clean. Unknown
  counters must not produce a verified no-loss claim.
- [ ] C03.13 Test equal marginal rates with zero joint coverage, partial
  batch responses, tuple skew failures, and a late writer failure at stop.
- [ ] C03.14 Reopen real writer-produced sidecars and verify arm-time versus
  stop-time scope is explicit. Extend capture-quality, profile, manifest,
  tuning-capture MCP, and digest tests.
- [ ] C03.15 Acceptance: a pass covers the declared capture interval and
  required persisted tuples; every loss is scoped to the relevant capture or
  explicitly unknown. The four-minute missing-data case must fail.

## Handoff

- [ ] C03.16 Publish the metric definitions, sidecar version, and examples of
  pass/fail/unverified results. Apply the shared validation gate.
