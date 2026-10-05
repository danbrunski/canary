# E07 — engine physics model coupled to the ROM-backed ECU twin

Requested 2026-10-04: couple the ECU twin to an engine simulator, validate it
against logged telemetry, and simulate tuning changes by loading complete
ROM images. Follow [E00](E00-second-brain-README.md), [E05](E05-ecu-application-twin.md)
for ECU semantics and [E06](E06-rust-twin-core-trial.md) for the Rust trial.
This document plans the simulator; it does not claim that a plant model or
validated tuning predictor already exists.

## Two models with an explicit interface

The **ECU twin** models the controller: sensor values/state plus an exact ROM
produce injector commands, ignition timing, throttle/cam commands and internal
quantities. The **engine plant** models the physical response: actuator
commands, environment, hardware parameters and physical state produce air
and fuel flow, mixture, pressures, torque and sensor observations. The two
exchange timestamped values through a simulation scheduler. Target AFR is a
controller objective; the plant must calculate delivered mixture from air
and fuel rather than setting measured AFR equal to the target.

Start with a mean-value model (behavior averaged over engine cycles), using
mass balance and bounded physical state plus empirically identified
parameters. Add cylinder-resolved combustion only for a demonstrated need.
The ROM does not supply all plant parameters: injector flow/dead time,
pressure behavior, breathing, friction and spark-to-torque response need
hardware facts or measurements. Engine geometry and other vehicle facts
remain profile data. Keep ECU integer arithmetic distinct from the plant's
continuous equations and numerical solver.

Loading the whole ROM means W08 accepts the complete image, verifies its
identity and resolves all currently modelled code paths/tables/constants.
It does not make undecoded bytes executable. A coverage manifest lists
consumed, inactive-for-this-scenario and unknown/unmodelled regions. A change
whose dependencies reach unknown logic cannot receive a qualified prediction
for the affected outputs. Full SH7058 instruction/peripheral emulation is a
separate possible implementation, not an assumed capability of this plant.

**Current firmware consequence:** Fuel Comp is applied inside BFS; BFS feeds
both Target AFR X and timing-trim X. The chain is therefore approximately
MAF/rpm × K → Fuel Comp → manifold-filling/transient terms and limits → BFS
→ target/timing lookups → final commands. Use the decoded exact operations
and order, not this explanatory shorthand. The throttle-model fill path also
includes learned idle-air correction, purge and throttle-sensor fallback.
See [the updated decode](../../ecu-firmware-analysis.md#fuel-comp-load-input-0xffff118a-and-how-bfs-is-formed).
A Fuel Comp edit cannot be simulated solely as a percentage change in fuel
while holding every downstream ECU quantity fixed.

## Simulation modes and first slice

| Mode | Supplied from logs/scenario | Predicted and checked | Claim allowed |
|---|---|---|---|
| ECU replay | Recorded sensors and declared internal-state checkpoints | ECU intermediates/commands | Reproduction of decoded controller logic |
| Plant replay | Recorded actual actuator inputs, environment and explicit boundary conditions | Physical outputs and sensor response | Plant fidelity under the observed command history |
| Conditional ROM comparison, first slice | Same prescribed rpm/airflow/thermal conditions and initial state for each ROM | Qualified ECU command chain, delivered fuel and steady lambda | Tuning effect conditional on those fixed operating conditions |
| Coupled rollout, later | Driver demand, environment, road/load and initial states | ECU commands and evolving engine/driveline/sensor state | Dynamic tuning effects within the validated envelope |

First qualify warm, steady open-loop fuel response in the existing naturally
aspirated configuration. Use recorded airflow and imposed rpm as explicit
boundary conditions; predict delivered fuel/lambda only when injector command,
flow/dead-time and pressure assumptions are supported. Unknown final injector
logic limits ROM comparison even when plant replay using measured injector
pulse is possible. Retain commanded φ as a separate ECU output/reference.
This slice does not predict new airflow, rpm, torque or acceleration.

Then add throttle/manifold filling, breathing and sensor dynamics, followed
by torque/friction/driveline and qualified spark/cam effects. Cold start,
wall wetting, purge transients, knock, exhaust temperature and boost need
their own state/parameters and validation before extending claims. The
supercharger is a hardware intervention with a new plant parameter set.

## Checklist

- [x] **E07.1 — Specify controller/plant/scenario contracts.**
  Define typed units, clocks, solver step, ECU update schedule, actuator
  limits, sensor sampling/quantization/delay and state initialization/reset.
  Declare each quantity as measured input, prescribed boundary, inferred
  state or predicted output. Track ECU program/image/slot, plant version,
  hardware configuration, parameter evidence and scenario hash.
  **Accept:** all four modes have unambiguous ownership of every input/state;
  no circular same-step dependency is hidden. Predicted outputs cannot also
  enter as measured inputs except for an explicitly labelled state-estimation
  mode whose scores are separate from open-loop prediction.
  *Done 2026-10-05 (specification; nothing simulates yet):*
  - **Schema and data.** `src/analysis/sim_contract.py` (layer 5, generic) and
    `data/nissan-xterra-2008/engine_sim_contract.yaml` (`contract_version` 1,
    `plant_version: unbuilt-0`). It declares units and the clocks: the ECU
    tick (stated 10.24 ms, `period_status: unverified`), a crank-segment event
    clock, a 1.28 ms solver step (`chosen`) that must divide the ECU tick, and
    irregular acquisition. The ECU task order φ → dead time → pressure
    correction → transients is given as entries 37/41/42/44 of the table
    `0x0219D0` that `0x020EF2` walks. It also holds 3
    reset events, one hardware configuration with 6 parameters (each
    `known`/`identifiable`/`requires_measurement` with evidence) and 39
    quantities.
  - **Quantity types.** Each quantity is `ecu`/`sensor` (integer width,
    signedness, RAM word, scale, a `confidence` and an optional `note`) or
    `plant`/`actuator`/`environment`/`driver` (continuous). Scored channels
    (`observed_as`) are kept apart from leak-only aliases (`leak_aliases`:
    PID 0x44 for φ, PID 0x0C for the rpm copy). A sensor states its sample
    clock, quantisation and delay; an undecoded quantisation or an unmeasured
    delay is left absent, never guessed. An
    actuator states its command and limits. Dependencies are `same_step` or
    `previous_step`.
  - **Modes.** All four modes assign every quantity exactly one role. The
    refusals are listed under Accept below.
  - **Manifest cross-check.** `check_against_application_model` checks the
    task and cycle start against E05's `schedule`. It checks each RAM word's
    width and signedness and each `manifest_node`'s write. Result: empty.
  - **Scenario.** `Scenario` carries the ECU program, image and slot (with
    its source), the plant version, the hardware configuration and parameter
    evidence. `scenario_hash()` is computed from the canonical form, never
    supplied. `check_scenario` refuses by name: `contract_mismatch`,
    `ecu_identity_count`, `active_slot_unresolved`, `program_mismatch`,
    `plant_identity_missing`, `boundary_missing`, `initial_state_missing`
    (missing history never defaults to cold or steady), and others.
  - **Accept (tests/test_sim_contract.py, 37 pass).**
    - Ownership: `test_all_four_modes_own_every_quantity_exactly_once`.
    - Cycles: `test_a_hidden_same_step_cycle_is_refused`. Making the trim
      read this tick's lambda closes injector → fuel → λ → sensor → trim, and
      the rollout is refused naming the cycle.
    - Initial values: `test_a_previous_step_read_needs_an_initial_value`.
    - Predicted-as-measured, aliases included:
      `test_a_predicted_output_cannot_enter_as_a_measured_input` (PID 0x44
      feeding ECU replay) and
      `test_a_supplied_value_cannot_be_read_from_the_ecus_own_reading_of_it`.
      A sensor's channels count for the quantity it measures:
      `test_a_sensor_carries_what_it_measures_even_when_the_mode_ignores_it`.
      `test_pid_0x44_is_a_leak_only_alias_of_phi`.
    - A comparison or rollout takes stated values only. The contract refuses a
      recorded channel relabelled as a prescribed boundary
      (`test_a_relabelled_recording_cannot_feed_a_comparison_or_rollout`).
      `check_scenario` refuses `recorded_value_refused`
      (`test_a_comparison_scenario_takes_stated_values_only`) and, in a
      replay, `predicted_as_measured`
      (`test_a_replay_boundary_cannot_be_read_from_a_computed_quantitys_channel`).
    - State estimation: `test_a_state_estimation_mode_may_assimilate_and_scores_separately`
      and `test_an_open_loop_mode_cannot_assimilate`.
    - Mode rules: `test_a_comparison_or_rollout_takes_no_measured_input`,
      `test_replay_modes_stay_on_their_side`,
      `test_the_ecu_reads_the_plant_only_through_a_sensor`,
      `test_a_held_fixed_internal_quantity_states_its_assumption` and
      `test_missing_history_never_defaults_to_a_state`.
  - **Open, recorded in the data (not acceptance):**
    - The ECU tick period is `unverified`. The manifest still lists the
      `0x020EF2` loop period as undecoded (`enrichment_tick_rate`), so
      enrichment timing stays in ticks
      (`test_the_ecu_tick_is_indexed_in_the_main_loop_table_and_its_period_unverified`).
      The firmware doc's "10.24 ms task table (`0x21A30`)" was corrected:
      `0x21A30` is the address of entry 24 of `0x0219D0`.
    - The injector pulse's 10 µs scale and its index-0 = bank-1 mapping are
      inferred. It is observed through the cycle-start copy `0xFFFF2710`,
      one cycle late
      (`test_undecoded_facts_are_absent_or_labelled_inferred`).
    - Sensor delays (λ from B06.3, MAF, coolant, rpm) are unmeasured.
    - The pedal channel is not decoded.
    - Injector flow, dead time and MAF bias `requires_measurement`.

- [x] **E07.2 — Load complete ROMs with effect coverage.**
  Reuse W08 and E05's application manifest for all modelled fuel, timing,
  target-AFR, axes, constants and branch settings. Diff baseline/candidate
  images and trace changed bytes to affected computations and outputs.
  Preserve unknown bytes; do not infer their meaning or treat them as having
  no effect. Simulation edits create isolated candidate artifacts/provenance.
  **Accept:** identical ROMs and states produce identical results; a known
  fuel edit changes the correct path, including BFS/axis consequences.
  Changed unknown dependencies, unsupported program or unresolved active
  slot produce named partial/unevaluable outcomes. Images stay outside Git;
  no simulator path sends an ECU programming request.
  *Done 2026-10-05 (effect coverage; the simulator outputs themselves are
  E07.3/E07.6's):*
  - **Load.** `src/analysis/rom_effects.py` (layer 5). `load_rom` takes the
    whole image and checks size, the calibration ID against W08's map, and
    E05's `check_image`. It then resolves every table, axis, constant and
    calibration condition each manifest node reads on the active slot. A slot
    table's other-slot copies are classed `inactive_for_scenario`.
    `outside_decode` lists constants or conditions off their decode. The
    image is kept whole. `coverage` counts the calibration bytes as consumed
    1,185, inactive 3,336, mapped-unmodelled 74, checksum 8, unknown 52,741
    and erased-unknown 172,032 (ZS44A-20260920, slot 1).
  - **Manifest and map additions (BFS consequences).** Two nodes were added
    to `ecu_application_model.yaml`, now `model_version` 3:
    `fuel_comp_load_byte` (`0x03C3A4`, the fill tables) and
    `base_fuel_schedule` (`0x03C62C`, BFS written at `0x03CAE4`/`0x03CB46`:
    Fuel Comp, its axes, K, the BFS ceiling and cal `0x8AE1`). The existing
    `load_input_x` node, whose span `0x03CC22` lies in the same function, was
    moved from `0x03C4D0` to `0x03C62C`: after the split, `0x03C4D0` is the
    throttle-area routine. Both are `not_modelled`. 15 RAM words were
    declared for them, and `load_input_x` now depends on
    `base_fuel_schedule`. X stays observed, so no output's path or blockers
    change: the E05/E02 tests pass. `rom_map.yaml` gained `bfs_ceiling`
    (`0xD8B3`, 16 × u8).
  - **Diff.** `compare_roms` classes every changed byte as `consumed`,
    `inactive_for_scenario`, `checksum`, `mapped_unmodelled` or `unknown`. It
    traces changed nodes forward through `depends_on` to each output,
    including through observed nodes. Each output is `unchanged`, `changed`,
    `unevaluable` (with reasons) or `unknown`:
    - `unmodelled:<node>`;
    - `observed_not_predicted:<node>`;
    - `held_fixed:<quantity>`, when an E07.1 mode supplies it;
    - `outside_decode:<node>`;
    - `unknown_bytes_changed`.

    `moved_lookups` names the tables read at a moved operating point. The
    twin's node reports `evaluable_at_equal_inputs`, which is false when a
    node on its changed path is off its decode. A change to a condition byte
    is consumed only if it touches the condition's mask. A change to any
    other bit of that byte is `unknown`, never inert. A wrong program,
    missing patch, unresolved or mismatched slot, or wrong size returns
    `unevaluable` with the reason.
  - **Candidates.** `make_candidate` edits mapped table cells only. It
    refuses `table_unknown`, `slot_invalid`, `cell_outside_table`,
    `value_out_of_range`, `duplicate_edit` and `no_op_edit`. It leaves the
    checksum words stale on purpose and records provenance
    (`flashable: false`). `write_candidate` writes `<root>/<sha>/image.sim` and
    `provenance.json`. It refuses `inside_checkout` and `inside_rom_library`.
  - **Accept.** The synthetic tests are `tests/test_rom_effects.py`, 25 pass:
    - `test_identical_roms_and_states_give_identical_results`;
    - `test_a_fuel_comp_edit_reaches_bfs_x_target_afr_and_the_timing_trims`;
    - `test_a_fuel_comp_axis_edit_is_the_same_path`;
    - `test_a_target_afr_cell_edit_reaches_only_the_command`;
    - `test_another_slots_copy_is_inactive_unless_it_is_the_active_slot`;
    - `test_unknown_bytes_are_never_assumed_to_have_no_effect`;
    - `test_a_mapped_table_no_node_reads_is_unmodelled_not_inert` (the Load
      Point Scaler slot copy);
    - `test_a_branch_setting_off_its_decode_makes_its_outputs_unevaluable`;
    - `test_a_twin_node_off_its_decode_is_not_evaluable_at_equal_inputs`
      (flips at 0x8B39, 0xD247, 0x8CCD);
    - `test_a_bit_outside_a_conditions_mask_is_unknown_not_inert`
      (0x8ADC bit 0);
    - `test_a_mode_that_holds_a_changed_node_fixed_is_named`;
    - `test_an_unsupported_program_is_unevaluable_by_name` and
      `test_an_unresolved_or_differing_slot_is_unevaluable_by_name`;
    - `test_candidates_are_written_apart_from_git_and_the_rom_library`;
    - `test_loading_tracing_and_writing_candidates_requests_no_ecu_write`
      (clean process, no `src.control`/daemon/dashboard/MCP/`can`/`serial`).

    The private-ROM tests are `tests/test_rom_effects_private_rom.py`; they
    skip by name without the images. Here 3 pass:
    - *20260920 → 20261004.* Only `fuel_comp_flow_axis` and
      `fuel_compensation (slot 1)` are consumed, plus the checksum words.
      `changed_nodes` is `base_fuel_schedule`. All three outputs are reached
      and are unevaluable on `unmodelled:base_fuel_schedule` and
      `observed_not_predicted:load_input_x`. The moved lookups are
      `target_afr_lookup` and `timing_trim_lookup`.
    - *k106 → 20260920.* One consumed byte, at `target_afr_lookup`. Ignition
      is unchanged, and the twin node is evaluable at equal inputs.
  - **Limits.** The injector chain is not yet in the manifest: dead time,
    `0x9254`, the pressure correction and short/min pulse. Neither are the
    manifold-filling tables (`0x9B99`/`0x9BD9`). A change to any of them
    therefore reads `unknown`, never no-effect. The candidate root is
    caller-supplied: no `daemon.*` path is declared until E07.7 runs it as a
    Pi job.

- [ ] **E07.3 — Build and identify the first fuel-response plant.**
  Implement air/fuel mass accounting, injector command-to-fuel conversion
  including dead time and supported differential-pressure dependence, and
  the observation model for measured lambda. Reuse B06's acquisition and
  exhaust/sensor-delay evidence without fitting lag separately on test runs.
  Establish which parameters are known, identifiable from available logs or
  require new measurement. Do not absorb MAF bias into injector flow and
  call both independently measured. Freeze a baseline hardware parameter set
  before testing another ROM.
  **Accept:** conservation/unit checks and limiting cases pass; nonempty
  held-out steady regions meet predeclared lambda/fuel error bounds and
  report uncertainty and coverage. Missing pressure/injector evidence makes
  the corresponding effect unresolved. Target AFR is never copied into
  measured lambda to manufacture agreement.

- [ ] **E07.4 — Validate from telemetry and an actual intervention.**
  Inventory raw command, MAF/rpm, lambda, thermal/barometric, voltage and
  available pressure channels with acquisition clocks. Split physical drives
  and interventions before parameter fitting; reserve independent drives
  and post-change outcomes. Compare with `compare_channels` and per-sample
  diagnostics; report steady/transient misses, missing data and observation
  latency separately. Use B08.6's recorded October 4 Fuel Comp prediction
  and exact old/new images as the initial intervention case when coverage
  permits. Preserve its original prediction and account for LTFT relearning.
  **Accept:** fixed parameters reproduce the baseline and predict the
  direction/magnitude of an unseen intervention within declared uncertainty;
  fitting a separate plant to each ROM cannot count as this test. Absent
  evidence stays pending. A baseline fit alone cannot qualify tuning effects.

- [ ] **E07.5 — Add dynamic air, torque and vehicle response in stages.**
  Add manifold mass/energy balance and cylinder breathing, then spark/lambda
  efficiency, friction, rotational inertia and road/driveline load. Separate
  physical actuator response from ECU commands, including cam/throttle
  dynamics. Reuse H07's validated wheel-power/load measurements where
  available; do not calibrate torque against a fuel-derived torque estimate
  built from the same signals. Name required MAP/fuel-pressure measurements,
  controlled spark/cam variation and torque references before claiming
  parameter identifiability. Natural closed-loop logs alone may not excite
  the effects of changing those controls.
  **Accept:** each added subsystem has independent validation and bounded
  applicability; step-size refinement and mass/energy consistency establish
  numerical convergence. Spark edits cannot produce qualified power/knock
  predictions without relevant response data. Prescribed-rpm results are
  never presented as acceleration predictions.

- [ ] **E07.6 — Run paired tuning scenarios without leaking observations.**
  Simulate baseline/candidate ROMs with the same hardware parameters, driver
  scenario, environment and declared initial states. In coupled rollout,
  recompute all downstream state: do not feed recorded post-change BFS,
  trims, airflow or rpm back into predictions of those quantities. Declare
  initialization and reset of learned ECU state, including flash-related
  resets. Return traces/deltas for supported outputs, uncertainty, coverage,
  affected ROM cells and unresolved dependencies. Keep synthetic scenario
  outputs separate from observed evidence and baseline training data.
  **Accept:** a negative-control ROM reproduces the baseline; a known edit
  passes the held-out intervention test; unsupported cells/regimes cannot
  be ranked as a reliable improvement. Optimizing against the simulator is
  deferred until these effect predictions qualify and still requires later
  physical verification.

- [ ] **E07.7 — Integrate the simulator on the Pi through E06/E04.**
  Trial pure Rust controller and plant libraries behind a small scheduler
  interface, with Python owning datasets, fitting/evaluation, ROM loading
  and evidence/UI. ECU arithmetic parity remains bit-exact; continuous plant
  parity uses predeclared solver tolerances and convergence tests. Benchmark
  full coupled runs, not just one lookup. Prefer a simple typed interface;
  consider FMI export only if tool interoperability becomes useful.
  **Accept:** an isolated Pi job compares two ROMs and persists reproducible
  results visible on the phone. Scenario sweeps and fitting run through
  E04's bounded jobs only after collection stops; a lightweight live plant
  observer needs its own measured budget before enablement. The first twin's
  1 ms budget is not an assumed budget for a whole engine simulator. Missing
  resources defer jobs without blocking collection. Rust adoption still
  follows E06's evidence-backed decision.

## Measurement gaps and reuse

The [H07 driving dyno](H07-driving-dyno.md) owns road-load/power validation,
[B08](B08-firmware-informed-behavior.md) owns firmware-informed behavior and
the intervention comparison, and E05 owns decoded controller logic. Reuse
their measurements; none becomes completed merely because the simulator
uses it. Log acquisition rate bounds what fast dynamics can be identified.
Sparse RAM windows cannot establish cycle-by-cycle transient accuracy.

This architecture follows established separation of engine airflow and torque
submodels; structured torque models require spark/lambda efficiency and
friction information beyond controller maps. See the
[MathWorks SI engine model description](https://www.mathworks.com/help/autoblks/ug/spark-ignition-core-engine-breathing-and-torque-production-1.html).
It is an architectural reference, not a MATLAB dependency or evidence that
our vehicle model is calibrated. [FMI](https://fmi-standard.org/docs/3.0.2/)
is an optional future interchange interface for coupled models.
