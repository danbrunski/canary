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
| `sim_cells.aggregate_cells` / `axes_for` (E08.4) | Per-cell statuses and quantiles. **In progress 2026-10-15** (see W09.0) |
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

- [ ] **W09.0 — Prerequisite: E08.4 per-cell aggregation.** Started
  2026-10-15 on the laptop by an implementer. Check E08.4's box and the
  working tree before starting: if it is not committed, finish it to E08.4's
  own Accept list first. The contract W09.4 renders is `CellAggregation.as_dict()`:
  `grid {name, row {quantity, unit, breakpoints}, col {…}}`, `outputs`,
  `cells[] {row, col, n_points, n_pairs, weight, regime_share, edited,
  moved_lookup, outputs {<name>: {status, supported_share, delta {p05, p50,
  p95}, relative_delta {…}, quantiles_over, reasons[]}}}`, `edited_cells`,
  `caveats`. If E08.4 changed any field name, follow E08.4 and note it here.

- [ ] **W09.1 — `src/analysis/rom_editor.py`: preview and simulate as pure functions.**
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
    "Decisions"). `grid: Literal["fuel_comp", "fuel_targets"]` picks the
    aggregation axes (E08.4 `axes_for`). `load_basis` is `sim_design.LoadBasis`.
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
    7. `aggregate_cells(result, design, axes_for(grid, base.image, rom_map,
       slot=1)).as_dict()`.

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

- [ ] **W09.2 — Job kind and routes.**
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

- [ ] **W09.3 — ROM Editor page: choose, edit, preview, download the spec.**
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

- [ ] **W09.4 — Simulate form and results.**
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
    Δpulse. Closed-loop cells show `fuel_trim_total_pct` per E08.4. Each
    status is a label or hatch. Edited cells are outlined, and
    `moved_lookup` cells are marked. Clicking a cell shows n points, pairs,
    weight, quantiles, regime mix, support share and reasons. Design summary
    and provenance (hashes, seed, draws) sit below. **Download result JSON**
    for the laptop.
  - **Accept:** vitest with a hand-built `cells` payload covering every
    status. Each status label renders, and the caveat block precedes the
    first grid in DOM order. A missing environment field blocks Run and is
    named. 409 deferred and 400 refused render their reasons.

- [ ] **W09.5 — MCP tools** (`docs/mcp-server.md` is the source of truth):
  `preview_rom_edit(sha, label, edits)` and `simulate_rom_edit(sha, request)`.
  Both are thin wrappers over the HTTP routes, so an agent can run the same
  experiment the page does. **Accept:** tool tests as the existing ROM
  library tools have, with the doc entry.

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
