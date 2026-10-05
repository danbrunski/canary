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
  - **Remaining:**
    1. A revised plant structure, with a new `plant_version`, a new baseline
       and newly declared bounds. The in-sample failure points at the
       idle/short-pulse region and the 10–40 g/s tail. It must then pass
       on **never-scored held-out drives** (truck). The four above are spent.
       Log PID 0x42 with the pulse DIDs on those drives, so that Ts is not
       taken from fuel-cut reads.
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
  - **BFS is a conditioning value, not a held one.** BFS formation is
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
  - **Remaining:**
    1. "A known edit passes the held-out intervention test": E07.4 must reach
       `pass` (a qualified plant and post-change drives, both truck).
    2. Coupled rollout: E07.5's dynamic plant.
    3. BFS formation, Fuel Comp and the manifold filter are not modelled, so
       Fuel Comp/K/axis edits stay unresolved. The short-pulse correction is
       undecoded. The injector-chain bytes are not in the manifest, so a
       change there reads `unknown`.
    4. Closed-loop trim response is not modelled, so closed-loop points are
       unsupported.

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
