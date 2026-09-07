# C03 — Evaluate complete captures with attributable losses and real tuples

Priority: high. Dependencies: C01 temporal pairing and C02 explicit rounds for
final joint-coverage accounting. Counter/lifetime fixes can start immediately.

## Problem and code entry points

`CanObdLoopStats.capture_rates()` in
[obd_scheduler.py](../../../../src/collection/obd_scheduler.py) evaluates at most
60 seconds. The stop route persists that snapshot as the capture verdict.
A local reproduction with samples only during seconds 240–300 of a
five-minute capture reported 60 observed seconds, a 0.25 s maximum gap, and
`pass`.

[health.py](../../../../src/models/health.py) passes cumulative storage drops to
[capture_quality.py](../../../../src/collection/capture_quality.py), which describes
them as losses during this capture. The same evaluator marks the minimum of
per-channel rates as exact tuple coverage if round timestamps are enabled.
Neither a shared stamp nor a median cadence establishes complete tuple yield.

Other consumers: [control routes](../../../../src/dashboard/routes/control.py),
[tuning_capture.py](../../../../src/mcp_server/write_tools/tuning_capture.py),
[capture_manifest.py](../../../../src/control/capture_manifest.py), and
[tuning_digest.py](../../../../src/analysis/tuning_digest.py).

## Implementation checklist

- [x] C03.1 Add the five-minute counterexample, two captures separated by an
  earlier storage loss, and equal channel rates with disjoint successful
  rounds as regression tests.
  _done 2026-09-07: regression tests — 5-min capture with samples only in 240–300 s (`pass` → `fail`, `acquisition_gap`, `missing_initial_s` 240); earlier storage loss then clean capture (later capture `pass`, delta 0); equal rates with disjoint rounds (`tuples.complete` 0 of 240, `tuple_incomplete`)._
- [x] C03.2 Separate trailing live statistics from capture-lifetime
  statistics in names and schema. Maintain first/last observation, total
  successful observations, missing initial/final intervals, maximum gap,
  and bounded cadence-distribution summaries for each capture member.
  _done: `src/collection/capture_lifetime.py` `CaptureLedger` — per member first/last observed, observations, missing initial/final, max gap, bounded-reservoir median/p95 interarrival, long gaps; reset at every capture start; `trailing.*` (60 s) kept separately and labelled._
- [x] C03.3 Keep cadence, coverage, and yield separate. Median interarrival
  time measures cadence; observed/eligible duration and long gaps measure
  coverage; persisted complete tuples measure joint yield. Never substitute
  count divided by total wall time for cadence.
  _done: cadence = 1/median interarrival, coverage = observed vs eligible duration with long gaps, yield = persisted complete tuples; no count ÷ wall-time anywhere (`docs/capture-quality.md`)._
- [x] C03.4 Define pause accounting explicitly. Retain total elapsed and
  intended-active durations, record pause windows, and exclude intentional
  pauses only from metrics whose contract says so. Do not silently exclude
  engine-off gaps from a capture that promised continuous acquisition.
  _done: `elapsed_s`, `intended_active_s`, `pause_windows` with reasons (`operator` / `passive`) recorded from the pause and passive hooks; only contract-declared metrics exclude pauses; an engine-off gap in a continuous capture is scored as a gap (tested)._
- [x] C03.5 Snapshot loss-counter epochs at capture start and reconcile
  them at stop. Cover collector-to-ingest losses, storage queue/write losses,
  and available kernel/socket receive losses. Mark unknown or reset epochs as
  unverified instead of manufacturing zero loss. Keep dashboard-only drops
  separate from recording loss.
  _done: `capture_losses.reconcile_losses` — start/stop epochs (`capture_epoch.json` at arm) → verified / reset / unknown with delta null for unknown or reset; recording (storage, can_receive, obd_collect, can0 kernel counters) vs dashboard (broadcast, live) vs derived scopes._
- [x] C03.6 Connect `QueueDropCounter` to attributable health accounting;
  collector drops currently logged outside the storage sink must not vanish
  from the capture verdict. Distinguish raw records from derived expansions.
  _done: the canable collector's `ThreadHandoff`/`QueueDropCounter` stats reach health as the `obd_collect` sink with raw responses vs derived records distinguished._
- [x] C03.7 Remove `tuple_bound_is_exact` inference from `round_timestamps`.
  Count actual tuples satisfying member availability and declared acquisition
  skew, with no observation reuse. Keep any rate bound explicitly a bound.
  Expose scheduled, attempted, complete, persisted, and incomplete counts.
  _done: `tuple_bound_is_exact` removed; `capture_coverage.count_persisted_tuples` counts per (generation, round_id) with skew ≤ the profile's new `max_tuple_skew_s` and no observation reuse; exposes scheduled/attempted/complete/persisted/incomplete/skew_rejected; `tuple_rate_bound_hz` stays a labelled bound._
- [x] C03.8 Make capture finalization establish a generation boundary, drain
  or reconcile that generation's queued/in-flight records, flush its writer,
  and then finalize persisted quality. Avoid writing a passing sidecar before
  the final records' persistence outcome is known.
  _done: `control/capture_finalize.finalize_capture` — generation bump → bounded drain → writer flush → judge from rows on disk → sidecar; flush failure, drain timeout, row shortfall → `persistence_loss`; an unobserved queue → `unverified`, never a pass._
- [x] C03.9 Version quality sidecars and expose identical semantics in HTTP,
  MCP, dashboard, and digest. Historic sidecars containing only a trailing
  snapshot must retain their limited scope. Recompute persisted coverage on
  demand where possible; missing historical loss counters remain unknown.
  _done: sidecar `schema_version` 2 with `scope` and a persisted interval (`started_wall`/`ended_wall`); v1 files read as `trailing_snapshot`; identical semantics over HTTP (`GET /control/capture/quality[/{session_id}]`), MCP (`get_capture_quality`), dashboard types/panel, and digest; `recompute_capture_quality` scores the recorded interval with historic losses `unknown`._
- [x] C03.10 Replace automatic “raise budget”/“lower rate” advice with the
  observed failing constraint: unanswered input, rate shortfall, acquisition
  gap, tuple skew, or persistence loss. An old drop incident is not proof of
  the current limiting resource.
  _done: verdicts name the failing constraint (`unanswered_input`, `rate_shortfall`, `acquisition_gap`, `tuple_incomplete`, `tuple_skew`, `persistence_loss`, `unknown_loss_counters`) with evidence; budget/rate advice removed from evaluator, routes, and the MCP docstring._

## Regression and acceptance checklist

- [x] C03.11 Test missing starts, mid-capture stalls older than 60 seconds,
  missing tails, pauses, profile switches, reconnects, and clock resets.
  _done: `tests/test_capture_lifetime.py` — missing start, stall > 60 s, missing tail, pauses, profile switch, reconnect, clock regression._
- [x] C03.12 Test pre-capture losses do not fail a clean later capture;
  capture-local losses fail even if the final minute is clean. Unknown
  counters must not produce a verified no-loss claim.
  _done: pre-capture losses do not fail a later clean capture; capture-local losses fail despite a clean final minute; unknown/reset counters never produce a verified no-loss claim._
- [x] C03.13 Test equal marginal rates with zero joint coverage, partial
  batch responses, tuple skew failures, and a late writer failure at stop.
  _done: disjoint rounds, partial batch response, tuple skew rejection, late writer failure at stop._
- [x] C03.14 Reopen real writer-produced sidecars and verify arm-time versus
  stop-time scope is explicit. Extend capture-quality, profile, manifest,
  tuning-capture MCP, and digest tests.
  _done: real writer-produced sidecars reopened through the stop route; arm-time (`live`) vs stop-time (`stop`) scope explicit; capture-quality, profile, manifest, tuning-capture MCP, and digest tests extended._
- [x] C03.15 Acceptance: a pass covers the declared capture interval and
  required persisted tuples; every loss is scoped to the relevant capture or
  explicitly unknown. The four-minute missing-data case must fail.
  _done: a pass covers the declared interval and required persisted tuples; every loss is scoped or `unknown`; the four-minute case fails end-to-end through `POST /control/capture/stop` (verifier reproduced)._

## Handoff

- [x] C03.16 Publish the metric definitions, sidecar version, and examples of
  pass/fail/unverified results. Apply the shared validation gate.
  _done: `docs/capture-quality.md` (definitions, sidecar v2 schema, loss reconciliation, pass/fail/unverified examples, constraint vocabulary) + `docs/mcp-server.md` / `docs/headless-api.md` rows. Gate green._
