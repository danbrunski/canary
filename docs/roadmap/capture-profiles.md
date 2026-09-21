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
- **Synchronous rows, not interleaved samples.** A capture is expected to
  read like a datalog: every channel at one instant, one row per sample.
  Multi-PID batching is most of the way there — up to 6 PIDs share a single
  Mode-01 request *and a single timestamp* — so it is on by default
  (`collection.obd_batch_max_pids`, narrowed automatically if an ECU
  refuses). Per-channel rate weights are the opposite idea and belong to
  background monitoring, where slow signals must not crowd out fast ones;
  in the capture editor they are an advanced control, off by default.
- **Open: a *round* is the operator's unit, not a batch.** With 12
  channels selected and a batch ceiling of 6, one logical sample spans two
  requests ≈ 3 ms apart and therefore two timestamps — but the operator
  reasonably expects all 12 values on one row, the way every datalogger
  (UpRev included, whose K-line channels are polled sequentially and
  stamped as one row) presents them. Batch-internal sharing is fixed; the
  cross-batch half is a design decision, see Phase 5.
- **Label both names.** Every operator-facing surface says "OBD PIDs
  (Mode 01)" and "Manufacturer DIDs (Mode 22)" — some pages spoke
  protocol, others spoke common name, and the reader had to know they
  were the same thing.
- **Budget honesty**: profiles may raise the bus budget for bounded
  captures. The profile API and editor expose the resolved identifier-level
  plan, separating channel reads, diagnostic requests, raw frames, decoded
  rows, and complete tuples; batched and single-request fallback demand are
  reported independently. Capture manifests persist that same plan. Profiles
  may override `priority_channels` (`null` inherits collection defaults;
  `[]` disables the lane), and infeasible priority or total demand is visible
  before capture. Exploratory support walks additionally obey
  `collection.obd_discovery_budget_requests_per_second`. Watch
  `storage.queue_depth` — the 25 req/s overload incident (2026-05) was a
  writer bottleneck, not a bus fault.
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
  problem in archive/dashboard-ux.md), profile dropdown, Start/Stop. Reuses
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
  **Half closed 2026-09-20 — `B/FUEL SCHDL` yes, `A/F ALPHA` no.** UpRev's own
  stream definition places B-FUEL SCHDL at DID `0x1208`, and `raw x 0.00049`
  reproduces its displayed ms on 405/405 rows of the simultaneous CSV
  (`raw/2048` reproduces 360/405 and is refuted); the provider is promoted to
  confirmed. The same capture identified `0x1102`/`0x1125` as UpRev's
  `LT Fuel Trim B1/B2` — but those are **not** alpha: they sit flat at steady
  idle (one change in 82 rows) and step between learned cells, where alpha by
  this task's own signature oscillates around 100 %. The short-term correction
  still has no DID. See `data/analyses/2026-09-20-uprev-usb-road.md`.
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
- [x] **4.3 — Target-AFR reconstruction.** Derived channel from the
  ROM's target table (operator-exported grid, stored per vehicle
  profile + tune revision) looked up by logged rpm/load — TARGET AFR
  is not a CONSULT param and is not expected on CAN without
  ReadMemoryByAddress + security access.
  **Software complete; artifact blocked.** `TargetAfrMap` is strict,
  tune-revision keyed, validates grid shape/axes, performs bounded bilinear
  lookup, and emits `derived:TARGET_AFR` plus per-bank error. The actual ROM
  target grid has not been exported into the scoped calibration file, so no
  target values are invented from the opportunistic log.
  **Closed 2026-09-21 by a different route than the one written here.** The
  parenthesis above was right: the target is reachable by ReadMemoryByAddress,
  and it needed no security access. The ECM's FINAL commanded equivalence
  ratio is a Q14 word at RAM `0xFFFF26BE`, catalogued as
  `ECM_TARGET_EQUIV_RATIO` (`data/dids.yaml`, `memory:`) and polled as
  `mem:ECM_TARGET_EQUIV_RATIO`. Evidence: 314 samples spanning phi
  1.000-1.1641 (14.70-12.63 AFR) in `_capture-fuel-target-research-
  2026-09-20T224503`, and `build_tuning_frame` reporting `commanded_afr:
  available`. That is the commanded target after every enrichment and cut,
  which a table lookup by logged rpm/load could only approximate. The
  `TargetAfrMap` grid path stays as a cross-check against the ROM.
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
  **2026-09-21 data point, not a closure:** the all-DID `rom-trace` profile
  (19 DIDs + 5 memory windows, 184 req/s budget) ran in motion with all 22
  requests at their configured rates — 8 Hz fast, 1 Hz slow, one timeout in
  ~2,700 requests. It lasted four minutes before another agent's daemon
  restart ended it, so there is still no WOT-pull shakedown and no per-band
  sample count. The `fuel-map` profile itself remains unexercised.
- [~] **4.5 — AFR-error surface from a capture session.** Run the
  engine-tuning-support 2.1 binned surface on a `_capture-fuel-map-*`
  session end-to-end: rpm × load cells, error vs reconstructed
  target, per bank — the CANary-native version of the 2026-07-31
  UpRev log analysis.
  **Software complete; data blocked.** `afr_error_surface` produces per-bank
  RPM × load median/IQR/count/sparse cells and excludes censored samples.
  End-to-end execution waits on 4.3's ROM grid and 4.4's first real
  `_capture-fuel-map-*` session.
  **4.3 no longer blocks this (2026-09-21).** With the commanded target read
  directly, `delivery_error_from_semantic_frame` ran end to end on an all-DID
  capture for the first time: 257 grid cells, 234 removed as not-enrichment,
  23 remaining at +2.0 % — inside the ±2.4 % per-cell floor, so a
  demonstration that the chain executes, not a finding. The rpm x load
  *surface* on a real pull capture is still owed, and waits only on 4.4.

## Phase 6 — Running a capture (operator ergonomics)

- [x] **6.1 — Pause / resume.** `/control/capture/pause` suspends polling
  without closing the session, so a resumed capture continues the same log
  rather than starting a new one. Paused means *stop asking* — there is no
  point spending bus budget on rows nobody wants. Any profile switch
  (start, stop, rollback) clears it, and it does not survive a restart:
  a daemon should come back collecting, not silently paused.
- [x] **6.2 — Spacebar hotkey.** Pause/resume from the keyboard, since a
  capture is run between pulls with hands busy. Ignored while focus is in
  an input, textarea, select, button or contenteditable — a space typed
  into the tune-revision note must not stop recording.
- [x] **6.3 — Live recording readout.** RECORDING/PAUSED, rows written to
  this session, and the current value of every captured channel. Answers
  "is it logging?" without waiting for a flush or opening the file.
  `rows_written` counts rows accepted into the *current* session and
  resets on every rotation.
- [x] **6.4 — "End capture" rather than "Stop".** Stopping is not a save
  step: the writer flushes on its own timer, so the log so far is already
  on disk. The panel says so.

## Phase 5 — Sweep mode: one row per sample (design decision owed)

Batching fixed the *within-a-batch* case: the PIDs in one request now
share one timestamp (previously each took its own clock read while the
shared payload was decoded — `test_every_pid_in_a_batch_lands_on_one_timestamp`).
Beyond six channels a sample still spans several requests, so the rows
don't line up, which is the thing an operator actually consumes.

The scheduler is event-driven — each request carries its own due time and
is issued when the budget allows — so there is no "round" object to hang a
shared timestamp on. Three ways to get one, in ascending cost:

- [ ] **5.a — Round token, true timestamps kept.** The loop marks the
  batches issued in one pass over a capture profile's channel list with a
  shared `sample_id`; readings keep their real acquisition time, and
  query/export pivots on the id. Nothing is falsified and rows become
  trivial to build, at the cost of a new column consumers must know about.
- [x] **5.b — Round timestamp, acquisition time preserved alongside.**
  Every reading in a round is stamped with the round's start; the true
  acquisition instant moves to a second field. Rows line up with no
  consumer changes. This is what dataloggers do, and the ~3 ms it papers
  over is well inside the resample grid — but it does mean the primary
  timestamp is a scheduling artifact rather than a measurement.
  **Done.** `CaptureProfile.round_timestamps` (on for `fuel-map`, off for
  `default` until multi-pid-polling.md 4.3 validates the arrival pattern
  the resampler sees). The true instant rides along as `acquired_at` in
  reading metadata, so scheduler drift stays diagnosable. Poll *tiers* are
  suspended in round mode for the same reason weights were demoted: they
  are a monitoring optimization, and a channel on a slower tier could
  never share a round with the rest of the operator's selection.
- [ ] **5.c — True sweep scheduling.** A capture profile stops using
  per-channel due times entirely: poll every enabled channel back to back
  (`ceil(n/6)` requests), stamp the round, sleep to the next round.
  Simplest mental model and exactly the datalogger contract, but it is a
  second scheduling mode inside `run_can_obd_loop`, which already carries
  backoff, DID windows, budget pacing, freeze frames and DTC polling.

Recommendation: **5.b**, with 5.c later if per-channel due times prove to
be the wrong shape for captures generally. 5.a is the most conservative
and the least useful — it moves the grouping problem onto every consumer.
Operator decision owed before implementing.
