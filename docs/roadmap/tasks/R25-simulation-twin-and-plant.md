# R25 — Simulation, twin and plant analysis: one pipeline, one kit, split files

Part of the 2026-11-12 review ([R22](R22-refactor-README.md)). Scope: the
simulation half of `src/analysis/` (`sim_*`, `twin_*`, `paired_scenarios`,
`plant_validation`, `fuel_plant*`, `engine_dynamics`, `ecu_*`, `target_phi*`,
`rom_*`, `change_evaluation`, `candidate_promotion`, `artifact_lineage`, the
`*_evidence` minters), `tools/rom_sim.py` and `rust/`. Structure, duplication
and efficiency only; the physics of these models is [M01](M01-physics-model-accuracy.md).
Counts are review snapshots; locate code by symbol. Nothing here has been
implemented. Shared acceptance rules are in R22.

**Context.** R01–R21 did not cover this half (only R11 touched it, and its
time-lookup consolidation is in use). It was written quickly through E05–E09
and it shows one pattern throughout: a second module needs something the first
keeps private, and imports the underscore name. There are 23 such imports
(`sim_montecarlo` → `paired_scenarios` 10, `plant_validation` →
`fuel_plant_eval` 6, `sim_cells` → `paired_scenarios` 4, `target_phi_stream` →
`target_phi` 2, `paired_scenarios` → `rom_effects` 1). Most items below end by
making one of those a public name with one owner.

The cross-cutting copies found here (the reasoned-refusal exception, `_Strict`,
load-YAML-and-hash, canonical hashing, `_aware`) are in
[R24](R24-shared-primitives.md) and are prerequisites for R25.2 and R25.5.

**Persisted hashes.** Design, contract, rules and evidence hashes are stored.
Any item that moves a model or a serializer needs a fixture proving the hash
did not change.

- [x] **R25.1 `run_paired` runs the whole ECU chain eleven times per point.**
  `paired_scenarios.run_paired`: inside the loop over points, each point with
  a λ delta runs
  `for case in cases.values(): cp = _pair_point(kit, stages, sides, point, stfts, {**overrides, **case})`.
  `_pair_point` calls `_ecu_chain` for both sides (the ROM table lookups) and
  then the fuel chain. The Xterra profile has ten sensitivity cases, and the
  ECU chain does not depend on plant overrides, so ten of the eleven ECU
  evaluations per side repeat the first. `sim_montecarlo._build_cache` already
  has the right shape (ECU once per combination, plant vectorized per draw).
  - [x] Profile `run_paired` on a 100-point grid design first; record the
    split between ECU chain and plant. *(2026-11-13, HEAD a154a241, cProfile,
    10x10 `design grid` 91169e3b… on ZS44A-20260920 → 20261004, 99 of 100
    points carry a λ delta, 1,090 `_pair_point` calls = 1 + 10 cases per
    point.)* Of 4.13 s profiled: ECU chain (`_ecu_chain`, 2,180 calls) 0.41 s
    (10 %); pulse stage (`stated_pulse`, 2,178) 0.45 s (11 %);
    `plant_parameters` (2,178) 0.24 s (6 %); the plant the remaining ~72 %,
    and most of that is not the kernel: per call, building the one-row sample
    frame (`stated_plant_samples`) and turning it into inputs (`plant_inputs`)
    took ~1.7–1.9 ms against ~0.17–0.29 ms for `fuel_plant.predict`, and
    neither reads a plant parameter.
  - [x] Compute the two ECU sides once per point and vary only the fuel
    closure per case (`_pair_loop` already takes one). `paired_scenarios.
    _PointChain`: both `ecu_chain`s once per point, each side's pulse stage
    and plant inputs once (lazily, as before), one `plant_parameters` per case
    per run (`functools.cache`, resolved on first use); a case reruns only
    `twin_runner.plant_predict_inputs`. `twin_runner.plant_predict` is split
    into `plant_stage_inputs` + `plant_predict_inputs` (same arithmetic, still
    the runner's stage). Pinned by
    `test_run_paired_runs_each_ecu_side_once_per_point_and_reruns_only_the_plant`
    (call counts 2n ECU / 2n pulse / 2n(1+cases) plant, and the range equals
    the per-case full `_fuel_chain` recomputation exactly). Measured, HEAD vs
    tree, best of 3, shared laptop: 100-point grid 2.10–2.40 s → 0.64 s
    (~21 → 6.4 ms/point); 200 points of the WOT drive design f4332b8d… 4.15 s
    → 0.89 s (20.7 → 4.4 ms/point). `run_monte_carlo` (20 draws) only gains
    in its `run_paired` self-check: grid 0.50 → 0.41 s, 917-point drive design
    2.55 → 2.58 s (noise; `_build_cache`'s per-combination ECU dominates).
  - Must not change: `relative_delta_range_over_assumptions`, bit for bit.
    Monte Carlo's `_verify_assembled` self-check compares against `run_paired`
    and will say so. *Held:* `run_paired(...).to_dict()` (plain, and on draw 1's
    overrides) byte-identical HEAD vs tree on the grid design and on 200 drive
    points; `tools.rom_sim run` (20 draws, seed 1) `result.json`,
    `traces.parquet` and `report.html` byte-identical HEAD vs tree on both
    designs, and across two HEAD runs of one seed.

- [x] **R25.2 `run_monte_carlo` copies `run_paired`'s prologue, and the copies
  have drifted.** Both carry the `image_hash_mismatch` loop, the
  plant-identity check (Monte Carlo's message omits `hardware_configuration`),
  the `learned = {…}` derivation (Monte Carlo writes a literal `0.0` where
  `run_paired` uses `NEUTRAL_TRIM_PCT`) and the `unresolved` assembly.
  `sim_montecarlo` imports ten private names from `paired_scenarios`
  (`_LOAD_BYTE`, `_PLANT_OUTPUTS`, `_ecu_chain`, `_fuel_side`, `_pair_loop`,
  `_refuse_recorded_sources`, `_resets`, `_Side`, `_side`,
  `_with_contract_mode`).
  - [x] A `paired_run.py` with public `check_run_inputs`, `learned_state` and
    `plant_unresolved`, used by both; publish the ten names from one owner.
    `src/analysis/paired_run.py` owns the prologue (`check_run_inputs`:
    recorded sources, contract and mode, image hashes, plant identity with
    hardware configuration, then the resets; `learned_state`;
    `plant_unresolved`), plus `refuse_recorded_sources`,
    `check_contract_mode`, `candidate_resets`, `NEUTRAL_TRIM_PCT` and
    `LEARNED_TRIMS`. `paired_scenarios` publishes `PLANT_OUTPUTS`, `PairSide`,
    `pair_side`, `ecu_chain`, `fuel_side`, `pair_loop` (and `FuelFn` in
    `__all__`); `_LOAD_BYTE` is replaced by `paired_steady.FILL_BYTE`, its
    owner. Tests: `test_both_paired_entry_points_refuse_a_plant_mismatch_in_the_same_words`,
    `test_the_monte_carlo_learned_state_is_run_paireds`,
    `test_monte_carlo_imports_no_private_name_from_the_paired_modules`
    (AST, no `_` name from either module).
    Behaviour change, deliberate: Monte Carlo now runs the whole
    `check_run_inputs` before `sample_draws`. With several faults at once,
    the argument refusals (`draws_invalid`, `parameter_unknown`) therefore
    come after the contract, image, plant and event refusals rather than
    before (for example, image + recorded source was `image_hash_mismatch`
    and is now `recorded_value_refused`). Every single-fault input gives the
    same reason and text as before, and nothing pinned the old order.
    Verified 2026-11-13: `run_paired` dumps and `rom_sim run` outputs
    (result.json, traces.parquet, report.html) were byte-identical to HEAD on
    a new 68-point grid, the 917-point drive design, seeds 2/7/13 and nonzero
    trims. `run_paired` was 5.4–6.9x faster, and Monte Carlo was no slower.

- [ ] **R25.3 Two drivers of the simulate pipeline.** `rom_editor._simulate`
  and `tools/rom_sim.py` `cmd_run` (153 lines) both run design →
  `require_base` → `run_monte_carlo` → `compare_roms` → axes → `aggregate_cells`
  → `ignition_timing_block` → `ignition_timing_points`, and each has its own
  `_template` building the same `PairedScenario`. The tool hard-codes
  `slot_source="default_slot"` where the library uses `SLOT_SOURCE`, and the
  two result documents already differ (`cells` against `cell_table`). A
  pipeline change has to be made twice, and the dashboard and the CLI can
  disagree without anything noticing.
  - [ ] `rom_editor.simulate_cells(…)` and one `scenario_template(…)`; each
    caller keeps its own document assembly.
  - [ ] A test that runs one edit through both and compares the cell results.

- [ ] **R25.4 Three kit types, three `load_kit`s, and the same YAML parsed
  several times per request.** `paired_scenarios.SimKit` and
  `twin_runner.TwinKit` share eight fields; the `RunnerKit` Protocol exists
  only to bridge them, and `TwinKit` has no importer outside its module.
  `paired_scenarios.load_kit`, `engine_dynamics.load_kit` (a different
  3-tuple) and `sim_montecarlo.load_sim_kit` share a name;
  `load_sim_kit` loads the plant, then `kit_for_plant` loads it again, reads
  every `fuel_plant*.yaml` and calls `load_b06` a second time.
  `load_application_model` (130 ms per parse of
  `ecu_application_model.yaml`, per `src/yaml_load.py`) is called
  independently from `control/brain_state`, `control/twin_identity` (twice),
  `dashboard/routes/rom_maps`, `paired_scenarios.load_kit` and
  `twin_runner.load_twin_kit`, with no cache.
  - [ ] `SimKit(TwinKit)`; delete `RunnerKit`;
    `load_kit(profile_dir, *, plant_version=None)` with one plant load; rename
    `engine_dynamics.load_kit`.
  - [ ] The loaders go through R24.3's cached `load_yaml_model`. The sha256
    stays the hash of the raw file bytes.

- [ ] **R25.5 Evidence minting rebuilds its envelope per tool.** There are
  eleven `Evidence(` constructions across `delivery_evidence`,
  `compare_evidence`, `twin_evidence`, `behavior_residual_evidence`,
  `candidate_promotion` and `change_evaluation`; only
  `delivery_evidence._Minter.record` is shared. `_session_window` exists three
  times with the same footer-else-started-at rule and three error messages,
  and the twin copy does not pass `started` through `_aware`. `Provenance(…)`
  is built six ways. Each `mint_*` hand-builds its refusal as a second
  `Evidence`. `mint_fuel_pressure_droop` is 318 lines with the droop math
  inline.
  - [ ] A golden-record test per tool first: records are persisted and hashed.
  - [ ] Move `_Minter` to `src/evidence/` as `EvidenceEnvelope` with
    `session_window()`, `record()` and `unevaluable()`; the other mints use it.
  - [ ] Split `_window_means`, `_bracketed_state` and `_long` out of
    `mint_fuel_pressure_droop`.

- [ ] **R25.6 `plant_validation.py` (2,920 lines) is three modules, and one
  scoring rule is written three times with two answers.**
  - Seams (imports already run one way): schema and rules, lines ~141–850
    (`InterventionCase` alone is 206 lines) → `plant_validation_schema.py`;
    the λ/trim intervention evaluation stays; ECU-output evaluation from
    ~1621 (`ecu_output_reads`, `ecu_effect_test`, `evaluate_ecu_outputs`,
    `score_ecu_outputs`, `ecu_null_control`) → `plant_validation_ecu.py`.
  - `lambda_effect_test` and `trim_effect_test` share about 25 identical
    lines (reset filter, `match_pairs`, direction and within scoring, an
    11-key result tail). `ecu_effect_test` has a third copy.
  - **The copies disagree when no pair predicts a change.** The λ and trim
    tests set `direction = 0.0`, which fails `_verdict`. The ECU test sets
    `direction = None`, and `_ecu_verdict` returns
    `pass … direction_not_applicable`. One of these may be intended (a schema
    comment near `EffectAcceptance` says such a test "has no direction"); it
    should be one function that says which.
  - `InterventionVerdict` and `EcuOutputVerdict` are two copies of an
    HMAC-sealed dataclass.
  - [ ] Operator question first: is "no signed pair" a pass or a fail for the
    λ and trim tests? Record the answer here.
  - [ ] One `score_effect(observed, predicted_mid, band, acceptance)` used by
    all three; a `Sealed` mixin; then the file split.
  - Must not change: `rules_sha256` values and seals.
    `ecu_output_reads` also repeats the first sixty lines of
    `fuel_plant_eval.build_samples`; that part is R24.7.

- [ ] **R25.7 The ignition-timing slice lives inside two unrelated files.**
  `paired_scenarios.py` (2,409 lines) carries about 700 lines under the banner
  "E09.7: ignition timing, ECU side only" (`TimingState`, `BaseTimingImage`,
  `timing_tables`, `ignition_timing_at`, `ignition_timing_cells`, …) that
  `run_paired` never calls. `sim_cells.py` (1,539) has the other half
  (`timing_reach_reasons`, `ignition_timing_block`, `ignition_timing_points`)
  and imports eight timing names plus four private ones back.
  - [ ] `src/analysis/sim_timing.py` holding both (about 1,000 lines);
    re-export `ignition_timing_*` from `sim_cells` until `rom_editor`,
    `tools/rom_sim` and the tests are repointed.

- [ ] **R25.8 Three "operating point" types and re-declared vocabulary.**
  `sim_design.DesignPoint` (pydantic), `sim_montecarlo.DesignPoint` (a
  different frozen dataclass of the same name) and
  `paired_scenarios.OperatingPoint`, with adapters between them. The rail and
  battery source vocabulary is declared three times, twice as identity dicts
  (`_RAIL_SOURCES = {"stated": "stated", …}`) whose only job is to narrow a
  `str`. `"fuel_comp_load_byte"` is a constant in one module, an alias in two
  and a bare literal in two more; `"stoich_air_per_open_ms"` is a literal in
  two files and a constant in a third; `fuel_plant_eval.py` multiplies by a
  literal `16384` where `injector_pulse.PHI_Q14` exists.
  - [ ] One declaration of the source literals; rename the Monte Carlo point
    `McPoint`; export `FILL_BYTE` and `LUMPED_SCALE` from one module.
  - Designs are hashed: field names and values stay.

- [ ] **R25.9 Functions that are several steps in one.**
  - `candidate_promotion.evaluate_candidate` (235 lines): four gates,
    numbered 2, 1, 3, 4 in the order they run. Split into
    `_contamination_reasons`, `_independent_drives`, `_judge_test_drives`,
    `_known_answer_gate`, and number them in order.
  - `sim_design.build_drive_design` (262) and `build_grid_design` (172) share
    the "attach plant-derived MAF, MAP, battery and rail sources and build a
    point" step: `_place_point`.
  - `artifact_lineage.py` (1,848): the last ~230 lines are an AST reader of
    `src/signals/derived.py` constants (`parse_derived_definitions`, …),
    unrelated to catalog lineage → `derived_definitions.py`; the
    session-reading recheck group → `lineage_recheck.py`.
  - `target_phi.py` (1,818): the pydantic definition (~25 classes) →
    `target_phi_definition.py`. `target_phi_stream.py` already imports the
    private `_assess` and `_Series`; publish them.

- [ ] **R25.10 "Nearest breakpoint, ties low" derived three times.**
  `sim_cells.nearest_index(hit)` (integer rule), `rom_trace.nearest_cell_index`
  (float rule) and `rom_trace.nearest_index(axis, value)`: the same tie rule,
  and one name with two signatures.
  - [ ] `AxisHit.nearest` and `nearest_breakpoint(breakpoints, value)` in
    `src/decode/ecm_lookup.py`, which already owns the axis search. The tie
    cases in `test_sim_cells` pin it.

- [ ] **R25.11 Small items.**
  - `fuel_plant.load_b06` imports `Eligibility` inside the function. A
    transitive import walk shows no cycle behind it; hoist it.
  - `_byte` / `_word` range validators are copied in `ecu_state_models.py`,
    `decode/fuel_comp_load.py` and `decode/air_charge.py`, each with its own
    `BYTE_MAX` / `WORD_MAX`.
  - `ManifoldPlantProfile.get` and `ParameterRegistry.get` are the same scan.
  - Five private git-subprocess helpers (`artifact_lineage`,
    `plant_validation`, `validation/report`, `control/update_runner`,
    `dashboard/version`). One `git_output()`.
  - `fuel_plant_eval.py` writes `sorted(sessions)` in four places. It looks
    like determinism rather than chronology; confirm none of the consumers
    reads it as time order (`AGENTS.md`, "Never order sessions by directory
    name").

- [ ] **R25.12 Operator question: where does one ECM program's model live?**
  `ecu_state_models.py` (1,765 lines) is the decoded RAM and calibration
  logic of a single program (`0xFFFF2EE0`, `UPREV_MAGIC`), and
  `paired_scenarios.py` hard-codes `0xFFFF283F` and `COOLANT_BYTE_OFFSET = 50`
  in its timing section. `AGENTS.md` wants vehicle facts in data. These are
  bound to the program-identity check and to the Rust contract vectors, so
  moving them to YAML is not a cheap win. The cheap step is to name the
  module for its program, the way `recipes/nissan_xterra_2008.py` is. Whether
  a second platform's ECM model is planned decides how far to go.

## Looked at and fine

- Python and Rust constants (`CONTRACT_VERSION`, `KERNEL_VERSION`,
  `STATE_MODEL_VERSION`) are duplicated by hand and pinned by
  `tests/test_ecu_twin_contract.py` and `test_ecu_twin_state_vectors.py`; both
  sides run the same vectors. The Rust core has no importer under `src/`
  (`tools/bench_ecu_twin.py` and tests only), which is E06's design.
- `sim_montecarlo`'s shape is sound: no per-draw re-read or re-derivation.
- `twin_runner._run_bfs/_x/_phi/_pulse/_plant` look alike but build
  stage-specific results; a base class would not pay.
- `sim_report.py` is the only HTML renderer in `src/`.
- This half does not bypass `analysis/compare.py`: pairing goes through
  `match_ordered` / `PairingPolicy`.

## Not reviewed

Bodies of `rom_editor` beyond `_simulate`, `rom_levers`, `rom_effects`,
`rom_map`, `rom_edit_spec`, `ecu_application`, `twin_command_change`,
`twin_investigation`, `air_charge_twin`, `sim_report`'s SVG code, the Rust
`target_phi.rs` / `fuel_comp_load.rs` / `uprev.rs`. No profiling was done:
R25.1 and R25.4 give call counts, not seconds.
