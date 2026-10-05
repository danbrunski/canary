# E01 — evidence, investigation memory and interventions

Follow [E00](E00-second-brain-README.md). Implements capabilities 1, 2 and
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

- [ ] **E01.3 — Persist and retrieve memory on the Pi.**
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

- [ ] **E01.4 — Extend intervention history and enforce prediction order.**
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

- [ ] **E01.5 — Render evidence-backed cards without a language model.**
  Extend existing cards with deterministic templates showing what happened,
  supporting/refuting evidence, alternatives, unknowns and next measurement.
  Read all numbers from E01.2 and make recurrence/retraction visible. E04 owns
  their HTTP/MCP/dashboard integration, using this single representation.
  **Accept:** complete, absent-input, conflicting-evidence and retracted
  cases render meaningful cards with no model call; every numerical claim
  resolves, and unavailable evidence never renders as "normal".

- [ ] **E01.6 — Commit known-answer investigation cases.**
  Preserve compact real evidence inputs plus tool-generated expectations for
  absent channels, misleading correlation, calibration change, recurrence
  and retraction. Include the project retractions as narratives with links
  to reproducible evidence where available; narrative alone is not a label.
  Separate incidents/drives before defining evaluation cases.
  **Accept:** the real analysis-to-store-to-card path reproduces each known
  disposition and reference; a negative control fails. Missing original
  inputs are named gaps, and synthetic cases are labelled as such.
