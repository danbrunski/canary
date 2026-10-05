# E02 — deterministic target-equivalence-ratio twin

Follow [E00](E00-second-brain-README.md) and the parent
[first-slice specification](../ecu-second-brain.md#first-ecm-twin-slice-target-equivalence-ratio).
Serves capability 3 and supplies command predictions to capability 5. The
required runtime is the Pi; development can use stored sessions elsewhere.
This predicts commanded φ from observed lookup inputs, not fuel delivery.

This is the first increment of the [application-code twin](E05-ecu-application-twin.md).
E05.1 records the decoded dependency graph and E05.2–E05.4 extend its upstream
calculations, state transitions and outputs. An observed intermediate is
labelled as an input; agreement downstream cannot validate its computation.

Scope: warm, steady, open-loop fuelling in independently validated rpm/load
regions, with stable map identity and no fuel cut. Qualify ordinary lookup
and the sustained pedal-enrichment override separately. Cold/warm-up,
closed-loop overrides, transients/DFCO recovery, map changes, protection
regimes, boost and other ECU programs remain outside the first evidence.

## Checklist

- [x] **E02.1 — Declare applicability and typed inputs.**
  Profile data declares raw/decoded units, required windows, warm and
  steady-region bounds, dwell, coverage, mode source, freshness and acquisition
  skew. Inputs are `mem:ECM_FUEL_TARGET_MAP_X_TRACE`,
  `mem:ECM_ENGINE_SPEED_RAM`, `mem:ECM_PEDAL_ENRICH_FLAGS`,
  `mem:ECM_ACTIVE_MAP`, `mem:ECM_UPREV_MAGIC` and
  `mem:ECM_UPREV_MAP_SELECTOR`; reference output is
  `mem:ECM_TARGET_EQUIV_RATIO` plus its sentinel. Use
  `mem:ECM_AIR_CHARGE_SLOT_103A` for the independent X-formula check.
  Declare the coolant source (`ECM_COOLANT_TEMP` DID 0x1101 or
  `obd:COOLANT_TEMP`) and mode evidence such as `obd:FUEL_STATUS` explicitly.
  Derive gates from inputs/mode, never residual size. Preserve raw X counts;
  do not feed its display scale directly to the firmware's raw BFS axis.
  **Accept:** missing gates, wrong units/program, unsupported clocks,
  sentinel, stale input and uncovered region produce named unevaluable
  results. Negative controls expose an X-scale or rpm-decode mistake.
  *Done 2026-10-04:* `data/nissan-xterra-2008/target_phi_twin.yaml`, validated by
  `TwinDefinition` in `src/analysis/target_phi.py`. Inputs are typed
  `units: raw_counts` with bit widths, and each stored value is returned to raw
  counts through its own `data/dids.yaml` decode. A value that does not land on
  whole counts refuses the session (`wrong_units`). The file also declares
  identity rules, the reference word and sentinel, the X-formula BFS channel, a
  coolant source list (`did:ECM_COOLANT_TEMP`, then `obd:COOLANT_TEMP`), and
  `obd:FUEL_STATUS` states with eligibility. Steadiness is from `obd:RPM`/`obd:MAF`
  over 2 s. Freshness is per gate, skew is 250 ms, and regions are raw-count
  bounds. Gate thresholds are marked `proposed`.
  Required windows are declared under `acquisition`: the memory window of every
  channel, and `back_to_back: [fuel_target, fuel_axis, spark, flags_2841]`
  within `max_span_s` 0.25. The schema refuses a channel with no window, a
  tuple window missing from `back_to_back`, and a span wider than the skew.
  At evaluation the declared windows are checked against `data/dids.yaml`
  (`window_mismatch`). The schema also refuses a display unit, an undeclared
  mode state, a round clock, a missing gate and a steadiness channel without
  exactly one limit (`test_the_definition_refuses_an_undeclared_or_unsafe_contract`,
  `test_a_missing_gate_section_is_refused`,
  `test_a_window_the_catalog_disagrees_with_refuses_the_session`).
  `test_each_unmet_condition_has_a_named_state_and_a_null_qualified_residual`
  (28 cases) reaches every per-sample reason through `evaluate_session`, and
  `test_every_reason_the_evaluator_names_is_reached_by_a_test` keeps that
  list equal to the evaluator's vocabulary.
  - Unevaluable: `sentinel`, `input_stale:load`/`:flag`/`:tuple_spread`,
    `input_not_captured:load`, `unsupported_clock`, `map_uninitialised`,
    `slot_ambiguous`, `slot_unresolved`, `map_switching`,
    `map_identity_unbracketed`, `gate_missing:coolant`/`:mode`,
    `gate_stale:coolant`/`:mode`, `mode_unknown`,
    `steadiness_unobserved:obd:RPM`/`:obd:MAF`, `uncovered_region` (×2),
    `enrichment_gate_state_unknown`.
  - Excluded: `cold`, `closed_loop`, `mode_transition`, `transient`.
  - Pending: `awaiting_input:load`, `awaiting_mode_read`,
    `awaiting_identity_read`.
  - Whole-session refusals: `wrong_units`, `gate_unreadable` and
    `window_mismatch` (`test_a_load_byte_stored_under_another_decode_refuses_the_session`,
    `test_an_unreadable_gate_source_refuses_the_session`). `wrong_program`
    is in E02.2. Negative controls:
  `test_the_override_known_answer_exposes_an_rpm_decode_mistake` (the rpm word
  at 50 rpm/count lands in row 0, 0x92 ≠ 0x95) and
  `test_the_x_relation_exposes_an_x_scale_mistake` (stored X fed as raw matches
  <10/194, a 1.1× scaler <100/194).

- [~] **E02.2 — Resolve the exact ROM and active table.**
  Consume W08's library and W05's verified axis definitions. Every prediction
  names plain-image SHA-256, program/profile, active slot, table/axes,
  decoder and evaluator versions. The reference image is
  `a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50`
  (`ZS44A-20260920`). For this program, verify default slot 1 when active-map
  reads 0 with valid magic and cold-boot provenance; refuse unresolved state.
  Compare the active RAM Target AFR bytes with the selected library slot
  before qualifying live use. Keep ROM images/disassembly outside Git.
  **Accept:** a wrong hash, missing table, unverified mapping or ambiguous
  slot refuses by name. A tool-produced RAM/ROM comparison establishes byte
  identity. **Vehicle required** for this read if no recorded evidence exists.
  *Partial 2026-10-04 (resolution done; RAM/ROM byte identity remains):*
  `resolve_table` (W08 image) and `table_from_calibration` (transcribed table)
  both first consume W05's `rom_viewer.yaml` trace mapping through
  `check_trace_mapping`. That refuses `mapping_unverified` or
  `mapping_mismatch`. Then `image_hash_mismatch`, `wrong_program`,
  `table_missing`, `table_unverified` (slots 2–5 are `inferred`),
  `axis_unverified` and `unmodelled_logic` (the gate constants or the 0xD247
  curve differ from the modelled ones). `resolve_slot` gives slot 1 for magic
  0xAA5555AA with active map 0 and selector 0, and refuses `map_uninitialised`
  or `slot_ambiguous`. The evaluator requires agreeing identity reads on both
  sides of each sample (`map_identity_unbracketed`, `map_switching`). Every
  prediction carries provenance: plain-image sha256, revision, slot, table
  and axes, table source, calibration ID, profile-file sha256, and
  `EVALUATOR_VERSION`/`KERNEL_VERSION`. Tests:
  `test_resolution_refuses_by_name` (6), `test_slot_resolution_names_unresolved_state` (5),
  `test_resolution_consumes_w05s_trace_mapping_and_refuses_by_name` (3), and
  `tests/test_target_phi_private_rom.py` (4, separate, skips by name without
  the image). That file stores the a46baa24… image in a temporary W08
  `RomLibrary` and resolves through it. Slot 1 equals the committed table and
  axes, the scaler is 44019, slot 2 refuses `table_unverified`, and the fixture
  accounts identically. **Remaining (vehicle):** the RAM-vs-ROM comparison.
  Read 0xFFFFB260 as two `0x23` reads (63 + 1 bytes), compare with slot 1, and
  record it. Until then the application model's node `target_afr_ram_copy`
  (twin `assumed`, E05.1) blocks every qualified residual
  (`ram_copy_unverified`). The profile also says why the
  default-slot rule alone is not proof: a battery-backed RAM copy can predate
  a reflash.

- [x] **E02.3 — Preserve the October 4 known-answer fixture.**
  Audit existing fixtures first and reuse any newly committed ROM-trace slice
  that satisfies this contract. Source:
  `_capture-ram-study-2026-10-04T204557`, 20:45:56–21:12:49Z. Commit raw
  telemetry, exact retained times, content hashes, channel/decode versions,
  image hash, gates and pairing policy under `tests/fixtures/ground_truth/`
  (or reference an equivalent existing fixture without copying it).
  Retain the two enrichment observations and neighbours, ordinary lookup
  coverage, and all 194 BFS/X pairs. Run the real evaluator to mint expected
  records. Keep private-ROM checks explicitly separate from committed
  telemetry/lookup-vector regression; a missing local ROM is not a pass.
  **Accept:** the two recorded commands are exactly φ **1.1640625**
  (`0x4A80 / 16384 = 0x95 / 128`); the image-scaled X relation
  `min(255, floor(BFS * 128 / 44019))` reproduces **168/194 exact** and
  **26/194 differing by 1–4 raw counts, both signs**. All retained samples
  have lineage. Absent eligible tuples produce a named capture requirement.
  *Done 2026-10-04:* `tests/fixtures/ground_truth/target_phi.yaml` with a new
  slice, `target-phi-slice/` (4,151 rows, 144 KiB). The slice has the command
  word, sentinel, pedal flags, magic/active/selector, `FUEL_STATUS`, `COOLANT_TEMP`,
  `RPM` and `MAF` over the whole capture. It was cut by
  `tools/make_ground_truth_fixture.py --only`; that command is in the tool's
  docstring. It references the existing ROM-trace slice for X, the rpm word and
  BFS (194/180/194 rows, the whole capture) instead of copying them. Both files
  are pinned by sha256, along with per-channel counts, the decode, revision,
  image hash, slot and scaler, and the frozen gates (checked against the
  profile). Every one of the 174 command samples is in `records`, minted by
  `evaluate_session` (`python -m tests.test_target_phi`). Each record keeps its
  row and acquisition time. The fixture reproduces the full local session
  record for record. Measured:
  - Both commands read word 0x4A80 = 0x95 << 7, φ exactly 1.1640625. The test
    reads, out of the fixture with `read_channels`, the last rpm word and flag
    byte before each command. It asserts they are the YAML's vector (rpm raw
    236 / 276 at 0.862 / 0.877 s; flag 1 / 0 at 1.002 s). The forced-last-column
    lookup at that rpm word then gives cell 0x95, word 0x4A80 exactly.
  - The second command's nearest flag read (1.0 s earlier, mid gear change) is
    clear. Neither vector is a 250 ms tuple, so both stay unevaluable.
  - X relation: 194 same-read pairs (separation 0), **168 exact**. The 26 misses
    are −4 ×1, −2 ×1, −1 ×6, +1 ×16, +2 ×2: both signs, 1–4 counts.
  - `capture_requirement: no_eligible_tuples` for both branches.

  Tests: `test_the_two_enrichment_commands_are_exactly_the_forced_last_column`,
  `test_the_x_relation_reproduces_168_of_194_and_its_misses_both_signs`,
  `test_every_october_4_command_sample_is_accounted_for`,
  `test_the_fixture_pins_the_decodes_it_was_written_under`. The private-ROM
  checks are in their own file, which skips by name.

- [~] **E02.4 — Implement and verify the firmware lookup arithmetic.**
  Reproduce raw axis search, clamping, fixed-point bilinear interpolation
  and rounding. Use the measured pedal-enrichment flag's bit 0 to force the
  last load column; do not reconstruct its threshold/hysteresis yet. Trace
  the remaining final-command writers to establish applicability before
  calling the lookup a final-command prediction. Share one pure kernel
  between streaming and stored-session adapters at the appropriate layer,
  and make it the same axis-search/interpolation function W05.1 uses for
  the ROM Maps ECM cell (`src/analysis/rom_trace.py`), never a second copy.
  The RAM table at `0xFFFFB260` is 64 bytes and this ECM caps a `0x23` read
  at 63 bytes (64 draws NRC `0x12`), so E02.2's comparison needs two reads.
  **Accept:** independent known lookup vectors cover both branches, interior
  cells, edges and rounding; E02.3 reproduces. Every eligible steady pair
  must meet the proposed **one Q14 count (1/16384 φ)** target, with nonempty
  ordinary and override coverage. This accuracy is a requirement to verify,
  not a result already measured. Insufficient coverage stays partial.
  *Partial 2026-10-04 (arithmetic done; final-command path and accuracy not
  established):* `src/decode/ecm_lookup.py` (layer 0) implements the
  arithmetic, read from the 2026-10-04 listing of `0x027114`, `0x027194`,
  `0x027448`, `0x026F10` and `0x026F40`. It has the top-down raw axis search,
  clamping with a zero fraction, the truncating Q16 fraction, interpolation that
  truncates toward the first operand both ways, and row-major bilinear. The
  same `search_axis` now serves `rom_trace.axis_position`; the old copy is
  removed. `test_the_rom_maps_ecm_cell_uses_this_search` spies on it, and
  `tests/test_rom_trace.py` stays 29/29. `target_phi.predict` is the pure
  per-tuple entry point for stored and (E04) streaming use. It forces the last
  column from flag bit 0 and applies the enrichment gate.
  `tests/test_ecm_lookup.py` (15) holds hand-worked vectors on the
  ZS44A-20260920 table: interior (four cells, 146), row-only and column-only
  interpolation, truncation where nearest rounding differs (144 vs 145,
  143/131), clamped edges and the corner, the forced column (0x95 at rpm words
  128–288), and the refusal of fractional coordinates. The override branch is
  covered here and in `test_the_pedal_flag_forces_the_last_column...`. E02.3
  reproduces.
  Final-command writers, partly traced (`docs/ecu-firmware-analysis.md`,
  "Target AFR lookup to final command"):
  - Results under 130 are replaced by 0x80, with hysteresis 1, so a lookup of
    129 refuses `enrichment_gate_state_unknown`.
  - A 0xD247 excess scaling is the identity in this image.
  - `FUN_00055B5E` forms the word from (w26F6 + w27DC) × w26C0 >> 14, with a
    cap, a slew limit and fuel-cut zero.
  - `w26C0` is traced (2026-10-04, desk, from `~/ecu-re/out/full.asm`). It is
    the enrichment multiplier: `FUN_00055DB2` writes it at `0x0568B4`,
    choosing by the state byte `0xFFFF14C4` among 0x4000, a ramp, and
    0xFFFF174B << 7.
  - `w26F6` = clamp(0x4000 + L[0xFFFF175C] + w26E8 + w1736) and `w27DC` (a
    curve term clamped to 0..3768) are additive corrections; neither is
    identified.

  So the final word equals the lookup only when the state byte selects
  0xFFFF174B and those corrections sum to zero. No qualified residual is
  issued (`final_command_path_unverified`). That blocker is now the E05.1
  application model's (`data/nissan-xterra-2008/ecu_application_model.yaml`),
  which the twin consumes; the twin's own `final_command.status` is gone.
  **Remaining:**
  1. ~~Desk: identify the `0xFFFF14C4` states and the four correction
     terms.~~ *Traced 2026-10-04 (E05.1):* see
     `docs/ecu-firmware-analysis.md`, "Final-command path, traced". The model
     records it node by node.
     - **States.** `0xFFFF14C5` is a request: 8 when cold (coolant byte < 115,
       65 °C), 1 at lookup 0x80, and otherwise 2/4/6 by rpm band (3,100 /
       3,400 rpm), or 3/5/7 with the pedal flag. `0xFFFF14C4` commits it
       through tick-stepped timers. States 4 and 5 give the lookup word
       directly. States 2, 3 and 6 ramp (+0x180 / −0x130 per tick), and give
       the lookup once flag `0xFFFF2867` bit 7 records the ramp reached it.
       State 1 gives 0x4000. A hold counter, `0xFFFF2947` (or
       `0xFFFF2BA9` < 0x20), forces 0x4000.
     - **Corrections.** `w27DC` is zero unless the multiplier is exactly
       0x4000, so it is zero throughout enrichment. `L175C` and `w26E8`'s
       second term are zeroed under shared flags. `w1736` is a gated, capped
       enrichment. The floor `w1724` and the alternate multiplier writer
       `0x055A04` are calibration-dead, and checked on each image. During
       pedal enrichment there is no cap and no slew.
     - **Confidence.** Read from code; not observed. The correction terms'
       purposes and inputs, and `0xFFFF2947`'s decrement, remain unidentified
       (the model's `uncovered`). Implementing the state machine would still
       need those inputs, so the closure is item 2. Separately, inferred:
       DID 0x1134 reads `0xFFFF26C0`, so it is the multiplier's high byte.
  2. Vehicle, a cheaper closure: one 58-byte read from 0xFFFF26BE covers the
     command word, `w26C0` and `w26F6`. A second read adds `w27DC`. Capturing
     these settles, per sample, whether the command was the lookup.
  3. Verify the one-Q14 target. October 4 has **0 eligible tuples in either
     branch**. Per lookup input (`accounting.input_pairing`): X paired within
     250 ms of 19/174 command samples, the rpm word 9/174, the flag 0/174.
     Their nearest reads were a median 1.50 s, 0.88 s and 1.12 s away. (The
     earlier 120/18 figures were primary-reason counts, not per input.) Next
     measurement: E02.7's capture, reading the profile's `back_to_back`
     windows consecutively.

- [x] **E02.5 — Pair predictions and account for every sample.**
  Emit sample/time/input references, skew, applicability/reason, lookup
  cells/weights, predicted/observed φ and `observed_phi - predicted_phi`.
  Use acquisition-time pairing through the shared comparison machinery;
  initial maximum skew is 250 ms, never a carried-forward five-second RAM
  value. Inspect differences and ratios; use exact-value checks for constant
  branches where regression is unresolvable. Keep same-read firmware update
  timing distinct from acquisition skew.
  **Accept:** all candidates partition into pending, unmatched/unevaluable,
  eligible and excluded states without duplicate target use. Steady
  passes/misses and transient differences/misses are reported separately.
  The 26 known X misses cannot disappear from accounting or justify a wider
  φ tolerance. Missing/skewed/sentinel samples have null qualified residuals.
  *Done 2026-10-04:* `evaluate_session` in `src/analysis/target_phi.py` turns
  each command sample (value or sentinel) into exactly one candidate. Each
  lookup input is paired one-to-one with the samples by
  `src.signals.pairing.match_ordered` (compare's pairing core) within 250 ms,
  and the whole tuple's spread is also held to 250 ms. Nothing is carried
  forward.
  - Each record carries: sample row and acquisition time; input references
    with skew and a `same_read` flag, kept apart from acquisition skew; the
    tuple skew; identity reads and gate values; and the state, primary reason
    and all reasons.
  - It also carries: branch, lookup cells and Q16 weights, predicted and
    observed word and φ, `difference_phi` (observed − predicted), ratio,
    `within_tolerance`, and `qualified_residual_phi`. The qualified residual is
    null unless the sample is eligible and no run-level blocker stands.
  - The summary partitions candidates into pending, unevaluable, excluded and
    eligible, with reason counts and `duplicate_target_use`. It reports steady
    pass/miss per branch and transient differences separately, plus coverage
    and the capture requirement.
  - The X misses are counted in their own check and play no part in the φ
    tolerance (1 word).

  Tests: `test_a_complete_steady_open_loop_override_tuple_is_eligible_and_passes`,
  `test_a_miss_is_counted_not_absorbed`,
  `test_a_transient_keeps_its_diagnostic_difference_separately`,
  `test_a_sample_still_waiting_for_reads_is_pending_while_the_session_records`,
  the 28-case named-state test (all qualified residuals null), and
  `test_every_october_4_command_sample_is_accounted_for`. `duplicate_use`
  counts every observation, command sample or input read, cited by more than
  one record, read back from the records.
  `test_duplicate_use_counts_an_input_read_placed_in_two_tuples` shows it
  failing on a duplicated record. It also shows two commands 0.1 s apart
  sharing one X read: one pairs, the other goes stale.
  Identity and mode brackets use two distinct reads. A read stamped at the
  sample's own instant is only the prior
  (`test_a_switch_right_after_an_identity_read_at_the_sample_is_caught`).
  October 4: 174 candidates, all unevaluable. Primary reasons:
  `input_stale:load` 120, `sentinel` 21, `input_stale:rpm` 18,
  `map_identity_unbracketed` 15. `duplicate_use` 0. Per-input pairing is under
  E02.4.
  The eligible and transient paths are therefore exercised on synthetic
  sessions only.

- [x] **E02.6 — Publish qualified evidence and command-change predictions.**
  Persist through E01; expose through E04. Explain positive φ residual as a
  richer ECM command than the reproduced logic, with omitted logic, timing
  and provenance among possible causes. Link command residuals to the
  investigation without relabelling them as delivery error or a fault.
  For a proposed ROM change, record an exact-image/cell command prediction
  before the intervention, conditional on stated inputs and region.
  **Accept:** cards resolve every value and image reference; unavailable
  coverage remains unevaluable; a zero residual makes no engine-health claim.
  A what-if record cannot be mistaken for measured output or confirmed
  outcome, and creates no ECU write request.
  *Done 2026-10-04 (laptop; no hardware acceptance):*
  - **Evidence.** `src/analysis/twin_evidence.py` (`TWIN_TOOL_VERSION` 2, so
    a re-run supersedes v1 records as a recompute). Every twin record,
    refusals included, is labelled `evidence_class: ecu_command` and
    `engine_health: not_assessed`. It names `image_sha256`, `slot` and
    `table`, and the application model's `blockers`, `omitted_logic` (the
    blocked nodes, new `Applicability.blocked_nodes`) and `uncovered_logic`.
    A refusal resolved no table, so it labels all three `unknown`, never
    `none`. A qualified run keeps the sign *beyond the twin's tolerance*:
    `residual_sign` is positive/negative only past `tolerance_words` (one Q14
    word), `zero` when every residual is within it (a +1-word pass agrees, it
    is never "richer"), else mixed. Values: per-sign counts, `tolerance_phi`
    and the raw max and min. **Behaviour change:** evaluated twin evidence now
    carries `Result.residual`, the raw largest residual, unless signs are mixed.
  - **Investigation.** `src/analysis/twin_investigation.py`
    `investigate_command_residual` opens or updates one investigation per
    direction (`twin:target_equiv_ratio:richer|leaner|mixed|agrees|unevaluable`).
    A positive residual is titled "The ECM commanded richer than its
    reproduced target logic". It has three open hypotheses: omitted logic,
    acquisition timing and provenance. None is credited with the evidence,
    which is the investigation's observation, so none is ranked. Each has a
    next measurement.
    Non-twin evidence is refused (`NotEcuCommandError`). The delivery loop
    refuses twin evidence (`NotEngineResponseError`). A `related`
    engine-response record is provenance only, never an observation.
    Unevaluable evidence supports no hypothesis. Every twin investigation
    carries the unknown that engine health and delivery are not assessed.
    The brain's `twin_session_evaluation` job (`src/control/brain_jobs.py`)
    links every record it mints and returns the ids as `investigations`. A
    re-run re-states the same investigation, which the ledger deduplicates.
  - **What-if.** `src/analysis/twin_command_change.py`. It takes an exact
    base table and either a target image (`change_from_images`, with bytes
    naming every changed range outside the reproduced table) or the Fuel
    Targets editor's `Patch` (`change_from_cell_edits`). It runs
    `target_phi.predict` (the shared kernel) before and after at each cell's
    breakpoint inputs, per cell or per region. Refusals by name:
    `no_change`, `table_mismatch`, `program_mismatch`, `axis_changed`,
    `patch_source_mismatch`, `old_value_mismatch`, `no_op_edit`,
    `duplicate_edit`, `cell_outside_table`, `mixed_direction`,
    `no_evaluable_point`. `record_command_change` writes one E01
    `Prediction` per region, carrying a new `WhatIf`. The quantity must be
    `what_if:…` and only a what-if's may be. It states the base and target
    hashes, assumptions, blockers, `not_reproduced` and every point. A
    region's `magnitude` is set only when every evaluable change is equal,
    zeros included.
  - **Ordering.** At record time a what-if is linked only to the flash the
    caller names (`intervention=`, refused `intervention_mismatch` unless it
    installs the target image), or to a flash of the target image made
    *after the base became current*. That second case is the change already
    made, which the ledger derives as `post_hoc`. A flash of the same image
    *before* the base (re-flashing a previous calibration, e.g. a restore)
    is not this change, so the prediction stays unlinked.
    `MemoryLedger.what_if_intervention` then picks the first matching flash
    after the record time. `command_change_order` and the card use it, giving
    prior, post-hoc or `None`. **Limit:** a cell edit set names no image, so
    it can be ordered only against a flash named at record time; the card
    says so.
  - **Operator entry point.** `tools/record_command_change.py --base
    <revision|sha|image file> (--target … | --patch editor.json) [--region
    ordinary:R0-R1:C0-C1|override:R0-R1] [--investigation ID]
    [--intervention ID]`. It is meant to run before a flash: it computes and
    records on the scoped ledger and requests no ECU write. A held library
    image is read through W08. Otherwise the transcribed table is used and
    `not_reproduced` is `unknown`.
  - **Cards.** The E01 card renders a what-if under `WHAT_IF_NOTICE` in the
    predictions section (`WhatIfView`). Every value carries the prediction's
    id. The card also shows its order (`order`, `ordered_against`,
    `order_note`). **Behaviour change:** card JSON's `PredictionView` gains
    `what_if`, which is `null` for every plain prediction. For a what-if,
    `intervention`/`order` are the flash it is ordered against.
  - **Exposure.** No new route. `/control/brain/memory?kind=prediction` and
    `/control/brain/investigations/{id}/card` serve what-ifs, with MCP
    `query_memory`/`get_investigation_card` byte-equal. `/control/brain/evidence/{id}`
    refuses a what-if id (400). The dashboard's BrainPage does not render
    card predictions yet; that is E04.5's surface.

  Tests: `tests/test_twin_investigation.py` (9) has
  `test_twin_evidence_names_its_class_image_and_the_logic_it_omits` (the image
  resolves to the revision through `match_image`) and
  `test_a_positive_residual_reads_as_the_ecm_commanding_richer`, where every
  number on the card is from a record.
  `test_a_zero_residual_makes_no_engine_health_claim`,
  `test_unavailable_coverage_stays_unevaluable` and
  `test_command_residuals_and_engine_response_never_cross`,
  `test_a_pass_within_the_tolerance_agrees_and_never_reads_as_richer`,
  `test_a_refusal_names_the_image_it_could_not_read` (logic `unknown`) and
  `test_the_brain_twin_job_links_each_minted_record_to_its_investigation`
  cover the other cases. `tests/test_twin_command_change.py` (23) checks the cell edit set
  (`rom-targets-known-edit.json`): k106 r7c4 φ 0xA2/128 → 0xA3/128, +1/128,
  which equals the 20260920 transcription and the image-pair path.
  20260920 → 20261004 (Fuel Comp restore) gives `no_change` on both branches,
  64 + 8 points. `test_a_what_if_cannot_stand_where_measurements_go`
  refuses it as an outcome's evidence, as an investigation observation and in
  `detect_discrepancy`/`resolve_evidence`, and refuses the label being dropped.
  `test_computing_and_recording_a_what_if_requests_no_ecu_write` runs it in a
  clean process. No `src.control`/`src.daemon`/`src.dashboard`/`src.mcp_server`,
  python-can, serial or non-naming `src.collection` module gets loaded.
  `test_the_brain_memory_and_card_routes_serve_a_what_if_unchanged` checks
  HTTP equals MCP.
  `test_re_flashing_a_previous_image_is_not_that_flash_again` covers the
  restore case: 20261004 → 20260920 stays unlinked past the September flash,
  then reads prior against the later flash, on the card too.
  `test_a_named_intervention_must_install_the_target` and
  `test_the_cli_records_a_what_if_from_an_image_pair_or_a_patch` cover the
  rest.
  `tests/test_twin_command_change_private_rom.py` (2) skips by name without the
  images and passed here. On the real images, the 20260920 → 20261004 what-if
  is `no_change`, and `not_reproduced` names the "Fuel Compensation load
  axis" (0x00CB2A-0x00CB38) and "Fuel Compensation (Map 1)"
  (0x0E0671-0x0E075F). k106 → 20260920 changes exactly r7c4, with nothing
  else outside the table but checksums.
  **Limits:** a what-if has no noise floor (the command is a word, not a
  noisy measurement), so the ledger allows only an `unresolvable` outcome
  against it. Every real twin record is still blocked (E02.2/E02.4), so
  qualified residuals and the richer/leaner investigations run on synthetic
  sessions only.

- [ ] **E02.7 — Qualify missing regions with targeted measurements.**
  First close E02.2's RAM-copy check and E02.4's arithmetic/applicability
  gaps. If October 4 lacks steady open-loop tuples, capture the declared
  channels at sufficient cadence across ordinary cells and sustained
  enrichment, preserving transition neighbours and an independent drive.
  Record coverage before and after; qualify only cells/regimes supported by
  evidence. **Vehicle required.**
  **Accept:** a tool report names each qualified branch/region, pair count,
  timing distribution and pass/miss count under frozen gates. Remaining gaps
  name the next measurement. E04.3 must also pass before live enablement.

## Follow-on logic, excluded from this slice

E05 owns implementation beyond the first slice. These measurements do not
broaden E02's applicability until their corresponding paths qualify:

| Unresolved logic | Next evidence |
|---|---|
| Cold/warm-up, closed-loop overrides and protection logic | Trace command writers/flags; capture cold-to-warm and mode changes with those inputs. |
| Transient enrichment and DFCO entry/recovery | Faster paired target/input/flag capture through tip-in, lift-off and recovery; preserve internal-update and acquisition uncertainty. |
| Pedal threshold and hysteresis | Trace the input of curve `0xCEDB`; repeat threshold crossings while observing pedal, flag and command. |
| Map switching | Observe an operator-selected switch with `ECM_MAP_SELECT_INPUT`, selector and active map; compare RAM table with library slot before/after. |
| Full BFS qualification, throttle-fill units and Load Point Scaler source | Fuel Comp X, purge, sensor fallback and idle-air correction structure are traced; Fuel Comp acts inside BFS before its filter, transient terms and ceiling. Capture MAF/rpm/BFS, fill, corrections and filter state with acquisition pairing to verify exact arithmetic/update order across steady and transient regions; retain neighbouring-byte controls. Physical units and remaining constant/diagnostic/learning-loop meanings stay open. E05 owns reconstruction; B08 evaluates behavior context. |
| Timing trims, slot `+0x260`, unnamed curves/maps | Identify each reader/output first; capture timing flags and independent timing/cam/throttle references appropriate to the candidate. |

Findings and confidence belong in profile data and
[firmware analysis](../../ecu-firmware-analysis.md), with evidence links.
Do not declare any of these decoded from correlation alone.
