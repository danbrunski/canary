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

- [x] **W09.0 — Prerequisite: E08.4 per-cell aggregation.** Landed as
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
    be the run's (`effects_mismatch` otherwise). *Done 2026-10-15:* `build_candidate`'s
    trace cannot be reused (it runs without the contract and mode and is not
    returned as an `EffectReport`), so `rom_editor` recomputes it with
    `compare_roms(..., contract=, mode=)` (see W09.1's evidence). E08.4 is
    ticked in the E08 doc.
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
    - *Mocked e2e done 2026-10-15 (the real-image walkthrough above stays
      open):* `e2e/romEditor.spec.ts` + `e2e/fixtures/romEditorApi.ts`, desktop
      chromium only, 1280x600 so the 16 x 16 table is partly below the fold
      (asserted). 5 tests, 5 passed in ~25 s (20/20 on `--repeat-each=4`);
      `romMaps.spec.ts` and `emptyDaemon.spec.ts` still green. Covers: starts
      unloaded (one Open ROM… button, Recent ROMs newest first although the
      API answers oldest first, no grid/form/table request/`?rom=`); open a
      file through the real file chooser (upload POST body, name, octet-stream;
      header shows the file name, sha prefix, `can simulate`, `?rom=` written);
      Fuel Compensation, ONE click selects exactly one cell (regression for the
      click bug), shift-click makes a 6-cell range, a scale without rounding is
      refused `rounding_undeclared`, scale +2 % nearest, Preview sends the exact
      cells/scale/rounding, the after table outlines the six changed cells with
      `+1.6 %`, Download spec is `<label>.edit-spec.yaml` holding the mocked
      `spec_yaml`, and no image/candidate link or button exists (only
      "Download spec", no blob anchors, no request beyond list/upload/tables/
      preview); Simulate form (nothing defaulted, Baro empty blocks Run and is
      named by "What blocks Run", the request carries all 21 fields), Run with
      the mocked result (caveat block before any table/figure/status cell, all
      six statuses plus `CL`/`∅` labels, Download result JSON equals the mocked
      response); an unrecorded base shows `preview only`, Simulate disabled
      with `base_not_a_recorded_revision` (also after a preview); UpRev's
      encrypted backup refused by its server text with nothing loaded. The
      mocks reuse the vitest fixtures (`src/test/romLibraryFixtures.ts`,
      `romSimFixtures.ts`) by import, so there is one copy; the sim result's
      `cells` validates against `sim_cells.CellTable` (rechecked 2026-10-15).
      Not covered: phone project, drag-select, keyboard selection (vitest has
      them), a real daemon, real images.
    - *Laptop half done 2026-10-15. Still open: the Pi measurement and the Pi
      browser walkthrough.* `tests/test_rom_editor_private_rom.py`
      (`pytestmark = slow`). It skips by name when `OPERATOR_ROMS["r20261004"]`
      is absent. Result: 9 passed, 1 xfailed (strict) in 11–22 s with
      `-n0 -m slow`. The 20261004 image's decoded sha256 equals its
      `tune_revisions.yaml` `image_sha256` (`5587f7e1…`). `revision_match`
      against the tracked registry returns `matched`, and so does the
      library's own match after an `.OsirisROM` upload.
      - **The grid the form suggests is not 16 × 16 on this image.** Fuel
        Comp's fill axis is raw 16..255, which decodes to 7.14–113.84 %.
        `edgesFromBreakpoints` therefore ends at 117.19 %, and the design
        refuses it as `load_outside_model`. After "Trim load edges to 0-100 %",
        the edges are rpm 200…6,600 step 400 × load 3.571429…96.42857, 100.
        That is 16 × 14 = **224 points**. This is
        `test_the_form_suggests_these_edges_and_only_the_trimmed_grid_can_run`,
        a Python port of `suggestEdges`/`trimLoadEdges` checked against those
        literal numbers. Every value is stated: baro 81, IAT 25, coolant 88,
        battery 14, rail 350, throttle 20, pedal flag 0, all learned values 0,
        seed 20261015, 4 draws.
      - **1. Negative control** (`_simulate`, base bytes as the candidate):
        outcome `identical`, every reach `no_change`, 224/224 cells visited.
        No output in any visited cell is `changed` or `partly_supported`, and
        every computed delta is 0. Every pair is equal on both sides. 146
        cells are closed loop and 78 open loop. Unsupported outputs name only
        these reasons: `closed_loop_trim_authority_undecoded`,
        `short_pulse_undecoded` (16 cells), and
        `target_lookup:enrichment_gate_state_unknown` + `not_computed` (one
        cell: 4,000 rpm, fill bytes 191–192, inside the enrichment gate's
        hysteresis band). BFS and φ are `unchanged` in the other 223 cells.
        **Known over-refusal, locked in on purpose:** in that cell BFS is
        computed (finite) before the target lookup, yet reads `unsupported`.
        `paired_scenarios.run_paired` (`reasons = list(gate) + …`, ~line 1017)
        gives the lookup's gate reason to every output, BFS included. That is
        cautious rather than falsely confident. The test asserts it with a
        comment pointing at that line, so fixing it flips the test
        deliberately. `src/analysis` is not edited here.
      - **2. K +5 %** (`scale 1.05 nearest`): 28,715 → 30,151, so
        r = 1.0500087. All 308 supported open-loop pairs satisfy
        BFS_c − r·BFS_b ∈ (−9.17, +9.62) counts. That bound is E08.7's,
        re-derived here with f = max Fuel Comp 139/128 in place of a flat 128.
        The measured spread was −4.59…+4.82 counts, with base BFS 1,984–29,508,
        and every pair moved. 77 all-open-loop cells read `changed`. Their
        relative ΔBFS p50 lies within −5.5e-4…+2.4e-3 of r − 1 (the wider end
        is the lowest-BFS cells). The gate-ambiguous cell reads `unsupported`
        (the known over-refusal above).
      - **3. Target AFR +4 on the WOT column** (col 7, the top X breakpoint
        and the column the forced-column flag reads; 146/149/149/149/149/153/
        155/159 → +4). The WOT row/column has to be reached through the
        pedal flag (bit 0 set), because no grid point reaches it by X (below).
        Setup: one rpm cell 4,600–5,000 (centre 4,800, as E07.6), the form's
        three highest fill cells, and E07.6's environment and learned state
        (LTFT 2.34), 200 draws.
        - Δφ is exactly **+0.03125 at every pair** (157/128 → 161/128).
        - At each point and drawn fill byte, BFS, φ and the pulse equal E07.6's
          own `run_paired` exactly. λ p50 is within 2e-4 of its central value,
          and p05–p95 lies inside its range over assumptions (E08.7's
          tolerance).
        - The reference `PairedScenario` is built **by hand** from the stated
          `E076_LEARNED` / `E076_WOT_ENV`, never the editor's template. The
          template's learned state and each design point's environment are
          asserted equal to those constants. So a mutation forcing LTFT 0 in
          `rom_editor._template` fails twice: on the learned-state assertion,
          and, with that assertion removed, on the `run_paired` pulse
          comparison.
        - Measured: BFS 24,636/24,820, 27,296/27,496 and 29,080; pulse
          +0.23, +0.26 and +0.28 ms; λ p50 −2.423, −2.455 and −2.486 %.
      - **Finding (strict `xfail`, assertion kept literal):** E07.6's written
        WOT figures do not reproduce at the grid's WOT point. The pulse and λ
        miss for different reasons.
        - **What the figures are.** E07.6 stated them on its *synthetic*
          image: flat Fuel Comp 128, every slot-1 Target AFR cell +4, pedal
          flag 0, BFS stated at 42,000. E07.6's own test asserts only their
          signs. The "−2.52…−2.49 %" beside them is that run's one-point
          spread over the plant's assumptions, not a tolerance.
        - **At the grid's top fill cell** (98.2 %, BFS 29,080): pulse
          +0.28 ms; λ p05/p50/p95 −2.507/−2.486/−2.468 %.
        - **The pulse is a BFS/airflow condition.** The pulse step is
          BFS-proportional. E07.6's own point (4,800 rpm, 150 g/s, MAP 79,
          fill byte 200, forced-column flag set) on this image gives BFS
          41,768, X 121, Δφ +0.03125 and pulse +0.41 ms. So the written pulse
          needs about 41.8k BFS here. E08.7's ~41,997 threshold belongs to the
          synthetic image.
        - **The λ range is not reproducible on 20261004 at any BFS near
          42k.** It is a property of the synthetic image plus the 0.01 ms
          pulse quantum.
          - E07.6's own point here: λ −2.483 % (range −2.498…−2.468).
          - At BFS exactly 42,000 (MAF 150.83 g/s): λ −2.529 %
            (−2.544…−2.514) and pulse +0.42 ms.
          - Both ranges fall outside −2.52…−2.49. λ steps with the pulse
            quantum as MAF moves by 0.01 g/s.
          - These are pinned by
            `test_at_e076s_own_points_on_this_image_the_pulse_holds_and_the_lambda_range_does_not`
            (hand-built scenario through `run_paired`).
        - **Why no grid reaches ~41.8k BFS here.** The cause is the plant
          version's volumetric efficiency, not the grid's structure.
          - A grid takes fill as MAP/baro and solves MAF through the sim kit's
            plant, `fuel-mv-1` (`speed_density_capped`). Its VE is the working
            assumption 0.7 (`data/nissan-xterra-2008/fuel_plant.yaml`
            `manifold_pressure`, `requires_measurement`, bracket 0.4–1.0).
          - That gives ~103 g/s at the grid's 100 % fill (102.9 g/s at MAP
            79.6 kPa, 4,800 rpm, baro 81).
          - fuel-mv-3's identified VE at 4,000–5,000 rpm is 0.9055
            (`fuel_plant_mv3.yaml` `ve_curve`), which would give ~133 g/s.
          - `test_no_grid_point_reaches_e076s_wot_bfs_or_the_wot_column_by_x`
            asserts that the plant is fuel-mv-1, that load edges
            [99.9, 100] at 4,800 rpm keep MAF < 110 g/s, and that BFS stays
            < 41,768.
        - **With the pedal flag 0, the WOT column is reached by no point of
          the form's grid.** The visited X columns are 0–2, and Δφ = 0
          wherever it is computed. An operator who edits the WOT column and
          states pedal flag 0 sees nothing anywhere.
      - **4. Fuel Comp region** (`fuel_comp_light_load.yaml`; `spec_sha256`
        equals the fixture's): the edited cells are exactly (2,2), (2,3) and
        (3,2) 130 → 133, (3,3) 128 → 131 (rpm rows 2–3 × fill cols 2–3, as
        E08.1), and (0,0) 128 → 126.
        - The `fuel_comp` overlay marks exactly those 5 cells.
        - Per pair, "reached" is computed from the axes alone: an edited
          interpolation corner with non-zero weight, with the rpm word against
          the axis byte × 4 and the fill byte byte-for-byte. 27 of 896 pairs
          (8 points at 400/1,200/1,600 rpm) reach it.
        - The other 869 pairs have Δ = 0 in every output. BFS moved at 20
          pairs, all of them reached, and the 5 edited cells are the only
          cells where BFS reads `changed`.
        - Every reaching pair runs **closed loop** under this environment, so
          Δλ is 0 at all 896 pairs. The change is carried by
          `fuel_trim_total_pct` at 16 reached pairs, `unsupported` because
          the trim authority is undecoded. The page's open-loop λ map shows
          nothing for this edit.
      - **GUI path:** `run_job("rom_sim")` on a `<sha>.bin` in `tmp_path`
        equals `simulate_edits`, survives `pickle` round-trip, and serialises
        to JSON. `test_the_route_simulates_the_uploaded_osiris_file`: the
        `.OsirisROM` goes through `POST /control/rom/library` (plain sha back,
        `matched`), then `POST …/simulate` through `run_off_loop`
        (in-process executor, the child's own `load_kit`). The response is
        200 and equals `simulate_edits` through JSON, 224 points.
      - **Mutation checks** (scratch copies outside the tree; each fails):
        - dropping the upper interpolation corner (case 4: an unreached
          pair's BFS moves);
        - r = 1.04 (case 2: errors of 196–300 counts);
        - Δφ 0.0234375 (case 3);
        - `rom_editor._template` forced to LTFT 0 (case 3: fails on the
          learned-state assertion, and without it on the hand-built
          `run_paired` pulse comparison at (0, byte 191)).
      - **Measured on the laptop** (offline, `.venv/bin/python -I
        <scratchpad>/measure.py child|inproc <grid> <draws>`). The edit is
        the light-load spec above with the environment of case 1 and seed
        20261015.
        - **Paths:** `child` is `asyncio.run(workbench_jobs.run_off_loop(
          "rom_sim", …))`, the real `spawn` child under
          `digest_address_space_bytes` = 4,096 MiB here. Peak RSS is
          `getrusage(RUSAGE_CHILDREN).ru_maxrss` after the children are
          joined. `inproc` is `run_workbench_job` in the same fresh process
          under the same `RLIMIT_AS`, for `VmPeak`, the virtual peak that
          `RLIMIT_AS` actually bounds.
        - **Machine:** Intel Core Ultra 9 185H, 12 logical CPUs, 15,997 MiB,
          WSL2 kernel 5.15.167.4, Python 3.12.3. 1-min load 0.6–1.0
          throughout.

        | grid | points | draws | wall (child, incl. spawn) | child peak RSS | VmPeak (inproc) | response JSON | `visited_only` |
        |---|---|---|---|---|---|---|---|
        | form 16 × 14 | 224 | 200 | 4.5 s | 188 MiB | 871 MiB | 705,543 B | 663,116 B |
        | form 16 × 14 | 224 | 1,000 | 8.5 s | 277 MiB | 961 MiB | 710,118 B | 667,691 B |
        | form 16 × 14 | 224 | 2,000 | 11.9 s | 390 MiB | — | 710,084 B | 667,657 B |
        | 16 × 16 (fill 0–100 % in 16) | 256 | 200 | 4.5 s | 190 MiB | — | 705,749 B | 663,322 B |
        | 16 × 16 | 256 | 1,000 | 9.1 s | 294 MiB | 977 MiB | 710,290 B | 667,863 B |
        | 32 × 32 (the cap) | 1,024 | 1,000 | 34.1 s | 682 MiB | — | 755,301 B | 712,874 B |
        | 32 × 32 (the cap) | 1,024 | 2,000 | 51.1 s | 1,196 MiB | 1,891 MiB | 757,089 B | 714,662 B |

        - **Scaling:** RSS is about 180 MiB plus ~0.5 KB per point × draw.
          `inproc` wall time is 1.3–7 s below `child`, which is spawn and
          imports.
        - **Response size:** about 0.7 MB whatever the draws (the 256 Fuel
          Comp cells, 224 of them visited, even for 256 or 1,024 points).
          W09.5's `visited_only` filter saves only ~6 % on a full grid; its
          "< 60 KB" holds for sparse grids only.
        - **Limits:** `DIGEST_TIMEOUT_SECONDS` = 900 s; the child's
          `RLIMIT_AS` is 50 % of RAM clamped to 1,024–4,096 MiB.
        - **Pi estimate (a guess, not measured):** if the Pi is 3–6× slower,
          the form grid at 1,000 draws takes ~25–50 s, at 2,000 draws ~35–70 s,
          and the cap (1,024 × 2,000) ~150–310 s. All are under 900 s. RSS
          and VmPeak do not scale with CPU speed but may differ on aarch64.
          The cap's 1.2 GiB RSS / 1.9 GiB virtual fit an 8 GB Pi's 4,096 MiB
          ceiling, but not a 2 GB Pi's 1,024 MiB floor.
        - `MAX_DRAWS` is unchanged (2,000); the operator decides after the Pi
          numbers.
  - Tick E08.8 in the E08 doc with a pointer here.

- [ ] **W09.7 — Later, named so they are not implied:** persisted runs under
  `/data/canary/sim/<run>/` (declared in `tests/test_state_dir.py`) with a
  run list. Drive designs from pre-change sessions (E08.2 `build_drive_design`)
  as a second point source. Slots 2–5 with a stated active slot. Timing-trim
  editing (traced but `unsupported` until a torque plant exists, E07.5). The
  E08.6 HTML report as a download.
  Found by the independent verifier on the real ZS44A-20261004 image
  (2026-10-15), to be done in the simulator, not the page:
  - Name the working-assumption VE in the simulation caveats/assumptions
    (fuel-mv-1 ve 0.7, `requires_measurement`, bracket 0.4-1.0; fuel-mv-3
    identifies 0.9055 at 4,000-5,000 rpm). Today only
    `plant:manifold_pressure_unmeasured` appears, and the grid's 100 % fill
    sits at ~103 g/s (BFS ~29k) against the truck's ~38-42k, so pulse and BFS
    deltas at the grid top are understated by about 30 % (relative lambda is
    roughly unaffected). Lives in `sim_design` / the plant, E08 domain.
  - `run_paired` applies the target-lookup gate reason to `base_fuel_schedule`
    (`paired_scenarios.py` ~1017) although BFS is computed before the lookup:
    an over-refusal, cautious not false.
    `tests/test_rom_editor_private_rom.py` marks it as a known over-refusal.

- [x] **W09.8 — "Open ROM…" on the ROM Editor page.** Requested 2026-10-15
  by the operator via the E08 desk-launcher session: "we shouldn't have to run
  with the rom folder, it should be browsable from the gui", then: "just think
  about how a tool would normally work. You want to edit a rom, you click a
  load button, it opens a file browser, you navigate to the folder, select
  the file, and open." Frontend only; no backend change.
  - The page's primary control is an **Open ROM…** button (an **Open another
    ROM…** once something is loaded) that opens the browser's native file
    dialog: a plain `<input type="file">` with no `accept` filter, so an
    `.OsirisROM` or an odd extension is never hidden. Dropping a file on the
    group does the same. With nothing stored the empty state is that one
    button and no picker.
  - One step: `uploadRomImage` -> the answered entry becomes the base (held
    locally until the library refresh lands, so no other image shows in
    between; `?rom=<sha>` written) -> the header shows the file's own name,
    the sha prefix, the `revision_match` chip and `can simulate` / `preview
    only` (the same `SimulateChip` the picker uses). The library's sha dedup
    is behind the scenes: an existing entry gets one quiet line ("X is already
    in the library; nothing new was stored"), not a dialog or a step.
  - **Starts unloaded** (operator's reading, 2026-10-15: a normal tool starts
    empty, then you open a file). No base is auto-selected, however many
    images the library holds; the table, operation, pending-edit, preview
    and Simulate sections appear only once a base is loaded, and nothing is
    previewed, simulated or persisted before that. Only a `?rom=<sha>` in the
    URL (a deliberate link: the desk launcher, a reload) loads a base at
    start; a sha not in the library leaves the page unloaded with a quiet
    "is not in the library" note. This reverses an earlier draft of this
    item that kept W09.3's first-entry auto-select.
  - The stored images are a secondary **Recent ROMs** list (BasePicker, rows
    unchanged), **newest first by the entry's `added_at`** (the field the
    library sets on every entry; the API's own order is not relied on),
    one click to load, each with its `revision_match` chip and can simulate /
    preview only. W09's rule stands: Simulate only when `revision_match
    .status === 'matched'`; an unrecorded image previews and the Simulate slot
    gives `base_not_a_recorded_revision`.
  - Refusals by reason, as the library page does (`refusalText`): UpRev's
    encrypted backup `.bin` by the server's own name and reason, 413, 401; an
    empty or over-2 MiB file is refused client-side without uploading. A
    refusal loads nothing and keeps the current base. No extension check
    (the server detects the format from the bytes), which the library page
    keeps.
  - Evidence 2026-10-15: `RomEditorPage.test.tsx` 31 tests (14 new or
    rewritten for W09.8): starts unloaded with a non-empty library (no grid,
    no Edit/Pending/Preview/Simulate sections, no URL, no table fetch, no
    POST, nothing in localStorage); recents newest first when the API answers
    oldest first; one click on a recent loads it and writes `?rom=`; a
    `?rom=` link in the library loads, one outside it stays unloaded with the
    note; empty library is one Open ROM… button, no picker, no `accept`; a
    picked file loads in one step (POST body/headers, URL, header name,
    `preview only`, Simulate disabled with its reason); a recorded revision
    shows `can simulate`; already-in-library note; encrypted-backup refusal by
    name, selection and URL unchanged; 413/401/empty; drop; pending edits
    survive an upload-driven switch and do not leak to the new base; no image
    download after an upload (the existing no-download assertions unchanged
    and green). The W09.3 and W09.4 tests that relied on auto-select now set
    `?rom=` (`renderEditor()` in `RomEditorPage.test.tsx` and
    `RomEditorSimulate.test.tsx`) or click a recent entry. Gate in
    `src/dashboard/static`: `pnpm typecheck`, `pnpm lint` (no suppression
    changes), `pnpm format:check` clean; vitest `--no-file-parallelism` on
    RomEditorPage, RomEditorSimulate, romEditor/, RomLibraryPage: 4 files / 99
    passed; `vitest related` on the touched files 15 files / 285 passed.
    Files: `panels/tuning/RomEditorPage.tsx`, `romEditor/OpenRom.tsx` (new),
    `romEditor/BasePicker.tsx` (`SimulateChip` extracted),
    `test/romLibraryFixtures.ts` (`ENTRY_UNRECORDED`).
    Not run: `pnpm e2e` (no browser walkthrough of the dialog).

- [x] **W09.9 — Coarse and fine runs, a tunable Monte Carlo, a cost estimate and
  Cancel on the Simulate form.** Requested 2026-10-15 by the operator: "for the
  ROM simulation, make sure we can tune the monte carlo runs and have a coarse
  and fine simulation, so a user doesn't have to run an hour long analysis every
  time they want to check something". Frontend and documentation only; the
  backend already takes `draws`, `seed` and the edges as required fields
  (`tools/rom_sim.py` has no coarse/fine concept to align with: its flags are
  `--draws` and `--design`).
  - **Presets are buttons that fill the form, never defaults.** *Coarse (quick
    check)* fills the RPM and load edges with **every other Fuel Comp
    breakpoint** of each axis (edges rebuilt from those, so each grid point
    still sits on a breakpoint; about a quarter of the points) and **200
    draws**. *Fine* fills the full suggested edges (as the form suggests them)
    and **1,000 draws**. Both cut a load edge past 100 % to the model's span,
    as the Trim button does, and say so. After a click the form prints what it
    filled ("Coarse (quick check) filled RPM edges (9 edges, 8 cells), load
    edges ... and draws (200): 64 points x 200 draws. Nothing else was
    touched."). Editing a filled field flips the state to **Custom** (derived
    from the field values, so typing the preset's value back restores it).
    The preset is page state: not stored, and the seed, environment, learned
    state, grid choice and candidate events are never touched. Pressing a
    preset clears the "remembered from your last run" mark on `draws` (the
    existing remembered-values behaviour is unchanged).
  - **The draw numbers and their basis** (`lib/romSimSize.ts`, `PRESET_DRAWS`,
    both below `MAX_DRAWS` = 2,000): Coarse 200 is the count E08 reads cell
    quantiles from (E08.3's Fuel-Comp-restore entry: "three draws give only a
    coarse per-cell spread, so cell quantiles should be read from" the 200-draw
    run; E08.5 and E08.7 use 200) and W09.6's low row. At 224 points W09.6
    measured 4.5 s for 200 draws against 8.5 s for 1,000: start-up dominates a
    small run, so fewer than 200 saves almost nothing. Fine 1,000 is E08.3's
    stated speed target and W09.6's reference row. 2,000 is the cap, not a
    recommendation. E08 states no minimum draw count or quantile-resolution
    limit; none is invented.
  - **Coarse means `no_points`, not `unchanged`.** The Coarse preset's
    description says so on the button's page text, and a result below the Fine
    preset's draws OR points (judged from the result's own `cells.draws` and
    `design.points` against the Fine grid's size for this base, so a hand-set
    low run is flagged like a clicked one) carries a **Coarse run** banner above
    the caveat block and the maps: the draws and points, why it is below Fine,
    the `no_points` sentence with the result's own count of such cells (when
    points are the reason), and "quantiles over few draws are noisy" worded from
    E08.3 (three draws give only a coarse per-cell spread). No other statistical
    claim is made.
  - **Cost estimate beside Run:** "N points x M draws = K point-draws", then
    either "About T s from your last R runs on this server" or "Not yet measured
    on this server" with the labelled reference ("the dev laptop took 8.5 s for
    224 points x 1,000 draws (W09.6)", the measured row nearest the size, and
    the fit's value for it). The model is `t = overhead + rate x points x
    draws`, fitted (least squares on relative error) to W09.6's seven laptop
    rows only: **3.3 s + 2.3e-5 s x points x draws**, off by -20 % to +16 %
    against those rows, with no run under 224 points timed. The page shows that
    fit. This browser stores its own runs (`canary.romSim.timings`, version 1,
    last 8, `try`/`catch`; per origin, so per server): a successful run's size
    and the page's elapsed timer, never a failed or cancelled one. The server
    estimate is the fit scaled by the **median of observed / modelled** over
    those runs, a ratio against the model rather than a seconds-per-point-draw,
    so a coarse run (mostly start-up) and a fine one (mostly draws) calibrate
    the same number. Checked: one coarse row (224 x 200, 4.5 s) alone estimates
    224 x 1,000 at 8.85 s against the measured 8.5 s (+4 %), and the fine row
    alone estimates 224 x 200 at 4.32 s against 4.5 s (-4 %).
  - **Cancel stops waiting; it cannot stop the server.** Read
    `workbench_jobs.run_off_loop`: the child's task is held behind
    `asyncio.shield`, no route cancels a `rom_sim` job (the only cancel route is
    the ECU backup's), and it ends only when it finishes or at the 900 s
    timeout. So the button is "Cancel (stop waiting)" and says "the server
    finishes this run (about N s, from your earlier runs here | the dev-laptop
    fit; not yet measured on this server) before another can start". It aborts
    the request (the `signal` `simulateRomEdits` already took), drops any later
    result, records no timing, and the page then says the daemon is still
    running it. A different request meanwhile is `409 analysis_busy`
    (`child_busy`), and when that follows a cancel on this page the busy alert
    says it is probably the cancelled run, with the estimated time left. An
    identical request re-attaches to the running job (`_IN_FLIGHT` is keyed by
    the request), which the page and `docs/headless-api.md` say. No backend
    route was added.
  - **Evidence 2026-10-15.** Tests, all in `src/dashboard/static`:
    `lib/romSimSize.test.ts` 23 (presets within `MAX_DRAWS`, Coarse = a quarter
    of Fine and centred on breakpoints, the fit equals its rows within the stated
    error, localStorage validation and blocked storage, scale and the
    coarse/fine non-distortion above, wording); `SimResults.test.tsx` +7
    (banner for few draws and points, for draws alone, for points alone, not for
    a fine run, above the maps, `no_points` cells keep `–` and their "no points"
    name, unknown Fine size judges draws only); `RomEditorSimulate.test.tsx` +19
    (presets fill exactly edges and draws and nothing else, Custom flips and
    returns, preset not remembered and the draws mark clears, disabled without
    breakpoints; banner after a Coarse-preset run, a hand-set run, a
    hand-cut-grid run and not after Fine, the request still the 21 documented
    fields; estimate with and without stored runs, learned from the elapsed timer
    and stored as points x draws, not stored on a failure; Cancel aborts the
    signal, says what it does, shows no result, records nothing, explains the
    following 409, and does not claim a 409 it did not cause). The existing
    21-field parity test, `RomEditorPage.test.tsx` 31, `romSim.test.ts` 29 and
    `romSimView.test.ts` 23 stay green: `vitest run --no-file-parallelism` over
    those six files, 175 passed. `pnpm typecheck`, `pnpm lint` (no suppression
    change), `pnpm format:check` clean. `pnpm exec playwright test
    e2e/romEditor.spec.ts --project=chromium`: 6 passed, one new (Coarse
    pressed, edges and draws filled and counted, Custom on an edit, Run, the
    banner; the request carries 21 fields and 9 rpm edges). Files:
    `lib/romSimSize.ts` (new), `romEditor/SimForm.tsx`, `romEditor/SimResults.tsx`,
    `RomEditorPage.tsx`, `types/romEditor.ts` (`SimRunContext.fineReference`),
    the tests above, `e2e/romEditor.spec.ts`; docs `headless-api.md` ("Coarse vs
    fine" under the simulate route) and the `simulate_rom_edit` row in
    `mcp-server.md`.
  - **Not done, on purpose:** the `simulate_rom_edit` docstring is the tool's
    description and is pinned by `tests/fixtures/mcp_tool_snapshot.json`, so the
    note is in the docs only. The Pi's own timings are still unmeasured (W09.6
    owes them); the reference line is the laptop's and is labelled so.
  - **The WOT column is stated, not silent (2026-10-15, found by the
    verifier on the real ZS44A-20261004 image).** The pedal flag's bit 0 forces
    the last Fuel Targets (Target AFR) column (`rom_map.yaml` `fuel_targets`;
    `target_phi_twin.yaml` `forced_column_flag`), so with the flag stated 0 no
    simulated point reads it and an edit to that column shows no change
    anywhere (`no_points` on the target_afr grid, `unchanged` elsewhere). Not
    silent-wrong, but it read as "no effect". Frontend only: the flag field's
    hint now says what bit 0 does (`PEDAL_FLAG_HINT`); `SimForm` shows a
    non-blocking `role="note"` before Run when a pending edit writes the last
    column of a Fuel Targets table (`editsTouchWotColumn`, the column derived
    from the loaded table's own width, `cells` and `all` selectors; a `region`
    selector is not resolved) and the stated flag is a whole number with bit 0
    clear; nothing when the flag is empty (Run names it) or bit 0 is set, and
    it never offers or sets a flag. `SimRunContext.wotColumnEdited` (computed
    by the page at Run) puts the matching line in the result's caveat block.
    Vitest: `romSim.test.ts` +5, `RomEditorSimulate.test.tsx` +5,
    `SimResults.test.tsx` +3 (hint, warning for flag 0 and 2, not for 1 / 3 /
    empty / 1.5, not for a middle column, Fuel Comp or K edits; caveat likewise;
    the 21-field request test unchanged). No e2e assertion added.

## Not in scope

- **Writing the ECU or producing a flashable file.** See "Decisions"; the
  Flash page (W07) owns that, under AGENTS.md's two conditions.
- **Optimising a ROM against the simulator.** `paired_scenarios.optimise`
  stays refused (E07.6).
- **Running on the Pi during collection.** Refused by `capture_deferral`.
