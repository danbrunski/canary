# E01 — evidence, investigation memory and interventions

Follow [E00](../E00-second-brain-README.md). Implements capabilities 1, 2 and
the record contracts for 5. All stores and required queries run on the Pi.
Existing starting points include `src/analysis/llm_assist/cards.py` and
`store.py`, `src/vehicle/tune_revisions.py`,
`src/behavior/accepted_baselines.py`, and the existing JSONL/state-dir
primitives. Audit these before introducing a schema or store.

## Checklist

- [x] **E01.1 — Define shared state and record contracts.**
  Inventory existing consumers and extend investigation, evidence,
  prediction, intervention and outcome records with vehicle/program identity,
  applicability, units, quality, valid time and record time. Supersession
  retains the old record and its superseded time. Define a shared result
  status that distinguishes unevaluable reasons from measured deviation.
  **Accept:** schema tests reject missing conditions/provenance, an
  unevaluable result with a qualified residual, and cross-vehicle references;
  a replay round-trip preserves both times and supersession history.
  *Done 2026-10-04:* audit — `llm_assist` cards/store (free-text
  `EvidenceReference`, no producer), `vehicle/tune_revisions.py` (flashes),
  `vehicle/vehicle_ledger.py` (physical changes, operator-editable),
  `behavior/accepted_baselines.py` and `plausibility`'s own
  `EvidenceRecord` — none held investigations/predictions/outcomes, so the
  contracts are a new layer-1 package `src/evidence/` (`records.py`,
  `ledger.py`) on the shared `JsonlStore` primitive; interventions point at
  the existing tune-revision / vehicle-ledger record (`source`/`source_ref`)
  rather than copying it. Six kinds (evidence, investigation, prediction,
  intervention, outcome, retraction) share `VehicleIdentity` (profile, plus
  required-but-nullable `vin`/`program_id`/`calibration`), required
  `Conditions.regime` (noise-floor matching rule), `Provenance`,
  `valid_from`/`valid_to` and ledger-minted `id`/`recorded_at`; `Quantity`
  carries units, `Quality` the observation count and clock basis, and
  `Result` the shared status (`evaluated` | `unevaluable` with one of seven
  named reasons; an unevaluable result with a residual is refused).
  Supersession appends a record naming what it `supersedes` (or a
  `Retraction`); the ledger derives `superseded_at`/`superseded_by`/
  `retracted_by` and never rewrites a line. Runtime path
  `daemon.memory_ledger` = `control/<slug>/memory.jsonl` under `state_dir`
  (`DAEMON_STATE_LAYOUT`, `scope_config`). Evidence:
  `tests/test_evidence_memory.py` (21 tests: missing
  conditions/provenance/identity/valid time, unevaluable+residual,
  cross-vehicle append/reference/resolve, kind-checked references,
  `test_replaying_the_log_preserves_both_times_and_supersession_history`),
  `tests/test_state_dir.py::test_investigation_memory_is_per_vehicle_state`,
  `tests/test_layering.py` (`evidence: 1`).

- [x] **E01.2 — Mint and resolve evidence at the producing tool.**
  Evidence records contain tool/code version, source session/window/sample
  references, input and artifact hashes, conditions, computed values and
  evaluation policy. Have tools persist the record and return its id.
  Replace free-text numerical card claims with values rendered from resolved
  evidence. Sweep all card producers/readers and retain old cards as explicitly
  unverified historical narrative when they cannot be re-evaluated.
  **Accept:** an invented id, wrong-vehicle id or value differing from its
  record is rejected; existing legitimate producers still round-trip. A
  recomputed result links to its inputs and supersedes rather than silently
  overwriting old evidence. This supplies LLM-assist S.5's shared contract.
  *Done 2026-10-04 (revised after verification):* `Evidence` records carry
  tool, tool version and `code_version` (hash of the producing modules'
  source including the minting module, `src/evidence/provenance.py`),
  `SourceRef` session/window/channels/sample counts, input and artifact
  sha256s, conditions, `policy`, values with units, labels, quality and
  result. `MemoryLedger.append` mints the id (over the whole stored record,
  record time and supersession links included) and record time, refusing
  producer-supplied ones; identical content returns the existing record only
  while that record is *current* — content re-stated after it was superseded
  (A→B→A) or retracted is a new record superseding whatever is current. A
  schema-invalid stored line is reported as a corrupt record, not an unminted
  id. Minting tool: `src/analysis/compare_evidence.py`
  `mint_channel_comparison` (hashes the pairs, session chunk list and
  catalogs; refusal → `unevaluable` `missing_input`/`refused`). It states
  its own regime (`all conditions … unfiltered`; no caller `conditions`
  argument exists) and resolves the calibration from the capture manifest or
  `tune_revisions.yaml` for the session (unknown otherwise). Operator entry
  point: `tools/mint_comparison.py` (scoped config → ledger → id). Cards
  (`llm-assist-card/2`) cite `EvidenceCitation` ids; prose refuses any
  digit-bearing token that is not an identifier (`3000rpm`, `80kPa`, `1e3`,
  `x3` refused; `obd:SHORT_FUEL_TRIM_1`, `0x1206`, `B1` allowed);
  `resolve_card` rejects invented ids, other-vehicle ids/cards, quoted values
  differing from the record and claimed sessions no cited evidence read, and
  returns `retracted` (not `verified`) for a card citing retracted evidence;
  `LlmAssistCardStore.append(card, ledger=…)` refuses anything not
  `verified`. Swept producers/readers: no card producer exists; readers
  `GET /analysis/llm-assist/cards` and `LlmAssistPanel.tsx` (numbers from the
  records, rejected/retracted/historical states shown); first-contract cards
  load as `UnverifiedHistoricalCard`. Evidence:
  `tests/test_llm_assist_cards.py` (36), `tests/test_compare_evidence.py` (5,
  incl. `test_a_recompute_on_changed_data_supersedes_and_links_its_inputs`,
  `test_the_tool_states_its_own_conditions_and_calibration`,
  `test_the_cli_mints_into_the_scoped_ledger`),
  `tests/test_evidence_memory.py` (24, incl. A→B→A, re-mint after
  retraction, corrupt line), `LlmAssistPanel.test.ts` (9). Remaining
  outside this item: no MCP/HTTP route mints evidence yet (E04 owns the
  HTTP/MCP integration); `lambda_delivery`, noise-floor significance and the
  plausibility evaluator's own `EvidenceRecord` do not mint yet — each
  adopts `src.evidence` where its E02/E03 consumer needs it.

- [x] **E01.3 — Persist and retrieve memory on the Pi.**
  Extend existing stores under per-vehicle `state_dir`. Add exact queries by
  incident, conditions, calibration/program, valid time and record time;
  start with structured filters and text search. Keep live appends bounded
  through E04's handoff and make retry/replay identity explicit so the same
  event does not create duplicate findings. Do not add an embedding service
  without a measured retrieval gap.
  **Accept:** restart preserves records; a repeated event is retrieved as a
  recurrence; queries answer both "what preceded the flash?" and "what did
  we believe then?" correctly across a retraction. `test_state_dir` coverage
  accounts for every new runtime path and replay cannot alter live memory.
  *Done 2026-10-04 (laptop; the Pi persistence/restart demonstration is
  E04's):* `MemoryLedger` keeps one parsed index per file, shared by every
  instance in the process; each read/append stats the file and parses only
  bytes appended since (another instance's or process's included), re-reads
  whole only on a shrink or new inode, and terminates a torn tail line before
  appending so a crash cannot swallow the next record. Measured on the laptop
  (scratch bench, 10,000 evidence records, 13.2 MB file): append 0.36 ms each
  at 10k (old whole-file re-read: 0.10 s at 1k and 0.24 s at 2k records,
  measured), cold load after restart 0.78 s for 10,200 records, exact
  queries 1–28 ms, text search 50 ms warm / 311 ms first, index ~9.6 KB per
  record (96 MB at 10k by tracemalloc — fine at the expected hundreds; a
  lazy-parse/compaction is owed if the Pi ledger nears 10k). `MemoryQuery`:
  kinds, incident, recurrence key, subject, session, exact regime,
  vehicle_state/operating_mode/calibration/program (`ANY` vs `None` =
  unknown), `valid_at`/`valid_before`/`valid_after`,
  `recorded_after`/`recorded_before`, `as_of` (supersessions, retractions and
  retirements only as recorded by then), case-insensitive all-terms text
  search; no embedding service. Retry/replay identity: identical content
  while current is the same record; the same `subject` over the same data
  (a session in common, overlapping windows) supersedes (recompute); the same
  subject over other data, or a shared `Evidence.incident`, is a recurrence
  (`MemoryLedger.recurrences`, `recurrence_key`). Subject supersession is now
  vehicle-scoped (it used to supersede another vehicle's record on a shared
  file). Replay isolation: the live ledger refuses evidence read from a
  `_replay-*` session (`ReplayIsolationError`); `memory_ledger_for(config,
  replay_id=…)` opens `control/<slug>/_replay/<id>.memory.jsonl`. Read path:
  `tools/memory.py query|card`. Evidence: `tests/test_evidence_retrieval.py`
  (14, incl. `test_restart_preserves_records_and_their_derived_state`,
  `test_another_instance_and_another_writer_are_seen_without_a_full_reread`,
  `test_a_retry_is_the_same_record_and_a_repeated_event_is_a_recurrence`,
  `test_what_preceded_the_flash_is_a_valid_time_question`,
  `test_what_we_believed_then_survives_a_retraction`,
  `test_replay_output_cannot_enter_live_memory`),
  `tests/test_state_dir.py::test_investigation_memory_is_per_vehicle_state`
  (replay path). Remaining for E04: the daemon's replay controller does not
  open a replay ledger yet (no daemon producer mints memory), and no HTTP/MCP
  route serves `MemoryQuery`.
  *Revised after verification (2026-10-04):* the cached index fingerprints
  the file's first 4 KiB and the 512 bytes before its read offset and
  re-reads whole on any mismatch (an in-place rewrite on the same inode, any
  length, or a delete/recreate that reuses the inode); an append is
  refresh + decide + write under an exclusive `fcntl.flock` on the ledger
  file, so two processes recomputing one subject leave exactly one current
  record; a retraction cannot be retracted or superseded (restate the
  finding instead). Tests:
  `test_an_in_place_rewrite_on_the_same_inode_is_reread`,
  `test_two_processes_recomputing_at_once_leave_one_current_record` (two
  subprocesses with a delayed write), `test_a_retraction_cannot_be_retracted`
  (with `as_of`).

- [x] **E01.4 — Extend intervention history and enforce prediction order.**
  Extend the tune-revision model to reference hardware, fuel and instrument
  changes as well as flashes, reusing the runtime ledger/override path.
  Calibration records carry exact image hashes. Predictions state affected
  conditions, direction, supported magnitude and a condition-matched noise
  floor; outcomes record no effect, unresolvable effect or measured change.
  Link accepted baselines and retired references to their investigation.
  **Accept:** a prediction recorded after an intervention is post-hoc and
  cannot count as confirmed; a noise floor from another regime is refused;
  a hardware intervention retires affected references without deleting them.
  A missing historical fact remains unknown rather than backfilled by guess.
  *Done 2026-10-04:* `src/vehicle/interventions.py`
  `intervention_history(registry, events)` joins `tune_revisions.yaml`
  flashes and vehicle-ledger events (runtime JSONL under `state_dir`, new
  optional `VehicleEvent.intervention`: hardware | fuel | instrumentation,
  accepted by `POST /control/vehicle-ledger` and carried through a dashboard
  edit) into one effect-ordered history; `src/evidence/interventions.py`
  `record_intervention` writes the memory `Intervention` pointing at it
  (`source`/`source_ref`, `source_digest`; `source_drift` reports
  edited/removed). `Intervention` now requires `affects` (channels or `*`),
  carries `effective_precision` (a ledger date is `day`, at UTC midnight —
  the earliest bound), and a calibration must state `image_sha256`
  explicitly: copied from the revision's plain-image hash, `None` where none
  was recorded (never the container `rom_sha256`). `Prediction.noise_floor`
  is a `NoiseFloorRef` (sd with unit, regime, sigma threshold, source and
  hash; `analysis.noise_floor.floor_reference`) refused unless its regime is
  the prediction's character for character. Order is derived, never stated:
  `MemoryLedger.prediction_order`/`assess` compare the prediction's minted
  `recorded_at` with the intervention's `valid_from` (or the outcome
  evidence's start); a post-hoc verdict is `post_hoc`, never `confirmed`.
  Outcomes must be evaluated under the prediction's regime; `no_effect` and
  `measured_change` need the floor (else `unresolvable`) and must sit on the
  right side of it. New kind `Reference` (accepted baseline / behavior model,
  `investigation` link): an intervention whose `affects` covers its channels
  retires it as historical (`LedgerEntry.retired_by/retired_at`, derived; the
  record stays current and on disk; a retracted intervention retires
  nothing). `AcceptedBaselineOverride.investigation` (optional) +
  `investigation_links` (linked / unlinked / unresolved) +
  `override_reference`. Unclassified `mod`/`repair` events are listed as
  unknown kind and refused by `record_intervention`. Evidence:
  `tests/test_evidence_interventions.py` (10, incl.
  `test_a_prediction_recorded_after_its_intervention_is_post_hoc_and_never_confirmed`,
  `test_a_noise_floor_from_another_regime_is_refused` against the tracked
  `noise_floor.yaml`,
  `test_a_hardware_intervention_retires_affected_references_without_deleting_them`,
  `test_an_unclassified_physical_change_stays_unknown`,
  `test_the_tracked_flash_history_records_into_memory_without_guessing`),
  `tests/test_vehicle_ledger_routes.py::test_an_event_is_classified_as_an_intervention`,
  `VehicleLedgerPage.test.tsx` (edit keeps the classification). Not done
  here: no dashboard control sets the classification yet (API only), and
  nothing records the existing flashes or the shipped accepted baselines
  into memory automatically (an operator/E03 step).
  *Revised after verification (2026-10-04):* `NoiseFloorRef.source_sha256`
  is required and `sigma_threshold` ≥ `DETECTABLE_SIGMA` (2.0, which
  `noise_floor.SIGMA_THRESHOLD` now reads); at append the ledger re-reads the
  cited floor file (`src/evidence/floors.py` `verify_floor`) and refuses a
  hash, sd, unit or regime that differs — a relabelled or invented floor is
  refused. `floor_reference(path, channel, conditions=)` reads the file once
  for both hash and entry. A non-`unresolvable` outcome citing evidence that
  starts before the intervention took effect is refused.
  `Intervention.source_digest` is required whenever `source != "none"`;
  `source_drift` returns `no_digest`, `edited`, or `mismatch:<fields>`
  (effect time, precision, kind, image hash). `TuneRevision` has
  `flashed_at_precision` (exact | approximate | floor; unset = unknown),
  merged from the tracked file; `Intervention.effective_precision` is
  required (instant | day | approximate | floor | unknown) and only
  instant/day can order a prediction — otherwise order and verdict are
  `undetermined`, never `confirmed`. `tune_revisions.yaml` gains
  `flashed_at_precision` only where each entry's own text states it:
  ZS04C-stock floor, k105 and k106 approximate, 20260920 and 20261004 exact
  (their notes pin the boundary to a capture frame / the last post-program
  CRC response); osiris-initial is left unknown. MCP `record_vehicle_event`
  takes and passes `intervention` (so a correction no longer erases it);
  `docs/mcp-server.md`, `docs/headless-api.md` and the MCP tool snapshot
  updated. Tests: `test_a_relabelled_or_invented_floor_is_refused_against_its_source`,
  `test_an_outcome_cannot_rest_on_data_measured_before_the_change`,
  `test_a_sourced_intervention_carries_its_digest_and_drift_is_named`,
  `test_flash_precision_comes_from_the_revision_never_silently_instant`,
  `tests/test_mcp_tools.py::test_record_vehicle_event_correction_keeps_the_intervention`,
  `tests/test_write_tool_schema_contract.py` (intervention in the body).

- [x] **E01.5 — Render evidence-backed cards without a language model.**
  Extend existing cards with deterministic templates showing what happened,
  supporting/refuting evidence, alternatives, unknowns and next measurement.
  Read all numbers from E01.2 and make recurrence/retraction visible. E04 owns
  their HTTP/MCP/dashboard integration, using this single representation.
  **Accept:** complete, absent-input, conflicting-evidence and retracted
  cases render meaningful cards with no model call; every numerical claim
  resolves, and unavailable evidence never renders as "normal".
  *Done 2026-10-04:* `src/evidence/cards.py` `investigation_card(ledger,
  id, as_of=…)` → `InvestigationCard` (`canary-investigation-card/1`, the
  one JSON representation for E04) and `render_text` (fixed template):
  what happened, hypotheses with supporting/refuting evidence, alternatives,
  conflicts, retracted/superseded evidence, predictions with order, floor and
  outcome verdicts, unknowns (stated + derived: unknown calibration/program,
  each unevaluable result), next measurement (stated + derived from the
  unevaluable reason or missing evidence), narrative under a fixed
  "not evidence" notice. Numbers exist only as `CardValue`s copied from a
  resolved record with its id; `Investigation` title/question/hypothesis
  labels/unknowns/next measurements refuse numerals (tokenizer moved to
  `src/evidence/text.py`, shared with the LLM-assist cards, whose
  `evidence_payload` now lives here too). `evidence_state` has no
  normal/ok value (no_evidence, unevaluable, retracted, conflicting,
  partially_evaluable, evaluated); unevaluable evidence renders its reason
  as "not a normal result". Recurrence count and retraction reason are shown
  per evidence item; `as_of` renders the earlier belief. Evidence:
  `tests/test_evidence_cards.py` (10: complete, absent-input, no-evidence,
  conflicting, retracted + as-of, predictions/outcomes, numeral refusal,
  non-investigation id, no model imports, CLI), each rendered card checked
  token by token so every digit-bearing token comes from a record (with a
  negative control that an injected number is caught). E04 owns the
  HTTP/MCP/dashboard surfaces.
  *Revised after verification (2026-10-04):* predictions and outcomes are
  read with `include_superseded=True`; `PredictionView`/`OutcomeView` carry
  `standing`, `superseded_by`, `retracted_by`, `retraction_reason`, rendered
  as `RETRACTED by …` / `SUPERSEDED by …` and listed under retracted or
  superseded records. Test:
  `test_a_retracted_prediction_and_outcome_stay_on_the_card_labelled`.

- [x] **E01.6 — Commit known-answer investigation cases.**
  Preserve compact real evidence inputs plus tool-generated expectations for
  absent channels, misleading correlation, calibration change, recurrence
  and retraction. Include the project retractions as narratives with links
  to reproducible evidence where available; narrative alone is not a label.
  Separate incidents/drives before defining evaluation cases.
  **Accept:** the real analysis-to-store-to-card path reproduces each known
  disposition and reference; a negative control fails. Missing original
  inputs are named gaps, and synthetic cases are labelled as such.
  *Done 2026-10-04:* `tests/fixtures/ground_truth/investigation_cases.yaml`
  (`canary-investigation-cases/1`, loaded by `src/analysis/known_cases.py`)
  declares drives, then incidents, then cases. **Drives** (4) are each a
  committed slice plus `full_session` facts counted over every chunk of the
  original local session (`tools/make_investigation_cases.py
  --verify-sources` re-counts them; all four matched): `drive-slice`
  (2026-08-03T163440), `did-speed-slice` (rom-trace 09-21T142926), new
  `rom-trace-absent-slice` (rom-trace 09-21T195647, 19 KiB, made with
  `tools/make_ground_truth_fixture.py`), `target-phi-slice` (ram-study
  10-04). **Incidents** (9) carry narrative, links and `gaps`; the schema has
  no disposition field, every incident a step names must be declared, and an
  incident with no case must name its gap (the 0x6F7 wheel-speed agreement,
  whose occurrences are the flash case's evidence; achieved-Hz wall-clock, Mode 22 "session cycling", "K responds 1:1",
  RAM word ±14 % ratio drift, 0x1102 K-line table → linked to
  `test_did_agreement_can_fail`). **Cases** (3 real, one per incident; an
  incident cannot have two; recurrence occurrences must be on different
  drives): `absent-commanded-afr` (absent_channel + recurrence: PID 0x44 on
  two rom-trace captures → `unevaluable`/`missing_input`, 2 occurrences,
  card `unevaluable` with a capture next-measurement, calibration
  ZS44A-20260920), `ecm-calc-load-b` (misleading_correlation + retraction:
  the pre-b374ba4 catalog excerpt `ecm-calc-load-b-bindings.yaml` gives
  0x233:4~0x23D:3 r² 0.926 on same-instant pairs, against obd:ENGINE_LOAD
  r² 0.025, while ECM_RPM_23D vs obd:RPM is slope 0.9998 / r² 0.9987; the
  as-of card shows the old belief current and supported, today superseded,
  the binding reference retracted), `flash-20260920` (calibration_change +
  recurrence: the tracked flash recorded as an instant intervention with its
  image hash; "what preceded the flash" = the 08-03 wheel-speed evidence,
  after = the 09-21 one; the wheel-speed reference is not retired by a
  fuel-target flash; both drives 1:1 within the expectations.yaml bounds).
  `known` checks read tool output only and name their source; the
  tool-generated snapshot `investigation_cases.expected.yaml` (`--write`;
  refuses to write over a failing known answer) holds everything else the
  chain says. **Negative controls** (synthetic): WHEEL_SPEED_6F7_A scale
  +4 % fails both residual bounds; dropping ZS44A-20260920 from the flash
  history fails both calibrations; pointing the absent case at
  `target-phi-slice` raises `SliceAbsenceError` (PID 0x44 has 329 rows in
  the full ram-study session — the slice dropped it). `mint_channel_comparison`
  (and `tools/mint_comparison.py --incident`) gained an optional `incident`.
  Evidence: `tests/test_investigation_cases.py` (15 passed, ~5 s serial
  `-n0` on the laptop, 2026-10-04).
  **Defect found and fixed at the shared layer:** the calibration resolver
  dated a session by its directory name; 2026-08-03T163440 is named
  6 h 37 min before its first sample (23:11:54Z, clock stepped after boot),
  so the k106 drive resolved to ZS44A-k105. `TuneRevisionRegistry.for_session`
  / `resolve_session_revision` now date a session directory by its first
  stored sample (`src.storage.session_files.session_first_sample`, footers of
  the earliest chunk only) via `session_dated_at`, falling back to the name
  only with no telemetry; every `resolve_session_revision` caller inherits it,
  `scripts/evaluate_behavior_models.py` dates pieces by their parent's first
  frame row, `scripts/refit_behavior_models.py`'s flash-span check uses data
  time first, `scripts/inventory_behavior_inputs.py` passes the directory.
  `tests/test_analysis_tuning.py` asserted the wrong k105 answer and now pins
  the data-dated one (`test_a_session_is_dated_by_its_first_sample_not_its_name`);
  the two strict-defect checks are ordinary checks; the k106 note in
  `tune_revisions.yaml` is corrected.
