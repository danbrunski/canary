# E01 — evidence, investigation memory and interventions

Follow [E00](E00-second-brain-README.md). Implements capabilities 1, 2 and
the record contracts for 5. All stores and required queries run on the Pi.
Existing starting points include `src/analysis/llm_assist/cards.py` and
`store.py`, `src/vehicle/tune_revisions.py`,
`src/behavior/accepted_baselines.py`, and the existing JSONL/state-dir
primitives. Audit these before introducing a schema or store.

## Checklist

- [ ] **E01.1 — Define shared state and record contracts.**
  Inventory existing consumers and extend investigation, evidence,
  prediction, intervention and outcome records with vehicle/program identity,
  applicability, units, quality, valid time and record time. Supersession
  retains the old record and its superseded time. Define a shared result
  status that distinguishes unevaluable reasons from measured deviation.
  **Accept:** schema tests reject missing conditions/provenance, an
  unevaluable result with a qualified residual, and cross-vehicle references;
  a replay round-trip preserves both times and supersession history.

- [ ] **E01.2 — Mint and resolve evidence at the producing tool.**
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
