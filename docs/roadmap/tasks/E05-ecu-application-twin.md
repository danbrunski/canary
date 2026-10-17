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
  *Partial 2026-10-04 (manifest, loader, E02 backfill and read API done);
  dashboard view added at the desk 2026-10-13; seeing it on the Pi remains:*
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
  - **Dashboard (desk, 2026-10-13).** *Diagnose → Investigations* ends
    with an "ECU application model" panel
    (`panels/EcuApplicationModelPanel.tsx`, mounted in `BrainPage.tsx`;
    getter `api/ecuApplication.ts`, types `types/ecuApplication.ts` typed to
    the schema's literals). It reads `/control/ecu/application-model`
    through `useControlResource` + `ResourceGate` (token gate; the 404 for a
    vehicle without a model shows as the server's refusal) and shows: model
    version, program and patch, the running image (known revision/role, or
    "not checked against this program"); per output its status (`qualified`
    in the ok tone only when the report says so) with the blockers holding
    it and what closes each, the twin's path counted by role in reading
    order (implemented, observed, assumed, not modelled, calibration-dead),
    and the unevaluable/conditional regions; then the uncovered logic with
    what it blocks and the next evidence. Tests:
    `EcuApplicationModelPanel.test.tsx` (5 passed: role order, status text,
    token gate, full report, refusal). `pnpm typecheck`, `pnpm lint`,
    prettier clean.
  - **Remaining:** seeing the panel against the Pi's daemon (phone
    demonstration with E04.5).

- [~] **E05.2 — Extend upstream through air charge and fuelling.**
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
  *Partial 2026-10-05 (desk trace, kernel, the steady-state check and the
  listing-settled desk items done; the dynamic chain, the intermediates and
  the RAM copies need a capture):*
  - **Traced.** `0x03C4D0` is now split: `0x03C62C` forms BFS. Its readers
    and writers are manifest nodes in `ecu_application_model.yaml` (model
    4), with spans, widths, branches and calibration conditions:
    `airflow_qa`, `fill_mode_flags`, `air_charge_ram_copy`, `air_charge_tp`,
    `fuel_comp_lookup`, `fill_coefficient`, `fuel_comp_product`,
    `manifold_fill_filter`, `near_wot_flag`, `fill_transient`, `fill_blend`,
    `base_fuel_schedule` and `air_charge_init` (`0x02D5F4`, which seeds Fuel
    Comp and the filter at its fixed point at reset). The summary is in
    [ECM firmware analysis](../../ecu-firmware-analysis.md), "BFS formation,
    traced (E05.2)":
    - *Tp and K.* K is read at `0x03C6E6` (pointer `0x03C6E0`). Tp is
      `min(0x726F9, ⌊⌊K·Qa·226/256⌋/(3N)⌋) × 0x23CB >> 16`, computed as a
      24.8 product and a two-step divide with a `0x727` short cut. The two
      literals are now `rom_map.yaml` program constants.
    - *Fuel Comp.* The product is `min(0xFFFF, Tp·FC >> 7)`, using the
      verified axis roles.
    - *Filter, transient, blend, ceiling.* The filter's unique fixed point is
      `min(0xFFFF, 4·w105C)`; the transient term is
      `clamp_s16((Δ·160) >> 6)`; the blend and the `D8B3` ceiling follow.
    - *Load Point Scaler.* Both X divides load the word at `0x9416`; the slot
      copy at `+0x760` is never read.
    - *RAM copies.* K and Fuel Comp are read from the active-slot RAM
      (`0xFFFFB7A2`, `0xFFFFB6A0`), not the ROM. Node `air_charge_ram_copy`
      is `assumed`, under blocker `air_charge_ram_copy_unverified`.
      - *Partial, K only.* The drive check scores the slot's K 28,715 at
        median 0.9991, while the base K gives 1.0590, so the RAM K is within
        about 1 % of the slot's.
      - That does not identify the slot (all five hold 28,715) and says
        nothing about the Fuel Comp cells.
    - *Update order and periods.* BFS and the Fuel Comp lookup run in two
      dispatch groups (tables `0x021874` and `0x02189C`). The ATU compare ISR
      `0x02040C` paces them: it adds 320 per tick at φ′/32 and sets
      `0xFFFF84B9` bits — BFS every 4 ticks; Fuel Comp, the fill
      coefficient and the background main cycle `0x020EF2` every 10. At the
      inferred Pφ of 20 MHz a tick is 1.024 ms, so BFS runs every 4.096 ms and
      the others every 10.24 ms.
  - **Desk items settled from the listing (2026-10-05):**
    - *`w20E2` is 0 by calibration, not by code.* `0x03C2EC` computes
      `(b216A − 0x7B) × cal 0x874D >> 5` or `b6424 × cal 0x874D >> 5`. It
      skips the write while cal `0x86C8` bit 5 and `0x8ADE` bit 0 are both
      set. Calibration conditions now pin `0x874D` = 0 and `0x86C8` bit 5
      clear on `airflow_qa`, and the word `0x9066` = `0x0333` on
      `fill_coefficient`.
    - *Airflow pipeline.*
      - The MAF low-pass `w20A8` uses gain cal `0x86D9` (26/256) and is
        nudged one count when it stalls.
      - Off the transient model, the average `L20AC` (cal `0x8A68`, 128) can
        stall **one count below** a rising input. The twin's airflow interval
        now allows it (`steady_airflow_interval`, `AIRFLOW_FILTER_STALL`;
        `0x8A68` is pinned).
      - On the transient model, the lead path is decoded: gain cal `0x82C2`
        (45/8), pulsation cal `0x82BA`–`0x82BC`, carried residue `w20EC`.
        It is exact at a fixed point.
      - The hot-wire term decays to 0 after start. This one is read from the
        decompile text; the listing was checked only for its stores.
    - *`w1056` filter.* `0x022248` is a variable left shift, so with cal
      `0x89B3` = 1 the filter is a two-point average,
      `(w1054 + w273E + w20BA) / 2`, nudged when it stalls. It settles
      exactly.
    - *`w273E`* is the cycle-start copy of `w27CA`. The EVAP purge control
      writes `w27CA` at `0x06E2E8` as `(w1046 − w8DC4) × w38AA / 0x19000`,
      where `w1046` is the snapshot of `w1056`:
      - `w8DC4` is last run's `w27CA` (through `w8ED2`, `0x06F356`; 0 at
        init, `0x06F4AE`);
      - `w38AA = 4 × min(0x3FE6, u × w902C >> 10)` (`0x06DE02`–`0x06DE22`),
        where `u` is the controller's purge step and `w902C` a limit it
        learns from the A/F feedback words (names inferred).

      Its unit is the airflow word's: 149/25,600 g/s per count, from the SAE
      PID 0x10 scale and cal `0x83E9`. That it is purge vapour flow is
      inferred.
    - *`0xFFFF283B` bit 6* has one writer, `0x03B994` (store `0x03BB02`). It
      rises when all of these hold, and holds while `0xFFFF2834` bit 4 is
      set:
      - `0xFFFF283E` bit 6;
      - fill-model state `0xFFFF4088` = 2;
      - coolant at or above cal `0x872A`;
      - `0xFFFF2834` bit 4;
      - no MAF fault;
      - `|4·w105C − BFS|` within cal `0x8A7B << 8`.
    - *`0xFFFF2C91`* is written only by `0x03F048`: the median of the last
      three `min(255, 0xA2C2 / w2648)` (signed divide `0x021C48`), or
      `0xFFFF1206` while `0xFFFF28B1` bit 5 is set. That `w2648` is the
      crank-segment period is inferred; the code uses the result × 8 as a
      1.5625-rpm word.
    - *`0xFFFF4786`.*
      - Bit 7: `0x0398DE` sets it from I/O port `0xFFFFF75E` bit 4, and
        `0x0394D6` sets or clears it from state `0xFFFF407C`.
      - **Bit 6** is written at `0x039CF0` by `0x039904`: `and 0xBF`, then
        `or 0x40` when r7 ≠ 0. r7 starts as the old bit 6 (`0x03991C`). It
        is set when `0xFFFF28A5` bit 5 records the start conditions (bit 7,
        `0xFFFF2C91` < 0x18). It is cleared when `0xFFFF28A5` bit 6 records
        an end (bit 7 clear, `0xFFFF1206` > 0x4F, the battery-voltage and
        coolant-timer terms).
      - *Correction:* the previous round said bit 6 had no writer. My store
        scan matched `@rN` only at the end of a line, so it skipped every
        commented store. The fixed scan finds `0x039CF0`.
    - *Absolute units.* At steady state BFS ≈ 44.18 × K × (FC/128) ×
      MAF[g/s] / rpm, which is about 21,144 counts per g/rev at K 28,715.
      This comes from the code's constants and the airflow-word unit, and is
      only as good as the MAF curve. The fill byte and the area stay
      relative: no physical constant multiplies them.
    - *`w139E` / `w2CD0` / `w63FC` and the fallbacks* stand as traced
      before: purge area; the sensor-B substitution through `0xFFFF28B5`
      bits 7:6 from `0x072528`; limp-home on `0xFFFF484E` bit 2; idle air
      capped at cal `0x8ED2`. Only their history values are missing
      (`uncovered: fuel_comp_fill_terms`).
  - **Kernel.** `src/decode/air_charge.py` (`AIR_CHARGE_KERNEL_VERSION` 1)
    is pure: it imports only `ecm_lookup` and the standard library, and is
    added to the E06 `KERNEL_MODULES` purity checks. It refuses
    non-integers and out-of-domain words, and `maf_word_interval` refuses a
    word the PID handler cannot report.
    - *Contract gap.* The kernel is in `data/ecu_twin_contract.yaml` as
      Python-only `inventory`, with blocker `air_charge_not_in_contract`. A
      language-neutral form would need Rust symbols, vectors and a
      `contract_version` bump.
    - *Known answers.* Synthetic tests in `tests/test_air_charge.py`,
      including
      `test_the_two_step_24_8_divide_is_the_floor_formula_below_the_wrap`
      (5,000 random tuples),
      `test_the_filter_settles_on_one_fixed_point_from_either_side`,
      `test_every_reported_maf_word_inverts_to_exactly_its_airflow_words`
      (all 38,144 words),
      `test_the_steady_airflow_interval_allows_the_averages_one_count_stall`
      and `test_a_calibration_that_breaks_a_zero_or_a_constant_refuses_by_name`.
      The last flips `0x874D`, `0x86C8` bit 5, `0x8A68`, `0x9066`, `0x9067`
      and `0x8AE1` bit 5, and each refuses `unmodelled_logic`.
  - **Twin (steady state, independent inputs).**
    `src/analysis/air_charge_twin.py` predicts BFS from four inputs: the
    reported MAF word (DID 0x1209 / PID 0x10, inverted through cal
    `0x83E9`), the rpm word, K, and Fuel Comp. Fuel Comp comes from a logged
    `0xFFFF118A` when there is one, otherwise the exact band for that rpm.
    The result is an exact interval. Observed BFS/X are never inputs
    (`test_observed_bfs_is_scored_never_used`).
    - *Gates.* Frozen before scoring and pinned by
      `test_the_gates_are_the_ones_declared_before_scoring`.
    - *Pairing.* The evaluator pairs raw reads itself and counts every
      excluded read by reason.
    - *Regions assumed.* The cranking, MAF-fault and subtracting-form flags
      have no channel. Every scored sample carries `regions_assumed`, and the
      summary counts them: the score assumes those regions, it does not check
      them.
  - **Verified on drives** (`tests/test_air_charge_private_rom.py`, private
    image plus local sessions; skips by name without them):
    - *Data.* Seven ZS44A-20260920 captures, 14,843 DID 0x1208 reads; 458
      steady reads scored, all 458 under the three assumed regions.
    - *Result.* Observed/predicted median 0.9991, p10 0.9898, p90 1.0097,
      98.5 % inside the interval. Acceptance was median within 0.98–1.02,
      p10 ≥ 0.95, p90 ≤ 1.05, n ≥ 200.
    - *Controls.* On the same samples, all four fail the median band:
      - K from the neighbouring word `+0x760`: 0.65;
      - the multiplier from neighbouring byte `0x89D3`: 1.53;
      - the UpRev base K: 1.06;
      - the report taken as airflow: 1.71.
    - *Resolution.* Errors under ~1 % are not resolved: Fuel Comp held at 128
      still gives 1.0014, and a one-word rpm shift gives 1.0128. So this
      verifies the K·Qa/N structure and constants, not Fuel Comp's share.
    - *October 4 fixture.* It cannot be scored: all 194 BFS reads have no
      MAF read within 0.1 s
      (`test_the_october_4_fixture_cannot_be_scored_and_says_why`).
  - **Conditional, visibly.** New output `base_fuel_schedule_word`
    (`0xFFFF103A`, observed as `mem:ECM_AIR_CHARGE_SLOT_103A`, DID 0x1208
    alias) is `conditional`. Its blockers:
    - `air_charge_ram_copy_unverified`: `air_charge_ram_copy`;
    - `airflow_unmodelled`: `airflow_qa`;
    - `fill_byte_not_computed`: `fuel_comp_load_byte`;
    - `fill_dynamics_reference_only`: the five per-run references.

    Regions `not_steady`, `cranking`, `maf_fault` and
    `fill_subtracting_form` are unevaluable, and `fuel_comp_band` is
    conditional. Target φ is unchanged: X stays observed, its blockers are
    the same, and `tests/test_target_phi.py` plus the private-ROM file pass.
  - **Behaviour changes** (E07's doc is left to its owner):
    - *Fourth output.* Every `compare_roms` report and
      `/control/ecu/application-model` (`coverage_report`) now have a fourth
      output, `base_fuel_schedule_word`; model_version is 4.
    - *Fuel Comp edits in `rom_effects`.* The changed node is
      `fuel_comp_lookup`, and the reasons name
      `unmodelled:manifold_fill_filter` (and the other per-run references)
      instead of `unmodelled:base_fuel_schedule`. Tests updated.
    - *`load_rom` coverage* on ZS44A-20260920 slot 1: consumed 1,185 → 1,347
      and unknown 52,741 → 52,579 bytes. The fill time-constant maps and
      axes, the Tp constants and the new calibration conditions are now
      classed as read.
    - *More refusals.* An image that differs at any newly pinned calibration
      byte refuses `unmodelled_logic`.
  - **Remaining (vehicle):** *(the capture for (b)–(d) is E09.1's `twin`
    profile; the time-stepped implementation of (b) is E09.3.)*
    - **(a) RAM copies.** Read `0xFFFFB6A0`–`0xFFFFB7A3` (260 bytes, five
      0x23 windows) once at key-on and compare with the resolved slot. That
      closes `air_charge_ram_copy_unverified`.
    - **(b) Dynamic chain and the intermediates.** Capture, every round with
      DID 0x1209 and rpm, through throttle steps and steady holds:
      - 0x23 windows `0xFFFF1038`–`0xFFFF105D` (BFS, X, `w1054`, `w1056`,
        `w105A`, `w105C`);
      - `0xFFFF2100`–`0xFFFF2117` (airflow, Tp);
      - `0xFFFF20A4`–`0xFFFF20C7` (filter, blend, previous values, holds);
      - `0xFFFF20EC`–`0xFFFF20ED` (residue);
      - `0xFFFF2254`–`0xFFFF225A` (weight, Fuel Comp);
      - `0xFFFF1164`–`0xFFFF118A` (coefficient, weight, load byte);
      - flags `0xFFFF283A`–`0xFFFF283C` and `0xFFFF2899`–`0xFFFF289C`.

      This scores `air_charge_tp`, `fuel_comp_lookup` and
      `fuel_comp_product` exactly, scores the per-run references, and
      measures the group periods.
    - **(c) Fuel Comp's contribution.** A drive with
      `mem:ECM_FUEL_COMP_X_INPUT` read in every DID 0x1208/0x1209 round.
      Exact FC per sample; it also closes `fill_byte_not_computed` for
      those samples.
    - **(d) Start branch.** Read `0xFFFF4786`, `0xFFFF2C91`, `0xFFFF4A2D`
      and `0xFFFF283B` with BFS through key-on and crank (`cranking` region).
  - **Not settleable from the listing:**
    - *`0xFFFF289C` bit 5.* The fixed scan finds no store of any form and no
      block copy through the six copy helpers, and RAM is cleared at reset.
      A static listing cannot rule out a computed-pointer write; a live read
      of `0xFFFF289C` can.
    - *Pφ (so the 1.024 ms tick).* It is inferred from the SCI baud rates;
      no clock constant is in the code. Confirm by timing a tick-paced
      counter against acquisition time.
    - *Per-drive state.* The airflow lead path, the hot-wire term and the
      purge controller's learned limit behind `w273E` change per drive; only
      live reads give their values.
    - *Physical units of the fill byte and the throttle area.* They are
      relative in the code; only a measured relation, such as B08 against
      MAF, can give them one.

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
    - **(d) Overrides: desk part done 2026-10-13; qualification needs the
      vehicle, and the gaps below need the listing.**
      - *Dashboard.* The E05.1 panel lists them under "Firmware overrides"
        with regime, qualification, routine, condition → effect and the gap
        count (`EcuApplicationModelPanel.test.tsx`, 7 passed); a daemon older
        than `model_version` 5 omits the section.
      - *Data, not code.* `ecu_application_model.yaml` is `model_version` 5
        and has a new `overrides:` list of 15 entries, each with routine,
        span, nodes, condition, effect, RAM read and written, ROM constants,
        `established_by` (`listing` / `decompile_text` / `inferred`),
        `confidence`, `qualification` (`traced` / `fixture` / `vehicle`),
        `qualified_by`, `consistent_with`, `gaps` and `verify_with`:
        - start: `cranking_tp`;
        - warm-up: `cold_request_direct_enrichment` (coolant below 115 →
          state 5, the lookup word with no ramp) and
          `coolant_correction_26e8`;
        - closed loop: `closed_loop_phi_one` (any cell below the 130 gate
          → φ 1.0, multiplier 0x4000), `closed_loop_curve_term` and
          `closed_loop_feedback_stft` (0x05E368);
        - transient: `enrichment_ramp`, `command_cap_and_slew`,
          `bfs_fill_transient` and `injector_wall_film`;
        - fuel cut: `fuel_cut_zero_command`, `fuel_cut_recovery_slew`,
          `rev_limiter_cut`, `stationary_cut` and `limp_cut`.

        New `uncovered`: `closed_loop_enable`, `fuel_cut_paths` and
        `cold_fuel_comp_table`. Six RAM words were added (STFT B1/B2, the
        measured A/F words, the feedback target and the wall-film term).
      - *How established.* Consolidated from the 2026-10-04/05 listing and
        decompiler passes recorded in the manifest's nodes and
        `docs/ecu-firmware-analysis.md`. The listing (`~/ecu-re`) and the
        private ROMs are not on this machine, so no instruction was re-read.
        Each entry names its pass, and the decompile-only and unlocated
        entries name what they lack. Two have no routine address at all
        (`stationary_cut`, `limp_cut`).
      - *Honest by schema* (`src/analysis/ecu_application.py`, `Override`):
        - a qualification above `traced` needs `qualified_by`;
        - an `inferred` trace cannot qualify;
        - `confidence: observed` goes with qualification and only with it;
        - an unrecorded routine must be named as a gap;
        - the text may cite only the RAM the override reads or writes;
        - an override acting on an output must sit on that output's path,
          and must be held out by one of that output's regions until it
          qualifies on the vehicle. On `target_equiv_ratio` five new regions
          do that (`fuel_cut`, `closed_loop_hold`, `enrichment_ramp`,
          `command_cap_and_slew`, `coolant_correction`) with the existing
          `cold_state_5` and `fuel_cut_recovery`; on
          `base_fuel_schedule_word` the existing `not_steady` and `cranking`
          do it.

        No output's blockers, twin or status moved.
        `coverage_report` (and so `/control/ecu/application-model`) lists
        the overrides, and each output the ids acting on it.
      - *One fixture qualification.* `fuel_cut_zero_command` is `fixture`:
        the 2026-10-04 slice's 18-byte reads from `0xFFFF26AE` carry the cut
        state `0xFFFF26B8` and the command word in one reply. `0x3F` with a
        zero word on 21/21 sentinel reads, `0x00` with a non-zero word on
        153/153 others, the alignment pinned by the decoded channel, and the
        neighbouring bytes do not separate the two
        (`test_the_fuel_cut_state_and_the_zero_word_agree_on_every_october_4_read`).
        It is not `vehicle`: one drive, not held out. The other 14 are
        `traced`.
      - *Consistent, not qualifying* (fixture tests):
        - both STFT words read exactly `0x6400` on 21/21 cut reads, against
          13 and 12 of 153 outside cut;
        - `0xFFFF26B9` reads `0x03` on 173/174;
        - `0xFFFF2841` bit 1 (the 6,200/5,800 limiter pair) is clear on
          191/191.
      - *Tests.* `tests/test_ecu_overrides.py`, 25 passed: schema refusals,
        the report, unchanged blockers, the fixture checks. The existing
        application-model, state-model and target-φ tests pass.
      - *Remaining.*
        - **Desk, with the listing:** the writer of `0xFFFF26B8` and its
          place in the cycle (`fuel_cut_recovery_slew` holds only if the cut
          state is cleared after the fuel task); the stationary and limp-cut
          routines; what the rev limiter writes; the STFT enable; the store
          of `0xFFFF26DC`; the cold Fuel Comp table's readers.
        - **Vehicle:** each `verify_with`. The catalog's `fuel_target`
          window, read with all four members, is 57 bytes from
          `0xFFFF26AE`. It already carries `0xFFFF26B4`–`26B9`,
          `0xFFFF26BE`–`26CF` and `0xFFFF26DC`, so a capture made with it can
          score several overrides (none is in this repo).

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
  - **Remaining (vehicle).** *(E09.7 maps the base timing table, implements
    the seven nodes to the final byte and exposes Δtiming to the editor;
    (b)–(c) ride on E09.1's capture.)*
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
