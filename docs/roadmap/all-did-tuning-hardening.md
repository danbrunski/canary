# Roadmap — Canary-native all-DID tuning capture hardening

Status: **proposed** (2026-08-05). Straight to `main`, no feature branches.
Not yet queued in `OVERNIGHT-QUEUE.md` — the operator decides ordering.
Task-level truth is this document's checkboxes.

Cross-links:

- `tuning-analysis-toolkit.md` — the completed toolkit this hardens
- `capture-profiles.md` — capture lifecycle and the `tuning-did` profile
- `engine-tuning-support.md` — the tuning campaign and its measurement gates
- `multi-pid-polling.md` — scheduler/rate substrate
- `data/dids.yaml` — identified Mode-22 channels and access rules
- `data/analyses/did-sweep-20260804-koeo-baseline.md` — measured session
  exclusivity, 658 reads/s capacity, and the 2026-08-05 drive-sweep findings

## Why this exists

The current tuning toolkit was built around a real Mode-01 capture and then
an all-DID capture profile was added after the ECU's session behavior was
measured. Those two designs do not meet in the middle yet:

- the Xterra ECM blocks Mode 01 for the entire `0xC0` Mode-22 session;
- switching out of `0xC0` does restore Mode 01 immediately with `10 81`, but
  every brief Mode-22 sweep caused selected Mode-01 channels to earn long
  backoff, costing about 40 seconds of useful data per 1.5-second sweep;
- therefore a high-rate tuning capture must stay in Mode 22 continuously;
- the tuning digest and delivery-error functions still require
  `obd:RPM`, `obd:ENGINE_LOAD`, `obd:FUEL_STATUS`, PID `0x24` lambda, and PID
  `0x44` commanded equivalence ratio, all of which are absent by design in an
  all-DID capture;
- UpRev's K-line logger is too unreliable to be a required companion. It may
  stall during a drive with no recovery path. A successful UpRev log is useful
  cross-validation, never a prerequisite for capturing or analyzing a drive.

The review also found correctness gaps around capture metadata, tune revision
propagation, rate verification, arming rollback, noise-floor applicability,
session selection, and the confidence claimed for the cam-angle DIDs. This
roadmap fixes them as one system: capture, metadata, semantic decoding,
analysis, and reporting must share the same contracts.

## Pre-made decisions — do not reopen

These are operator-approved architectural decisions, not questions for an
implementer to revisit:

1. **Tuning captures are all-DID.** Hold the Mode-22 session for the complete
   capture. Do not reintroduce Mode-01/Mode-22 interleaving, periodic windows,
   or a scheduler tolerance layer for the resulting gaps.
2. **Passive CAN remains available.** Broadcast CAN costs no diagnostic
   request budget and is part of a tuning capture whenever the profile enables
   broadcast decoding.
3. **UpRev is optional.** No task, acceptance gate, or normal report may
   require a simultaneous UpRev log. If one succeeds, store it as an
   independent instrument and use it for additional validation.
4. **Analysis asks for meanings, not namespaces.** Callers request semantic
   roles such as RPM, measured AFR, commanded AFR, and fueling-active. One
   shared resolver selects the vehicle/profile-appropriate DID, passive-CAN,
   or artifact-derived source and records its provenance.
5. **Missing evidence removes one conclusion, not the report.** A capture
   without commanded AFR still reports pull inventory, measured AFR, injector
   PW, cam behavior, and achieved rates. It refuses only delivery error.
6. **ROM artifacts are first-class inputs.** When a commanded quantity is a
   calibration table rather than a live parameter, resolve it from the exact
   tune revision recorded with the capture. Never infer a tune revision from
   time when explicit capture metadata exists.
7. **Canary is a relative instrument until independently calibrated.** The
   current voltage-to-AFR table was fitted against UpRev's conversion. It is
   useful for consistent Canary-only A/B work but is not independent proof of
   absolute AFR. An aftermarket wideband or a newly identified independent
   ECM value may improve that later without blocking this roadmap.
8. **Vehicle facts stay in profile data.** The Xterra's `0xC0` session,
   `10 81` exit, DID names, signal priorities, target maps, and physical-action
   protocols belong under its vehicle profile/data. Shared code implements
   contracts that work for other vehicles and for TCM-equipped vehicles.

## Defect inventory this roadmap must close

| defect | current effect | owning phase |
|---|---|---|
| all-DID profile vs Mode-01-only analysis | flagship capture produces no WOT/delivery result | 2–3, 5 |
| voltage DID is not transformed by `delivery_error` | selecting the DID can treat volts as AFR | 2–3 |
| writer creates `capture_profile.json`; digest reads `control.json` | profile and explicit tune revision are lost | 1 |
| public analysis tools do not pass the tune registry | revisions return unknown; unknown/unknown reads as same | 1 |
| UpRev importer does not stamp tune revision | imported ground truth cannot be grouped reliably | 1 |
| 60-second rate divided by 60 after a 20-second wait | correct rate is under-reported and blended with pre-capture reads | 6 |
| MCP arming is two non-transactional HTTP calls | budget failure leaves a partially armed capture | 6 |
| disarm restores and then clears the prior budget | operator override is lost | 6 |
| stop response rotates before preserving capture path | MCP cannot return the ended capture directory it promises | 6 |
| noise-floor conditions are prose/defaultable | WOT floor can still gate idle/all-condition data | 7 |
| digest applies a per-cell SD to a band-average delta | `delta_sigma` compares different statistics | 7 |
| capture session start parser accepts only plain names | anomaly/ledger windows collapse to `end..end` | 1, 5 |
| live resolver infers active state from directory timestamps | current replay names work, but writer state and future isolated modes can silently diverge from naming rules | 8 |
| cam DIDs are named commanded/measured/mirror too strongly | cam tracking may compare the wrong physical quantities | 4 |

## Phase 1 — One authoritative capture manifest and tune identity

- [x] **1.1 — Typed capture-manifest reader.** Add one shared reader for
  `capture_profile.json`, returning profile name, profile snapshot, capture
  start, explicit tune revision, and storage/corpus properties. Reuse it from
  `src.control.sessions`, the tuning digest, catalog indexing, MCP session
  details, and any future tuning-frame builder. Do not make each caller know a
  sidecar filename or JSON shape.
  Acceptance: a session created through `ParquetTelemetryWriter.enter_capture_mode`
  resolves the same profile and revision through all consumers.

- [x] **1.2 — Define metadata precedence.** The capture sidecar's explicit
  `tune_revision` is authoritative. A revision registry lookup by session time
  is a fallback only when the sidecar field is absent. If both exist and
  disagree, surface a metadata conflict and refuse cross-revision comparison;
  never silently choose the timestamp inference.

- [x] **1.3 — Make tune identity tri-state.** Replace the current
  `same_revision = left == right` behavior with `true | false | unknown` (or
  an equivalent enum). `None`/`None` means unknown, not same. Comparison
  payloads carry both resolution source (`capture_manifest`, `registry`,
  `import_manifest`, `unknown`) and revision value.

- [x] **1.4 — Connect the registry to every public analysis surface.** CLI
  `delivery-error`/`compare`, MCP `get_delivery_error`/
  `compare_tuning_sessions`, and digest construction load the active vehicle's
  registry once and pass it through. Sweep all callers of `delivery_error`
  and `compare_sessions`; this is a contract change, not a per-tool patch.

- [x] **1.5 — Stamp imports without making UpRev required.** Extend
  `import_uprev_log.py` with `--tune-revision` and registry inference as a
  fallback; record value + resolution source in the manifest. Import remains
  useful without a revision, but comparisons then report revision unknown and
  cannot claim a same-tune null run.

- [x] **1.6 — Require profile + revision for automatic previous-capture
  selection.** `_previous_capture` must return no automatic comparison when
  either identity is unknown. It must read the real capture manifest, match an
  exact capture-profile name, match an exact tune revision, and order by
  `src.control.sessions.session_started_at`. A manually requested comparison
  remains available with explicit warnings.

- [x] **1.7 — Use the shared session timestamp parser everywhere.** Replace
  `src.analysis.digest._session_start`'s plain-directory parser with
  `src.control.sessions.session_started_at`. Sweep other session-name parsers.
  Acceptance: ordinary, `_capture-*`, and `_tuning-*` digests use the real
  start for anomaly and ledger windows.

- [x] **1.8 — Add production-shaped fixtures.** Tests that claim capture
  behavior create the session through the real writer or at least write the
  real `capture_profile.json`; a directory whose name merely begins with `_`
  is not sufficient proof of the metadata path.

## Phase 2 — Canonical semantic tuning frame

- [x] **2.1 — Define semantic roles.** Add a small typed vocabulary used by
  tuning analysis, initially:

  - `rpm`
  - `operator_demand`
  - `airflow_or_table_load`
  - `measured_afr_b1` / `measured_afr_b2`
  - `commanded_afr`
  - `fueling_active`
  - `injector_pw_b1` / `injector_pw_b2`
  - `coolant_temp`
  - `ignition_timing`
  - `cam_command`
  - `cam_measured_b1` / `cam_measured_b2`

  These are analysis roles, not new telemetry source names.

- [x] **2.2 — Profile-scoped provider registry.** Store candidate/confirmed
  providers and transforms under the vehicle profile, e.g. DID RPM first and
  passive broadcast RPM as a cross-check. A provider declares source spec,
  transform/artifact, unit, confidence required for automatic use, and whether
  it is independent or derived from another instrument. Do not hard-code the
  Xterra's DID names in shared analysis code.

- [x] **2.3 — `build_tuning_frame()`.** Build one aligned frame from semantic
  roles and return both values and a capability/provenance report. It routes
  through `read_channels`/`read_aligned`, AFR calibration, ROM target lookup,
  and the capture manifest. The output records the selected provider for every
  role and why a higher-priority provider was unavailable.

- [x] **2.4 — Section-level capability gates.** Define required roles per
  conclusion:

  - pull inventory: RPM + physical operator demand;
  - measured-AFR trace: RPM + measured AFR;
  - delivery error: measured AFR + commanded AFR + fueling active;
  - cam tracking: confirmed command + confirmed measured position;
  - spark comparison: ignition timing + matching axes;

  A missing role produces a named unavailable section, never an empty frame
  that looks like “nothing happened.”

- [~] **2.5 — Make all-DID the reference adapter.** DID roles now drive the
  all-DID tuning frame and digest without Mode-01 fallback; passive-CAN
  cross-check providers remain to be added. The adapter must resolve RPM,
  coolant, A/F voltages, injector PW, throttle, and cam candidates from DIDs
  plus passive CAN.

- [x] **2.6 — Preserve acquisition time.** Logical round timestamps may be
  used for table presentation, but dynamic analysis (cam command/response,
  cross-source alignment, lag) uses the true per-reading `acquired_at` time.
  If `acquired_at` is not persisted in Parquet today, add it or preserve an
  equivalent monotonic/UTC acquisition field before claiming millisecond lag.

- [ ] **2.7 — Cross-check without source substitution.** When DID and passive
  CAN both expose RPM/throttle, report their residual and timing offset, but do
  not silently merge instruments into one series. A failed cross-check marks
  provider health; it does not cause an undocumented fallback.

## Phase 3 — Canary-native fueling analysis

- [x] **3.1 — Land real ROM target artifacts.** Export the target AFR surface
  for every active tune revision into the profile-scoped
  `afr_calibration.yaml` `target_maps`. Record ROM filename/hash, export time,
  axes, units, and revision. No target is reconstructed from an unreliable
  UpRev log.

- [~] **3.2 — Prove the target-map axes are observable.** RPM is confirmed,
  and `data/analyses/bfs-provider-search-20260805.md` uses two independently
  aligned UpRev/CANary windows to reject the current passive substitutes; an
  exact Canary BFS provider remains unavailable. Name the exact live
  quantity indexing each ROM axis and its Canary provider. Do not substitute
  `ENGINE_LOAD`/`CAL/LD` merely because the units look plausible; the campaign
  already measured that calculated load moves with BFS. If the load/BFS axis
  is unavailable, either map it or limit the artifact to a validated WOT row/
  curve with that limitation encoded in its schema.

- [x] **3.3 — Voltage-DID measured AFR provider.** Route
  `did:ECM_AF_SENSOR_V_B1/B2` through `VoltageAfrTransfer` inside the semantic
  frame. The semantic frame retains raw voltage and carries censor flags beside
  transformed AFR. Never use `measured_is_lambda=False` as a shortcut that
  treats volts as AFR.

- [~] **3.4 — Record instrument lineage.** The tuning digest's measured-AFR
  section now states that the
  current voltage transfer was fitted to UpRev's voltage/AFR conversion and is
  therefore the same measurement family, not independent absolute truth.
  Extend the same language to every other AFR-facing result and UI surface.

- [ ] **3.5 — Validate injector-PW fuel-cut semantics.** Use a tagged physical
  protocol (steady fueled idle, closed-throttle decel, key-on engine-off) to
  establish the raw injector-PW DID representation of “fueling active” and
  “fuel cut.” Encode the discrete state/decoder from observed protocol values;
  do not invent a runtime fuzzy threshold to paper over unknown scaling.

- [ ] **3.6 — Physical-action WOT/pull segmentation.** Reuse the existing
  capture event path (`POST /control/captures/current/tags` and `tags.jsonl`)
  for what the operator physically did (pedal floored, pull start/end, held N
  rpm), then combine those tags with passive pedal/throttle evidence. Do not
  use the Mode-01 `ENGINE_LOAD > 85` gate in an all-DID capture. The automatic
  segmenter may infer the same state only after validation against tagged
  pulls.

- [ ] **3.7 — Canary-native delivery error.** Compute measured-vs-ROM-target
  AFR only where the target-map lookup is valid and injection is active.
  Report per-band/per-cell counts, censored samples, artifact revision, source
  lineage, and pooled-vs-balanced summaries. If commanded AFR is unavailable,
  publish the measured-AFR trace and a precise refusal for delivery error.

- [ ] **3.8 — Same-method null runs.** Capture two no-change all-DID drives
  under the same tune and operating protocol. Use them to establish Canary's
  own repeatability for voltage-derived AFR and cam/spark roles. Do not make
  the older UpRev null run the permanent noise model for a different capture
  instrument and cadence.

## Phase 4 — Missing-DID program and cam-role correction

- [x] **4.1 — Correct and promote the cam-role evidence.** The 12.4-minute
  all-DID capture confirms `0x113A` as command and `0x1135`/`0x1163` as two
  distinct measured positions; the obsolete mirror claim was removed. The
  provider registry promotes a neutral primary/secondary measured role, not a
  B1/B2 physical-bank assignment, until simultaneous evidence settles it.

- [ ] **4.2 — Targeted high-rate cam protocol.** Rotate only the three cam
  candidates plus both known/candidate VVT duties, RPM, and throttle during
  tagged steady idle, steady cruise, throttle transitions, and repeatable rpm
  holds. Use true acquisition timestamps. Test causality (command leads
  response), independent jitter, persistent bank separation, and scale
  stability across windows. UpRev is optional bonus evidence.

- [ ] **4.3 — Prioritize self-contained tuning DIDs.** Search/map in this
  order:

  1. commanded equivalence ratio / target AFR, if exposed;
  2. ECM-computed lambda/AFR;
  3. fuel/open-loop status;
  4. airflow, BFS, or the exact ROM load-axis value;
  5. ignition timing;
  6. the other measured intake-cam bank;
  7. bank-2 injector PW if distinct.

  Extend beyond `0x1400` where the existing analysis says the second cam
  position may live. Use `_`-prefixed research captures so sweeps never enter
  training.

- [ ] **4.4 — Canary-only causal identification.** When an already-planned
  tune revision changes a target or timing table, give the changed region a
  bounded, distinctive shape and use tagged pulls to identify DIDs that follow
  the commanded edit. Record the exact physical action and ROM edit, not which
  graph was selected. This is stronger evidence than ordinary correlation and
  does not depend on UpRev staying alive.

- [ ] **4.5 — Promotion gate.** A DID role becomes an automatic semantic
  provider only after independent windows show stable scale/offset, causal
  direction where applicable, plausible range, and out-of-sample agreement.
  Correlation alone cannot promote it. Store evidence sessions and rejected
  alternatives in `data/dids.yaml` or a linked analysis artifact.

- [x] **4.6 — Separate candidate inventory from the active poll map.** Unknown
  or ambiguous responders remain in sweep artifacts/candidate analyses rather
  than acquiring confident operational names in `data/dids.yaml`. The active
  capture profile polls only confirmed/high-enough providers and explicitly
  selected research candidates.

- [x] **4.7 — Capacity-budget each profile.** The bench measured 658 DID
  reads/s. A 12–15 DID set at 20 Hz requests 240–300 reads/s and is a reasonable
  starting envelope, not a universal constant. The saved profile records
  requested per-channel rate, expected total demand, and measured vehicle
  capacity evidence; live capture verification remains the acceptance gate.

## Phase 5 — Digest and report correctness

- [~] **5.1 — Rebuild the tuning digest on `build_tuning_frame`.** All-DID
  achieved-rate selection and the delivery adapter now consume semantic roles;
  the semantic delivery path carries its transformed/censored AFR rather than
  hard-coded Mode-01 channels. Legacy Mode-01 remains a compatibility wrapper,
  and all-DID pull inventory still needs physical-demand evidence.

- [~] **5.2 — All-DID pull inventory.** Tagged pulls now report DID-RPM
  samples/seconds, >2500-rpm seconds, measured-AFR samples, and separately
  unavailable fueling/commanded/target evidence; tag matching uses real epoch
  timestamps rather than assuming Parquet nanoseconds. Inferred pulls and a
  validated passive-demand gate still need drive evidence.

- [x] **5.3 — Partial reports are first-class.** All-DID digest names missing
  pull tags and missing delivery roles rather than reporting them as a zero
  WOT result, and reports per-bank censor counts and conclusion state.
  Distinguish:

  - capture did not contain a role;
  - role existed but no samples survived gates;
  - samples existed but were censored;
  - conclusion was computed.

  Do not collapse all four to `0`, `None`, or a swallowed exception.

- [x] **5.4 — Cam section requires confirmed roles.** Cam tracking now runs
  only when the semantic registry exposes confirmed command and measured
  providers; it reads those provider specs rather than shared-code DID names.

- [~] **5.5 — Honest stored-rate summary.** The digest reports per semantic
  provider duration, sample count, observed rate, median interval, and longest
  persisted gap. Capture pause intervals are not yet persisted, so these are
  explicitly wall-clock timings rather than a claim of active/unpaused rate.

- [x] **5.6 — Use manifest identity in the digest.** `capture_profile` and
  `tune_revision` come from Phase 1. The digest carries the exact target-map
  artifact hash and source-resolution method.

- [ ] **5.7 — Correct previous-capture statistics.** Compare matched semantic
  quantities with identical providers, filters, grids, and statistic
  signatures. Do not subtract two band averages and divide by a per-cell SD.
  If no compatible previous run exists, say so without selecting a different
  profile or unknown revision.

- [x] **5.8 — Fix the outer digest window.** Capture-prefixed session names
  produce correct anomaly and ledger windows through the shared parser. A
  production-shaped capture digest includes an event near the beginning of the
  capture and proves it is counted while later records are excluded.

## Phase 6 — Transactional capture control and truthful rate verification

- [x] **6.1 — One daemon-side arm transaction.** Move profile activation,
  per-capture rate override, bus-budget override/TTL, storage rotation, and
  capture-manifest creation behind one control operation. MCP calls one route;
  it does not orchestrate two state-changing HTTP requests.

- [~] **6.2 — Roll back every partial failure.** Arm failures now return a
  structured failed-step/restored-state response and roll back writer path,
  profile, and exact budget state; the budget-failure boundary is covered.
  Storage/manifest and post-arm verification failure injection still need
  explicit tests.

- [x] **6.3 — Capture-scoped rate epoch.** Add a rate metric whose counters
  begin when the capture becomes active. Pre-capture readings cannot enter it.
  Keep the daemon-wide trailing metric separately if useful for monitoring.

- [x] **6.4 — Correct the short-window denominator.** Before 60 seconds have
  elapsed, divide by the observed capture interval, not 60. Return
  `observed_seconds` and require a minimum verification duration/sample count
  before issuing a pass/fail verdict. Acceptance: a synthetic and live 20 Hz
  stream sampled after 20 seconds reports approximately 20 Hz, not 6.67 Hz.

- [x] **6.5 — Verify the active profile, not every historical channel.** The
  response lists requested rate, achieved rate, sample count, observed span,
  and pass/shortfall per selected channel. A missing channel is an explicit
  failure. Passive broadcast roles are labeled separately from requested DIDs.

- [x] **6.6 — Preserve budget semantics on disarm.** Stopping restores the
  exact pre-capture override, source, and remaining TTL (or an intentionally
  documented equivalent). The default MCP path must not immediately clear the
  restored operator override. An explicit separate “clear all overrides” verb
  may remain for callers that actually want it.

- [x] **6.7 — Return the ended capture path.** Save the capture directory
  before rotating the writer back to a normal session and return it from the
  daemon route and MCP disarm response, along with its manifest and final rate
  summary.

- [~] **6.8 — Idempotent recovery.** Repeating arm with the same active
  capture reports existing state rather than splitting storage; repeating
  disarm reports the immediately-ended capture rather than clearing unrelated
  state. The endpoint documents that this response cache is in-memory. Daemon
  restart behavior *during an active capture* still needs a durable recovery
  contract rather than an assumption that the previous stop response survives.

- [x] **6.9 — Update stale documentation.** `docs/mcp-server.md`,
  `docs/headless-api.md`, and `tuning-analysis-toolkit.md` describe the
  capture-scoped rate observation, budget restoration, returned capture path,
  and idempotent retry behavior.

## Phase 7 — Noise-floor contracts that cannot be misapplied

- [ ] **7.1 — Replace free-form conditions with a typed statistic signature.**
  A floor entry must include instrument/provider, quantity, operating-regime
  filter, grid/cell aggregation, axes/bins, per-cell vs aggregate statistic,
  minimum samples, tune relationship, and provenance sessions. Remove the
  default `conditions: unspecified`; old incomplete artifacts load as legacy/
  unusable for significance, not silently valid.

- [ ] **7.2 — Enforce compatibility.** A comparison may apply a floor only
  when its statistic signature matches. A mismatch refuses significance and
  explains the differing fields. Merely displaying the floor's condition
  string beside an incompatible result is not enforcement.

- [ ] **7.3 — Separate per-cell and aggregate uncertainty.** Record and use a
  per-cell floor for per-cell deltas. If the digest wants significance for a
  band-balanced or pooled aggregate, measure/recompute the null distribution
  of that exact aggregate. Never divide an aggregate delta by a per-cell SD.

- [ ] **7.4 — One annotation path.** Digest, CLI, MCP, and library callers use
  the same compatibility check and annotation function. Remove manual
  `delta / sd` calculations from `tuning_digest.py`.

- [ ] **7.5 — Canary-native floor artifact.** After Phase 3.8, create a floor
  for the all-DID voltage-derived AFR path and relevant semantic roles. Keep
  the UpRev floor labeled as `instrument: uprev`; it remains valid only for
  UpRev comparisons with its original 50 ms/matched-cell signature.

- [ ] **7.6 — Honest small-sample reporting.** Carry `n_cells`, null-pair
  count, mean bias, and uncertainty limitations into every verdict. Do not let
  a five-cell floor produce a context-free binary “distinguishable” label.

## Phase 8 — Live-session and public-surface consistency

- [ ] **8.1 — Resolve the active writer session without parsing names.** The
  shared live-session fix in `41abaa8` correctly covers current normal,
  capture, tuning, and timestamp-based replay names. It still infers runtime
  state from a directory naming convention rather than the writer/controller's
  authoritative active path. Return that active path directly or persist a
  typed session manifest with `started_at` for every isolated mode, so a new
  prefix or identifier format cannot make valid live data disappear. Training
  discovery remains a separate question and continues excluding `_`
  directories.

- [ ] **8.2 — Sweep all live readers.** `/status`, `/telemetry/latest`, OBD/DID
  coverage, dashboard initial state, replay guard, and MCP status must resolve
  the same active session contract. Tests cover normal, capture, tuning,
  replay, freshly rotated/unflushed, and post-disarm states.

- [x] **8.3 — Semantic MCP/CLI responses.** Expose the tuning-frame capability
  report, role provenance, capture manifest identity, and partial-section
  refusals. `measured_spec` may remain as an expert override but cannot bypass
  transforms or unit validation.

- [ ] **8.4 — UpRev failure is non-fatal.** Watch/import/alignment failures
  produce an optional-instrument status and leave the Canary capture/digest
  complete. No digest field says “capture failed” solely because UpRev is
  absent, stalled, or unaligned.

- [ ] **8.5 — Remove stale absolute-truth language.** Documentation and UI
  distinguish ECM-computed lambda, voltage-derived AFR calibrated against
  UpRev, ROM commanded target, and any future independent wideband. Never call
  two transforms of the same factory sensor independent validation.

## Phase 9 — End-to-end acceptance

- [ ] **9.1 — Software reference capture.** Build a production-shaped
  `tuning-did` fixture using the real capture writer, manifest, raw DID hex,
  passive decoded CAN, acquisition timestamps, pause intervals, and an ended
  capture path. It must exercise every Phase 1–8 contract without fabricating
  Mode-01 rows.

- [ ] **9.2 — Stored real-capture regression.** Pull one real all-DID capture
  into a gitignored local fixture location and record checksum + hand-audited
  expected summaries. Tests skip when the corpus is absent but the live
  acceptance run must execute it before closing the roadmap.

- [ ] **9.3 — Parked live shakedown.** Read `vehicle_state` first. Arm the
  all-DID profile at a conservative requested rate/budget, verify profile and
  per-channel achieved rate, tag KOEO/idle/rev actions, disarm, and confirm
  `10 81` returns Mode 01 immediately. Confirm the response names the ended
  capture directory and the prior budget is restored.

- [ ] **9.4 — Drive capture without UpRev.** Record tagged steady cruise and
  repeatable pulls using Canary alone. Acceptance does not mention UpRev:
  continuous DID coverage, passive CAN coverage, no session switching, no
  writer drops, usable measured-AFR trace, injector fuel-state evidence, and
  correct partial/full digest sections.

- [ ] **9.5 — Repeatability pair.** Repeat the same protocol without a tune
  change, compute the Canary-native floor, and demonstrate that automatic
  comparisons select the exact profile/revision/provider/statistic match.

- [ ] **9.6 — Tune-change pair.** Record before/after captures around a known
  tune revision with exported target artifact and explicit manifest identity.
  Show the intended target change in the resolved cells and quote only
  compatible uncertainty. Any successful UpRev log is attached as optional
  corroboration after the Canary result exists.

- [ ] **9.7 — Whole-repo commit gate.** Run `ruff check .`,
  `ruff format --check .`, `mypy src`, and full `pytest`; run dashboard
  `pnpm test` + `pnpm typecheck` for any UI/schema changes. Commit with explicit
  pathspecs in the shared working tree.

## Suggested implementation order

1. **Phase 1 first:** the rest cannot be trusted while profile/revision
   identity is disconnected from the capture that produced the data.
2. **Phase 2 next:** one semantic frame prevents another round of per-caller
   source fixes.
3. **Phase 6 in parallel with Phase 2–3 only where files do not overlap:** it
   protects the next real capture and makes rate evidence truthful.
4. **Phase 3 then Phase 5:** make the all-DID report useful before expanding
   the DID catalog.
5. **Phase 7 before publishing significance:** deltas may be computed earlier,
   but no sigma/distinguishable verdict ships until signatures match.
6. **Phase 4 is iterative vehicle work:** neutralize overconfident mappings
   early; promote new roles only as captures prove them.
7. **Phase 8 and 9 close the loop:** every public surface and real-drive path
   must consume the same contracts.

## Definition of done

This roadmap is complete when a tuning drive can be captured and analyzed
with Canary alone, entirely inside the Mode-22 session, and the resulting
report truthfully answers every question supported by its recorded roles. It
must identify the exact capture profile, tune revision, provider and artifact
for each conclusion; preserve operator budget/session state across arm/disarm;
verify the requested rate over the actual capture epoch; refuse incompatible
noise floors; and leave ambiguous DIDs ambiguous. A working UpRev log may add
evidence, but its absence cannot prevent completion.
