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
| Timing-trim tables at `+0x020`/`+0x120`, cells masked `& 0x7F`, selected by a flag at `0xFFFF2842`; rpm axis `0xCC93`, BFS-X axis `0xCC83` | Preserve the corrected axis roles. Table locations are verified; Main/High Det meaning and the final timing chain still need evidence. |
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

- [ ] **E05.3 — Reproduce state transitions and additional fuel regimes.**
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

- [ ] **E05.4 — Add ignition and later identified outputs incrementally.**
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
