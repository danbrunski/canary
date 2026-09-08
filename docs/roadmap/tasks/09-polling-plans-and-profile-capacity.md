# C09 — Improve polling plans and express actual transport capacity

Priority: medium after correctness. Dependencies: C02 rounds, C03 quality,
C04 hybrid scheduling, C06 discovery ownership; C10 verifies rate changes.

## Problem and existing capabilities

Mode 01 batching already exists in
[obd_scheduler.py](../../../src/collection/obd_scheduler.py) and
[can_obd_collector.py](../../../src/collection/can_obd_collector.py). It batches
only currently due eligible PIDs, so fragmented due times and oversized plans
can reduce batch occupancy. Normal polling also probes the decoder catalog
beyond the support bitmap, with escalating backoff already implemented.

[capture_profiles.py](../../../src/collection/capture_profiles.py) supports Mode 01
weights, but selected DIDs share one target rate. Slow context such as coolant
therefore consumes the same request cadence as fast cam/fueling channels.

DID batching is now integrated into the active scheduler using the existing builders/splitters,
`CanObdClient.query_mode_22_batch()`, and `sweep_dids_batched()` already exist.
[test_multi_did_batching.py](../../../tests/test_multi_did_batching.py) includes
an observed multi-DID request. Earlier roadmap claims that the ECM refuses all
DID batching are historical, not a reason to duplicate or discard this work.
Support must still be established for the actual selected DIDs and widths.

The vehicle's `fuel-did-research` capacity text assumes three DIDs per active
request, which the collector does not currently do. Capacity must describe
the actual path, not a capability available only in a probe tool.

## Implementation checklist: scheduling and demand

- [x] C09.1 Build one resolved polling-plan representation for scheduler,
  profile preview, capture manifest, and quality checks. Resolve aliases and
  deduplicate by service, ECU, and identifier before computing demand.
  _done: `ResolvedPollingPlan` is built after identifier resolution and is
  consumed by live scheduler metrics, `/control/capture/profiles`, schema-v2
  capture manifests, live quality, and persisted quality recomputation._
- [x] C09.2 Separate channel reads/s, diagnostic requests/s, raw frames/s,
  decoded rows/s, and complete tuples/s. Include session control and discovery
  overhead. Report best-case batched demand and single-request fallback demand
  separately; do not equate diagnostic request count with total bus load.
  _done: the shared payload reports all five units, explicit session/discovery
  overhead, nested-cadence best-case batching, single-request fallback, and an
  incomplete/lower-bound marker when response widths are unknown._
- [x] C09.3 Improve Mode 01 batch occupancy using C02 round membership and
  compatible deadlines. Do not pull a slow channel to fast cadence merely to
  fill a batch, and do not delay a critical tuple to wait for optional context.
  _done in C02/Q26: `select_batch_companions` confines companions to the open
  round's unattempted members, preserves tier boundaries outside synchronous
  capture rounds, and orders `required_tuple` members first. Regression coverage:
  `test_companion_selection_respects_poll_tier`,
  `test_required_tuple_members_are_batched_first`, and the same-round batch
  attribution cases in `test_capture_rounds.py`._
- [x] C09.4 Keep mode/model-critical priority channels profile-configurable.
  Verify their demanded share is feasible and expose infeasible plans rather
  than assuming a small priority list cannot starve other requests.
  _done: nullable `priority_channels` inherits collection defaults while an
  explicit tuple owns the profile lane. The active selector uses it, and plan
  preview reports priority-only feasibility against the effective budget._
- [x] C09.5 Budget exploratory probes separately from confirmed capture
  work, retaining recovery for sparse support bitmaps and previously answering
  channels. Reuse current miss backoff; avoid permanent unsupported decisions
  based only on silence or one failed diagnostic session.
  _done: `obd_discovery_budget_requests_per_second` independently paces
  exploratory walks while the global budget remains authoritative. Existing
  sparse-bitmap, silence/retry, responder recovery, and miss-backoff behavior
  remains covered._

## Implementation checklist: DID cadence and batch integration

- [x] C09.6 Add a backward-compatible per-DID cadence field, such as
  `mode22_rate_weights` alongside the existing DID selection list. Missing
  weights default to 1. Validate finite positive values and selected-member
  keys, resolving aliases consistently. Define rate semantics for continuous
  and windowed profiles explicitly.
  _done: `CaptureProfile.mode22_rate_weights`; old YAML loads with `{}` and
  therefore weight 1. Continuous sessions scale each member's scheduler rate;
  windowed sessions open only for members due at that cycle boundary. Runtime
  lookup resolves the persisted profile name through the shared DID alias map._
- [x] C09.7 Propagate DID cadence through absolute-rate resolution, due-time
  scheduling, profile editors, API/MCP models, demand calculation, quality
  floors, and manifest snapshots. Preserve operator-modified profiles during
  seed upgrades. Separate context rates from required fast-tuple floors.
  _done: absolute rates, due-time scheduling, dashboard editor/types, strict
  API schema, manifest snapshots, backward-compatible seed loading, quality
  promises/floors, and resolved demand calculation are wired and tested._
- [x] C09.8 Integrate existing multi-DID builders/splitters into the tapped
  scheduler request path. Scope capability by ECU/session, group only eligible
  same-round DIDs, and enforce the single-frame request limit of three DIDs.
  _done: `did_batch_max_dids` is an independent opt-in capability; the owned
  receive loop uses the shared builder/splitter and groups only eligible,
  same-ECU, same-round members, capped at three._
- [x] C09.9 Use measured full response payload widths for splitting, not a
  catalog signal's decoded slice width. Store width provenance with ECU/profile
  data. Unknown/truncated boundaries must not misattribute later bytes.
  _done: `DidEntry.response_length` is distinct from the decoded byte slice
  and requires `response_length_source`. Only three provenance-backed widths
  currently batch; every other DID stays single. The splitter stops at an
  unknown/truncated boundary._
- [x] C09.10 Preserve per-member accounting for partial responses, ECU
  responder identity, raw response bytes, acquisition time, request/round ID,
  and separate Mode 01/22 batch metrics. Shared receipt does not establish
  atomic internal ECU sampling.
  _done: batch members retain raw payload bytes plus shared receipt time,
  actual responder, request/round identity, and individual response/miss
  state. Physical requests reject another ECU's response. Mode 22 counters
  and width provenance are separate under scheduler batching metrics._
- [x] C09.11 Add capability failure/fallback behavior using the existing
  Mode 01 approach where appropriate. A rejected batch must not permanently
  suppress healthy individual DIDs. Recalculate effective demand and report
  quality shortfall when falling back to single requests.
  _done: three whole-batch failures disable Mode 22 batching for only that
  ECU/session; members receive no false miss/backoff and resume as healthy
  single reads. Runtime plan metrics use the narrowed effective batch size,
  and persisted quality reports rate shortfall against the unchanged promise._

## Implementation checklist: profile updates

- [~] C09.12 For normal monitoring, measure useful critical-channel coverage,
  probe overhead, and batching before changing the default budget. Reuse
  validated passive providers explicitly where appropriate; preserve source
  provenance and the model inputs they actually supply.
  _blocked on a live normal-monitoring capture; no default budget was changed._
- [x] C09.13 For fuel-map, make the required fueling tuple the scheduling and
  acceptance unit. Retain vehicle-specific unsupported-PID exclusions and
  record tuple rate and skew, not just aggregate channel throughput.
  _done through C02/C03 plus the vehicle profile: unsupported PID 0x25 is
  excluded, required tuple members schedule first, and persisted quality
  records tuple completion/rate/skew._
- [~] C09.14 For continuous DID profiles, identify fast dynamics and slow
  context from experiment requirements, lower context demand, and measure the
  resulting fast-channel coverage. Do not prescribe one vehicle's cadence to
  all vehicles.
  _blocked on an experiment-specific live capture to classify dynamics and
  measure released capacity; the schema/runtime cadence control is complete._
- [x] C09.15 For hybrid profiles, use C04's measured cycle capacity and retain
  cross-mode timing limits. Keep unsupported targets visibly experimental.
  _done: the vehicle profile retains C04's 0.25 s start-to-start cycle,
  measured 0.341 s cross-mode separation and achieved shortfall, and remains
  explicitly experimental._
- [~] C09.16 Correct capacity notes to state active transport, measured test
  duration/conditions, and actual request counts. Retain the old storage-loss
  incident as history, not a universal 24 request/s ceiling. Do not raise
  deployed rate defaults until C10 supplies sustained evidence.
  _partial: misleading DID batching/24 req/s claims are corrected and no
  defaults were raised._
  _C10 supplied request counts under measured conditions **on the synthetic
  bus only** (e.g. `tuning-did` 148 batched + 1093 single Mode 22 in ~10 s
  against 1613 single before C09; fuel-DID 199 batched + 990 single; hybrid
  40 batched), with the ECU-side count agreeing with the scheduler's every
  time. On that bus the budget was never the constraint, so batching bought
  request count, not rate. None of it licenses a capacity note about the
  truck's transport or a raised default — that is C10.10 on the Pi._

## Regression and acceptance checklist

- [x] C09.17 Test alias duplicates, heterogeneous rates, critical-channel
  fairness, Mode 01 fallback, DID batch widths, missing members, unknown widths,
  responder mismatch, partial desynchronization, and per-service capabilities.
  _done across `test_polling_plan.py`, `test_can_obd_collector.py`,
  `test_multi_pid_batching.py`, `test_multi_did_batching.py`, and
  `test_diagnostic_discovery.py`._
- [x] C09.18 Extend existing multi-PID/multi-DID, profile, scheduler, and
  capture-quality tests. Verify old YAML loads identically and edited built-ins
  are not overwritten.
  _done: additive profile fields default compatibly; existing old-YAML and
  operator-modified seed-upgrade regressions pass, alongside manifest and
  resolved-plan quality tests._
- [x] C09.19 Verify a known-answer decode through both single and batch paths
  before interpreting novel DID results. Record real selected-DID batch
  support and sustained collection on the Pi in C10.
  _done for software: the same fake ECU values decode identically through
  scheduler batch sizes 1 and 3. Real selected-DID support/sustained evidence
  remains explicitly owned by C10._
- [x] C09.20 Acceptance: requested and fallback demand match the actual
  resolved plan, context rates release measurable request capacity, batching
  preserves attribution, and promised rate changes pass persisted quality.
  _done: focused regressions cover demand arithmetic and fallback
  recalculation, heterogeneous context cadence, per-member attribution, and
  resolved-plan-backed live/persisted quality contracts._

## Handoff

- [x] C09.21 Record profile/schema migration, actual measured rate changes,
  capability evidence, and relevant roadmap checkbox updates. Apply the shared
  validation gate; leave unverified hardware targets incomplete.
  _done: profile migration is additive (`priority_channels: null` inherits and
  existing `mode22_rate_weights: {}` remains 1x); capture manifests advance to
  schema v2 with `resolved_polling_plan`; no deployed rate/budget default was
  raised. Focused C09 tests, dashboard tests/typecheck, and the repository gate
  are recorded below. A 2026-09-07 Pi status attempt timed out, so C09.12 and
  C09.14 remain hardware-blocked and C09.16 remains assigned to C10._

## Validation evidence (2026-09-07)

- C09 focused Python regression: 237 passed, including window-boundary cadence.
- Dashboard: 238 tests passed; TypeScript typecheck passed.
- Whole-repository gate: ruff, formatting, mypy, and 3,467 pytest tests passed
  (9 skipped).
