# Lossless diagnostic discovery and request eligibility

Roadmap task C06 (`docs/roadmap/tasks/06-diagnostic-discovery.md`). This is
the contract for *who receives frames* on the daemon's OBD bus, how support
discovery (Mode 01 PID groups, Mode 06 MID groups) is scheduled, cached and
reported, and which diagnostic request kinds may go out under which capture
profile in which session phase. It builds on `docs/capture-rounds.md` (C02
rounds), `docs/hybrid-scheduling.md` (C04 phases, `next_wake_at`) and
`docs/capture-quality.md` (C03 loss scopes).

## The defects this replaces

1. `CanObdClient._request_many()` / `_collect_response()` took frames straight
   off the bus and discarded anything that was not the awaited response.
   `build_poll_requests()` ran the whole Mode 01 / Mode 06 support walk
   through that client at loop start, and `_retry_mode06_discovery()` ran it
   again synchronously after ECU contact — outside the collector's `on_frame`
   tap and outside the request budget. Every broadcast frame that arrived in
   those windows was lost from raw capture, and nothing counted it.
2. Eligibility lived in two places with two vocabularies: `DidWindowController`
   answered by phase with a hard-coded kind list, `tuning_profile.request_eligible`
   answered by channel selection with `dtc` special-cased inline. Whether a
   focused Mode 01 capture would serve a Mode 06 sweep or a DTC-triggered
   freeze frame was an accident of which branch ran first.

### Regression evidence (fake clock, `tests/test_diagnostic_discovery.py`)

`ScriptedEcu` now injects numbered broadcast frames (id `0x280`, payload =
sequence number) around every reply — one before the response lands, the
rest inside the quiet window or the timeout wait — and records every frame
`recv` hands out. The fake never drops a frame, so a handed-out frame that
never reached `on_frame` was **consumed untapped**, not lost upstream in a
kernel queue; the test names the missing sequence numbers.

| scenario | before | after |
|---|---|---|
| startup Mode 01 walk (`auto` PIDs, 2 groups, 3 injected frames per request) | **8 of 136** handed-out frames never reached `on_frame`: broadcast sequences 1–6 and both support responses | 124 handed out, 124 tapped, 0 untapped, 0 duplicated |
| Mode 06 walk silent at startup, retried after contact | **13 of 155** untapped: sequences 1–3 and 5–12 plus the responses | 155 / 155 / 0 / 0; probes at 0.000 s (silent), 0.263 s (re-armed by the first RPM answer), 0.317 s (group 0x20) |

Both are asserted exactly-once (no frame missing, none duplicated, none
foreign) for: success, a silent group (timeout), a refused group (NRC 0x11),
a sparse map, two responding ECUs (`0x7E8` + `0x7E9`), a hybrid capture, and
a reconnect that resumes a half-finished walk.

## Receive-ownership invariant

**While `run_can_obd_loop` owns a bus, every frame taken off it reaches
`on_frame` exactly once, whatever was waiting for it.**

- `can_obd_client.receive_frame(bus, timeout, on_frame)` is the one place a
  frame is taken off an OBD-owning bus. It hands the frame to the tap
  *before* any caller looks at the arbitration id.
- The collector's `_recv_and_tap` (polls, batches, session control, drains)
  wraps `receive_frame` with parse-error and `frames_seen` accounting.
- `CanObdClient(on_frame=…)` routes its own receives (`_request_many` for
  support probes, `_collect_response` for queries) through the same function
  with the same tap. The loop constructs its client with `_FrameTap` — the
  same callable that counts its own receives — so a probe waiting on the bus
  captures every broadcast frame that arrives meanwhile.
- No second reader: discovery reuses the client's ISO-TP single-frame parser
  and the client's quiet-window multi-responder merge; there is no discovery
  parser of its own. Each probe is a single synchronous call on the loop's
  thread; the client's receive slice is bounded (`RECV_SLICE_SECONDS`, 50 ms)
  and polls `should_stop`, so a stop or capture deadline ends a probe within
  one slice (`test_discovery_stops_promptly_during_a_silent_probe`: 50 ms
  after the stop, against a 1 s request timeout).
- Standalone use is unchanged: `CanObdClient` with `on_frame=None` (scripts,
  `scripts/preflight.py`, the VIN probe in `src/profiles.py`, the DID sweep
  transport in `src/analysis/did_scan.py`) receives as before, just in 50 ms
  slices.

### Callers swept (C06.8)

| caller | path | status |
|---|---|---|
| `run_can_obd_loop` startup discovery | loop-driven probes through the tapped client | converted |
| `_retry_mode06_discovery` | removed; the walk re-arms on contact | converted |
| `build_poll_requests(client, spec)` | `client=None` (the loop) builds without sending; a client (standalone) still discovers synchronously through `query_supported_*`, which now share the loop's walker (`discovery.walk_support`) | one parser |
| `record_can_obd` / `scripts/record.py --obd-source canable` | runs the same loop | converted with it |
| `can_collector.record` | pure sniffer, no request path | n/a |
| `POST /control/obd_request` (`src/control/obd_request.py`) | opens its **own** bus handle, never the daemon's; on SocketCAN every socket receives every frame, so the daemon's raw capture is unaffected; on a single-handle adapter (slcan) the open fails as `AdHocRequestBusError` rather than sharing the loop's handle. Bus-owner-exclusive by construction — documented, not routed through the loop. | documented |
| `src/control/can_activity.py` | own handle, receive-only, same model | documented |
| `scripts/preflight.py` `check_obd` | standalone client, `query_supported_mode_01_pids()` | unchanged API |

Accounting (`CanObdLoopStats`, no per-frame logging): `discovery_requests_sent`,
`discovery_responses`, `discovery_negative_responses`, `discovery_timeouts`,
`discovery_seconds`, `discovery_frames_seen` (frames tapped *while a probe
waited* — the frames the old path consumed). `requests_sent` still counts
every frame on the wire; `session_control.channel_requests_sent` now
subtracts discovery as well as session control.

## Discovery as scheduler work

`src/collection/discovery.py`. One `SupportWalk` per item (`mode01` when any
PID token is `auto`; `mode06` when `service_mids: auto` and the monitor poll is
enabled), pure data replaced on every probe:

- **One probe per budget slot.** The loop issues at most one support-group
  request per tick, charged to `next_budget_at` like any request, timed out by
  `request_timeout_seconds`, tapped, stop-aware.
- **Conditional traversal.** Group `k+1` is probed only when some responder
  set the continuation bit in group `k`. Every responder's bitmap is kept
  apart (`supported_by_responder`) and merged for the plan; the probe PIDs
  themselves are dropped.
- **Answered / refused / silent are three outcomes.** `answered` advances the
  walk. An NRC in `WALK_ENDING_NRCS` (`serviceNotSupported`,
  `subFunctionNotSupported`, `requestOutOfRange`) **completes** the walk at
  that group — a definitive answer. Any other NRC (busy, conditions not
  correct) and a timeout park the walk: `complete` stays `False`,
  `next_probe_at = now + DISCOVERY_RETRY_SECONDS` (30 s), and the first
  ordinary ECU answer re-arms it immediately (`on_contact`). **A timeout is
  never persisted as proof of unsupported.**
- **Installing results.** `mode01`: `apply_mode01_support` takes the claimed
  PIDs off the fast probe timeout — the only effect the Mode 01 bitmap has;
  every decoded PID is still probed directly (sparse-map handling unchanged).
  `mode06`: `mode06_requests(mids)` appended once, deduplicated by MID.
  A positive bitmap with zero bits completes with no MIDs and is not retried
  (it is an answer); the old "zero MIDs → retry after contact" case now
  applies only to silence.
- **Where the slot comes from (C06.6).** With no capture profile active
  (`default`), a due probe goes at the head of the line *between rounds* —
  what the synchronous startup walk did before the first poll (measured:
  groups 0x00 and 0x20 at 0.000 s and 0.055 s, then polling). With a capture
  profile active, a probe may only take a slot **no round member needs**:
  the previous round must be closed and the next boundary
  (`RoundScheduler.next_open_at`) at least one budget slot away. When the
  plan fills every round (`test_discovery_reports_pending_when_the_capture_is_at_capacity`:
  two PIDs at 4 Hz on 8 req/s), discovery never runs and is reported
  `capture_at_capacity`; the tuple's readings are untouched. Under a
  fuel-map-style tuple on 32 req/s, discovery completes with
  `rounds_incomplete == 0` and every probe after the first poll.
- **Session gating.** A probe is `discovery` in the matrix below: deferred in
  a Mode-22 window and lapse, ineligible under a continuous DID capture.
  `test_mode06_discovery_waits_for_the_mode01_phase_of_a_hybrid_capture`
  asserts no `06 xx` probe lands inside any held-session interval (entry to
  exit + grace); `test_continuous_did_capture_never_runs_mode06_discovery`
  asserts zero probes for the whole capture and
  `discovery.deferred_reason: continuous_did_capture`. **A DID answering does
  not make Mode 06 eligible** — the phase does.
- **Stop and reconnect.** Every wait still goes through `_wake_for` /
  `next_wake_at`; a pending probe waiting on the budget adds the budget slot
  as a wake candidate. Loop exit (`return` or raise) leaves the state in the
  registry; the next loop on the same vehicle resumes at the next unanswered
  group (`test_reconnect_resumes_a_walk_from_its_state`: first run answers
  0x00 and stops; second run probes only 0x20; third run probes nothing).

### Caching and invalidation

Process-wide registry keyed by vehicle identity: `CanObdSpec.discovery_key`,
set by the daemon (`collectors.py`) to `discovery.vehicle_key(vin, make,
model, year)` — the VIN when configured, else `make-model-year`. Standalone
specs (recorder, tests) leave it `None` and cache nothing. Responder identity
is inside the state (per-responder maps), so a vehicle whose transmission
module answers on `0x7E9` is not confused with one that does not.

Invalidation is a cache version: `discovery.reset_discovery()` bumps it and
clears the registry; a running loop notices on its next tick, restarts every
walk (results already installed stay — a re-walk only adds), and the
`cache_version` is reported. Control surface (HTTP only, no MCP tool):

- `GET /control/obd_discovery` — the registry: per vehicle, per item.
- `POST /control/obd_discovery/reset` — bump and clear; returns the new
  `cache_version`. A daemon restart also clears it (process memory).

### Metrics (`/status.obd_scheduler`)

`discovery`: `key`, `cache_version`, `pending: [items]`, `deferred_reason`
(the last *structural* reason since the previous probe — `session_window`,
`session_lapse`, `continuous_did_capture`, `paused`, `capture_at_capacity`;
`budget` / `round_open` only when nothing structural is in the way),
`requests_spent`, `seconds_spent`, and per item: `complete`, `pending_group`,
`groups_answered`, `attempts`, `last_result: answered|nrc|timeout`,
`last_nrc`, `next_probe_in_s`, `supported_count`, `responders`.

`deferred`: bounded (`MAX_DEFERRED_REPORT`, 64) list of
`{channel, kind, verdict, reason}` for every request that is wanted right now
(due and unsuppressed) but deferred or ineligible, plus `discovery:<item>`
rows while a walk waits. Dashboard types: `DiscoveryStatus`,
`DeferredRequestStatus` on `ObdSchedulerStatus` (`types/control.ts`); no
panel renders them yet.

## Eligibility matrix (C06.7)

`src/collection/request_eligibility.py`, `evaluate(kind, profile_class,
phase, *, selection, paused, passive, forced)`. Both gates are projections of
it: `tuning_profile.request_eligible` passes selection and pause with phase
`none`; `DidWindowController.request_verdict` passes the phase with no
selection (plus its own `attempted_this_window` for a DID already read in
the open window); the loop calls it once with everything
(`_loop_request_verdict`) for selection and for the `deferred` report.

Profile classes (`tuning_profile.classify_profile`): `default` (the seeded
profile mirroring config), `focused_mode01` (a non-default profile without
Mode 22), `mode22_owning` (a profile with enabled DIDs, windowed or bare),
`continuous_did` (`did_continuous: true`). Phases: `none` (no session
controller — bare DIDs), `mode01`, `window`, `lapse`.

`passive` → deferred `passive` and `paused` → deferred `paused` come first
for every kind. Then the table (generated from `matrix_rows()`):

| kind | profile class | none | mode01 | window | lapse |
|---|---|---|---|---|---|
| `pid` | default | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `pid` | focused_mode01 | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `pid` | mode22_owning | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `pid` | continuous_did | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) |
| `custom` | default | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `custom` | focused_mode01 | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `custom` | mode22_owning | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `custom` | continuous_did | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) |
| `did` | default | eligible | deferred (`awaiting_window`) | eligible | deferred (`session_lapse`) |
| `did` | focused_mode01 | ineligible (`profile_without_mode22`) | ineligible (`profile_without_mode22`) | ineligible (`profile_without_mode22`) | ineligible (`profile_without_mode22`) |
| `did` | mode22_owning | eligible | deferred (`awaiting_window`) | eligible | deferred (`session_lapse`) |
| `did` | continuous_did | eligible | deferred (`awaiting_window`) | eligible | deferred (`session_lapse`) |
| `dtc` | default | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `dtc` | focused_mode01 | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `dtc` | mode22_owning | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `dtc` | continuous_did | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) |
| `service` | default | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `service` | focused_mode01 | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `service` | mode22_owning | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `service` | continuous_did | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) |
| `freeze_frame` | default | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `freeze_frame` | focused_mode01 | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `freeze_frame` | mode22_owning | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `freeze_frame` | continuous_did | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) |
| `forced_poll` | default | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `forced_poll` | focused_mode01 | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `forced_poll` | mode22_owning | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `forced_poll` | continuous_did | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) |
| `discovery` | default | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `discovery` | focused_mode01 | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `discovery` | mode22_owning | eligible | eligible | deferred (`session_window`) | deferred (`session_lapse`) |
| `discovery` | continuous_did | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) | ineligible (`continuous_did_capture`) |

An `eligible` cell is then subject to the profile's **channel selection**
(`selection_applies`): always for `pid`, `custom`, `did`
(`not_selected`, `mode01_disabled`, `mode22_disabled`); for `service`
(Mode 06) only outside `default` — a focused or hybrid capture polls monitor
sweeps only when it names `MONITOR_RESULTS` or `AUTO`, the default profile
mirrors config. `dtc` is admitted by every profile (the 2026-07-27
post-flash drive), and `freeze_frame` rides that admission: a freeze frame is
one-shot work that fires only when the admitted DTC poll finds a stored code
(`test_focused_capture_serves_a_dtc_triggered_freeze_frame`, each frame PID
exactly once, `responder_id` on every reading, and the unselected `SPEED`
reported as `ineligible: not_selected` rather than dropped). This changes
the previous implicit behaviour, where a focused profile's channel list
silently filtered the triggered freeze frame out.

`forced` (`trigger_poll`) lifts the selection but not the phase: a forced
Mode 01-side channel is served in `none`/`mode01` and deferred through a
window; a forced DID still waits for its window (`awaiting_window`). The loop
keeps the forced index until the request is actually served, so a forced
request queued during a window survives until a compatible phase. Nothing in
the table exits a continuous DID capture to serve Mode 01-side work — those
cells are `ineligible`, never deferred-then-forced.

Physical requirements the table honours come from the profile and
`data/dids.yaml`, not from shared code: which sub-function opens the session,
the exit grace, and that this ECM is silent on Mode 01-side services while a
session is held (C04). The table only says *which phase* a kind needs.

## Provenance (C06.11)

`ObdReading.responder_id` (persisted as `metadata_json.responder_id`) carries
the answering module's id for every reading the Canable collector emits;
Mode 06 requests keep the MID on the wire (`06 <mid>`) and in the reading
(`MONITOR_RESULTS` payload begins with the MID); the discovery snapshot lists
`responders` per item. `tests/test_diagnostic_discovery.py::test_readings_carry_responder_provenance`.

## What remains synchronous, and why

- **A single probe is synchronous** on the loop's thread — one request, one
  bounded, sliced, stop-aware wait — exactly like every poll the loop issues.
  Conversion means the *walk* is scheduler work; the unit of work is still
  one request/response. Nothing else on the loop's path calls `bus.recv`.
- **`build_poll_requests(client, spec)` with a client** discovers
  synchronously for standalone callers (`scripts/preflight.py`, tests that
  build a plan from a static fake). Inside the loop it is always called with
  `None`; there is no synchronous discovery on the loop's bus at all.
- **Mode 22 capability** is not discovered by the loop (nothing did before
  either); the DID plan comes from config, the catalogue and profiles, and
  `src/analysis/did_scan.py` remains the standalone sweep tool.

## Hardware procedure (C06.12 — pending, orchestrator/operator)

Not attempted here. On the Pi, with the daemon on this commit:

1. `GET /status` — record `vehicle_state` first. `ignition_only` is enough
   for support discovery (KOEO answers Mode 01 group bitmaps); `engine_off`
   or `no_data` means the probes will time out and park, which is itself a
   result worth recording (`discovery.items.*.last_result: timeout`,
   `next_probe_in_s`).
2. Note `obd_scheduler.discovery` at rest: `pending`, `requests_spent`,
   `seconds_spent`, per-item `last_result`, `responders`, and
   `can_receive` / `can0.rx_dropped` from `/status`.
3. `POST /control/obd_discovery/reset`, then watch `discovery` every 5 s
   until `pending` is empty: expect `mode01` to complete in ≤ 7 probes and
   `mode06` in ≤ 8, at one probe per budget slot, with no `rounds_skipped`
   growth beyond one round per timed-out probe. Record `discovery_frames_seen`
   (frames received *during* probes — before this change those were the
   frames raw capture lost).
4. Arm `tuning-hybrid-research`, reset discovery again, and confirm no
   `06 xx` request appears inside a held session: `session_control.sent`
   grows, `discovery.deferred_reason` reads `session_window` / `session_lapse`
   during windows and `discovery` completes only in the Mode 01 phase.
5. Arm `tuning-did` (continuous) with a pending walk: `deferred_reason` must
   read `continuous_did_capture` for the whole capture and
   `discovery.requests_spent` must not move.
6. Pull the adapter and reconnect: the reconnect attempt must not re-probe
   groups already answered (`groups_answered` unchanged, `attempts` growing
   only for the pending group).
7. **No received-byte count alone proves no frames were lost upstream.**
   Compare `frames_seen` against `can0.rx_packets` from `ip -s link` over the
   same window and `rx_dropped` / `rx_over_errors` deltas; only a zero
   kernel-side drop delta plus equal counts is continuity evidence.

Not measured here: real SocketCAN behaviour of the 50 ms receive slice, the
ECM's actual support bitmaps and responder set, and what a probe timeout
costs a real round. Those are C06.12's and C10's.
