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

- [~] **E07.3 — Build and identify the first fuel-response plant.**
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
  *Partial 2026-10-05: plant built and identified, baseline frozen. It fails
  its own bounds in-sample as well as held out, so the plant's structure is
  wrong; this is not a generalisation gap.*
  - **Code.**
    - The physics is `src/analysis/fuel_plant.py`, generic and in layer 5:
      air per cylinder event, injector dP = rail gauge + baro − MAP, a √dP
      orifice law, Te = Ti − Td, λ = air / (s·f(dP)·Te), and fuel grams =
      stoich-air equivalent / AFRₛ.
    - The ECM's own integer pulse chain is `src/analysis/injector_pulse.py`.
      Its 16-point curve goes through `ecu_state_models.byte_curve16`.
      `dead_time_counts`, `pressure_correction_word` and `predict_pulse` are
      listed, with their widths, in the `data/ecu_twin_contract.yaml`
      inventory as Python-only kernels.
    - Samples, identification and evaluation are
      `src/analysis/fuel_plant_eval.py`.
    - The Xterra profile is `data/nissan-xterra-2008/fuel_plant.yaml`
      (`plant_version: fuel-mv-1`; the contract now names it).
    - `python -m tools.fuel_plant_check chain|identify|evaluate` reproduces
      every number below offline.
  - **Registry.** The contract's six parameters keep the same ids and
    statuses (`check_against_contract` returns nothing), and nine more are
    declared.
    - Only the lumped `stoich_air_per_open_ms` is identifiable. It is the
      product of injector flow, AFRₛ, λ gain and 1/MAF gain.
    - The registry refuses a component that claims to be log-fitted while any
      other component is not independently known. So MAF bias cannot be
      absorbed into injector flow and both called measured.
    - A parameter that `requires_measurement` carries no value. It runs under
      a bracketed `working_assumption` that names an unresolved effect:
      dead-time offset vs the ECM's Ts ±0.1 ms; rail 350 kPa [305, 371] where
      it is not logged; MAP by speed density at VE 0.7 [0.4, 1.0]; bank
      split; AFRₛ; and the λ delay.
  - **B06, through its own schema.** `load_b06` validates the `eligibility`
    block with B06's `Eligibility` schema. It takes three things from it:
    - **The frozen delay:** 0.10 s, bracket 0–0.5. A delay that is not frozen
      is refused, and so is one that differs from B06.
    - **The closed-loop band:** 0.01.
    - **The post-fuel-cut holdoffs:** 15 s closed loop, 6 s enrichment.

    Every recording in either regime's `informed_by` is refused as held-out
    evidence (`leaked_b06`). B06's whole-file `load_lambda_delivery` cannot
    load the committed `lambda_delivery.yaml` against the working tree's
    uncommitted `lambda_delivery.py` (required `sensor` / `relation` fields),
    so it is not used.

    **Extrapolation:** 0.10 s was fitted on enrichment RAM-target reads. The
    plant applies it to closed-loop reads and to injector-pulse anchors as
    well, where B06 estimated no delay. Over the bracket the steady score
    moves by less than 0.05 %.
  - **Ground truth (ECM chain).** The check is (DID 0x1206 − Ts) / predicted
    pulse, per sample, on the six tuning captures. φ comes from
    `mem:ECM_TARGET_EQUIV_RATIO` (bracketed reads), with BFS steady within
    3 % over 0.5 s.
    - Result: n = 2,361, median **1.0084**, p10–p90 0.958–1.025. The firmware
      session had 1.006 and 0.961–1.030.
    - Negative control, with the ECM's pressure correction removed: median
      **0.936**, p10–p90 0.891–0.968. That is outside [0.98, 1.04].
    - Tests: `test_the_decoded_chain_reproduces_the_logged_pulse_per_sample`
      and `test_removing_the_pressure_correction_fails_the_known_answer` in
      `tests/test_injector_pulse.py`. Both skip without the private image.
    - **This does not verify Ts.** Ts is the session's own fuel-cut pulse
      word (there is no PID 0x42 on these captures). It is subtracted from the
      measured side, and the prediction excludes it. The check covers G·k
      only. The Ts formula is checked only on 46 fuel-cut reads (±2 counts).
      The injector's physical latency is not checked at all.
  - **Split.** Declared before anything was scored.
    - Enforced: `check_drive_independence` groups the split with
      `physical_drives` and refuses a drive that has recordings on both sides
      (`leaked_drive`). The split is seven separate drives.
    - Identification: the 09-21 pair and 09-23T164007.
    - Held out: 09-22T170459, 09-22T235224, 09-23T142926 and the 10-04
      ram-study.
    - Bounds, also declared before scoring: |pooled median| ≤ 2 %, sample
      |err| p90 ≤ 8 %, ≥ 85 % of regions within ±4 %, and enrichment
      |median| ≤ 3 % (at least 3 regions).
  - **Frozen baseline `na-stock-2026-10-05.1`** (sha256 `6aec1db5…09220`).
    - s = **0.051063 g air/ms**, 90 % interval 0.050591–0.051501, from 89
      regions and 766 reads.
    - That is 0.912 × the datasheet/E10 prior. The 9 % is not assigned to
      any component.
    - The baseline, split, bounds and spent ledger are committed with this
      result.
  - **In-sample, on the identification drives (`identify`):** fails its own
    bounds.
    - Sample |err| p90 is 12.17 %, against a bound of 8 %.
    - Region pass fraction is 0.584, against 0.85.
    - The null model λ ≡ 1 does better, with |err| p90 3.35 %.
  - **Held-out (`evaluate`, 845 reads, 137 regions): FAIL.**
    - Passed: pooled median +1.69 %, and region median-of-medians +1.32 %
      (CI 0.64–1.95). Enrichment is −1.54 % (4 regions, 10 reads).
    - Failed: sample |err| p90 is 11.7 %, and the region pass fraction is
      0.62.
    - The null model λ ≡ 1 has |err| p90 2.33 %.
    - Per drive, the median error is +4.96 %, +0.69 % and +1.79 %.
    - Median error by MAF band (`median_error_by_maf_band_pct`):

      | MAF band | Median error | Reads |
      |---|---|---|
      | under 5 g/s | +1.70 % | 614 |
      | 5–10 g/s | +1.04 % | 24 |
      | 10–20 g/s | +22.2 % | 14 |
      | 20–40 g/s | +7.0 % | 25 |
      | 40–80 g/s | +0.85 % | 157 |
      | over 80 g/s | −0.55 % | 11 |

    - Sensitivity of the median: dead-time ±0.1 ms gives −4.4/+5.4 %; rail
      bracket +6.4/−2.6 %; VE +1.5/−1.2 %; bank split ±4.1 %.
    - Coverage is 15 % of fuelled warm reads.
    - The 10-04 ram-study contributed nothing: 188 reads are
      `not_co_observed` (Mode 22 and Mode 01 polled apart) and 5 are
      `missing_input`.
  - **Spent drives.** `evaluate` returns the recordings it scored. The CLI
    appends them to `spent_held_out`; all four held-out recordings are now
    there against `6aec1db5…`. The profile refuses to load with a spent
    recording held out under any other baseline or plant_version, and
    `evaluate` refuses to score one (`spent_held_out`).
  - **Accept items met:**
    - conservation, units and limiting cases (`tests/test_fuel_plant.py`,
      23 pass);
    - every read has a named state;
    - the target is refused as an input;
    - missing rail or MAP data names `rail_pressure_unmeasured` /
      `manifold_pressure_unmeasured`;
    - an unfrozen baseline and leaked drives, B06 recordings or spent drives
      are refused.
  - **fuel-mv-2 (2026-10-08, operator-approved E07.5 recommendation).**
    VE depends on rpm, and MAP refuses instead of capping. fuel-mv-1 is
    untouched: `fuel_plant.yaml` is byte-identical, and its baseline sha
    `6aec1db5…` and spent records still load
    (`test_fuel_mv_1_stays_frozen_and_fuel_mv_2_is_declared_beside_it`).
    - **Where it lives.** `data/nissan-xterra-2008/fuel_plant_mv2.yaml`,
      beside fuel-mv-1. `load_fuel_plant(dir, plant_version)` finds a version
      among `fuel_plant*.yaml` (`plant_files`; a version declared twice is
      refused); with no version it reads `fuel_plant.yaml`, which is still the
      version the contract and E07.4/E07.6 run.
    - **VE model (`manifold_model: speed_density_refusing`,
      `volumetric_efficiency: rpm_envelope`).** Per band of 500/1000/2000/
      3000/4000/5000/6500 rpm, the largest steady warm charge fraction on the
      identification drives (`fuel_plant_eval.identify_ve`; steady is the
      plant's own 2 s rpm/MAF-span rule on MAF reads; min 5 reads a band). On
      an NA engine charge fraction = VE × MAP/baro ≤ VE on every read, so the
      envelope is the smallest VE the drives allow, and is VE where they
      reached full breathing. It runs at max(envelope, 0.7), so a band the
      drives never filled keeps the old working value. Frozen envelope
      0.217/0.677/**0.888/0.895/0.906/0.873** (n 1875/72/805/286/24/5), so VE
      runs 0.7, 0.7, 0.888, 0.895, 0.906, 0.873. Load dependence is not
      modelled (only VE × MAP is identified without a MAP sensor). The fit
      inherits MAF's transfer error and is not a VE measurement (contract
      `parameter_fits`, E07.5).
    - **MAP.** `manifold_pressure_ve_curve` is steady speed density at VE(rpm).
      Above baro the read is NaN and `manifold_above_ambient` (the stage's own
      refusal, never a cap). It is `outside_ve_fit` outside the fit's bounds:
      500–6500 rpm, baro 79–82 kPa, and IAT 23–50 °C. IAT is bounded because
      on this truck INTAKE_TEMP tracks airflow, so the ideal-gas fill does not
      divide the charge density out. `apply_manifold_refusals` names those
      reads as states, so they are never scored. A capping plant is left
      bit-identical
      (`test_the_revised_map_refuses_above_ambient_where_fuel_mv_1_caps`).
    - **Baseline `na-stock-2026-10-08.mv2`** (sha256 `0180c207…21576`; the
      VE bounds, IAT included, are part of the hash). It comes from
      `tools.fuel_plant_check identify --plant-version fuel-mv-2` on the same
      three identification drives, before any comparison was run:
      s = **0.050792 g/ms**, 90 % interval 0.050357–0.051198, 89 regions,
      764 reads (2 more refused as `manifold_above_ambient`). That is 0.53 %
      below fuel-mv-1's s.
    - **Held out: none possible.** Every steady-scoreable recording is either
      identification or spent by fuel-mv-1, and no other recording has pulse
      reads co-observed with MAF and λ. So `split.held_out: []` with a named
      `held_out_pending`.
      - `plant_qualification` refuses a plant with no held-out recording, and
        the profile refuses a recording this baseline spent outside its
        `held_out` (`test_a_plant_without_held_out_drives_never_qualifies`).
      - `evaluate` refuses fuel-mv-2 by name (`held_out_pending`). It reads
        every plant file's spent ledger itself (required `profile_dir`;
        `fuel_plant.refuse_spent_elsewhere`), so a profile built in memory
        cannot score a sibling's spent drive
        (`test_fuel_mv_2_cannot_hold_out_a_drive_fuel_mv_1_spent`).
    - **Against fuel-mv-1, per sample on identical reads: a diagnostic, not a
      verdict.** `fuel_plant_eval.compare_versions` / `tools.fuel_plant_check
      compare --plant-version fuel-mv-1 fuel-mv-2` builds the samples once,
      drops reads either version refuses, and reports the per-sample |err|
      change overall and per MAF band. It returns no verdict, spends nothing,
      and refuses a recording that is neither in-sample nor already spent
      (`test_versions_are_compared_per_sample_on_spent_drives_only`).
      - **In-sample (the same 764 reads):** |err| p90 12.01 → **11.61 %**,
        region pass 0.573 → 0.618. Per sample, 48 % of reads are better and
        52 % worse (median change +0.21 pp). It still fails its own bounds
        in-sample.
      - **The four spent fuel-mv-1 held-out drives (726 common reads, 101
        regions).** fuel-mv-2 refuses 121 reads: 117 `outside_ve_fit`, all
        IAT 20–22 °C on 09-23T142926, and 4 `manifold_above_ambient`.
        |err| p90 11.74 → 12.21 %; region pass 0.564 → 0.554. Per sample,
        36 % of reads are better and 64 % worse (median change +0.54 pp).
      - **Per MAF band** (share better/worse; median change in |err|):

        | MAF band | Reads | Better / worse | Median change |
        |---|---|---|---|
        | under 5 g/s | 575 | 31 / 69 % | +0.54 pp |
        | 5–10 g/s | 21 | 19 / 81 % | +0.54 pp |
        | 10–20 g/s | 14 | 7 / 93 % | +0.12 pp |
        | 20–40 g/s | 18 | 100 / 0 % | −0.86 pp |
        | 40–80 g/s | 93 | 62 / 38 % | −0.97 pp |
        | over 80 g/s | 5 | 40 / 60 % | +0.25 pp |

        Below 20 g/s, idle runs at the same 0.7 VE, so only the 0.53 % lower
        s moves it.
        Before the IAT bound (841 reads, with the morning drive), 40–80 g/s
        was 48.7 % better / 51.3 % worse (median +0.07 pp). Only 20–40 g/s
        (n 23) improved there, at 82.6 %. So the 40–80 g/s gain rests on
        which drives the bound keeps, and is not robust.
      - **Reading.** VE(rpm) helps in 20–40 g/s. In 40–80 g/s whether it
        helps depends on the drive set. It makes idle, which is most reads,
        slightly worse, and it does not touch the idle/short-pulse and
        10–20 g/s tails that fail the bounds. fuel-mv-2 is not a
        better-qualified plant.
    - **Known limitation: idle VE (owed a load-dependent VE).**
      - Steady warm idle has charge fraction p50 0.151 (1,875 reads, 762 rpm,
        3.46 g/s, baro 81 kPa). At the 0.7 floor that is MAP 17.5 kPa.
      - A warm NA idle normally sits at ~28–35 kPa, which implies an idle VE
        of ~0.35–0.45.
      - So the floor, and "part-load VE = full-breathing VE", understate
        part-load MAP. That overstates injector dP by ~3 % at idle (~1.5 %
        flow), on 614 of the 841 spent-drive reads.
      - Recorded in the contract's `parameter_fits` evidence and in
        `fuel_plant_mv2.yaml`.
  - **fuel-mv-3: a load-dependent VE (2026-10-13, desk; not yet identified).**
    - **Where it lives.** `data/nissan-xterra-2008/fuel_plant_mv3.yaml`, beside
      fuel-mv-1 and fuel-mv-2, which are untouched (their baseline shas still
      pin in `tests/test_fuel_plant.py`). `manifold_model:
      speed_density_load_dependent` (`fuel_plant.REFUSING_MODELS`), with the
      contract's `fuel_delivery.implementation.other_plant_versions` and a
      reconciled `internal_models` entry (invariant
      `manifold_at_or_below_ambient`, no `parameter_fits` until a fit is
      frozen); `check_internal_models(contract, profile)` returns `[]`.
    - **Structure.** VE(rpm, MAP) = VE(rpm) × `part_load_breathing_factor`, the
      ideal-cycle residual-gas form: exhaust in the clearance volume expands to
      the manifold pressure, so the fresh charge is
      (r_c − (p_e/p_m)^(1/n)) / (r_c − 1), normalised at MAP = baro so the fitted
      full-breathing VE(rpm) (fuel-mv-2's rule, restated unchanged) holds there.
      `manifold_pressure_load_dependent` solves charge fraction = VE(rpm) ×
      f(x) × x for x = MAP/baro by bisection (the right side is monotone) and
      refuses above ambient / outside the fit as fuel-mv-2 does. It is physics,
      not a fit: only VE × MAP is identifiable without a MAP sensor. Registry:
      `compression_ratio` (known, profile fact 9.7), `residual_expansion_exponent`
      (known, physics 1.3) and `exhaust_pressure_ratio` (working assumption
      1.0 [1.0, 1.1], effect `exhaust_back_pressure_unmeasured`); the profile
      refuses a missing one, a non-`known` geometry/physics value and a bracket
      at which the residual fills the cylinder.
    - **Idle.** Charge fraction 0.151 at 762 rpm, baro 81 kPa, IAT 30 °C: MAP
      17.47 kPa (fuel-mv-2) → **21.84 kPa** (22.20 at exhaust 1.1 × baro;
      21.60–22.11 over n 1.25–1.35). A warm NA idle is ~28–35 kPa, so the
      factor closes about a third of the gap; overlap backflow and cam phasing
      are not modelled and stay inside the floor's bracket.
    - **Declared before any fit:** split (`declared_on: 2026-10-13`, the same
      three identification drives, `held_out: []` with `held_out_pending`) and
      acceptance (fuel-mv-1/2's bounds, re-declared 2026-10-13).
    - **Tests (`tests/test_fuel_plant_mv3.py`, 7 pass):**
      `test_fuel_mv_3_is_declared_unidentified_beside_the_frozen_versions`
      (no baseline, `baseline_not_frozen`, `plant_not_qualified: no frozen
      baseline`); `test_the_residual_gas_factor_and_its_limiting_cases` (1 at
      ambient, 0 where the residual fills the cylinder, → 1 as r_c → ∞,
      monotone, back-pressure lowers it); `test_without_residual_gas_it_is_fuel_mv_2s_map`
      (r_c = 1e12 reproduces `manifold_pressure_ve_curve` to 1e-9, same
      refusals); `test_the_map_solves_its_own_equation_and_never_exceeds_ambient`
      (round trip 1e-12, MAP ≥ fuel-mv-2's, baro at the envelope, refused
      above and outside); `test_idle_map_moves_toward_a_warm_na_idle`;
      `test_the_breathing_form_is_declared_never_fitted`;
      `test_identification_then_a_frozen_baseline_predicts` (the identify →
      freeze → predict path on the committed slice relabelled with a synthetic
      0.05 g/ms lambda: recovers 0.05, and λ = air/(s·f(dP)·Te) recomputed
      term by term to 1e-12). These are structure and unit checks; nothing
      was fitted to the truck and no drive was scored.
    - **Baseline `na-stock-2026-10-15.mv3`** (sha256 `8d54217e…7d523`, pinned
      in `test_fuel_mv_3_is_frozen_and_unqualified_beside_the_other_versions`,
      which replaces the unidentified test and keeps the `baseline_not_frozen`
      refusal on the declaration without its baseline). From `identify
      --plant-version fuel-mv-3` on the three identification drives:
      s = **0.050994 g/ms**, 90 % interval 0.050472–0.051402, 89 regions,
      764 reads (2 `manifold_above_ambient`), 0.40 % above fuel-mv-2's. The
      VE envelope is fuel-mv-2's to 1e-6. The contract's fuel-mv-3 internal
      model now carries the frozen fit's bounds (`check_internal_models`
      returns `[]`). A per-sample comparison was run the same day on a scratch
      copy holding these identical numbers before the freeze; nothing was
      re-fitted after it.
    - **Against fuel-mv-1/2, per sample (`compare --plant-version fuel-mv-1
      fuel-mv-2 fuel-mv-3`; fuel-mv-1 is named so its spent drives are
      admissible).** In-sample |err| p90 12.01 / 11.61 / **11.69 %**, region
      pass 0.573 / 0.618 / 0.618. On the spent drives (726 reads) 11.74 /
      12.21 / 12.24 %; against fuel-mv-1, 36 % of reads better and 64 %
      worse, idle (< 5 g/s, 575 reads) +0.68 pp median. The load-dependent VE
      is not the structural miss either.
    - **The STFT residual (2026-10-15, desk, all six scored drives; a
      diagnostic, not a verdict).** Per read, the plant error tracks the
      ECM's short-term trim one for one: err = **−0.92 × STFT − 0.15**
      (1,601 steady reads with |err| < 100 %, corr −0.69; −0.53 within the
      ~1 s regions, −0.32 between them). Removing that linear term alone takes
      |err| p90 from 11.6 % to **6.8 %**, inside the 8 % bound. The pulse
      carries STFT; measured λ (PID 0x24) stays within ~±3 %, which is why
      λ ≡ 1 beats every version.
      - *Not the λ delay.* Re-pairing λ at 0–1.5 s on the identification
        drives (eligibility fixed at 0.10 s) leaves corr(err, STFT) at −0.68 to
        −0.82 and |err| p90 at 11.4–13.9 %. B06's median-only sensitivity
        (< 0.05 %) hid this, because a limit cycle is symmetric about its
        median.
      - *Candidates.* EVAP purge enters the manifold downstream of the MAF, so
        its fuel is invisible to both the MAF and the pulse. The contract said
        "purge flow inside MAF"; that is corrected. The other candidate is a
        fast MAF transfer error. The within-region part (fast) argues that
        purge is not the whole answer. None of the six drives logs purge
        (PID 0x2E or DID 0x1110), so neither is tested.
      - *Not a plant input.* STFT is the controller's own estimate of the
        error, so feeding it in would be circular and would converge on
        λ ≡ 1. Closed-loop reads are therefore a weak test of the plant. The
        28 open-loop enrichment reads give median −2.9 %, |err| p90 4.9 %.
      - *Pairing leak.* 8 eligible reads pair 27–46 g/s MAF with an idle
        pulse and rpm (err > 100 %): the steady rule is admitting a MAF read
        from another instant.
      - Measurement now declared: `evap_purge_drive` (contract, `planned`;
        operator 2026-10-15). The `tuning` capture profile polls DID 0x1110 at
        2 Hz on the pulse DID's cycle from 2026-10-15.
  - **Remaining:**
    1. A revised plant structure, with a new `plant_version`, a new baseline
       and newly declared bounds. The in-sample failure points at the
       idle/short-pulse region and the 10–40 g/s tail. It must then pass
       on **never-scored held-out drives** (truck). The four above are spent.
       Log PID 0x42 with the pulse DIDs on those drives, so that Ts is not
       taken from fuel-cut reads. *VE and MAP done in fuel-mv-2 (above,
       2026-10-08):* VE(rpm) replaces 0.7 and MAP refuses above ambient as the
       `manifold_breathing` stage does (the contract's fuel-mv-2 internal
       model is `reconciled_by`). fuel-mv-2 still fails in-sample and has no
       held-out drive. Still owed:
       - the idle/short-pulse and 10–40 g/s structure;
       - the never-scored drives;
       - ~~a load-dependent VE~~ *done in fuel-mv-3: structure 2026-10-13,
         baseline frozen 2026-10-15 (above); no better than fuel-mv-2.*
       - the idle/short-pulse and 10–40 g/s structure. The in-sample residuals
         (2026-10-15, above) point at STFT, not breathing: a tuning drive with
         purge logged (`evap_purge_drive`) decides whether purge is a missing
         plant input before another VE/MAP revision. The short-pulse
         correction's decode (`0x9120`–`0x9126`) is still owed for the
         Te < 1.5 ms reads.
       - the steady rule's MAF pairing leak (8 reads, above). *Root cause
         traced 2026-10-15 (E08.4's verifier), on 09-23T142926 DID round
         3047.* The pulse, throttle (3.3 %) and BFS reads are idle. The
         paired Mode 01 round 3048 reads RPM 775 at +97 ms and MAF 31.71 g/s
         at +105.7 ms, 85 ms into a launch from standstill (pedal moving at
         +12 ms; MAF 3.66 g/s on the previous round). The pairing takes a
         same-round MAF that is newer than the pulse. The steadiness spans
         look back over the preceding 2 s from the pulse time, so they never
         see the step. Fix at source: a pulse read's partners must come from
         before the next change, or the steady window must include the
         partner reads' own times. Changing eligibility changes what every
         frozen fuel-mv-* baseline was identified on, so it ships as a new
         plant_version with its own baseline, never as an edit to a frozen one.
    2. A tuning-profile drive that logs the AEM rail pressure with the pulse
       DID (truck). No such drive exists yet.
    3. MAP, injector flow and injector latency still require measurement.

- [~] **E07.4 — Validate from telemetry and an actual intervention.**
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
  *Partial 2026-10-05: the machinery is built and tested; the verdict on the
  real case is `pending`, by name, for two reasons that need the truck.*
  - **Code.** `src/analysis/plant_validation.py` (layer 5);
    `data/nissan-xterra-2008/plant_interventions.yaml` (the case);
    `python -m tools.plant_validation inventory|case` reproduces the numbers
    below offline. E07.3's `SpentRecord` now carries the `verdict` each
    scoring returned (the four existing records: `fail`), and
    `fuel_plant.plant_qualification` derives qualification from those
    verdicts. It is never stated.
  - **Channel inventory** (`inventory`, 158 recordings under
    `~/canary-data/sessions/nissan-xterra-2008`). Each need's acquisition
    clock is the median and p90 interval of per-read arrival times:
    - Pulse DID: 0.50 s in the six tuning captures, 7.05 s in the 10-04
      ram-study, about 61 s in every live session. So the live sessions are
      not steady-scoreable.
    - MAF/λ: about 0.25 s in the captures and 0.67–1.0 s live.
    - AEM rail pressure: 0.01 s, in the seven 10-04 recordings only.
    - The Fuel Comp load byte `mem:ECM_FUEL_COMP_X_INPUT`: in
      `_capture-ram-study-2026-10-04T204557` only (192 reads).
    - Battery voltage: absent from the six tuning captures.
    - No recording starts after the flash.
  - **The case.** The flash `2026-10-04T23:02:33.183902Z` (`exact`) and both
    image hashes are read from `tune_revisions.yaml` (`resolve_boundary`), and
    a mismatch is refused (`image_mismatch`).
  - **The prediction of record is preserved, not re-dated.** It is commit
    `d0fda03` at `2026-10-04T23:01:15Z`, 78 s before the flash, so the order
    is `prior`. The record holds that commit's document sha256 `2b3a319b…`
    and the three table rows verbatim. `verify_prediction_of_record` re-reads
    the commit and returns `[]`.
    - It is **not on any ledger**, by operator decision (2026-10-05): it stays
      in git only, and commit `d0fda03`'s time is the order evidence. A ledger
      append would be minted 2026-10-05 and order `post_hoc`. A commit-dated
      prediction import is a possible later E01 mechanism.
  - **The split is declared before scoring.** `pre` is the plant's four
    held-out recordings; `post: []` is reserved. The split is by sample time
    against the flash: `straddles_intervention`, `wrong_side` and
    `undeclared_recording` are refused.
  - **A separate plant per ROM cannot count.** There is one profile argument,
    and `check_one_plant` refuses:
    - `plant_fitted_on_candidate_rom`;
    - `baseline_revision_mismatch`;
    - `refit_across_intervention` (a fitted or identification recording that
      the case reserves as post-change, or that starts after the flash);
    - `baseline_not_frozen`.
  - **Learned trim.** The flash resets LTFT to 0 % (contract event
    `ecu_reflash`), and relearning is not modelled. A post-change read with
    |LTFT| > 1 % is `ltft_relearning` and is excluded by name. A recorded
    post-change trim is only ever the observation.
  - **Effect tests.** Each is per matched pair: same regime, rpm ±5 %, fill
    byte ±4, baro ±2 kPa, IAT ±10 C. Pairs are one-to-one and nearest first,
    and a mirror pair is never counted twice. They are scored against a
    condition-matched floor: p90 |residual| over pre-vs-pre pairs from two
    recordings, or from one recording at least `floor.min_separation_s` =
    30 s apart (operator decision 2026-10-05, declared that day before any
    post-change drive). The 30 s comes from physics, not data: 60× B06's
    0.5 s λ-delay bracket end, 30× a warm wall-film time constant (≤ 1 s),
    and beyond STFT settling. Pairs within one drive share its slow states,
    so this floor can be narrower than one across drives.
  - **Rules frozen before the first post-change drive.** `frozen_rules` holds
    `rules_sha256()` over bands, learned state, matching, acceptance, floor,
    `split.pre`, `load_input` and its pairing tolerance (`f522ff53…`, frozen
    2026-10-05T09:19:40Z, while `post` is empty). The case refuses to load on
    `rules_changed` (any rule edited), `rules_not_frozen` (post listed without
    it), `rules_frozen_after_post` (re-frozen after a listed post-change
    recording started) and `recording_time_unknown` (a post-change name with
    no parseable start time; `_sim-*` exempt). `check_one_plant` refuses a
    fitted recording with no parseable start time the same way.
  - **Trim assumptions, stated.** LTFT acts in open loop: the decoded pulse
    formula adds STFT and LTFT into k_b with no open-loop gate
    (`docs/ecu-firmware-analysis.md:629`). STFT neutral in open loop is an
    assumption, not decoded. Both are in `learned_state.evidence` and the
    effect test's docstring.
  - **Sealed verdict.** `evaluate_intervention` returns
    `report["verdict_record"]`, an `InterventionVerdict` sealed with an HMAC
    under a process-private key. A typed or `dataclasses.replace`-edited
    verdict fails `verify()`.
    - Open loop: λ. The pre read's effective pulse × the predicted change ×
      the declared trim state, through the frozen plant at the post read's
      conditions.
    - Closed loop: STFT. Total trim needed = (1 + total before) / (1 +
      change) − 1, less the reset LTFT. The part due to the ROM alone is
      reported separately.
    - Bounds are predeclared: ≥ 20 pairs, ≥ 20 floor pairs, direction ≥ 0.7,
      within uncertainty ≥ 0.8. `insufficient_evidence` is distinct from
      `fail`.
  - **Real result (`case`): `pending`.**
    - `plant_not_qualified: baseline na-stock-2026-10-05.1 failed its
      predeclared held-out bounds`.
    - `no_post_change_data`.
    - Baseline reproduction, frozen parameters, pre side: steady 845 reads in
      137 regions, median +1.69 %, |err| p90 11.70 %, region pass 0.62
      (`fail`, the same as E07.3).
    - Transient (separately): 2,571 reads, median +4.77 %, |err| p90 38.4 %.
    - Missing (separately): 16 `missing_input`, 190 `not_co_observed`.
    - Observation-latency bracket: the median moves −0.02 % / −0.04 % at 0 s
      / 0.5 s.
  - **Tests.** `tests/test_plant_validation.py`, 12 pass. The real case:
    - `test_the_case_is_the_registrys_flash_and_its_prediction_came_first`;
    - `test_the_prediction_of_record_is_preserved_as_committed` (it skips when
      the commit is absent, as in a shallow clone);
    - `test_the_real_case_is_pending_because_the_plant_failed_its_bounds`
      (on the committed held-out slice: 152 reads, 38 regions, +1.90 %, as in
      E07.3);
    - `test_qualification_is_read_from_the_recorded_verdicts`;
    - `test_the_inventory_reports_acquisition_clocks_and_the_revision`;
    - `test_the_scoring_rules_cannot_change_after_a_post_change_drive_is_listed`;
    - `test_floor_pairs_inside_one_recording_keep_the_declared_separation`.
    - `test_a_plant_fitted_per_rom_cannot_count` also covers
      `baseline_revision_mismatch` and `recording_time_unknown`.

    A **labelled synthetic plant** (`synthetic: true`, `_sim-*` traces only)
    is identified, frozen and passes its own held-out bounds:
    - `test_a_qualified_synthetic_plant_passes_a_correctly_predicted_change`:
      WOT +4.25 % observed at median ≈ 4.25 %; floor 1.07 % on 48 pairs;
      trim test passes; verdict `pass`.
    - `test_a_reversed_change_fails_and_relearning_reads_are_not_tested`:
      `fail`; with LTFT relearned, `insufficient_evidence`.
    - `test_a_plant_fitted_per_rom_cannot_count`.
    - `test_the_split_is_by_sample_time_and_declared_first`.
    - `test_synthetic_and_recorded_never_mix`: `synthetic_mismatch`,
      `synthetic_as_observed` and `synthetic_plant_on_recording`.
  - **Load-input pairing refuses by name (2026-10-13).** Every effect test and
    the floor match pairs on the fill byte, so a side with eligible reads and
    none paired within `load_input_pair_tolerance_s` can form no pair.
    `load_input_pairing` reports per side (channel, tolerance, eligible reads,
    paired) in `report["load_input_pairing"]`, and such a side adds
    `load_input_unpaired: <side>: 0 of N eligible reads have <channel> within
    <tol> s` to `pending`; inside the effect tests an unpaired read is counted
    as `load_input_unpaired`, apart from `not_predicted`. On the committed slice
    the real case now reads pending `plant_not_qualified`,
    `no_post_change_data`, `load_input_unpaired` (pre: 0 of 152;
    `test_the_real_case_is_pending_because_the_plant_failed_its_bounds`). On
    the committed ram-study slice 0 of 192 fill-byte reads have a PID 0x0C read
    within 0.1 s (median gap 0.87 s), and that recording has no pulse anchors
    (E07.3: 188 `not_co_observed`), so today the case forms no load-input pair
    on either recording type. The frozen tolerance was not changed.
  - **Remaining:**
    1. A qualified plant: E07.3's remaining items 1–3, with never-scored
       held-out drives (truck). Until then the verdict stays
       `pending: plant_not_qualified`.
    2. Post-change drives on ZS44A-20261004, listed in `split.post` before
       scoring (truck). Log the fill byte with the pulse DID, λ, STFT/LTFT and
       battery voltage, both at WOT above 4,000 rpm and at part-throttle
       cruise.
    3. Only the ram-study logs the fill byte before the flash, so the real
       floor will come from within-recording pairs (the 30 s rule above);
       a floor across drives needs no pre-change data that can still be made.
    4. `compare_channels` is not used. The plant's prediction is not a stored
       channel, and the pre/post pairs cross recordings, which
       `compare_channels` does not do. Pairing uses `match_ordered` on the
       acquisition clock (as in E07.3) and the one-to-one matched pairs
       above; no aggregates are compared.

  - *Operator, 2026-10-13: keep `load_input_pair_tolerance_s: 0.1`.* Before
    the first post-change drive, change the capture so the fill byte is read
    in the same RAM window or poll round as the pulse DID, rather than
    widening the tolerance (0 of 152 pre-side reads pair at 0.1 s on the
    committed slice; median gap 0.87 s).
- [~] **E07.5 — Add dynamic air, torque and vehicle response in stages.**
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
  *Partial 2026-10-05: stage 1 (manifold mass/energy balance and cylinder
  breathing) is built, numerically converged and conserving, and bounded; it
  is **not validated**, because no independent measurement of manifold
  pressure or cylinder charge exists on this truck. Every other stage is
  declared, unbuilt, with the measurements it needs named. The claim
  refusals are declared and tested as a library gate; no producer calls it
  yet.*
  - **Code.** `src/analysis/engine_dynamics.py` (layer 5, generic);
    `data/nissan-xterra-2008/manifold_plant.yaml` (`plant_version:
    manifold-mv-1`); `python -m tools.manifold_check study|breathing-bound|
    standing` reproduces every number below offline.
  - **Contract (`contract_version` 2).** `SimContract.subsystems`: each stage
    declares what it computes, its status, a built stage's module, bounded
    `applicability` (hardware, quantity bounds, invariants, time resolution)
    and, when it integrates a state, `numerical` checks; every stage names its
    validation `references` and `required_measurements` (with the controls
    each one `excites`). The schema refuses a plant/actuator quantity owned by
    no stage or by two, a built stage without an envelope, a stateful one
    without numerics, and a stage that names neither an available reference
    nor a missing measurement. Eight stages: `fuel_delivery` (E07.3, built),
    `fuel_rail`, `manifold_breathing` (built), `throttle_flow`,
    `actuator_response` (throttle plate and cam phaser, separate from the ECU
    command), `torque_production` (spark/lambda efficiency, friction,
    pumping), `rotational_dynamics` (inertia, driveline, road load),
    `engine_thermal`. Two plant quantities were added, `manifold_temperature`
    and `cylinder_air_flow`; manifold pressure now depends on them (mass and
    energy balance), `engine_torque` reads the cylinder charge rather than the
    throttle flow, and in plant replay manifold pressure is a predicted
    output owned by `manifold_breathing` instead of a stated boundary.
    `manifold_pressure.observed_as` names `sensor:MANIFOLD_PRESSURE`, the
    channel a fitted MAP sensor would record.
  - **Two MAP models in plant replay, declared.** fuel-mv-1 is E07.3's
    frozen baseline, so its numerics were not changed: `fuel_plant.predict`
    still computes its own steady speed-density MAP at VE 0.7, capped at baro.
    The stage returns NaN and refuses (`manifold_above_ambient`) where that
    model caps, and has filling lag where it has none. The contract declares
    this as `fuel_delivery.internal_models` (owner `manifold_breathing`,
    mode `plant_replay`, where they differ, owed by the next fuel-plant
    `plant_version`, E07.3 remaining 1). `fuel_plant.COMPUTES_INTERNALLY`
    lists the same quantity, and `check_internal_models` refuses either list
    without the other; the schema refuses an internal model whose named owner
    is not the contract's owner
    (`test_the_fuel_plants_own_map_is_a_declared_exception`).
  - **Stage 1 physics.** States `m` and `E = cv·m·T` (adiabatic) or `m` at
    the intake temperature (isothermal). Inflow is the MAF boundary in replay;
    the cylinders draw `VE·(m/V)·Vd·rpm/120`. The VE is fuel_plant.yaml's
    `manifold_pressure` working assumption (0.7, bracket 0.4–1.0), read from
    there, never restated; the manifold volume is a `requires_measurement`
    working assumption (4.0 L, bracket 2–8 L); the energy-balance form is a
    declared choice whose alternative every sensitivity runs
    (`manifold_heat_transfer_unmodelled`). Test
    `test_the_steady_manifold_is_the_fuel_plants_speed_density_at_the_same_ve`:
    the settled manifold equals `fuel_plant.manifold_pressure_speed_density`
    to 1e-12, cylinder flow equals MAF, `T_m` equals IAT. Limiting case
    (`test_limiting_cases_no_inflow_empties_at_tau_and_isothermal_holds_temperature`):
    no inflow empties at the exact `exp(−t/τ)` within 1e-7 g, and an
    emptying adiabatic manifold cools by more than 10 K.
  - **Step-size refinement.** Declared before the study ran: ladder 1, 2, 4,
    8 of the contract step; at most 1 % of a 0.3→0.9 tip-in's amplitude at
    the step; observed order the method's own ±0.15. 18 cases: rpm 500 and 6500 × volume and VE bracket ends × both energy
    balances, plus the working case. Cylinder flow is checked against the
    exact solution (the mass ODE is linear); pressure and temperature against
    a Richardson extrapolation.
    - **Fixed-step Euler at 1.28 ms (E07.1's starting choice) FAILED** in 8
      of 18 cases, all at 6500 rpm: worst 6.6 % of the tip-in in cylinder
      flow, 3.9 % in pressure and 11.8 % of the temperature excursion (2 L,
      VE 1.0, τ 9.3 ms). Its order was 1.00–1.07: converging, too coarse.
    - **The contract's solver is now `rk4` at the same 1.28 ms** (the solver
      clock's evidence said "E07.5's step-refinement test decides it"):
      passes all 18; worst 2.9e-6 (flow), 4.9e-6 (pressure), 3.2e-5
      (temperature); observed order 4.03–4.08 where measurable (13 cases
      reach rounding first); state residual ≤ 1.6e-16 and breathing excess
      0.50 of its bound (consistency check below, added after the first
      study).
    - Tests: `test_the_contract_solver_converges_at_every_applicability_corner`
      and `test_the_refinement_study_can_fail_euler_at_the_same_step` (which
      also shows `subsystem_standing` reporting `numerics_unconverged`).
  - **Mass/energy consistency.** `consistency` checks a trace with code of
    its own (the first version was a ledger the integrator closed by
    construction, and a mutated integrator still closed it; replaced
    2026-10-05). Every stored `(m, E)` must follow from the previous one by
    one step of the declared method under the declared mass and energy
    equations, re-stepped independently (bound 1e-9, rounding). The reported
    cylinder flow must be the breathing law `VE·m/V·Vd·rpm/120` over each step
    on the stored states: the start value for Euler, the trapezoid within its
    error bound `2·(h/τ)²/12·|law − inflow|` for rk4. Non-finite or
    non-positive states are named `state_nonphysical`, and the residuals are
    taken over the physical steps only, so one bad sample cannot switch the
    check off for the rest (1.3× the law plus a negative mass at step 3 is
    still refused, `test_a_nonphysical_sample_does_not_hide_an_inconsistent_replay`).
    `test_consistency_holds_a_correct_trace_and_fails_a_mutated_integrator`:
    a correct tip-in has residual < 1e-12 under both methods. An integrator
    that draws 1.7× the law fails both checks (residual > 1e-3, excess > 10).
    One that books 0.6× the energy rate fails the state check (> 1e-6).
    Reporting MAF as the cylinder charge fails the breathing check.
  - **The replay runs only on verified numerics.** `replay_manifold` takes
    the step and method from the contract (no caller step). It refuses a
    study of another step, method or check (`numerics_unverified`), a failing
    or empty one (`numerics_unconverged`), and a trace that departs from the
    equations (`integration_inconsistent`). It names non-finite or negative
    state per sample (`state_nonphysical`) next to `manifold_above_ambient`.
    The verifier's repro (2 L, VE 1.0, 6500 rpm, MAF 40→120 g/s gave
    −3.9e6 kPa at a caller-chosen 20 ms step) is refused through every route,
    and is finite and positive on the contract step. A contract whose solver
    step is one whole ECU tick fails its own study and its replay is refused
    (`test_a_coarse_step_or_an_unverified_study_is_refused`). Replay and
    `subsystem_standing` both ask one helper, `numerics_refusal`, so a passing
    study of another step or method leaves the stage `numerically_verified`
    False (`test_standing_reads_a_report_only_for_the_contracts_own_step`).
  - **Bounded applicability** (contract): NA hardware only
    (`na_stock_injectors_2026_10`), rpm 500–6500, MAF 0.5–300 g/s, baro
    60–105 kPa, IAT −30–70 °C; outside is refused by name
    (`outside_applicability`), and another hardware configuration is refused as
    a new plant. Invariant `manifold_at_or_below_ambient`: a sample above baro
    is NaN with `manifold_above_ambient`, never clipped. Time resolution: one
    engine cycle (τ is only ~1–4 cycles at every rpm), reported per step as
    `resolved_period_s`. Test
    `test_replay_refuses_outside_its_envelope_and_never_clips_above_ambient`.
  - **Validation: none possible from the corpus, and refused as such.**
    The catalogs have no MAP (PID 0x0B unsupported; the `bindings.yaml` "MAP"
    byte retracted 2026-08-08). BFS (0x1208) is K × MAF / rpm through the
    ECU's own filling filter, so it is the ECU's model of the same filling
    from the same MAF; `derived:VE_AMBIENT_PERCENT` is MAF over the ambient
    fill. `check_reference` derives each reference's lineage from the
    contract graph, the derived-channel registry and its declared inputs, and
    refuses both (`reference_not_independent`, naming `mass_air_flow` and
    `engine_speed`): `test_bfs_and_the_ambient_ve_cannot_validate_a_manifold_driven_by_maf`.
    The independent reference (`sensor:MANIFOLD_PRESSURE`) is
    `requires_measurement: map_sensor`. Standing is derived, never stated:
    `manifold_breathing` is built, numerically verified, **not validated**
    (`test_the_manifold_is_built_and_converged_but_not_validated`), and only a
    recorded `pass` against an available, independent reference validates a
    stage (`test_only_a_pass_against_an_available_independent_reference_validates`:
    a pass against BFS does not). No stage is validated today; the fuel stage
    reads its own E07.3 verdict (`plant_not_qualified`).
  - **What the corpus does bound** (`breathing-bound`, the seven fuel-plant
    split recordings, 24,282 warm in-envelope MAF reads with rpm paired
    one-to-one): the charge fraction MAF / ambient fill, which equals
    VE × p_m / p_baro and is the one breathing quantity MAF identifies. It
    exceeds the shared working VE of 0.7 on **2,575 reads (10.6 %)**: median
    0.876 at 4000–5000 rpm, 0.839 at 5000–6500, p99 0.88–0.92 above
    3000 rpm, max 0.94. There the steady manifold at VE 0.7 would sit above
    ambient. So peak VE is at least ~0.9 and the 0.7 working value is
    contradicted at high load; fuel-mv-1 caps its MAP at baro there (owed by
    its next `plant_version`, above). 30 reads exceed 1.0; the ones inspected are
    single 0.25 s MAF reads 33–81 g/s above the previous read at 760 to
    2300 rpm: tip-in filling, where MAF is not the cylinder charge.
    Test `test_the_breathing_bound_counts_where_the_shared_ve_is_contradicted`
    (synthetic).
  - **Claims, refused** (`claim_refusals` / `require_claim`, read from the
    mode's roles and the derived standings):
    - `prescribed_rpm_not_acceleration` from plant replay and the ROM
      comparison, even with every stage validated
      (`test_prescribed_rpm_is_never_an_acceleration_prediction`); a rollout
      may claim acceleration only when every stage on its path is validated.
    - Spark: declared and tested as a library gate; no producer exists yet.
      A power/torque claim with `changed_controls=["spark"]` is
      `spark_response_unmeasured` (needs `controlled_spark_sweep`) even with
      every stage validated; knock is always `knock_unmodelled`
      (`test_spark_edits_cannot_produce_qualified_power_or_knock`). The first
      producer must derive `changed_controls` from the ROM diff
      (`compare_roms`) and call `require_claim`; that derivation is owed.
    - Torque: `derived:ESTIMATED_TORQUE_NM` is refused as a torque reference
      (`reference_not_independent`: `mass_air_flow`, `delivered_lambda`,
      `throttle_position_actual`) even if marked available; H07's
      road-dyno torque shares only the declared rpm index and is independent
      but `not_validated` (H07.12/H07.8 owed)
      (`test_torque_is_never_calibrated_against_the_fuel_energy_estimate`).
  - **Tests.** `tests/test_engine_dynamics.py`, 19 pass (the two that run
    the real study are in `tests/slow_tests.txt`; the standing and replay
    tests use a stub report). With the E07.1–E07.6 suites (`test_sim_contract`,
    `test_fuel_plant`, `test_paired_scenarios`, `test_rom_effects`,
    `test_rom_effects_private_rom`, `test_plant_validation`) and
    `test_layering`: 137 pass.
  - **Fuel-plant MAP reconciled (2026-10-08).** The contract's
    `fuel_delivery.internal_models` has one entry per form, each naming its
    `plant_versions`. The fuel-mv-1 entry is `owed_by` (capped).
    The fuel-mv-2 entry is `reconciled_by`, and declares the stage's invariant
    `manifold_at_or_below_ambient`. It also has a `parameter_fits` entry for
    VE: a `log_fit` from `mass_air_flow`, `engine_speed`,
    `barometric_pressure` and `intake_air_temperature`, which `inherits:
    [maf_transfer_bias]`, with bounds engine speed 500–6500 rpm, baro
    79–82 kPa and IAT 23–50 °C. Its evidence records the idle-VE
    limitation.
    - The fuel stage's `implementation` lists fuel-mv-2 as
      `other_plant_versions`. `COMPUTES_INTERNALLY` stays
      `("manifold_pressure",)`: both versions compute MAP themselves. The
      new `MANIFOLD_MODELS` maps each form to the invariants it enforces.
    - The schema refuses:
      - a reconciled entry without its owner's invariants;
      - a version the implementation does not declare;
      - a fit that inherits no unresolved error, or inherits a known
        parameter.
    - `check_internal_models(contract, profile_dir)` needs exactly one entry
      per declared version and quantity. It also checks each entry against
      that version's code: an invariant the code does not enforce, or fit
      bounds that differ from the frozen fit's, is a problem
      (`test_a_reconciliation_is_held_by_the_plant_not_stated`,
      `test_the_ve_fit_names_the_maf_error_it_carries`).
    - `standings(..., versions={"fuel_delivery": "fuel-mv-2"})` (CLI:
      `tools.manifold_check standing --fuel-plant-version fuel-mv-2`) drops
      `internal_model:manifold_pressure:owed_by_next_plant_version`. fuel-mv-1
      keeps it. fuel-mv-2 is still unvalidated:
      `plant_not_qualified: … has no held-out recording (held_out_pending)`.
    - Reading the stage's integrated trace was not chosen. In steady regions
      it equals speed density at the same VE, and the stage still runs the
      fuel plant's constant 0.7 (load_kit loads fuel-mv-1).
  - **Remaining, by stage, and what each needs:**
    1. *Stage 1 validation* (hardware): a MAP sensor logged at ≥ 100 Hz with
       MAF and rpm over idle, cruise, tip-ins/outs and WOT; and the manifold
       volume (water fill or CAD). Then a held-out scoring recorded in the
       contract's `validations`. Until then the subsystem stays unvalidated.
       **Operator decision 2026-10-08:** no MAP sensor can be installed before
       the supercharger build. The sensor comes with that build: the kit's
       Bosch TMAP behind the intercooler, read through a Pi ADC
       (`docs/roadmap/supercharger-build.md`, Phase 2 P2).
       **Operator decision 2026-10-08:** no MAP on the NA intake, ever. The
       kit's TMAP mounts after the intercooler, and no temporary NA port will
       be fitted. The NA manifold/breathing stage stays a best-effort fit
       (MAF/rpm/baro/IAT) and is never validated against measured MAP. Only
       the boosted plant gets MAP validation. The contract's `map_sensor`
       plan `consequence` says so.
       - In the contract every `map_sensor` required measurement
         (`fuel_delivery`, `manifold_breathing`, `throttle_flow`,
         `torque_production`) is `status: planned`, with a `plan` (decided
         on and by, arrives with, recorded in, consequence). The schema
         requires a plan exactly when the status is `planned`.
       - Planned is not available. The `map_sensor` reference stays
         `requires_measurement`, and `requires_measurement:map_sensor` stays
         in every standing
         (`test_the_map_sensor_is_planned_with_the_supercharger_and_still_unavailable`).
       - The supercharged engine is a new hardware configuration, so the
         sensor validates only the boosted plant (decision above).
    2. *Throttle flow* (unbuilt): the throttle effective area needs MAP
       outside choked flow.
    3. *Actuator response* (unbuilt): requested throttle word 0xFFFF2D28 with
       sensor A at ≥ 50 Hz; VTC commanded/actual cam channels (not
       catalogued).
    4. *Torque production* (unbuilt): a validated H07 torque reference
       (H07.12 NA baseline, H07.8 repeatability, H07.10 mass), controlled
       spark, lambda and cam sweeps on it, and MAP for pumping.
    5. *Rotational dynamics* (unbuilt): H07.5 road load, H07.6 inertia and
       H07.10 mass on the truck.
    6. *Fuel rail* and *engine thermal* (unbuilt): rail-pressure drives
       across the load range; cold-start warm-ups.
    7. E07.6's coupled rollout stays refused (`dynamic_plant_unbuilt`) until
       these exist; `rollout_blockers` lists all eight stages today.
    8. A producer of claims: derive `changed_controls` from the
       `compare_roms` diff (a timing-table edit means spark) and call
       `require_claim`. The refusals are a library gate until then.
    9. *Done for fuel-mv-2, 2026-10-08:* the fuel plant's own MAP and the VE
       finding. fuel-mv-2 fits VE(rpm) and refuses MAP above ambient (E07.3,
       "fuel-mv-2"). fuel-mv-1 is frozen and stays owed, by name, for itself
       only.

- [~] **E07.6 — Run paired tuning scenarios without leaking observations.**
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
  *Partial 2026-10-05: the steady paired slice runs, and the negative
  control and the refusals pass. "A known edit passes the held-out
  intervention test" waits on E07.4.*
  - **Code.** `src/analysis/paired_scenarios.py` (layer 5).
  - **One scenario object, two ROMs.** A `PairedScenario` holds the plant
    identity, the operating points and the declared learned state. Both ROMs
    run from it, so nothing but the image can differ. Each point becomes a
    contract `Scenario` and passes `check_scenario`: two ECU identities in a
    ROM comparison, one per ROM in a rollout.
  - **Per point and per ROM it computes:**
    - X from a shared BFS, by the twin's `x_from_bfs` and the image's Load
      Point Scaler;
    - the Target AFR lookup (`target_phi.resolve_table`/`predict`);
    - the pulse word (`injector_pulse.predict_pulse`, with each image's own
      chain calibration);
    - delivered λ and fuel per event from the frozen E07.3 plant (manifold
      pressure is a stated boundary: `PlantInputs.map_kpa`).
  - **Nothing recorded or computed is fed back.**
    - Channels are refused (`recorded_value_refused`, from the contract).
    - A stated value whose source names a recording is refused
      (`recorded_value_refused`, here).
    - BFS as a boundary is refused (`boundary_not_supplied_here`).
    - A rollout that prescribes rpm, airflow or the trims is refused
      (`boundary_not_supplied_here`). A rollout that passes the contract is
      refused as `dynamic_plant_unbuilt` (E07.5).
  - **BFS is a conditioning value, not a held one** *(2026-10-05; superseded
    2026-10-13 by the steady BFS below)*. BFS formation is
    unmodelled, so the comparison is conditional on a stated BFS shared by
    both ROMs. That is allowed only where `compare_roms` shows the change does
    not reach it. A Fuel Comp edit reaches it, and every output then reads
    `conditioned_on_changed_path:base_fuel_schedule`, with no candidate value
    and no delta.
  - **Learned state.** Both runs start from the stated STFT/LTFT. A
    `candidate_events` flash (`ecu_reflash`) resets what the contract resets
    on it: `ltft_b1`, to 0 %. Relearning is not modelled, and an event that
    resets an uncarried state is refused (`reset_unmodelled`).
  - **What a result returns.** Per output per point: base, candidate, delta
    and status (`supported`/`unsupported`) with its reasons:
    - closed-loop or cold regime;
    - `short_pulse_undecoded`;
    - ROM gates.

    For λ it also gives the lumped scale's parameter band and the relative
    delta's range over every working-assumption bracket end. Every result
    also has coverage per output, the affected ROM cells (`affected_cells`:
    table, slot, row, column, old and new raw), the unresolved dependencies
    (ROM reasons, plant effects, `plant_not_qualified`), the traced-only
    outputs' ROM status, and stated assumptions (steady: the final φ equals
    the gated lookup).
  - **Synthetic stays synthetic.** Results are labelled `simulation`, and the
    traces use a `_sim-<hash>` session:
    - identification and evaluation refuse it (`synthetic_as_observed`);
    - every memory ledger, replay ledgers included, refuses it as evidence
      (`SyntheticIsolationError`, in `src/evidence/ledger.py`);
    - `src.session.is_synthetic_session` is the one test.
  - **Tests.** `tests/test_paired_scenarios.py`, 9 pass (7 below, 2 more after them):
    - `test_the_negative_control_rom_reproduces_the_baseline_exactly`: an
      identical image gives every output equal base/candidate, delta 0.0, and
      a byte-identical repeat.
    - `test_a_target_afr_edit_is_computed_through_the_pulse_and_the_plant`:
      a +4-count edit to every slot-1 cell moves WOT φ by +0.03125, the pulse
      by +0.41 ms and λ by −2.5 % (−2.52…−2.49 % over the assumptions). It is
      still not rankable.
    - `test_a_fuel_comp_edit_leaves_its_downstream_unresolved_and_unrankable`.
    - `test_the_flash_resets_the_learned_trim_on_the_candidate_only`.
    - `test_recorded_or_computed_values_cannot_be_fed_back`.
    - `test_synthetic_traces_stay_out_of_evidence_and_baseline_data`.
    - `test_optimisation_is_refused` (`optimisation_deferred`, always).

    `rank_as_improvement(result, intervention=InterventionVerdict)` takes the
    sealed E07.4 verdict, never a string. It refuses:
    - an unsealed verdict, a non-`pass` one or a synthetic one;
    - a verdict whose image hashes or plant baseline differ from the result;
    - a synthetic plant (`synthetic_plant`) or an unqualified one;
    - any unsupported output or point.

    Plant outputs at a point whose regime the plant did not qualify in are
    unsupported (`regime_not_qualified:<regime>`). `PairedResult.assumptions`
    states the LTFT/STFT open-loop assumptions. Two more tests (9 pass):
    - `test_ranking_needs_a_sealed_matching_verdict_and_a_recorded_qualified_plant`;
    - `test_a_closed_loop_only_qualification_cannot_rank_wot_deltas`.

    The paired tests also pass against HEAD `e19a1c1`'s manifest and rom_map
    (E05.2 committed), in a scratch worktree.
  - **Steady BFS per ROM and the closed-loop fixed point (2026-10-13, desk).**
    `TOOL_VERSION` 2; the helpers are `src/analysis/paired_steady.py`.
    - **BFS is computed, not conditioned on.** Each ROM's BFS comes from
      `air_charge_twin.predict_steady_bfs` (E05.2's implementation of the
      manifest's `base_fuel_schedule`) at the point's prescribed airflow (PID
      0x10 word), rpm word and a stated fill byte `fuel_comp_load_byte`, with
      that image's K, Fuel Comp, axes, multipliers and ceiling. It feeds X, the
      Target AFR lookup, the pulse and the plant. A new `base_fuel_schedule`
      output carries the word and its exact interval. Stating BFS or X is
      refused (`conditioning_not_allowed`: computed here); the fill byte is
      the conditioned quantity, and an edit that reaches it (a fill-curve
      edit) gives `conditioned_on_changed_path:fuel_comp_load_byte` with no
      candidate values.
    - **What the steady fixed point absorbs is declared in the contract**, not
      in code: the comparison mode's `base_fuel_schedule` `init.absorbs`
      (`airflow_qa`, `fill_coefficient`, `manifold_fill_filter`,
      `near_wot_flag`, `fill_transient`, `fill_blend`). `StateInit.absorbs` is
      allowed only for `steady_state`, and `check_against_application_model`
      refuses an absorbed node that is not upstream of the quantity's manifest
      node, or a node the manifest does not implement. `SteadyCoverage`
      treats `unmodelled:<absorbed>` and `observed_not_predicted:load_input_x`
      as computed only when the change does not edit that node itself; every
      covered reason is restated as an assumption in the result.
    - **Closed loop runs at the trim's fixed point.** Same air, rail and
      manifold for both ROMs, so the same delivered λ needs the same metered
      open time: k_c = k_b·G_b/G_c (`PulsePrediction.g` now exposes G). The
      candidate's pulse, λ and fuel equal the base's and the new
      `fuel_trim_total_pct` output carries the change;
      `candidate_stft_at_its_ltft_pct` gives the STFT the candidate needs at its
      (flash-reset) LTFT. Closed-loop outputs carry
      `closed_loop_trim_authority_undecoded` (the clamp is not decoded), so
      they are computed but unsupported. A point whose target φ word differs
      between the ROMs is `closed_loop_setpoint_moved`, with no candidate fuel
      values: the A/F feedback target's relation to φ (`0xFFFF46B2`) is not
      decoded.
    - **Tests (`tests/test_paired_scenarios.py`, 16 pass;** the synthetic
      image now carries the 20260920 air-charge calibration — K 28715,
      multipliers 226/3, MAF scale 149, flat Fuel Comp 128 — so WOT BFS is
      39,604 at 4,800 rpm/150 g/s):
      `test_a_fuel_comp_edit_is_computed_through_steady_bfs` (+3 counts: BFS
      × 131/128, WOT pulse +0.35 ms, λ −2.28 %, supported; ignition stays
      traced/unevaluable, so not rankable);
      `test_a_k_edit_moves_bfs_one_for_one` (K +5 % → BFS +5 %);
      `test_an_edit_reaching_the_stated_fill_byte_is_unresolved`;
      `test_a_computed_quantity_cannot_be_stated`;
      `test_closed_loop_runs_at_the_trims_fixed_point` (cruise: pulse, λ and
      fuel deltas 0.0, total trim 2.34 → 0.44 %);
      `test_a_moved_closed_loop_setpoint_is_not_computed`;
      `test_the_steady_fixed_point_covers_only_unedited_absorbed_nodes`;
      `test_absorbed_nodes_must_feed_the_fixed_point`; and the nine earlier
      tests, updated (the negative control is still exact, now with BFS
      computed).
  - **Remaining:**
    1. "A known edit passes the held-out intervention test": E07.4 must reach
       `pass` (a qualified plant and post-change drives, both truck).
    2. Coupled rollout: E07.5's dynamic plant.
    3. *Steady BFS formation, Fuel Comp, K and its axes: done (above).* Still
       open: off-steady BFS (the airflow pipeline and fill filters are
       stateful, application-model `airflow_unmodelled`), the fill byte
       (stated, not computed: `fill_byte_not_computed`), an edit to any
       absorbed node itself (kept unresolved), the undecoded short-pulse
       correction, and the injector-chain bytes, which are not in the manifest
       (a change there reads `unknown`; the manifest is E05's data).
    4. *Closed loop: steady fixed point done (above).* Still open: the trim
       authority/clamp (until decoded every closed-loop output is
       unsupported), the setpoint's dependence on φ, and the STFT/LTFT split
       and settling dynamics.

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
