# E05 — expand the twin from decoded ECU application code

Follow [E00](E00-second-brain-README.md). Operator clarification, 2026-10-04:
the aim is a **digital twin of the ECU running on the Pi**, using the recent
application-code findings to predict and verify observations. E02's target-φ
calculation is the first executable slice. This checklist extends it into
connected, stateful ECU computations, each qualified against the real ECU.

Start from [ECM firmware analysis](../../ecu-firmware-analysis.md), its
live-verification section and W08's exact image. Reproduce decoded arithmetic,
control flow and state transitions as versioned executable models. The exact
MCU/peripheral behavior and substantial application logic remain unknown;
full instruction/peripheral emulation is not a prerequisite for the first
slices. A claimed output must name the code path it reproduces and what
still comes from observed RAM. Never present partial coverage as a complete
ECU replica.

## Findings the implementation must carry forward

| Application-code finding | Twin consequence |
|---|---|
| Target function `0x055DB2` calls `0x0269AC`; helpers implement raw rpm/load search, bilinear interpolation and clamping | Reproduce actual integer semantics and call dependencies, not a generic interpolation chosen for convenience. |
| Nissan's Target AFR pointer is redirected to RAM `0xFFFFB260`, slot `+0x220` | Use the UpRev active RAM copy's matching ROM slot; the old calibration table at `0x00A169` does not drive this patched path. |
| UpRev init/copy/selector routines `0x0DE1A4`, `0x0DE3E4`, `0x0DE358` | Model default slot 1, magic, selector debounce and committed slot separately; active-map 0 after cold boot is meaningful state. |
| Pedal flag at `0xFFFF2841` bit 0 forces X=`0xFF`; curve `0xCEDB` and +8-count hysteresis govern it | First consume the observed flag; later predict its transitions after identifying its input and update order. |
| X derives from BFS using Load Point Scaler; Fuel Comp is slot `+0x660`, K is `+0x762` | Extend upstream from observed X into computed BFS/air charge only after tracing the exact arithmetic/update order and scaler source against observations. Fuel Comp acts inside BFS, before the filter, transient terms and rpm-dependent ceiling. |
| Fuel Comp axes are rpm `0xCACD` and load `0xCB29`, opposite UpRev's labels; cells remain row-major | Use the code's axis roles and exact image. The October 4 restoration changed load breakpoints and slot-1 cells; preserve separate calibration cohorts. |
| Fuel Comp's `0xFFFF118A` writer `0x03C3A4` uses throttle-derived flow per revolution and saturating curves | Model this alpha-N fill path separately from MAF-derived BFS. Purge `w139E`, sensor-B fallback `w2CD0` and idle-air correction structure `w63FC` are traced. Relative units and full-chain/branch validation still limit physical interpretation and qualification. B08 evaluates its use as behavior-model context. |
| Timing-trim tables at `+0x020`/`+0x120`, cells masked `& 0x7F`; rpm axis `0xCC93`, BFS-X axis `0xCC83`. The trim lookup `0x04E6D8` reads trim B when warm and takes A only while `0xFFFF283F` bit 6 is clear (corrected 2026-10-05: not `0xFFFF2842`, whose bit 2 is the knock control's previous-cycle copy of `0xFFFF2840` bit 6) | Preserve the corrected axis roles. Table locations are verified. "Trim B always" is not established: the UpRev binding of `0xFFFF283F` is inferred until a live read. Main/High Det meaning and the final timing chain still need evidence. |
| Slot `+0x260`, many 1-D curves and flash-resident maps remain unidentified | Track unknown functions and the measurement needed to identify them; do not guess cam/throttle/torque semantics from shape. |
| CAN and K-line Mode 22 dispatch to different RAM pointer tables | Observation mappings must include transport. Apply catalog corrections at their owner; stale trim labels cannot validate the twin. |
| PID 0x44 and target RAM read the same `0xFFFF26BE` word | Their agreement verifies observation mapping, not two independent confirmations of the command algorithm. |
| Same-read BFS/X differed by 1–4 counts on 26/194 transient pairs | Acquisition simultaneity does not imply atomic firmware updates; state/update timing is part of fidelity and error accounting. |

## Checklist

- [~] **E05.1 — Build a versioned application model and coverage manifest.**
  Trace each proposed output backwards through writers, callers, RAM state,
  flags and ROM tables. Record function/address references, integer widths,
  signedness, rounding/saturation, branch conditions, state/update order,
  observed versus predicted dependencies, confidence and evidence ids.
  Identify compatibility using verified program/patch identity plus full
  image hash for calibration; never use calibration ID alone. Keep Nissan
  and UpRev disassembly/decompiled output local; commit behavioral contracts,
  profile metadata, our implementation and evidence references.
  **Accept:** the target-φ dependency graph resolves to E02 inputs and W08
  tables; every undecoded dependency marks affected outputs/regions as
  conditional or unevaluable. Wrong-program artifacts refuse by name.
  The dashboard can report qualified outputs and uncovered logic from this
  manifest. Backfill E02 with it without changing established predictions.
  *Partial 2026-10-04 (manifest, loader, E02 backfill and read API done; the
  dashboard view is E04.5's):*
  - **Manifest.** `data/nissan-xterra-2008/ecu_application_model.yaml`
    (`schema_version` 1, `model_version` 1) is validated by `ApplicationModel`
    in `src/analysis/ecu_application.py` (layer 5; no new package). It covers
    output `target_equiv_ratio` (0xFFFF26BE), traced through 20 nodes, each
    with its function/span and the RAM it reads and writes. All 89 addresses
    are declared once under `ram` with width and signedness (`unknown` where
    the purpose is not identified). The schema refuses a duplicate
    declaration and an observation at an undeclared address.
    `test_every_address_a_node_describes_is_one_it_reads_writes_or_observes`
    fails if a node's arithmetic or branch text cites a 0xFFFF____ address
    outside its reads, writes or observations. Each node also
    records its arithmetic, rounding, saturation, branches, ROM constants and
    calibration conditions; whether it is `observed`, `code`, `inferred` or
    `unknown`; its twin role; and evidence. The update order is the
    `schedule` of task `0x0575EC`, after the cycle-start copy `0x020EF2`.
  - **The graph resolves.**
    `test_the_graph_resolves_to_the_twins_inputs_and_w08_tables` checks three
    things. The observed inputs on the path of the predicted node
    (`enrichment_gate`) are exactly the twin's six channels. Every cited table
    is a `rom_map.yaml` id. Every `mem:` observation's address matches
    `data/dids.yaml` (`check_observations`; a moved address is named).
  - **Undecoded dependencies make the output conditional.** The schema
    refuses an `assumed`/`not_modelled` node on an output's path that no
    blocker names (`test_an_unresolved_dependency_cannot_be_dropped_silently`).
    It also refuses a blocker naming a resolved node
    (`test_a_blocker_cannot_outlive_its_cause`). Target φ is conditional on
    `final_command_path_unverified` (10 nodes) and `ram_copy_unverified`.
    `regions` mark the gate-band and stack-word cases unevaluable, and cold
    state 5 and fuel-cut recovery conditional. `uncovered` lists 8 pieces of
    unresolved logic.
  - **Identity.** Compatibility is the sha256 of the program spans
    (0x0–0x7FFF and 0x20000–0xDFFFF: what UpRev flashes never write). That
    hash is equal on all six local ZS44A images, five calibrations. Added to
    it: UpRev's patch marker (0x055EC4 = 0xFFFFB260), the calibration
    conditions behind each dead path, and the full image hash. `check_image`
    refuses `wrong_program`, `patch_missing` or `unmodelled_logic`. Without
    bytes, `check_image_sha` refuses `program_unverified`. Tests:
    `test_the_same_program_passes_and_each_mismatch_refuses_by_name`;
    `test_the_calibration_region_does_not_change_the_program` (a changed
    calibration ID or cell keeps the program hash);
    `test_every_listed_local_image_is_this_program` (private, skips by name).
    `program.images` holds only registry images
    (`test_the_listed_images_are_the_registrys`). Map1Change (3dc1f3d9…)
    passes `check_image` but is excluded: it is an unflashed research edit
    that no session or registry entry names.
  - **E02 backfill.** The twin's file lost `final_command.status` and
    `map_identity.ram_copy`; a status there is now refused as an extra
    field. `application_output: target_equiv_ratio` names the manifest
    output. `resolve_table` and `table_from_calibration` require the model.
    `bind_application` refuses `application_model_mismatch` /
    `output_undeclared`, and `resolve_table` also runs `check_image`.
    `qualification_blockers` come from the manifest
    (`test_qualification_follows_the_application_models_blockers`).
    Provenance names the model, and `EVALUATOR_VERSION` is 2. The fixture is
    unchanged: `tests/test_target_phi.py` and the private-ROM file pass
    80/80, including `test_every_october_4_command_sample_is_accounted_for`
    (same 174 records and blockers) and the image-backed accounting.
  - **Read API.** `coverage_report()` gives each output's status (`qualified`
    only with no blockers) and its path by twin role (implemented / observed
    / assumed / not modelled / calibration-dead), blockers, conditions,
    regions and uncovered logic. It is served at
    `GET /control/ecu/application-model` (control token; route snapshot
    updated, documented in `docs/headless-api.md`); see
    `test_the_route_serves_the_report_behind_the_control_token`.
  - **Remaining:** the dashboard view of this report is
    [E04.5](E04-pi-runtime-and-acceptance.md)'s job.

- [ ] **E05.2 — Extend upstream through air charge and fuelling.**
  Trace `0x03C4D0`, K's read at `0x03C6E0` and Fuel Comp readers including
  `0x02D5F4`. Carry forward the verified axis roles: `0xCACD` rpm and
  `0xCB29` load against RAM `0xFFFF118A`, the reverse of UpRev's labels.
  Its writer `0x03C3A4` is now decoded as throttle-derived flow per revolution
  through the `0xEAEC`/`0xEACC` saturating curves, not MAF airflow. Resolve
  the remaining `w139E` flow term, alternate throttle source `w2CD0`,
  correction `w63FC`, fallback conditions, absolute units, integer semantics
  and which Load Point Scaler copy is consumed. Build the
  dependency chain toward BFS → X → target φ from independently observed
  sensor inputs, retaining intermediate RAM observations for verification.
  **Accept:** each new intermediate reproduces known vectors and eligible
  independent-drive observations under predeclared tolerances and pairing;
  controls using wrong neighbouring bytes/scales fail. Feeding observed BFS
  may validate downstream logic but cannot count as predicting BFS. Unknown
  upstream dependencies remain visibly conditional. **Capture likely needed.**
  [B08.2/B08.3](B08-firmware-informed-behavior.md) can evaluate observed fill
  as behavior-model context before this entire upstream path is qualified;
  that evidence cannot be counted as reconstructing the fill computation.

- [~] **E05.3 — Reproduce state transitions and additional fuel regimes.**
  Decode and implement pedal enrichment hysteresis, map selection/init/copy,
  and then individually identified warm-up, closed-loop, transient and
  fuel-cut command overrides. Model time/state initialization explicitly:
  mid-drive attachment can use an observed state checkpoint with provenance;
  missing history must not silently assume cold boot or steady state.
  Measure transition/update timing rather than aligning each test trace to
  minimize its error. Keep the first slice's exclusions until each addition
  qualifies separately.
  **Accept:** held-out transition sequences reproduce predicted flags,
  branch/slot and command with frozen value/time tolerances. Reset/gap and
  map-switch cases become unevaluable until required state is known. Both
  transition misses and excluded samples remain counted. **Vehicle required**
  for unobserved map switching and new regimes.
  *Partial 2026-10-04 (desk decode and reference models done; the
  held-out transition acceptance needs captures):*
  - **Models.** `src/analysis/ecu_state_models.py` (`MODEL_VERSION` 1,
    layer 5) holds the pedal flag (`PedalFlagModel`, `pedal_flag_next`,
    `pedal_comparand`, `byte_curve16` for `0x026BBC`), UpRev init/selector/
    copy (`uprev_power_on`, `uprev_step`, `loaded_slot`) and the enrichment
    request and commit (`enrichment_request`, `enrichment_commit`,
    `enrichment_step`). No state defaults. A latch, timer, selector or
    committed state is known from an observed `Checkpoint` (source and
    acquisition time), or because this cycle's inputs fix it (a hysteresis
    input outside its band; a branch that zeroes `0xFFFF1748`). Otherwise
    the step returns `None` with a named reason
    (`hysteresis_band_state_unknown`, `warm_latch_unknown`,
    `rpm_band_latch_unknown`, `timer_unknown`, `commit_flag_unknown`,
    `uprev_debounce_unknown`, `uprev_state_unobserved`, …).
    `uprev_power_on` refuses unknown RAM. An unknown magic is never treated
    as a cold boot.
  - **Decoded.**
    - *Pedal flag.* The curve `0xCEDB` input `0xFFFF11CF` is
      `min(255, rpm word >> 1)`, exact for every `w1204`. The comparand
      `0xFFFF3CDC = min(255, w2D28 × 20571 >> 16)` is written by `0x05535C`,
      the fuel task's first call, so the flag compares this cycle's value.
      `w2D28` (task 25) is the throttle position the target area `w1A64`
      needs, through the inverted area curve. So the flag is about 78 % of
      *requested* opening, not measured pedal. The curve is 200 at every rpm
      on every listed image: the flag sets at 200 and clears below 192.
    - *UpRev.* Init writes the magic, zeroes `0xFFFFB000`–`0xFFFFB00F` and
      copies slot 1, unless the magic is already present (a warm start).
      This includes `0xFFFFB008`; the `data/dids.yaml` note says the
      opposite. The selector (task 111, after the fuel task):
      - bit 1 rising edges count while bit 3 alone is held;
      - releasing to 0 from 8 commits;
      - a selector of 0 or above 5 commits nothing, but is shifted into
        `0xFFFFB00C`;
      - the countdown `selector × 64` blinks `0xFFFF4858` bit 0.
      A commit's copy is first read by the next cycle.
    - *Enrichment states.* Cal `0x8794`/`0x8781`/`0x8796` (25/15/9) are the
      timer limits. Cal `0x8795`/`0x8782`/`0x877F`/`0x8780` = 0 kill four
      branches. The request's latch thresholds are calibration bytes too:
      cal `0x8C98` (0x73), `0x879C` (0x3E), `0x879B` and `0x87A0` (0x44),
      `0x879F` (0x28), and `0x879D` (0xFE, the `0xFFFF10C5` compare); only
      the hysteresis amounts are code immediates. All of these, and the
      16-entry curve `0xCEDB`, are `calibration_conditions`, so another
      calibration refuses `unmodelled_logic`
      (`test_every_constant_a_reference_model_implements_is_checked_on_the_image`).
      `0xFFFF1744`/`1746` are only ever decremented.
  - **Manifest.** `ecu_application_model.yaml` is now `model_version` 2. It
    adds the nodes `requested_throttle_position`, `pedal_comparand` and
    `uprev_init`, and corrected RAM meanings. Nodes carry a new
    `reference_model` with `reference_validation`
    (`synthetic`/`fixture`/`vehicle`). The schema allows it only on a node
    the twin does not implement, so the first slice's exclusions and target
    φ's blockers are unchanged. The schema checks pass:
    `test_reference_models_resolve_and_leave_the_target_phi_path_as_it_was`.
  - **Validated (synthetic, labelled):** 13 code-derived E05.3 tests in
    `tests/test_ecu_state_models.py`, among them
    `test_the_curve_input_is_the_rpm_word_halved_exactly`,
    `test_the_flag_needs_its_history_only_inside_the_eight_count_band`,
    `test_two_presses_while_holding_commit_map_two_and_blink_it_twice` and
    `test_transition_timing_is_counted_in_ticks_because_the_tick_rate_is_not_decoded`.
    In ticks: request 2 holds 30, and request 4 gives 2 for 17 then 6 for
    33.
  - **Fixture accounting (no model scored).** On the October 4 slice,
    196/196 UpRev reads classify (`loaded_slot`) as slot 1 by init, with no
    map change; `uprev_step` itself is not exercised (no switch, input not
    read per cycle). All 191 flag reads and the 4 observed transitions are
    counted unevaluable (`input_not_captured:0xFFFF3CDC`), none scored. So
    every `reference_validation` is `[synthetic]`. See
    `test_october_4_ran_slot_one_by_init_on_every_read_with_no_switch` and
    `test_every_flag_read_and_transition_of_october_4_is_counted_unevaluable`.
    The target-φ fixture is unchanged: `tests/test_target_phi.py` and
    `test_target_phi_private_rom.py` pass.
  - **Timing is a measured requirement, not fitted.** The `0xFFFF84C3`
    bit 7 tick rate and the `0x020EF2` period are not decoded. They are
    `uncovered: enrichment_tick_rate` and
    `ecu_state_models.TIMING_REQUIREMENT`.
  - **Remaining (vehicle):**
    - **(a) Pedal flag.** Capture one 0x23 window set holding `0xFFFF3CDC`,
      the flags window widened to `0xFFFF283F`–`0xFFFF2842`, `0xFFFF2D28`,
      `0xFFFF1A64` and the rpm word. Drive repeated pedal stabs through
      about 75–80 % requested opening, then score
      `account_pedal_flag`. Trace the `w1A64` writer
      (`uncovered: pedal_flag_inputs`).
    - **(b) Enrichment states.** Capture `0xFFFF14C4`–`0xFFFF14C5`,
      `0xFFFF1742`–`0xFFFF1749`, `0xFFFF2866`, `0xFFFF2875`, `0xFFFF2876`,
      `0xFFFF288A`, `0xFFFF174B`, `0xFFFF10C5` and `0xFFFF26C0`, held
      through enrichment in each rpm band (below 3,100, 3,100–3,400, above).
      This measures the tick, then gives held-out transition sequences.
    - **(c) Map switching** needs an operator decision (report) and the
      `0xFFFFB000`–`0xFFFFB00F` block with `0xFFFF2EE0` read through a known
      switch sequence.
    - **(d)** Warm-up, closed-loop, transient and fuel-cut overrides are not
      started.

- [~] **E05.4 — Add ignition and later identified outputs incrementally.**
  Start by tracing both timing-trim readers through the final ignition
  command, including table selection, masking, corrections and clamps. Keep
  the Main/High Det labels inferred until observed. Investigate slot `+0x260`
  and unnamed curves/maps only through identified readers and independently
  observed quantities; record a capture recipe once a candidate is testable.
  **Accept:** each added output has its own known-answer fixture, dependency
  manifest, input/output units, eligible regions, tolerances and independent
  validation. An isolated trim-table prediction is labelled an intermediate,
  not final ignition timing. Unidentified maps cannot emit qualified
  physical predictions. **New capture required where evidence is absent.**
  *Partial 2026-10-04 (desk trace and the intermediate done; independent
  validation needs captures):*
  - **Both readers traced.**
    - *Trim lookup, task 66 `0x04E6D8`.* Its load input is `0xFF` under the
      pedal flag, gated by cal `0x8ADC` bit 1. Each table is read by
      `0x026A64` + `0x026F9C`, which masks cells `& 0x7F` before the
      interpolation and ORs bit 7 of the lower-left cell back in. By coolant:
      - at or below 106 (cal `0x8CCD`): trim A only;
      - above 110 (cal `0x8CCC`): `0xFFFF1777` reads trim B while
        `0xFFFF283F` bit 6 (the latch UpRev logs as "HI DET") is set, else
        A; `0xFFFF177A` always reads A;
      - in between: a blend through the signed divide `0x021C48`, modelled
        as unevaluable.
    - *Knock control, task 67 `0x04A2E8`.* It reads both trims without the
      pedal column. It sets the knock retard to `13FE + (A − B) << 8` only
      when `0xFFFF2840` bit 6 is set and `0xFFFF2842` bit 2 is clear
      (`0x04A512`–`0x04A522`). Bit 2 is bit 6's copy from the previous
      cycle (written at `0x04A5D2`–`0x04A5FE`), so the branch fires on the
      one cycle bit 6 rises. `0xFFFF2842` read `0x04` on every October 4
      read: `0xFFFF2840` bit 6 was set on the cycle before every read.
    - *Correction.* The table *selector* is `0xFFFF283F` bit 6 (B by
      default when warm, A while it is clear), not `0xFFFF2842` as
      `rom_map.yaml` and the firmware doc say. Owners notified through the
      report.
  - **The chain to the timing byte** is manifest nodes:
    1. `timing_trim_terms`: `0x04BD24`, `0xFFFF2793 = max(0, trim − 0xFFFF11CB)`.
    2. `base_timing_sum`: `0x04CB9E`, into `0xFFFF1A58`.
    3. `base_timing_select`: `0x04CF0C`, into `0xFFFF27DA`.
    4. `timing_cycle_copy`: `0x0212C4`, into `0xFFFF274E`.
    5. `timing_retard_combine`: `0x04DF38`, into `0xFFFF2332` / `2338` / `1346`.
    6. `timing_rate_limit`: `0x04E0E0`, +4 / −15 per segment, 50 cap,
       −7 floor, into `0xFFFF232E`.
    7. `timing_final_byte`: `0x04E4AC`, `0xFFFF11FF = 110 − 0xFFFF232E`.

    That covers the selection, corrections and clamps. The manifest gained
    40 RAM entries in all (36 for ignition), each with width, signedness,
    units, confidence and a meaning; the evidence is on the nodes that read
    or write them. Output
    `ignition_timing` (PID `0x0E`; `mem:ECM_IGNITION_TIMING_RAM` as an
    alias) is conditional on `trim_ram_copy_unverified`,
    `high_det_latch_unobserved` and `ignition_chain_unmodelled`.
  - **Intermediate.** Output `ignition_trim_selected` (`0xFFFF1777`,
    `role: intermediate`, `feeds: ignition_timing`). The schema refuses
    each of these:
    - an intermediate without `feeds`;
    - one that feeds a non-final output;
    - one at the fed output's word, or off its path;
    - a final output without a channel.

    Status is `qualified` only with no blockers *and* a channel; with no
    channel it is `unobserved`
    (`test_the_trim_lookup_is_an_intermediate_of_ignition_timing_never_timing_itself`,
    `test_the_model_refuses_an_intermediate_labelled_as_final_or_a_loose_reference`).
    `timing_trim_lookup` is `implemented` by
    `ecu_state_models.selected_timing_trim` / `trim_lookup`.
  - **Known answers.**
    - Synthetic: `test_the_trim_lookup_masks_cells_and_carries_the_lower_left_flag`
      and `test_the_selected_trim_follows_coolant_the_high_det_latch_and_the_pedal_column`.
    - Private ROM, an axis-role and table-identity check (not independent
      validation of timing):
      `test_the_lookup_reads_k106s_edit_where_the_axis_roles_put_it`. Over
      all 256 grid points the lookup sees exactly k106's ten trim-A edits,
      rows 7–9 (about 2,800–3,600 rpm) × columns 12–15 (the highest load),
      +1/+2, and none in B. Three off-grid points match values hand-worked
      from the cells, including truncation toward the first operand.
    - The Main/High Det labels stay inferred (tables are named A/B).
  - **Remaining (vehicle).**
    - **(a)** Read `0xFFFFB060`/`0xFFFFB160` (512 bytes, 0x23 windows) once
      at key-on and compare with the resolved slot. That closes
      `trim_ram_copy_unverified`.
    - **(b)** Capture `0xFFFF1777`, `0xFFFF177A`, the flags window from
      `0xFFFF283F`, the X byte, the rpm word and coolant in one window set,
      warm, across part-throttle rpm/load. This adds a catalog channel for
      the intermediate and tests "B when bit 6". Add a warm-up pass through
      56–60 °C for the blend.
    - **(c)** Capture `0xFFFF2793`, `0xFFFF1A58`, `0xFFFF27DA`,
      `0xFFFF2332`, `0xFFFF1346`, `0xFFFF232E` and `0xFFFF11FF` together to
      locate the earliest disagreement.
    - **(d)** Identify the unnamed terms (`uncovered: timing_final_chain`).
    - **(e)** Slot `+0x260` and the unnamed curves are untouched.

- [ ] **E05.5 — Verify connected computations on the Pi.**
  Run each qualified increment through E02's per-sample accounting, E01
  evidence, E03 investigation and E04 parity/budget gates. Freeze predicted
  outputs before examining their corresponding target observations; prohibit
  using an output or its alias as an input to the path being validated.
  Compare intermediate and final residuals to locate the earliest supported
  disagreement without claiming a unique cause from it. Preserve program,
  image, model version, observed state and sampling uncertainty per result.
  **Accept:** a Pi trace shows input → predicted intermediate → predicted
  command → observed command, with evidence at each boundary. An intentional
  wrong-table/branch control is detected, shared-register aliases do not
  inflate evidence, and integrated load still passes E04.3. Broader coverage
  never silently changes previously qualified behavior.
