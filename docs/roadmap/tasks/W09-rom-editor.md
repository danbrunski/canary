# W09 — ROM Editor page: edit fuel tables, run them through the simulator

Requested 2026-10-15 by the operator: "Does the GUI have the ability to edit
Osiris fuel maps? Let's add that. I want to edit fuel maps and fuel targets, so
we can experiment with the engine physics simulator."

The answer on 2026-10-15 was no. The ROM Library page shows every table of an
image read-only (`RomTablesView`), and the **ROM Editor** nav entry is a
`planned` placeholder (`Workbench.tsx`, `tuning-rom-editor`; reserved by W10 in
`docs/roadmap/workbench-workflows.md`). The simulator exists only as a library:
E08.1 edit specs, E08.2 point designs, E08.3 paired Monte Carlo. The CLI and the
report are still open (E08.5, E08.6).

This checklist is [E08](E08-rom-edit-simulation.md)'s item E08.8 ("An Engine
Tuning page that takes an edit spec, runs a bounded job, and shows the
report"), brought forward at the operator's request ahead of E08.5–E08.7. It
was written on a laptop with no ROM images. **Implement and accept it on the
machine that has the `.OsirisROM` files** (`$CANARY_ROM_DIRS`), because W09.6's
acceptance runs on the real images.

## What exists and what this adds

| Exists | Used for |
|---|---|
| `rom_edit_spec.parse_edit_spec` / `build_candidate` (E08.1) | Every GUI edit is an E08.1 `Edit`; validation, refusals by name and provenance all come from there |
| `rom_image.table_values` / `tables_payload` | The before and after tables, in the payload shape `RomTablesView` already renders |
| `sim_design.build_grid_design` / `summary` (E08.2) | The operating points: a stated rpm × load grid |
| `sim_montecarlo.run_monte_carlo` / `MonteCarloDesign.from_sim_design` / `load_uncertainty` (E08.3) | Base vs candidate, paired draws |
| `sim_cells.aggregate_cells` / `fuel_comp_axes` / `target_afr_axes` (E08.4, `474c0267`) | Per-cell statuses and quantiles (calling notes in W09.0) |
| `control.workbench_jobs.run_off_loop` / `capture_deferral` | The bounded, single-flight spawn child, refused while a capture records |
| `control.rom_library.RomLibrary` (`app.state.rom_library`) | Base images by sha256, and `revision_match` |

## Decisions made here (operator request 2026-10-15; do not re-open)

- **Editable tables: the fuel chain, Map 1.** `fuel_targets`,
  `fuel_compensation` and `k_constant`, slot 1 only. The simulator runs slot 1
  with `slot_source: default_slot`, the slot the ECM runs after a cold boot
  (`rom_map.yaml` `map_slots`). An edit to slots 2–5 would show `not_reached`
  everywhere, so the page does not offer it. Anything else is refused
  server-side (`table_not_editable`, `slot_not_simulated`). Timing trims and
  the other slots are a later item, not this one.
- **A GUI edit is an E08.1 edit, never a second schema.** The request body's
  `edits` are `rom_edit_spec.Edit` dicts (`cells` / `all` / `region` with
  `set_raw` / `add_raw` / `scale`+`rounding`), wrapped in an `EditSpec` by the
  server. Duplicate-cell, no-op, out-of-range and masked-bit refusals are
  E08.1's and keep its reason names. "Set to an engineering value" (AFR 12.5,
  103 %) is inverted to a raw value in the browser and sent as `set_raw`. The
  page shows the chain (`12.5 AFR → raw 151 → 12.46 AFR`) so rounding is
  visible.
- **The candidate never leaves the simulator.** It is not added to the ROM
  Library, and its bytes are never offered for download. It is
  `flashable: false`, with stale checksums on purpose (E08.1). What the page
  offers is the **edit spec YAML**: reviewable, hashable, and runnable by
  E08.5's CLI later. A flash stays the Flash page's explicit per-operation act
  (W07, AGENTS.md).
- **The simulator needs a recorded base.** A grid design names
  `base_revision` (`tune_revisions.yaml`). Simulation is offered only when the
  library entry's `revision_match.status == "matched"`. Otherwise the page
  still previews edits, and the Simulate control is disabled with the reason
  (`base_not_a_recorded_revision`). A `reference` image (a tuner's base, never
  flashed) is not a revision.
- **Nothing is defaulted.** Environment (baro, IAT, coolant, battery, rail or
  `working_assumption`, throttle, pedal flag), learned state (STFT, LTFT,
  throttle-closed learned, idle-air learned, purge area), candidate events
  (`ecu_reflash` or none), `load_basis`, draws and seed are all required in the
  request (pydantic, `extra="forbid"`). This follows E08.2 (`environment_unstated`:
  home is ~80 kPa, trips reach 63 kPa) and E08.3 (draws and seed never
  defaulted). The browser may remember the operator's last stated values in
  `localStorage` (wrapped in try/catch). They are shown in the form, never
  applied silently.
- **The grid defaults to Fuel Comp's own breakpoints, made visible.** The
  rpm and load edges the form pre-fills are the midpoints between
  `fuel_comp_engine_speed_axis` / `fuel_comp_flow_axis` breakpoints on the base
  image, extended half a step at each end. That puts each grid point on a
  breakpoint. Those axes carry UpRev's swapped labels (`rom_map.yaml`, E08.1),
  so read them by decode quantity, never "rows". The edges are editable and
  are sent explicitly. The server applies no default.
- **Refusals cross the process boundary as data.** `SimulationRefusalError`,
  `EditSpecRefusalError` (a `RomEffectRefusalError`) and
  `SimDesignRefusalError` take `(reason, message)` and **do not survive
  pickling**. Checked 2026-10-15: `pickle.loads(pickle.dumps(e))` raises
  `TypeError: __init__() missing 1 required positional argument: 'message'`
  for all three. A refusal raised in the spawn child would reach the route as
  an opaque failure. So the child entry point catches them and returns
  `{"refused": {"reason", "message", "kind"}}`, and the route answers 400 with
  it. Don't "fix" this by changing the exception classes. The E08 suites
  assert on them as they are.
- **Bounded on the Pi.** Preview is cheap: a 1 MiB image, `compare_roms` and
  `table_values`, the same order of work as `GET …/tables`. It runs in the
  thread pool and needs no child. Simulate runs in the workbench child as job
  kind `rom_sim`. It is refused while a capture records
  (`capture_deferral`), and busy while another child runs. The grid is capped
  at `MAX_GRID_POINTS = 1024` (`grid_too_large`, checked before building) and
  draws at `MAX_DRAWS = 2000` (`draws_too_many`). W09.6 measures the real cost
  on the Pi and records it.
- **The results render as a DOM table, not the canvas `Heatmap`.** Every
  non-`changed`/`unchanged` status is a text label or hatch, never a colour
  that reads as a value (E08.6). Edited cells are outlined. Tests read the
  labels.
- **Caveats come first.** Before any grid: plant not qualified (and why),
  `structural_error_not_sampled`, parameter uncertainty only, every
  `assumptions` string, the two image identities, the edit list, the design
  coverage, draws and seed. E08.7's known answers are not reproduced yet
  (2026-10-15), so the page also says that until E08.7 is ticked. W09.6 is
  where they are first run through this path.

## File ownership (for parallel implementation)

| Item | New files | May edit |
|---|---|---|
| W09.0 | (E08.4's) `src/analysis/sim_cells.py`, `tests/test_sim_cells.py` | E08 doc, E08.4 entry |
| W09.1 | `src/analysis/rom_editor.py`, `tests/test_rom_editor.py` | — |
| W09.2 | `src/dashboard/routes/rom_editor.py`, `tests/test_rom_editor_api.py` | `src/control/workbench_jobs.py` (one kind), `src/dashboard/api.py` (one registration), `docs/headless-api.md` |
| W09.3 | `src/dashboard/static/src/panels/tuning/RomEditorPage.tsx`, `…/tuning/romEditor/*.tsx`, `src/dashboard/static/src/lib/romEditor.ts`, `src/dashboard/static/src/types/romEditor.ts`, tests beside each | `Workbench.tsx` (replace the placeholder), `nav.ts` (drop `planned`), `api/workbench.ts` (two functions) |
| W09.4 | `…/tuning/romEditor/SimForm.tsx`, `…/romEditor/SimResults.tsx`, tests | `RomEditorPage.tsx` |
| W09.5 | — | `src/mcp_server/…`, `docs/mcp-server.md` |
| W09.6 | `tests/test_rom_editor_private_rom.py`, `src/dashboard/static/e2e/romEditor.spec.ts` | this doc |

`src/analysis/rom_editor.py` is layer 5 (`tests/test_layering.py`). Like
every E08 module, it imports no `src.control`, daemon, dashboard, MCP, `can` or
`serial`. Assert that in a clean process.

Suggested dispatch tiers (CLAUDE.md): W09.1 Deep (scenario and learned-state
construction feeds the simulator), W09.2 Heavy (daemon + child + routes),
W09.3 and W09.4 Standard, each split if over ~8 code items, W09.5 Standard.
W09.6 is the orchestrator's own, on the ROM machine.

## Checklist

- [ ] **W09.0 — Prerequisite: E08.4 per-cell aggregation.** Landed as
  `474c0267` (`src/analysis/sim_cells.py`, 28 tests, real 20260920 → 20261004
  run). E08.4's box in the E08 doc was still unticked when this was written:
  tick it with that commit's evidence if no one has. W09 builds on that
  module as it stands. A second, independent implementation made on the
  laptop the same day was dropped in its favour, and no W09 item depends on
  it. Calling it:
  - **Axes:** `fuel_comp_axes(kit, base_image, slot=1)` (the default; grid
    cell (r, c) *is* table cell (r, c) by nearest-breakpoint membership in
    the lookup's own comparison domain) or `target_afr_axes(kit, base_image,
    slot=1)`. `sim_cells.Grid` is `Literal["fuel_comp", "target_afr"]`.
  - **Aggregate:** `aggregate_cells(result, sim_design, axes,
    effects=compare_roms(base, candidate, rom_map, kit.model, kit.model_sha256,
    base_slot=1, candidate_slot=1, slot_source="default_slot"))`. It takes the
    E08.2 `SimDesign` (not the `MonteCarloDesign`), and the effect trace must
    be the run's (`effects_mismatch` otherwise). E08.1's `build_candidate`
    already computes that trace, so reuse it rather than computing it twice.
  - **Payload:** `CellTable.to_dict()` (`schema_version: 1`; JSON Schema from
    `cell_table_schema()`, which `types/romEditor.ts` should mirror). Every
    cell is present, row-major. A cell has `row_value` / `col_value`, `n_points`,
    `pairs`, `weight`, `regime_shares`, `loop_shares`, `outputs {<name>:
    {status, pairs, supported_share, unsupported_share, support[],
    quantiles {over, pairs, weight, delta {p05, p50, p95}, relative_delta,
    relative_undefined_pairs} | null, changed_share, supported_effect}}`,
    `by_loop[]`, `edited` (raw old → new, or null), `row_axis_moved` and
    `col_axis_moved`. Top level: `rows` / `cols` axis info, `reach`,
    `caveats`, `unresolved`, `status_counts`, `overlay`, `unplaced`, the
    hashes and `headline_by_loop` (`open_loop` → `delivered_lambda`,
    `closed_loop` → `fuel_trim_total_pct`, setpoint moved → `target_phi`).
  - Measure the payload size of a 16 × 16 run in W09.6. If it is large,
    trim on the route (for example, drop `points` lists), never by dropping
    cells: `no_points` ≠ `unchanged`.

- [x] **W09.1 — `src/analysis/rom_editor.py`: preview and simulate as pure functions.**
  - `EDITABLE_TABLES = ("fuel_targets", "fuel_compensation", "k_constant")`,
    `SIM_SLOT = 1`, `MAX_GRID_POINTS`, `MAX_DRAWS`.
  - `library_base(image, revision_match) -> BaseImage`. A `matched` entry gives
    identity `source: revision` (so the spec's `base` is `{revision: R}` and
    the downloaded YAML resolves through the registry on any machine).
    Anything else gives `source: file`, `file: <sha>.bin`, `sha256: <sha>`.
    Check the bytes against the sha.
  - `editor_spec(base, label, edits) -> EditSpec` through `parse_edit_spec`.
    Refuse `table_not_editable` and `slot_not_simulated` before resolution,
    and keep every E08.1 reason unchanged.
  - `preview_edits(image, revision_match, label, edits, profile_dir) -> dict`:
    `build_candidate(..., slot=1, slot_source="default_slot")` with the
    profile's application model. Return `spec` (JSON), `spec_yaml`,
    `spec_sha256`, `candidate_sha256`, the E08.1 `provenance` whole (cells
    old → new, ranges, `effect.changed_nodes`, `moved_lookups`), `tables`
    (`table_values(candidate)` for the edited `(id, slot)` pairs only), and
    `sim_base {ok, revision, reason}`.
  - `SimRequest` (pydantic, `extra="forbid"`, every field required as in
    "Decisions"). `grid: sim_cells.Grid` (`fuel_comp` | `target_afr`) picks
    the aggregation axes (W09.0). `load_basis` is `sim_design.LoadBasis`.
  - `simulate_edits(image, revision_match, request, profile_dir) -> dict`:
    1. refuse a non-matched base (`base_not_a_recorded_revision`);
    2. refuse `grid_too_large` / `draws_too_many` before any work;
    3. `build_grid_design(rpm_edges, load_edges, base_revision=R, …)`, then
       `MonteCarloDesign.from_sim_design`;
    4. `build_candidate` as in preview;
    5. a template `PairedScenario(mode=sim_montecarlo.MODE, base=/candidate=
       EcuIdentity(program_id=kit.model.program.id, image_sha256=…, slot=1,
       slot_source="default_slot"), plant=PlantIdentity(version,
       hardware_configuration), points=(), learned=LearnedState(**stated,
       source="stated: ROM Editor"), candidate_events=…)`. The program id is
       `nissan-1ZS44A-uprev-osiris` on the Xterra, not the `ZS44A` the
       synthetic tests use;
    6. `run_monte_carlo(kit, template, design, base, candidate,
       load_uncertainty(profile_dir / UNCERTAINTY_FILE), draws=, seed=)`;
    7. `aggregate_cells(result, grid_design, fuel_comp_axes(kit, base.image,
       slot=1)` (or `target_afr_axes`), `effects=<the candidate's trace>)
       .to_dict()` (W09.0).

    Return `caveats` (result's, unchanged and first), `assumptions`,
    `unresolved`, `inert_parameters`, `coverage`, `outcome`, `provenance`
    (the result's: hashes, design, uncertainty, plant, seed, draws),
    `design` (`summary(grid)` plus its `GridSpec`), `candidate` (E08.1
    provenance), `cells`, `spec_yaml`.
  - `run_job(kind, params) -> dict`: the child-side entry for W09.2. It loads
    the image from `params["image_path"]`, checks `params["sha256"]`, calls
    the function and catches the four refusal classes into
    `{"refused": {"kind", "reason", "message"}}` (see "Decisions": they do not
    pickle).
  - **Accept** (`tests/test_rom_editor.py`, synthetic image from
    `tests/test_paired_scenarios` / `tests/rom_fixtures`, as
    `test_sim_montecarlo.py` builds it, with its registry entry stated in the
    test):
    - Preview of `fuel_compensation` region `scale 1.02 nearest` returns the
      same cells, sha and `spec_sha256` as calling `build_candidate` on the
      equivalent YAML spec (the route adds nothing of its own).
    - `table_not_editable` (`timing_trim_a`), `slot_not_simulated`
      (`fuel_targets_maps_2_5`), `duplicate_edit`, `no_op_edit` and
      `value_out_of_range` come back by name.
    - The downloaded `spec_yaml` round-trips: `load_edit_spec` → same
      `sha256()`.
    - Simulate: the negative control gives every visited cell `unchanged` at
      every draw. E08.1 refuses a no-op spec, so `simulate_edits` builds the
      candidate and then calls a private `_simulate(base, candidate_image,
      candidate_provenance, request, …)`. The test calls that with the base
      bytes as the candidate. `k_constant` `scale 1.05 nearest` gives ΔBFS
      relative p50 = the realised raw ratio − 1 in every supported open-loop
      cell, per pair (E07.6's known answer).
    - Each `SimRequest` field missing is refused by name (pydantic), and no
      field has a default (introspect `model_fields`).
    - `run_job` returns `refused` for each refusal class, and the returned
      dict pickles.
    - Clean-process import test: no ECU-facing package loaded.
  - *Done 2026-10-15, synthetic image only* (no `.OsirisROM` on this machine;
    the real-image runs are W09.6). `tests/test_rom_editor.py`, 24 tests
    green (`pytest tests/test_rom_editor.py tests/test_layering.py`: 35
    passed): `test_a_preview_is_build_candidate_on_the_equivalent_yaml_spec`
    (whole provenance equal, 20 cells 128 → 131), `test_edit_refusals_come_back_by_name`
    (the five named plus `k_constant` slot 3, `rounding_undeclared`,
    `no_edits`), `test_the_downloaded_spec_yaml_round_trips`,
    `test_the_negative_control_is_unchanged_in_every_visited_cell` (default
    4,000-5,600 rpm x 30-100 % grid, 6 visited cells: all 6 outputs exactly
    `unchanged` with supported share 1.0 and zero deltas in the 5 open-loop
    cells; in the one closed-loop cell (10, 6) BFS and target phi are
    `unchanged` and the 4 plant outputs are `unsupported`, by
    `closed_loop_trim_authority_undecoded` only; and, from the run itself,
    every computed pair is equal at every draw), `test_a_k_edit_moves_bfs_by_the_realised_raw_ratio_in_every_open_loop_pair`
    (ratio 30151/28715; each open-loop pair within one BFS step (4 counts) of
    r × base, cell p05/p50/p95 = r − 1 ± 5e-4 in all 5 open-loop cells; the
    sixth point runs closed loop; caveats lead the payload, after only `tool`,
    `tool_version`, `label`, `synthetic`), `test_no_request_field_has_a_default_and_each_missing_one_is_named`,
    `test_bounds_and_the_base_are_refused_before_any_work`,
    `test_run_job_returns_each_refusal_class_as_data_that_pickles` (all four
    classes plus `request_invalid`), `test_the_rom_editor_imports_nothing_that_talks_to_the_ecu`.
    Contract additions W09.2 must know: keyword seams `sha256=`, `kit=`,
    `registry=` on `preview_edits` / `simulate_edits` / `run_job`; `run_job`
    params also need `profile_dir`; kinds `rom_sim` and `rom_preview`;
    `candidate_events` is `"none" | "ecu_reflash"`; the route must catch
    `RomEffectRefusalError` (E08.1's `value_out_of_range` comes from
    `make_candidate` as that base class, not `EditSpecRefusalError`).
    `effects` are recomputed with `compare_roms(..., contract=kit.contract,
    mode=MODE)` instead of reusing `build_candidate`'s trace: `Candidate`
    exposes no `EffectReport`, and that trace runs without contract and mode,
    so its output statuses would not be the run's `rom_report`
    (`effects_mismatch`).

- [x] **W09.2 — Job kind and routes.**
  - `workbench_jobs`: add `"rom_sim"` to `JOB_KINDS` and `_CONFIG_ONLY_KINDS`.
    `run_workbench_job` dispatches it to `rom_editor.run_job` before the
    bindings check. Params carry `image_path`, `sha256`, `revision_match` and
    the request as JSON (`_job_key` hashes them, so an identical click
    in flight is shared).
  - `src/dashboard/routes/rom_editor.py`, `register_rom_editor_routes(app, *,
    config, resolved_control_token)`, using `app.state.rom_library` (register
    after the library in `api.py`). Control token on both:
    - `POST /control/rom/library/{sha}/edits/preview`, body `{label, edits}`,
      in the thread pool. 404 for an unknown sha, 400
      `{error: "edit_refused", reason, message}`, 409 when the vehicle has no
      map (as `GET …/tables`).
    - `POST /control/rom/library/{sha}/simulate`, body `SimRequest`, through
      `capture_deferral` then `run_off_loop("rom_sim", …)`. 409
      `analysis_deferred` / `analysis_busy` pass through as the workbench
      routes do. A `refused` result is 400 `{error: "sim_refused", reason,
      message}`. 422 is pydantic's.
  - `docs/headless-api.md`: both routes, bodies, every error shape.
  - **Accept** (`tests/test_rom_editor_api.py`, `TestClient` with a library
    in `tmp_path`, as `tests/test_rom_library_api.py` builds it):
    - Preview 200 on the synthetic image, then 400 with each refusal reason.
    - Simulate runs the real child via `run_off_loop` with an in-process
      executor (`executor=` seam), returns cells, and answers 409
      `analysis_deferred` while `tuning_profile` is active.
    - A refusal raised inside the child is a 400 with its reason, not a 503
      (this is the pickling trap, tested end to end).
    - `tests/test_layering.py` and `tests/test_state_dir.py` stay green (no
      runtime writes: nothing is persisted in this checklist).
  - *Done 2026-10-15, synthetic image only* (real-image runs are W09.6).
    `tests/test_rom_editor_api.py`, 42 tests green after the verifier's fixes (`pytest` over it,
    `test_rom_editor`, `test_route_table_snapshot`, `test_layering`,
    `test_state_dir`, `test_workbench_analysis`, `test_rom_library_api`,
    `test_dyno_workbench`, `test_control_classification`, `test_rom_trace`,
    `test_dashboard_api`, `test_pairing_routes`, `test_brain_routes`,
    `test_mcp_auth`: 281 passed). The fixture copies the Xterra profile into
    `tmp_path`, patches the application model's program hash to the synthetic
    image's and records the image in that copy's `tune_revisions.yaml`; the
    library is the real `register_rom_library_routes` and the image goes in
    through its upload route. Preview: `test_preview_is_the_pure_function_and_adds_nothing_of_its_own`
    (response equals `preview_edits`, 20 cells 128 → 131),
    `test_preview_refusals_are_a_400_with_their_reason` (`table_not_editable`,
    `slot_not_simulated`, `no_op_edit`, `rounding_undeclared`, and
    `value_out_of_range` as `kind: rom_effect`: E08.1's range check is the
    base class), `duplicate_edit` / `no_edits`, another program
    (`wrong_program`/`patch_missing`, not a 500), 404 (unknown, malformed,
    traversal sha), 409 with no map (also for simulate; 404 wins over 409),
    422. Simulate: `test_simulate_runs_the_child_and_returns_the_cells`
    (in-process executor via the `executor=` seam; response equals `run_job`;
    all 256 Fuel Comp cells present, 6 visited), 409 `analysis_deferred`
    while `tuning_profile` is active (no child submitted; preview still 200),
    409 `analysis_busy`, 503 `child_timed_out`, 400 `sim_refused` for
    `table_not_editable`, `value_out_of_range`, `grid_edges_invalid`,
    `draws_too_many`, `grid_too_large` and `base_not_a_recorded_revision`,
    422 for each of the 21 request fields missing, and for an extra or mistyped field.
    The pickling trap end to end: `test_the_refusal_trap_end_to_end_through_a_real_spawn_child`
    runs a **real `spawn` child** (three refusal kinds, each a 400 with its
    reason); checked that it bites by letting the child call `simulate_edits`
    directly, which answered 503 `child_failed` (`BrokenProcessPool`).
    `test_a_whole_simulate_crosses_the_real_spawn_boundary` (200 from a real
    child). `test_nothing_is_persisted_and_the_candidate_is_never_offered`
    (`tmp_path` tree and `repo_guard` snapshot unchanged, the library still
    holds the one base image, download returns it). Both routes 401 without
    or with a wrong token (the dependency wins over field validation, but
    FastAPI parses the body first, so a body that is not JSON is a 422 even
    without a token: `test_both_routes_require_the_token` pins both).
    Registering the editor without the library raises.
    Found and fixed on the way: the library matches an image against
    `active_registry(config)` (tracked + dashboard-recorded flashes) but
    `build_grid_design(registry=None)` reads only the tracked file, so a base
    the page offers to simulate could be refused `base_revision_unknown` in the
    child. `run_workbench_job` now passes `registry=active_registry(config)`;
    `test_a_revision_recorded_from_the_dashboard_is_simulated_as_the_library_sees_it`
    failed before and passes after.
    Verifier fixes (2026-10-15): (1) that eager `active_registry(config)` in the
    child raised `RegistryConflictError` (does not pickle) for an unreadable or
    conflicting operational registry: 503 `child_failed`. Fixed at two layers.
    The route refuses a base that is not `matched` **before** the capture
    deferral and before any child (400 `sim_refused`, `kind: simulation`,
    `reason: base_not_a_recorded_revision`, message carrying the library's
    reason; so the answer does not depend on whether a capture records), and
    the child (`workbench_jobs._run_rom_sim`) reads the history only for a
    matched base and returns an unreadable one as data
    (`flash_history_unreadable`). Tests:
    `test_a_conflicting_flash_history_is_a_400_and_spawns_nothing` (real-spawn
    harness, conflicting operational `tune_revisions.yaml`: 400, no dispatch)
    and `test_a_flash_history_that_breaks_after_the_check_is_a_400_from_the_real_child`
    (history changed between the route's match and the child's read: 400
    `flash_history_unreadable` from a real child). Checked by restoring the
    committed sources: the first answers 503 `child_failed` (BrokenProcessPool)
    at 1b72db60, and the second does when only the child fix is reverted.
    (2) The preview now runs `rom_editor.run_job("rom_preview")` in the thread
    pool, the one converter the child uses, so every class in
    `rom_editor.REFUSALS` is a 400 `edit_refused` (`test_preview_answers_every_refusal_class_as_a_400_not_a_500`,
    one case per class; five of the seven were a 500 before) and a
    `FileNotFoundError` is 409; `test_a_missing_vehicle_definition_is_a_409_on_both_routes`
    (no `ecu_application_model.yaml`; simulate raises it in a real child).
    (3) `docs/headless-api.md` lists every `kind` and says the `reason` list is
    open-ended.
    Decisions the doc did not settle: no separate `rom_preview` job kind
    (preview is the thread pool); 400 bodies also carry `kind`; a deferred
    simulate keeps the workbench's detail but its `laptop_path` says to
    download the image (the workbench's text tar-streams a session this job
    never reads); a `FileNotFoundError` for any other vehicle definition
    (contract, application model, uncertainty) is 409 `"ROM Editor definition
    unavailable for this vehicle: ..."`; `tests/fixtures/route_table_snapshot.json`
    regenerated (two rows). A 16 × 16 table with six visited points is
    ~370 KB of JSON: W09.6 measures a full grid, and trims on the route if
    needed (never by dropping cells).

- [x] **W09.3 — ROM Editor page: choose, edit, preview, download the spec.**
  - Replace the placeholder in `Workbench.tsx` and drop `planned` from
    `nav.ts`. Props `{token, status}` as `RomLibraryPage`.
  - **Base:** a library entry picker (`getRomLibrary`), showing each entry's
    label, `revision_match` and whether it can be simulated. The URL keeps the
    chosen sha, as the library page does.
  - **Table:** Fuel Targets, Fuel Compensation, K (Map 1). The grid shows
    decoded values with raw on hover (reuse `ValuesGrid`'s look; the Fuel Comp
    axis note from `editor_disagreement` is shown). Select cells by click,
    shift-click range and drag. Keyboard: arrows move, shift+arrows extend.
  - **Operation:** set to value (engineering units, inverted per the table's
    `decode`: linear `(v − offset) / scale`, reciprocal `numerator / v`,
    rounded nearest; a raw-only table such as K offers raw), add raw, or
    scale % with rounding chosen, with no default rounding (E08.1). Each
    applied operation becomes one pending `Edit` on the exact `cells`
    selected.
  - **Pending edits:** a list (remove any; label field), then **Preview**,
    which calls the route. Show the after table with changed cells outlined
    and Δ% per cell, the cell list old → new (raw and decoded), the reached
    outputs (`effect.changed_nodes`) and any refusal by reason. Pending edits
    survive a reload (`localStorage`, per sha, try/catch).
  - **Download spec:** the preview's `spec_yaml` as
    `<label>.edit-spec.yaml`. There is no candidate-image download anywhere.
  - `lib/romEditor.ts`: `rawForValue(decode, value)`, `buildEdits(selection,
    op)` and `selectionRange`, pure and unit-tested. That includes the
    reciprocal Fuel Targets case (12.5 AFR → 151 → 12.46) and a value whose
    raw falls outside u8, which is refused in the browser before sending.
  - **Accept:** vitest for the lib and the page (fixtures in
    `test/romLibraryFixtures.ts`; preview mocked): select → scale 2 % nearest
    → preview shows the after values; a refusal reason is rendered; no
    download of image bytes exists (assert no `downloadRomImage` call or
    candidate link). `pnpm typecheck`, `pnpm lint`, `pnpm format` clean.
  - *Done 2026-10-15, mocked API only* (the W09.2 route was being written in
    parallel; its real response shape is unreconciled). Files: `lib/romEditor.ts`,
    `types/romEditor.ts`, `panels/tuning/RomEditorPage.tsx`,
    `panels/tuning/romEditor/{BasePicker,CellGrid,OperationPanel,PendingEdits,PreviewResult}.tsx`,
    `api/workbench.ts` (`previewRomEdits` only), `Workbench.tsx`, `nav.ts`.
    `lib/romEditor.test.ts` 25 tests (12.5 AFR → raw 151 → 12.46; 5 AFR and
    0.1 AFR refused as `value_out_of_range` before sending; no default
    rounding; float-noise case 1.1 × 100 = 110; be16 K range; Map 2 refused
    `slot_not_simulated`; `previewRefusal`; localStorage round trip, corrupt
    and blocked storage). `RomEditorPage.test.tsx` 15 tests (select by
    click/shift-click/drag/arrows; scale 2 % nearest → POST body has the exact
    `cells`, `scale: 1.02`, `rounding: "nearest"` → after table shows 95.31
    with `+1.7 %` and outline; a server refusal renders by reason; reload
    keeps pending edits per sha; no `controlBlob` call, no link, the only
    download button is "Download spec"; the Simulate slot is empty for a
    matched base and names `base_not_a_recorded_revision` otherwise).
    `pnpm typecheck`, `pnpm lint` (no suppression changes), `pnpm format:check`
    clean; 24 related vitest files / 207 tests green.
    Contract notes: (1) `GET …/tables` now sends each decode's `scale`/`offset`/
    `numerator`/`mask` (`rom_image._decode_payload`), and the page tests use
    fixtures with them; `lib/romEditor.decodeParams` still reads the formula
    when they are absent (one unit test keeps that path). (2) The library page
    does not keep the sha in the URL (the hash router rewrites anything after
    `#wb/<page>`), so the editor keeps it in `?rom=<sha>`. (3) The 400 body is
    read as `{reason, message}` at the top level or under `detail`; a page test
    feeds it the daemon's real `{detail: {error, kind, reason, message}}`.
    (4) `emptyDaemon.spec.ts` visits this page now that it is not `planned`; the
    verifier ran it and it passed. `pnpm e2e` otherwise stays with W09.6
    (`romEditor.spec.ts`).
    *Click bug, found by the W09.4 agent in Chromium, fixed 2026-10-15:* with
    the table partly below the fold one click on a cell selected 7–11 cells
    (`CellGrid` focused the grid on mousedown, which scrolled the page under the
    held button, and `mouseenter` then extended while the `dragging` flag was
    stale). Reproduced with a scratch Playwright spec on a `vite build` +
    `vite preview` of the page (mocked daemon): 1280×600 selected 7 cells
    (rows 2–8) before the fix in 3 of 3 runs, and exactly 1 after, as at 1280×2400
    (1 before and after). Fix: `focus({ preventScroll: true })`, and a drag
    extends only while `event.buttons & 1`, a zero ending the drag. Test:
    "never extends a drag unless the primary button is down, and focuses without
    scrolling" fails with either half of the fix removed.

- [x] **W09.4 — Simulate form and results.**
  - **Form** (disabled with the reason unless the base is a matched revision,
    or while preview has a refusal): grid choice (Fuel Comp axes / Fuel
    Targets axes for aggregation), rpm and load edges (pre-filled from the
    base image's Fuel Comp breakpoints as in "Decisions", editable, showing the
    point count against `MAX_GRID_POINTS`), the stated environment, learned
    state, candidate events (a required choice between "same learned state
    on both" and "fresh flash resets trims"), `load_basis` (shown as its one
    value with its meaning), draws, and seed (with a "new seed" button). Empty
    fields block Run and name what is missing. Last values come back from
    `localStorage`, visibly.
  - **Running:** one request, with a spinner and elapsed time. Show
    `analysis_deferred` (a capture is recording: "pull the images and run on
    the laptop"), `analysis_busy` and `sim_refused` by reason.
  - **Results:** the caveat block first (see "Decisions"), then grids for
    relative Δλ p50 (diverging, centred at 0), p05–p95 width, ΔBFS and
    Δpulse. Each cell's main figure follows `headline_by_loop` and the cell's
    `loop_shares` (closed loop shows `fuel_trim_total_pct`, per E08.4), and
    `by_loop` is in the cell detail. Each
    status is a label or hatch. Edited cells are outlined, and
    `moved_lookup` cells are marked. Clicking a cell shows n points, pairs,
    weight, quantiles, regime mix, support share and reasons. Design summary
    and provenance (hashes, seed, draws) sit below. **Download result JSON**
    for the laptop.
  - **Accept:** vitest with a hand-built `cells` payload covering every
    status. Each status label renders, and the caveat block precedes the
    first grid in DOM order. A missing environment field blocks Run and is
    named. 409 deferred and 400 refused render their reasons.
  - *Done 2026-10-15, mocked API only* (the real-daemon and real-image walk
    is W09.6). Files: `panels/tuning/romEditor/{SimForm,SimResults,SimCaveats,SimMaps}.tsx`,
    `lib/romSim.ts` (form values as text, `checkSimForm`, edge suggestion,
    `localStorage`, `simFailure`), `lib/romSimView.ts` (a port of
    `sim_report.py`'s marks, picks and hatches), `types/romEditor.ts` (the
    request, `CellTable` and result), `api/workbench.ts` (`simulateRomEdits`
    only), `RomEditorPage.tsx`, fixtures `test/romSimFixtures.ts` and
    `test/romSimDoc.ts`. Tests: `lib/romSim.test.ts` 29, `lib/romSimView.test.ts`
    23, `romEditor/SimResults.test.tsx` 20, `RomEditorSimulate.test.tsx` 23
    (page level, the W09.3 "Simulate slot" test rewritten for the form):
    112 tests green in those four plus `RomEditorPage.test.tsx` (after the
    verifier's follow-ups, below; 140 across them, `romEditor.test.ts`,
    `themeClasses` and `layering` before); `pnpm typecheck`, `pnpm lint` (no suppression
    change), `pnpm format:check` clean; `vitest related` over every touched
    source: 24 files, 22 green first time, the other two (my page test's 5 s
    timeouts under load, since given a 30 s budget, and the W09.3 flake below)
    green on re-run.
    - **The 21 fields:** the Run request's keys equal the keys of the JSON
      example in `docs/headless-api.md` (read from the doc by
      `test/romSimDoc.ts`, in both `romSim.test.ts` and the page test) and the
      body is asserted value for value.
    - **Every status:** the hand-built payload (`romSimFixtures.ts`, a 3 x 4
      grid) has all six `sim_cells.STATUSES` in the open-loop lambda map plus
      other-loop and relative-undefined cells; each renders its label (`-1.52`,
      `0`, `U`, `P+0.40`, `–`, `NR`, `CL`, `∅`) and a `data-status`; the
      TS `STATUSES` is asserted equal to the list parsed out of
      `src/analysis/sim_cells.py`; only `changed`/`unchanged` cells have a
      background colour, every other mark a gradient hatch. The payload
      validates against `sim_cells.CellTable` and renders through
      `sim_report.render_report` (checked once, scratch).
    - **Caveats first:** `compareDocumentPosition` puts the caveat region
      before the first map, and nothing table-like precedes it; it prints the
      plant qualification and why, `structural_error_not_sampled`,
      `parameter_uncertainty_only`, every other caveat, every `assumptions`
      string (result and design), unresolved, both image shas, the edit list,
      coverage, draws, seed, candidate events, and the E08.7 line. E08.7 is `[~]`
      in the E08 doc (2026-10-15): items 1-4 pass in the suite through
      `tools/rom_sim.py` (`37fde9ba`), the post-flash truck scoring is owed.
      The page says exactly that: known answers 1-4 are reproduced in the
      test suite through the CLI path, have not been run through this page
      on real images yet (W09.6 first), and item 5 is still owed.
      `KNOWN_ANSWERS_REPRODUCED` in `lib/romSim.ts` is the one switch to flip
      when E08.7 is fully ticked.
    - **Form:** nothing defaulted (no radio checked, every field empty); one
      missing field (baro) disables Run and is named, `fireEvent.submit` sends
      nothing; edges suggested from Fuel Comp's breakpoints by decode quantity
      (identical with rows and columns swapped), 16 x 16 = 256 of at most
      1,024 shown, a load edge past 100 % (the model span) blocks Run until
      the operator presses "Trim load edges to 0-100 %"; 33 x 33 refused as
      `grid_too_large`; remembered values come back marked, lose the mark when
      edited, can be cleared, and storage that throws does nothing.
    - **Errors by reason:** 409 `analysis_deferred` (capture message, "Pull the
      images and run the edit spec on the laptop", the server's
      `laptop_path`), 409 `analysis_busy`, 400 `sim_refused` (reason and
      class), 503 `analysis_unavailable`, 422 by field.
    - **Browser:** the page was driven once in headless Chromium against a
      stubbed daemon (scratch spec, not committed; `romEditor.spec.ts` is
      W09.6's): form, run, caveats, three lambda maps with hatches, legend,
      outlined edited cells and the moved-breakpoint marks all render.
    - Decisions the doc did not settle: (1) the lambda view draws one map per
      loop kind with pairs, as E08.6 does (open loop relative lambda, closed
      loop `fuel_trim_total_pct` in percentage points, setpoint moved
      relative target phi), each cell naming the other loop (`CL`, `OL`, `SP`)
      where all its pairs are in another; a closed-loop lambda is never drawn
      as a lambda effect. (2) The grid, the edges and `candidate_events` are
      never remembered (they describe this experiment or this base); the
      other fifteen stated values are, marked. (3) The suggested edges are
      used exactly as the Decisions say; where the fill axis' top breakpoint
      is 100 % the last edge is past the model and Run says so rather than
      the page clamping. (4) "Moved-lookup marks" are `row_axis_moved` /
      `col_axis_moved` (a ▲ on the axis tick and the cell, as E08.6) and the
      overlay's `moved_lookups` listed in the caveat block and above the maps;
      no cell-level mark exists in the payload. (5) Result JSON is the
      response, compact, as `<label>.sim-result.json`; no image download
      exists. (6) The W09.3 URL test
      (`lists each image ... keeps the sha in the URL`) failed once under a
      load average of 14 (the sha is written by an effect after the table
      renders) and passes alone: W09.3's, not touched.
    - **Found, not fixed (W09.3's `CellGrid.tsx`):** in a real browser a single
      click on a Fuel Comp cell selected 11 cells when the table was partly
      off screen: `onMouseDown` calls `element.current?.focus()`, the focus
      scrolls the page under the held button, and `onMouseEnter` extends the
      selection while `dragging` is true. With a tall viewport (2400 px) the
      same click selects 1 cell. Fix: `focus({ preventScroll: true })`.
    - **Verifier follow-ups (2026-10-15):** (1) the E08.7 line reworded as
      above. (2) A typed integer beyond 2^53 - 1 (the seed) is refused by
      name in `checkSimForm` (`parseNumber` refuses non-safe integers, "is
      beyond 9007199254740991 ... can send exactly"), never rounded by
      `Number()`: `9007199254740991` sends as typed, `...992`, `...993` and
      `1e20` are refused. (3) The form is `aria-label="Simulate form"`; the
      section keeps `Simulate` (`RomEditorPage.tsx` untouched; one query line
      in `RomEditorPage.test.tsx`). (4) A `changed` cell whose median is
      exactly 0 prints `0*`, not unchanged's `0`, its accessible name says
      "changed, median exactly 0 (some pairs move)", and the legend of a map
      with one has a "changed, median 0" entry.

- [x] **W09.5 — MCP tools** (`docs/mcp-server.md` is the source of truth):
  `preview_rom_edit(sha, label, edits)` and `simulate_rom_edit(sha, request)`.
  Both are thin wrappers over the HTTP routes, so an agent can run the same
  experiment the page does. **Accept:** tool tests as the existing ROM
  library tools have, with the doc entry.
  Evidence: `src/mcp_server/tools/rom_editor.py` (read delegations in
  `server.py`, no audit: nothing the daemon keeps changes, and a simulate
  response is ~370 KB); `tests/test_mcp_rom_editor.py` (verbatim
  bodies, bearer token, a daemon refusal per status 400 edit_refused / 400
  sim_refused / 404 / 409 map, deferred, busy / 422 / 503 reaching the caller
  with status and body, simulate timeout > 900 s, sha confined to its path
  segment, and the docstring and fixture both pinned to
  `SimRequest.model_fields`, all 21, no default); tool snapshot regenerated
  (+2 tools); docs/mcp-server.md rows. `simulate_rom_edit(visited_only=True)`
  (default) cuts `cells.cells` to `n_points > 0` via the pure
  `visited_cells_only`, stating `cells_omitted` and `cells_filter`; nothing else
  in the body is touched, and `visited_only=False` is byte-identical to the
  daemon's. Tests: kept = exactly the visited cells, omitted + kept = 256,
  other keys and `status_counts` untouched, input not mutated; measured on a
  256-cell / 6-visited fixture with full outputs: 394,541 bytes full, 31,992
  filtered (< 60 KB). 23 tests in `tests/test_mcp_rom_editor.py`.

- [ ] **W09.6 — Real-ROM acceptance (on the machine with the images).**
  - `tests/test_rom_editor_private_rom.py`, skipping by name when the images
    are absent, as `tests/test_fuel_comp_restore_private_rom.py` does. Through
    `rom_editor.simulate_edits` on ZS44A-20261004 (a matched revision):
    1. **Negative control** (W09.1's `_simulate` seam, base as candidate):
       every visited cell `unchanged`.
    2. **K +5 %** (`k_constant` `scale 1.05 nearest`): ΔBFS per supported
       open-loop pair equals the realised raw ratio (E07.6).
    3. **Target AFR +4 counts** on the WOT row: E07.6's WOT figures
       (φ +0.03125, pulse +0.41 ms, λ −2.5 %) within E07.6's stated tolerance.
    4. **Fuel Comp region edit** (`tests/fixtures/rom_edit_spec/
       fuel_comp_light_load.yaml`, slot 1): `edited` cells are rpm rows 2–3 ×
       fill cols 2–3 on the `fuel_comp` grid (E08.1's private check), and Δλ
       moves only where the lookup reaches those cells.
  - Measure on the laptop and then on the Pi (after collection stops,
    `vehicle_state` `parked` or `bench`): wall time and peak RSS of a default
    Fuel Comp grid (16 × 16) at 200 and at 1,000 draws. Record both here. If
    the Pi exceeds `DIGEST_TIMEOUT_SECONDS` or the child's address-space
    ceiling at 1,000 draws, lower `MAX_DRAWS` to what passed, and note it.
  - Browser walkthrough on the Pi dashboard: pick the 10-04 image, scale the
    light-load Fuel Comp region +2 %, preview, download the spec, simulate,
    and screenshot the result. `pnpm e2e` gets `romEditor.spec.ts` with the
    API mocked (as `romMaps.spec.ts`).
  - Tick E08.8 in the E08 doc with a pointer here.

- [ ] **W09.7 — Later, named so they are not implied:** persisted runs under
  `/data/canary/sim/<run>/` (declared in `tests/test_state_dir.py`) with a
  run list. Drive designs from pre-change sessions (E08.2 `build_drive_design`)
  as a second point source. Slots 2–5 with a stated active slot. Timing-trim
  editing (traced but `unsupported` until a torque plant exists, E07.5). The
  E08.6 HTML report as a download.

## Not in scope

- **Writing the ECU or producing a flashable file.** See "Decisions"; the
  Flash page (W07) owns that, under AGENTS.md's two conditions.
- **Optimising a ROM against the simulator.** `paired_scenarios.optimise`
  stays refused (E07.6).
- **Running on the Pi during collection.** Refused by `capture_deferral`.
