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

DID batching is absent from the active scheduler, but builders/splitters,
`CanObdClient.query_mode_22_batch()`, and `sweep_dids_batched()` already exist.
[test_multi_did_batching.py](../../../tests/test_multi_did_batching.py) includes
an observed multi-DID request. Earlier roadmap claims that the ECM refuses all
DID batching are historical, not a reason to duplicate or discard this work.
Support must still be established for the actual selected DIDs and widths.

The vehicle's `fuel-did-research` capacity text assumes three DIDs per active
request, which the collector does not currently do. Capacity must describe
the actual path, not a capability available only in a probe tool.

## Implementation checklist: scheduling and demand

- [ ] C09.1 Build one resolved polling-plan representation for scheduler,
  profile preview, capture manifest, and quality checks. Resolve aliases and
  deduplicate by service, ECU, and identifier before computing demand.
- [ ] C09.2 Separate channel reads/s, diagnostic requests/s, raw frames/s,
  decoded rows/s, and complete tuples/s. Include session control and discovery
  overhead. Report best-case batched demand and single-request fallback demand
  separately; do not equate diagnostic request count with total bus load.
- [ ] C09.3 Improve Mode 01 batch occupancy using C02 round membership and
  compatible deadlines. Do not pull a slow channel to fast cadence merely to
  fill a batch, and do not delay a critical tuple to wait for optional context.
- [ ] C09.4 Keep mode/model-critical priority channels profile-configurable.
  Verify their demanded share is feasible and expose infeasible plans rather
  than assuming a small priority list cannot starve other requests.
- [ ] C09.5 Budget exploratory probes separately from confirmed capture
  work, retaining recovery for sparse support bitmaps and previously answering
  channels. Reuse current miss backoff; avoid permanent unsupported decisions
  based only on silence or one failed diagnostic session.

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
- [~] C09.7 Propagate DID cadence through absolute-rate resolution, due-time
  scheduling, profile editors, API/MCP models, demand calculation, quality
  floors, and manifest snapshots. Preserve operator-modified profiles during
  seed upgrades. Separate context rates from required fast-tuple floors.
  _partial: absolute rates, due-time scheduling, dashboard editor/types,
  strict API schema, manifest snapshots, backward-compatible seed loading, and
  capture-quality promises/floors are wired and tested. The shared resolved
  demand calculation does not exist until C09.1/C09.2 and remains._
- [ ] C09.8 Integrate existing multi-DID builders/splitters into the tapped
  scheduler request path. Scope capability by ECU/session, group only eligible
  same-round DIDs, and enforce the single-frame request limit of three DIDs.
- [ ] C09.9 Use measured full response payload widths for splitting, not a
  catalog signal's decoded slice width. Store width provenance with ECU/profile
  data. Unknown/truncated boundaries must not misattribute later bytes.
- [ ] C09.10 Preserve per-member accounting for partial responses, ECU
  responder identity, raw response bytes, acquisition time, request/round ID,
  and separate Mode 01/22 batch metrics. Shared receipt does not establish
  atomic internal ECU sampling.
- [ ] C09.11 Add capability failure/fallback behavior using the existing
  Mode 01 approach where appropriate. A rejected batch must not permanently
  suppress healthy individual DIDs. Recalculate effective demand and report
  quality shortfall when falling back to single requests.

## Implementation checklist: profile updates

- [ ] C09.12 For normal monitoring, measure useful critical-channel coverage,
  probe overhead, and batching before changing the default budget. Reuse
  validated passive providers explicitly where appropriate; preserve source
  provenance and the model inputs they actually supply.
- [ ] C09.13 For fuel-map, make the required fueling tuple the scheduling and
  acceptance unit. Retain vehicle-specific unsupported-PID exclusions and
  record tuple rate and skew, not just aggregate channel throughput.
- [ ] C09.14 For continuous DID profiles, identify fast dynamics and slow
  context from experiment requirements, lower context demand, and measure the
  resulting fast-channel coverage. Do not prescribe one vehicle's cadence to
  all vehicles.
- [ ] C09.15 For hybrid profiles, use C04's measured cycle capacity and retain
  cross-mode timing limits. Keep unsupported targets visibly experimental.
- [ ] C09.16 Correct capacity notes to state active transport, measured test
  duration/conditions, and actual request counts. Retain the old storage-loss
  incident as history, not a universal 24 request/s ceiling. Do not raise
  deployed rate defaults until C10 supplies sustained evidence.

## Regression and acceptance checklist

- [ ] C09.17 Test alias duplicates, heterogeneous rates, critical-channel
  fairness, Mode 01 fallback, DID batch widths, missing members, unknown widths,
  responder mismatch, partial desynchronization, and per-service capabilities.
- [ ] C09.18 Extend existing multi-PID/multi-DID, profile, scheduler, and
  capture-quality tests. Verify old YAML loads identically and edited built-ins
  are not overwritten.
- [ ] C09.19 Verify a known-answer decode through both single and batch paths
  before interpreting novel DID results. Record real selected-DID batch
  support and sustained collection on the Pi in C10.
- [ ] C09.20 Acceptance: requested and fallback demand match the actual
  resolved plan, context rates release measurable request capacity, batching
  preserves attribution, and promised rate changes pass persisted quality.

## Handoff

- [ ] C09.21 Record profile/schema migration, actual measured rate changes,
  capability evidence, and relevant roadmap checkbox updates. Apply the shared
  validation gate; leave unverified hardware targets incomplete.
