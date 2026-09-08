# C06 — Preserve capture while discovering and polling diagnostics

Priority: high for data completeness. Dependencies: coordinate session
eligibility with C04 and scoped loss reporting with C03.

## Problem and code entry points

`CanObdClient._request_many()` in
[can_obd_client.py](../../../src/collection/can_obd_client.py) receives unrelated
broadcast frames and discards them. `build_poll_requests()` performs startup
discovery through this client, and the active collector can retry Mode 06
discovery synchronously after ECU contact. These paths bypass the collector's
normal `on_frame` tap and scheduled request budget.

Diagnostic eligibility also spans two gates: `DidWindowController` and
[tuning_profile.py](../../../src/collection/tuning_profile.py). Focused Mode 01
profiles explicitly admit DTCs but can filter monitor and freeze-frame
requests by telemetry channel selection. Audit and make this policy explicit.

## Implementation checklist

- [x] C06.1 Inject numbered broadcast frames between support responses and
  during response waits. Reproduce missing frames in startup and Mode 06 retry
  discovery; distinguish an untapped consumed frame from kernel/queue loss.
  _done 2026-09-07: `ScriptedEcu` injects numbered broadcast frames between support responses and during waits and records every frame it hands out; before the fix the startup walk lost 8 of 136 handed-out frames from raw capture and the Mode 06 retry 13 of 155 (consumed untapped, not upstream loss); both now 0._
- [x] C06.2 Establish one receive/tap ownership path for the daemon bus.
  Every received frame must reach raw capture exactly once, whether consumed
  by telemetry polling, discovery, session control, response-pending handling,
  multiframe assembly, or a retry.
  _done: `CanObdClient.receive_frame()` is the only `bus.recv` site while the loop owns the bus, invoking the `on_frame` tap before any id filtering; loop and client share one `_FrameTap`; verifier's injection matrix (startup walk, Mode 06 retry, multiframe ISO-TP with flow control, response-pending 0x78, batched Mode 01, session control 0x10, reconnect) all exactly-once with zero duplicates._
- [x] C06.3 Reuse the existing client and ISO-TP implementation through that
  path. Do not create concurrent readers on the same Bus or a second response
  parser solely for discovery. Keep standalone client use compatible.
  _done: discovery reuses the client's single-frame parser and quiet-window merge through `receive_frame`; no second reader or parser; standalone `CanObdClient(on_frame=None)` unchanged._
- [x] C06.4 Convert support discovery to bounded scheduler work, with stop
  and reconnect handling. Charge each actual request to the effective budget
  and obey the current diagnostic session's eligibility. Do not run Mode 06
  discovery inside a continuous DID window merely because a DID answered.
  _done: startup Mode 01/06 discovery is loop-driven `SupportWalk` work — one probe per budget slot, matrix-gated by session phase, stop-aware (returns within one 50 ms slice), reconnect resumes from the registry; Mode 06 never probes inside a continuous DID capture (`deferred_reason: continuous_did_capture`). Each single probe request/response remains synchronous on the loop thread like every poll; `build_poll_requests(client, spec)` still discovers synchronously for standalone callers only._
- [x] C06.5 Preserve multi-responder support-map merging, conditional group
  traversal, quiet-window behavior, negative-response distinction, and sparse
  support-map handling. A timeout must not be persisted as proof of permanent
  lack of support.
  _done: multi-responder maps merged (0x7E8 + 0x7E9), conditional group continuation, quiet window, NRC vs timeout distinguished; ending NRCs complete the walk, busy NRCs and timeouts park with a 30 s re-probe and are re-armed by ECU contact; nothing persisted as unsupported._
- [x] C06.6 Keep unknown/unsupported exploratory probes out of capture-critical
  slots where possible. Expose pending discovery and discovery cost instead
  of silently delaying selected telemetry. Any cached capability must be
  scoped to ECU identity/vehicle and invalidatable.
  _done: under an active profile discovery uses idle slots only and reports `capture_at_capacity` when there are none; `discovery` metrics (pending, requests/seconds spent, per-item last result); registry keyed by `vehicle_key` with `cache_version`, `GET /control/obd_discovery`, `POST /control/obd_discovery/reset`._
- [x] C06.7 Specify eligibility for DTC, Mode 06, freeze-frame, custom PID,
  and forced-poll work for each profile/session. Honor physical ECU session
  requirements; explicitly report deferred/ineligible requests. Preserve
  queued one-shot work until a compatible phase, without inventing permission
  to exit a deliberately continuous capture.
  _done: `src/collection/request_eligibility.py` table (8 kinds × 4 profile classes × 4 phases, all 128 cells) is the single policy behind `tuning_profile.request_eligible` and `DidWindowController.request_verdict`; deferred/ineligible requests reported with reasons; forced polls and triggered freeze frames preserved until a compatible phase; a focused Mode 01 profile now admits a DTC-triggered freeze frame._
- [x] C06.8 Sweep `CanObdClient` discovery callers, collector startup/retry,
  recorder, control requests, and probe tools. Add request/frame/discovery
  accounting without per-frame logging overhead.
  _done: client discovery callers, collector startup/retry (`_retry_mode06_discovery` removed), recorder and probe tools (`on_frame=None`), and `POST /control/obd_request` (own bus handle, documented as bus-owner-exclusive) swept; `CanObdLoopStats.discovery_*` counters without per-frame logging._

## Regression and acceptance checklist

- [x] C06.9 Assert every injected raw frame is delivered exactly once for
  success, timeout, NRC, sparse maps, multiple responding ECUs, and reconnect.
  _done: exactly-once delivery asserted for success, timeout, NRC, sparse maps, multiple responders, reconnect (`tests/test_diagnostic_discovery.py`, 37 tests, 3× `-n auto` clean)._
- [x] C06.10 Verify discovery respects budget and session gates, yields to
  due critical telemetry, and stops promptly. Test a selected DID capture with
  pending Mode 06 discovery and a focused Mode 01 capture with a DTC-triggered
  freeze-frame request.
  _done: budget charging (8 req/s held with a 4 Hz profile, RPM cadence untouched), session gates, yield to due members, prompt stop; selected DID capture with pending Mode 06; focused Mode 01 capture with a DTC-triggered freeze frame served and Mode 06 deferred._
- [x] C06.11 Extend collector/client/trigger-poll/control tests; verify service
  requests retain their identifiers and per-ECU response provenance.
  _done: collector/client/trigger-poll/control tests extended; `ObdReading.responder_id` persisted in metadata for per-ECU provenance._
- [~] C06.12 On hardware, compare frame continuity and discovery counters
  around startup/recovery. Read vehicle state first; no received-byte count
  alone proves no frames were lost upstream.
  _pending hardware 2026-09-08: Pi still unreachable. Procedure in `docs/diagnostic-discovery.md` (read `vehicle_state`; compare raw-frame continuity and discovery counters around startup and recovery; a received-byte count alone does not prove no upstream loss)._
  _workstation half done 2026-09-08: `discovery-recovery` (added for this) is `normal-monitoring` plus an adapter fault at 7 s, so continuity and discovery counters can be read across a recovery, not just a clean startup. Frame continuity holds — the sequence ledger reconciles with **0 missing** on both sides and the push identity holds — which is exactly the claim a received-byte count cannot make. The counters do not: both runs report discovery `requests_spent: 3`, but the ECU counted **6** support probes after the reconnect (`cache_version` 2 → 4), so discovery's declared bus cost is per-walk while its actual cost is per-walk × generations. Recorded as finding 14 and deliberately **not** fixed — making the counters cumulative crosses the registry's intended reset semantics. Table in `docs/sustained-validation.md` ("Discovery across startup and recovery")._
  _pending hardware 2026-09-08: Pi unplugged (operator-confirmed). Kernel counters are `unavailable` on this host, and the upstream loss layer the item is about is precisely what only hardware can show; procedure in `docs/diagnostic-discovery.md`._
- [x] C06.13 Acceptance: discovery cannot silently consume raw capture data
  or bypass session/budget policy, and deferred diagnostics are visible.
  _done: single tapped receive site, budget wait reason, and matrix phase gate make silent consumption or policy bypass unrepresentable; deferred diagnostics visible in `/status`._

## Handoff

- [x] C06.14 Document the receive-ownership invariant and diagnostic-mode
  eligibility matrix, with regression evidence. Apply the shared validation gate.
  _done: `docs/diagnostic-discovery.md` (receive-ownership invariant, generated eligibility matrix, scheduling/caching rules, regression evidence, hardware procedure) + `docs/headless-api.md` routes. Gate green._
