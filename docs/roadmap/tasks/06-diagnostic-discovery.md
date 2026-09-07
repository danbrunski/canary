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

- [ ] C06.1 Inject numbered broadcast frames between support responses and
  during response waits. Reproduce missing frames in startup and Mode 06 retry
  discovery; distinguish an untapped consumed frame from kernel/queue loss.
- [ ] C06.2 Establish one receive/tap ownership path for the daemon bus.
  Every received frame must reach raw capture exactly once, whether consumed
  by telemetry polling, discovery, session control, response-pending handling,
  multiframe assembly, or a retry.
- [ ] C06.3 Reuse the existing client and ISO-TP implementation through that
  path. Do not create concurrent readers on the same Bus or a second response
  parser solely for discovery. Keep standalone client use compatible.
- [ ] C06.4 Convert support discovery to bounded scheduler work, with stop
  and reconnect handling. Charge each actual request to the effective budget
  and obey the current diagnostic session's eligibility. Do not run Mode 06
  discovery inside a continuous DID window merely because a DID answered.
- [ ] C06.5 Preserve multi-responder support-map merging, conditional group
  traversal, quiet-window behavior, negative-response distinction, and sparse
  support-map handling. A timeout must not be persisted as proof of permanent
  lack of support.
- [ ] C06.6 Keep unknown/unsupported exploratory probes out of capture-critical
  slots where possible. Expose pending discovery and discovery cost instead
  of silently delaying selected telemetry. Any cached capability must be
  scoped to ECU identity/vehicle and invalidatable.
- [ ] C06.7 Specify eligibility for DTC, Mode 06, freeze-frame, custom PID,
  and forced-poll work for each profile/session. Honor physical ECU session
  requirements; explicitly report deferred/ineligible requests. Preserve
  queued one-shot work until a compatible phase, without inventing permission
  to exit a deliberately continuous capture.
- [ ] C06.8 Sweep `CanObdClient` discovery callers, collector startup/retry,
  recorder, control requests, and probe tools. Add request/frame/discovery
  accounting without per-frame logging overhead.

## Regression and acceptance checklist

- [ ] C06.9 Assert every injected raw frame is delivered exactly once for
  success, timeout, NRC, sparse maps, multiple responding ECUs, and reconnect.
- [ ] C06.10 Verify discovery respects budget and session gates, yields to
  due critical telemetry, and stops promptly. Test a selected DID capture with
  pending Mode 06 discovery and a focused Mode 01 capture with a DTC-triggered
  freeze-frame request.
- [ ] C06.11 Extend collector/client/trigger-poll/control tests; verify service
  requests retain their identifiers and per-ECU response provenance.
- [ ] C06.12 On hardware, compare frame continuity and discovery counters
  around startup/recovery. Read vehicle state first; no received-byte count
  alone proves no frames were lost upstream.
- [ ] C06.13 Acceptance: discovery cannot silently consume raw capture data
  or bypass session/budget policy, and deferred diagnostics are visible.

## Handoff

- [ ] C06.14 Document the receive-ownership invariant and diagnostic-mode
  eligibility matrix, with regression evidence. Apply the shared validation gate.
