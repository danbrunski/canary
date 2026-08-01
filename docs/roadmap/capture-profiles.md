# Roadmap — capture profiles: operator-selectable collection modes

Status: **implemented; truck follow-ups remain** (2026-07-31). Straight to `main`, no feature
branches. Not yet queued in `OVERNIGHT-QUEUE.md` — operator decides
ordering.

## Why this exists

UpRev's K-line logger is unreliable (dropped columns, flaky sessions),
and the 2026-07 fuel-map campaign showed CANary can replace it for
*measurement* if collection can be shaped per task: the co-logged
2026-07-27 drive caught 23 throttle≥70% samples via the normal rotation
vs UpRev's 537, and `src/collection/tuning_profile.py` already proved
the fix (narrow the channel set, raise the bounded budget; the bus
sustains 667–1387 reads/s vs the 8 req/s default).

What's missing is the operator-facing half: today `tuning_profile` is
one hard-coded channel set toggled process-wide, invisible in the UI,
and its captures land in the normal session stream. The operator asked
for (2026-07-31):

- A dashboard **tuning/logging interface**: default mode = current
  collection (CAN broadcast + whatever OBD/DID rotation bandwidth
  allows, feeding ML anomaly detection); a button to **start a tuning
  session with a selected profile** of PIDs/DIDs; stop (or radio
  toggle) returns to default mode.
- Tuning sessions write a **separate log file** (own session
  directory).
- The **default anomaly-detection channel set is itself configurable**
  through the same interface.
- Profiles are **named and saveable** — the operator can create,
  edit, and keep multiple logging profiles.

## Guiding constraints

- **Naming**: call these **capture profiles**. "Profile" alone already
  means per-VIN vehicle profiles (`src/profiles.py`) — do not overload.
- **Corpus isolation**: non-default captures default to a
  `_capture-<profile>-<ts>/` session directory (leading `_` excludes it
  from training discovery, live listing, retention — existing
  convention). A per-profile `corpus_eligible: true` opt-in lifts that.
- **Mode 22 ⊥ Mode 01**: holding the `10 C0` DID session silences
  standard OBD (see `data/dids.yaml` access notes). A profile that
  includes DIDs therefore *owns* that trade explicitly instead of the
  scheduler interleaving blackouts. The 0xC0 in-motion side-effect A/B
  (parked first) from `data/analyses/2026-05-16T223328.md` remains a
  prerequisite for DID-heavy driving profiles.
- **No delay the ECU doesn't ask for.** The ECM answers a request in
  ~1.5 ms, so every multi-second number in the window scheduler is
  policy: the 60 s cadence is a *background-monitoring* choice (now
  overridable per profile via `did_interval_seconds`), and the lapse
  exists only because the session had to expire on its own — with
  `exit_session` declared, a window ends by handing the bus back
  explicitly. Slowness belongs in configuration, where an operator can
  see and change it, never baked into the scheduler.
- **Label both names.** Every operator-facing surface says "OBD PIDs
  (Mode 01)" and "Manufacturer DIDs (Mode 22)" — some pages spoke
  protocol, others spoke common name, and the reader had to know they
  were the same thing.
- **Budget honesty**: profiles may raise the bus budget for bounded
  captures, but watch `storage.queue_depth` — the 25 req/s overload
  incident (2026-05) was a writer bottleneck, not a bus fault.
- **Model-input guardrail**: editing the *default* profile can starve
  the behavior models. Validation must cross-check the default
  profile's channel set against channels required by active
  `behavior_models.yaml` recipes and warn (not block) when removing
  one, naming the affected models.
- Vehicle-general: profiles are data (per vehicle profile scope), not
  code. No Xterra-isms in shared paths.

## Phase 1 — Profile model + persistence (overnight-eligible)

- [x] **1.1 — `CaptureProfile` schema.** Name, description, Mode-01
  channel set with per-channel rate weights, Mode-22 DID set, bus
  budget override, broadcast decode on/off, `corpus_eligible`,
  `storage_prefix`. Pydantic, strict. Reserved name `default`.
- [x] **1.2 — Persistence.** `capture_profiles.yaml` under the active
  vehicle profile's scope dir (survives redeploys, travels with the
  vehicle). Seed with two built-ins: `default` (mirrors today's
  rotation exactly) and `fuel-map` (Phase 4).
- [x] **1.3 — Default-profile indirection.** The collector reads its
  rotation from the `default` capture profile instead of hard-coded
  channel lists; behavior is byte-identical when the seeded default is
  untouched. Includes the model-input guardrail validation above.
- [x] **1.4 — Generalize `tuning_profile.py`.** The runtime gate keeps
  its shape (process-wide, `/status`-visible, restart-free) but
  activates an arbitrary named profile rather than the one hard-coded
  set. The existing hard-coded set becomes the seeded `fuel-map`
  starting point.

## Phase 2 — Session lifecycle + control surface (overnight-eligible)

- [x] **2.1 — Capture session start/stop.** `/control/capture`
  (start: profile name → rotates to a new session directory with the
  profile's storage prefix, activates the profile; stop: reverts to
  `default`, rotates back to a normal session). Sidecar metadata
  records profile name, snapshot of its channel set, and active
  ROM/tune revision note if provided (ties into engine-tuning-support
  4.2 flash provenance).
- [x] **2.2 — `/status` + MCP.** `capture_profile` field in `/status`
  (name + started_at + session dir); MCP tools
  `list_capture_profiles` / `start_capture` / `stop_capture` so agent
  sessions can drive captures the way they drive replay.
- [x] **2.3 — CRUD API.** Create/update/delete profiles over
  `/control/capture/profiles`; `default` is editable but not
  deletable; validation from 1.3 runs on every save.
- [x] **2.4 — Safety net.** Capture state does not survive daemon
  restart (restart → `default`), mirroring `tx_gate` semantics. A
  stuck DID-holding profile must never become a deployment posture.

## Phase 3 — Dashboard UI

- [x] **3.1 — Capture panel.** Mode indicator (always visible when a
  non-default profile is active — same lesson as the invisible-replay
  problem in dashboard-ux.md), profile dropdown, Start/Stop. Reuses
  the token-gated panel pattern.
- [x] **3.2 — Profile editor.** Channel picker sourced from the live
  supported-channel/DID inventory (not a hard-coded list), rate
  weights, budget override, corpus-eligibility toggle, save / save-as
  for named profiles. Editing `default` surfaces the model-input
  warnings inline.
- [x] **3.3 — Session list integration.** Capture sessions appear in
  the sessions panel tagged with their profile name despite the `_`
  prefix (listing currently hides `_` dirs — show them under a
  "captures" filter rather than mixing with drive sessions).

## Phase 4 — The `fuel-map` profile earns its keep (needs truck)

The first real profile, closing the UpRev-replacement gaps identified
2026-07-31. Cross-links: `engine-tuning-support.md` Phases 2–3,
`multi-pid-polling.md` 3.3, `data/dids.yaml`.

- [~] **4.1 — Identify `A/F ALPHA-B1/B2` and `B/FUEL SCHDL` among the
  138 responder DIDs.** Signature matching via the physical-action
  protocol (alpha oscillates ~100 % closed-loop, pins in open loop;
  B/FUEL SCHDL tracks MAF/rpm and the injector-PW candidate DID), or
  cross-correlation against an opportunistic UpRev co-log
  (`tools/import_uprev_log.py` + cross-reference tooling).
  **Blocked on a simultaneous physical-action DID capture.** The repository
  has the 138-responder four-state snapshots and a synchronous UpRev log, but
  no time-aligned raw-DID stream from that drive; correlating unrelated
  timestamps would fabricate an identification.
- [x] **4.2 — V→AFR transfer.** Export the A/F sensor transfer table
  from the UpRev ROM editor; decode `ECM_AF_SENSOR_V_B1/B2` (and/or
  Mode-01 λ PIDs 0x24/0x25) to AFR as a derived channel. Note the
  10.0 floor: flag pegged samples as censored, don't average them.
  Landed in `src/analysis/afr.py` and the profile-scoped
  `data/nissan-xterra-2008/afr_calibration.yaml`. The transfer was recovered
  from 1,212 synchronous voltage/AFR pairs in the 2026-07-29 UpRev co-log;
  both banks independently agree. Mode-01 lambda is preferred when it carries
  data — a present-but-empty column (bank-2 lambda is unsupported on this ECU)
  falls through to the voltage DID rather than shadowing it. Both ends of the
  sensor's usable range are censored: the 10.0 rich floor and, above
  `censored_ceiling_afr` (18.0), decel fuel cut, which is no injection at all
  rather than a lean mixture and would otherwise read as a large lean error.
- [~] **4.3 — Target-AFR reconstruction.** Derived channel from the
  ROM's target table (operator-exported grid, stored per vehicle
  profile + tune revision) looked up by logged rpm/load — TARGET AFR
  is not a CONSULT param and is not expected on CAN without
  ReadMemoryByAddress + security access.
  **Software complete; artifact blocked.** `TargetAfrMap` is strict,
  tune-revision keyed, validates grid shape/axes, performs bounded bilinear
  lookup, and emits `derived:TARGET_AFR` plus per-bank error. The actual ROM
  target grid has not been exported into the scoped calibration file, so no
  target values are invented from the opportunistic log.
- [~] **4.4 — 0xC0 in-motion A/B** (parked first), then a WOT-pull
  shakedown of the profile: per-band sample counts vs the UpRev
  benchmark (537 samples/40 min), storage queue_depth clean.
  **Needs truck.** The live daemon was unreachable from this workspace on
  2026-07-31 (`GET /status` timed out), so `vehicle_state` could not be
  established and neither the parked A/B nor WOT pull was attempted.
  The seeded `fuel-map` profile deliberately ships **no** `bus_budget_override`
  — channel narrowing alone is the safe half of the win. Raising the budget is
  this task's job, stepped by hand against
  `telemetry_sinks.storage.queue_depth`: 25 req/s dropped 2491 records on
  2026-07-29 and the writer-side root cause was never established (batching
  landed in `312be7b` but is unvalidated live).
- [~] **4.5 — AFR-error surface from a capture session.** Run the
  engine-tuning-support 2.1 binned surface on a `_capture-fuel-map-*`
  session end-to-end: rpm × load cells, error vs reconstructed
  target, per bank — the CANary-native version of the 2026-07-31
  UpRev log analysis.
  **Software complete; data blocked.** `afr_error_surface` produces per-bank
  RPM × load median/IQR/count/sparse cells and excludes censored samples.
  End-to-end execution waits on 4.3's ROM grid and 4.4's first real
  `_capture-fuel-map-*` session.
