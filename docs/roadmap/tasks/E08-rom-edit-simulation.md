# E08 — edit a ROM, simulate it, see the effect (with uncertainty)

Requested 2026-10-15 by the operator: "edit a tuning ROM, run it in the sim,
maybe Monte Carlo style, to view effects of tuning changes." This turns E07's
library pieces into a workbench: an edit (or an edited ROM file) goes in, and a
per-cell picture of what changes comes out, with the spread from the plant's
uncertainty and an explicit label wherever the simulator cannot say.

Follow [E07](E07-engine-plant-simulator.md) for the plant, the paired-scenario
rules and the effect tracing it builds on, and [B08](B08-firmware-informed-behavior.md)
for the Fuel Comp chain. This document plans the workbench; it does not claim
any simulated effect is a qualified prediction. The plant is not qualified
(E07.3), so every result says so.

## What exists and what this adds

| Exists (E07) | Used for |
|---|---|
| `rom_image.load_image` | `.OsirisROM` and plain 1 MiB images in; UpRev's encrypted backup `.bin` refused by name |
| `rom_effects.make_candidate` / `write_candidate` | A sim-only candidate image from mapped-cell edits (`flashable: false`, stale checksums, outside git and the ROM library) |
| `rom_effects.compare_roms` | Every changed byte traced to the outputs it reaches, unknown bytes never inert |
| `paired_scenarios.run_paired` | Both ROMs at the same steady points: BFS, X, Target AFR, pulse, λ, fuel, trims, each `supported` or carrying its reasons |
| `fuel_plant.sensitivity_cases` / `resolve_parameters(overrides=)` | The plant's working-assumption brackets |
| `fuel_comp_samples.session_samples` / `fuel_comp_restore.points_from_samples` | Fill-byte intervals reconstructed from logged drives (N02) |

What is missing, and what this checklist builds:

1. an edit specification an operator can write (table, region, operation);
2. operating points drawn from real driving, without breaking E07.6's rule that
   no recorded value feeds a prediction;
3. a Monte Carlo over the plant's declared uncertainty, paired across the two
   ROMs (common random numbers);
4. per-cell aggregation that keeps each kind of empty distinct;
5. one CLI, and a self-contained report page;
6. known-answer validation before anyone reads a novel result.

## Decisions made here

- **Points from drives come through a frozen design, never a recording.**
  `OperatingPoint.source` must not name a recording
  (`paired_scenarios._refuse_recorded_sources`). That rule exists to stop
  post-change observations leaking into predictions. A point *design* is a
  frozen, hashed artifact (`sim_design`), built from **pre-change** drives
  only, and recorded on the base ROM's revision (`tune_revisions.yaml`). Every
  point's `source` is `design:<sha256>`. The builder refuses a drive recorded
  on any other revision (`drive_not_on_base_revision`). The run refuses a
  design whose base revision is not the scenario's base image
  (`design_revision_mismatch`). The E07.6 guard is unchanged. A grid design
  (rpm × load, no drives) is the other kind, `source: design:<sha256>` as well.
- **The Monte Carlo covers parameter uncertainty only.** Structural error is
  real and large (E07.3, 2026-10-15: per-read plant error ≈ −0.92 × STFT,
  |err| p90 11.6 %). It is not a sampled parameter, so every result carries
  `structural_error_not_sampled` plus the plant's qualification reason. The
  report prints both above the first chart.
- **Deltas, not absolutes, are the product.** Base and candidate share every
  draw, so common-mode plant error cancels in relative Δλ. The lumped scale
  `stoich_air_per_open_ms` cancels exactly (λ ∝ 1/s on both sides). The engine
  verifies that numerically (E08.3) rather than assuming it, and the report
  shows absolute λ only as a secondary view.
- **A distribution is a declared fact, never a default.** A bracket says where
  a parameter lies, not how likely each value is. The form (uniform,
  triangular, truncated normal) is a required field per parameter, with
  evidence, in `data/<vehicle>/sim_uncertainty.yaml`. A parameter in the plant
  registry with no declared distribution is refused (`distribution_undeclared`),
  never silently uniform.
- **Nothing here talks to the ECU.** Like `rom_effects`, every new module and
  the CLI import no `src.control`, daemon, dashboard, MCP, `can` or `serial`.
  A test asserts it in a clean process.

## File ownership (for parallel implementation)

| Item | New files | May edit |
|---|---|---|
| E08.1 | `src/analysis/rom_edit_spec.py`, `tests/test_rom_edit_spec.py`, `tests/fixtures/rom_edit_spec/*` | — |
| E08.2 | `src/analysis/sim_design.py`, `tests/test_sim_design.py` | — |
| E08.3 | `src/analysis/sim_montecarlo.py`, `data/nissan-xterra-2008/sim_uncertainty.yaml`, `tests/test_sim_montecarlo.py` | `src/analysis/paired_scenarios.py` (plant-override hook only) |
| E08.4 | `src/analysis/sim_cells.py`, `tests/test_sim_cells.py` | — |
| E08.5 | `tools/rom_sim.py`, `tests/test_rom_sim_cli.py` | — |
| E08.6 | `src/analysis/sim_report.py`, `tests/test_sim_report.py` | — |
| E08.7 | `tests/test_rom_sim_known_answers.py`, `tests/test_rom_sim_private_rom.py` | — |

Every new `src/analysis` module is layer 5 (`tests/test_layering.py`). Use
the existing `RomEffectRefusalError` / `SimulationRefusalError` style: a
refusal carries a stable `reason` string that tests assert by name.

## Checklist

- [x] **E08.1 — Edit specification: what the operator writes.**
  A YAML edit spec that resolves to `rom_effects.CellEdit`s on a named base
  image, so an edit is reviewable data, not a script.
  - **Schema** (pydantic, `extra="forbid"`): `schema_version`, `base`
    (exactly one of `revision:` a `tune_revisions.yaml` id, resolved by plain
    sha256 through the registry the way `fuel_comp_rom.RevisionImageResolver`
    does, or `file:` a path to an `.OsirisROM`/plain image plus its required
    `sha256`), `label`, and a list of `edits`.
  - **An edit** names `table` (a `rom_map.yaml` id), `slot` (required for a
    slot table, refused for an absolute one, as `make_candidate`), a
    selector, and one operation.
    - Selectors: `cells: [[row, col], ...]`; `all: true`; or `region` with
      `rpm: [lo, hi]` and/or `load: [lo, hi]` in the table's **own axis
      units**, resolved through that image's axes. A cell belongs to a region
      when its breakpoint lies inside it, inclusive. A region that selects no
      cell is refused (`region_empty`). A region on a table whose axes are not
      `verified` in `rom_map.yaml` is refused (`axis_unverified`).
    - Operations: `set_raw: <int>`, `add_raw: <int>`, `scale: <factor>`. A
      `scale` requires `rounding: nearest|floor|ceil`; there is no default
      (`rounding_undeclared`). A resulting value outside the cell dtype is
      refused (`value_out_of_range`, from `make_candidate`). It is never
      clamped.
    - Two edits that touch the same cell are refused (`duplicate_edit`).
      Composition order is not a thing the spec can express.
    - An edit whose every cell ends at its old value is refused
      (`no_op_edit`). So is a scale that rounds every selected cell back to
      itself; the refusal names the cells.
  - **Second input route: an already-edited ROM file** (e.g. saved from
    UpRev). `load_candidate_file(path, rom_map)` goes through
    `rom_image.load_image`. The UpRev backup `.bin` refusal stays by name.
    The result carries `compare_roms`'s changed ranges as its "edits" for the
    report, so both routes produce the same provenance shape.
  - **Output:** `resolve_edits(spec, rom_map, image) -> list[CellEdit]`, and
    `build_candidate(spec, ...) -> Candidate` through `make_candidate` (never
    re-implementing cell writes). The provenance records the spec's sha256,
    the resolved cell list with old/new raw values, and the base identity.
  - **Accept:**
    - Each selector and operation has a hand-worked test on a synthetic
      image, including a region on Fuel Comp, whose UpRev labels are swapped
      (`rom_map.yaml`: rows `fuel_comp_engine_speed_axis` `0xCACD`, columns
      `fuel_comp_flow_axis` `0xCB29`; the `+0x660` row of "Map-slot tables"
      in `docs/ecu-firmware-analysis.md`). The region test must fail if the
      axes are transposed (mutation check).
    - `scale: 1.02, rounding: nearest` on a 128 cell gives 131 (130.56 →
      131). The same with `floor` gives 130.
    - Every refusal above is asserted by name.
    - An `.OsirisROM` file route and a spec route that make the same bytes
      give identical candidate image sha256.
    - Clean-process test: importing the module loads no ECU-facing package.
  *Done 2026-10-15:*
  - **Module.** `src/analysis/rom_edit_spec.py` (layer 5). `EditSpec` /
    `BaseRef` / `Edit` / `Region` are pydantic, `extra="forbid"`;
    `parse_edit_spec` / `load_edit_spec` turn the schema's own refusals into
    `EditSpecRefusalError` (a `RomEffectRefusalError`) by reason, anything
    else `spec_invalid`. `resolve_base` takes a revision through
    `fuel_comp_rom.RevisionImageResolver` (its refusals keep their reasons)
    or a file through `rom_image.load_image` plus its plain-image sha256.
    `resolve_edits(spec, rom_map, image)` returns only the cells whose value
    changes, already accepted by `make_candidate`. `build_candidate` and
    `load_candidate_file` both go through `compare_roms` and give one
    provenance shape: route, label, base identity, both hashes,
    `spec_sha256` (canonical content, so comments do not change it), the
    per-edit records (selected / changed / unchanged cells, region axes,
    indices and breakpoints), `cells` (old → new raw, by address), `ranges`
    and the effect outcome. An identical candidate file is accepted, with no
    cells and no ranges (E08.7's negative control).
  - **Additions beyond the text, each enforcing a constraint:** a region
    must state `units` for each bounded axis, equal to the axis decode's
    unit (`raw` without one), so a range in raw counts is refused
    (`unit_undeclared`, `unit_mismatch`), not read as %. The rpm axis is the
    one whose decode quantity is `rpm`, never "the rows". `scale` is exact
    decimal on the factor as written: 100 × 1.1 `ceil` is 110, where float
    gives 111. `nearest` rounds a half up: 50 × 1.01 = 50.5 → 51 (half-even
    would give 50). On a masked table (the timing trims, `Decode.mask` 0x7F,
    bit 7 a flag the ECM strips) `add_raw` / `scale` act on `raw & mask` and
    keep `raw & ~mask`, so the N01 spark-trim retard is expressible. A
    masked result outside `0..mask` is `value_out_of_range`, never carried
    into the flag, and a `set_raw` that would change a bit outside the mask
    is `masked_bits_changed`. A mask that is not contiguous low bits refuses
    arithmetic (`arithmetic_on_masked_cells`); none exists in the Xterra map
    today. A spec built on another base is `base_mismatch`. A candidate `compare_roms` cannot trace
    is refused by its reason (`wrong_program`).
  - **Tests.** `tests/test_rom_edit_spec.py`, 72 passing. Hand-worked
    Fuel Comp axes are written into the modelled synthetic image (rpm raw
    12…128 ×50, fill 16…144 ×100/224, cells 128), and so are the slot-1
    `timing_trim_a` cells (row 0 0x85, 0x05, 0xFF, 0x00; rows 1–15
    alternating flagged 0x94 / plain 0x14):
    - `test_cells_set_raw_names_exactly_its_cells`,
      `test_all_add_raw_on_an_absolute_axis_table`,
      `test_a_fuel_comp_region_selects_rpm_rows_and_fill_columns` (rpm
      [1000, 1800] × load [20, 30] % → rows 2–6 × cols 4–6, 15 cells,
      128 → 131), `test_region_bounds_are_inclusive_and_one_sided`,
      `test_a_breakpoint_inexact_in_float_is_still_inside_its_exact_bound`
      (fill raw 24 decodes to 10.714285714285715; the bound 75/7 =
      10.714285714285714 keeps column 1).
    - Transposition: `test_the_region_test_fails_under_transposed_axes`
      swaps the table's axes and gets rows 4–6 × cols 2–6. Mutating the code
      as well was checked: swapping the row/col assignment fails 5 tests, and
      taking rpm as "the rows" fails the transposed test with
      `unit_mismatch`.
    - `test_scale_rounding_is_declared_and_exact[nearest|floor|ceil]`: 128 →
      131 / 130 / 131.
    - `test_scale_is_decimal_exact_and_nearest_rounds_half_up`,
      `test_a_tie_with_an_even_floor_rounds_half_up` and
      `test_a_scale_that_leaves_some_cells_is_not_a_no_op` (10 × 1.02 stays
      10 and is recorded as unchanged).
    - Masked trims: `test_arithmetic_on_a_masked_trim_keeps_the_flag`
      (`add_raw -3`: 0x85 → 0x82 and 0x05 → 0x02; scale ×2: 0x85 → 0x8A;
      rows 1–15 retarded by 3 give 120 × 0x91 and 120 × 0x11),
      `test_set_raw_that_keeps_the_flag_is_allowed` (0x90 on 0x85 passes;
      0x05 refused, naming the cell), `test_a_gapped_mask_refuses_arithmetic`.
    - Refusals by name: `test_schema_refusals_are_named` (20 cases, every
      `SCHEMA_REASONS` entry, which
      `test_every_named_schema_reason_is_exercised` pins) and
      `test_resolution_refusals_are_named` (22 cases: `table_unknown`,
      `slot_invalid`, `cell_outside_table`, `region_empty`, `unit_mismatch`,
      `axis_not_found`, `no_op_edit`, `value_out_of_range` ×4 on plain cells
      and ×3 on masked ones (past 127, below 0, scale ×30), and
      `masked_bits_changed` ×3, including `set_raw 50` on `all` of the trim).
      `duplicate_edit` is covered three ways: twice in one edit, across two
      changing edits, and edit A selecting [0, 0] but leaving it (10 × 1.02
      rounds back) while edit B sets it. `make_candidate` never sees A's
      cell, so only the spec's own overlap check catches that case.
    - Mutation checks, each reverted, each failing at least one test:
      half-even instead of half-up (the even-floor tie fails); dropping the
      cross-edit overlap check (the A/B duplicate case fails); zero `_inside`
      tolerance (the inexact-breakpoint test fails); not preserving the
      flag bits (the masked-trim test fails); dropping the `set_raw` mask
      check (4 tests fail).
    - Also by name: `axis_unverified`, `no_op_edit` naming the cells,
      `value_out_of_range` unclamped from `make_candidate`, the UpRev backup
      (`image_unreadable` with its format name) on both routes,
      `image_hash_mismatch`, `image_not_found`, `no_surviving_image`,
      `revision_unknown`, `revision_resolver_missing`, `base_mismatch` and
      `wrong_program`.
    - `test_the_file_route_and_the_spec_route_make_the_same_candidate`: an
      `.OsirisROM` of the spec's bytes gives the same sha256, cells, ranges,
      effect and provenance keys.
    - `test_resolving_and_building_requests_no_ecu_write`: the clean-process
      import test, covering load, resolve, build, write and the file route.
  - **Example spec.** `tests/fixtures/rom_edit_spec/fuel_comp_light_load.yaml`.
    A private check on the real ZS44A-20261004 image (resolved through the
    registry, slot 1) gives rpm rows 2–3 (1200, 1600) × fill cols 2–3
    (21.43, 28.57 %), 5 cells changed, e.g. 130 → 133. The file route on
    those bytes gives the same sha256, cells and ranges. Scratch only, not
    in the suite.

- [x] **E08.2 — Operating-point designs: where the simulated engine runs.**
  A frozen, hashed set of `OperatingPoint`s, each with a weight, built either
  from pre-change drives or from a grid.
  - **Drive design** (`build_drive_design(sessions, profile_dir, base_revision)`):
    - Sessions must all be recorded on `base_revision` (resolve each session's
      start time against `tune_revisions.yaml` flash times). Otherwise
      `drive_not_on_base_revision`, naming the session.
    - **Points are warm steady reads:** the fuel plant's own steady rule,
      `fuel_plant_eval.build_samples` with `state == "eligible"`, so
      eligibility is one definition shared with E07.3, not a second one.
      `post_fuel_cut`, `cold`, `transient`, `lambda_rail`, `missing_input`
      and `not_co_observed` reads are counted per state, not dropped
      silently.
    - **Fields:** rpm, maf_gps, baro_kpa, iat_c, coolant_c and battery_v come
      from the read. rail_kpa is the AEM channel when measured; otherwise the
      plant's rail working value, with `rail_kpa_source: working_assumption`.
      map_kpa comes from the plant's own manifold model at that point, with
      `map_kpa_source: <manifold_model>` (no MAP sensor exists). throttle_pct
      is PID 0x11 when co-observed within the pairing tolerance; otherwise the
      point is refused (`throttle_unpaired`). pedal_flag is stated 0 unless
      logged, recorded as `pedal_flag_source`.
    - **The fill byte** `fuel_comp_load_byte` is the reconstructed interval
      from `fuel_comp_samples.session_samples` (N02), joined on the same read
      time. A point without an interval is unplaced under N02's own reason.
      The interval is carried whole (`fill_byte_lo`, `fill_byte_hi`), and
      E08.3 decides how it is sampled. It is never collapsed to a midpoint.
    - **Weight** is dwell time: the interval to the next eligible read,
      capped at the steady rule's window, so a 2 Hz drive and a 15 Hz drive
      weight the same driving equally. Never count of reads.
    - **Regime** comes from the read's FUEL_STATUS / commanded φ exactly as
      the plant labels it (`closed_loop`, `enrichment`, `unknown`), so E08.4
      can split results by regime.
  - **Grid design** (`build_grid_design(rpm_edges, load_edges, ...)`): one
    point per cell centre of a stated grid in Fuel Comp's axis units. Every
    environmental field is stated in the call. There is no default baro or
    IAT (`environment_unstated`); the operator's home is ~80 kPa and trips
    reach 63 kPa, so neither is assumed. MAF is solved from rpm and the
    stated load through the plant's manifold model at those conditions.
  - **Artifact:** a `SimDesign` (pydantic) with `kind: drive|grid`,
    `base_revision`, `base_image_sha256`, the sessions and their revision
    check, per-state counts, the points table, and `sha256()` computed over
    the canonical content, never supplied. `write_design(design, root)`
    writes `<root>/<sha>/design.parquet` + `design.json`, refusing
    `inside_checkout` (same rule as `write_candidate`). Every
    `OperatingPoint.source` produced from it is `design:<sha256>`, which
    passes `_refuse_recorded_sources` without any change to it. A test
    asserts that a source string naming a session would still be refused.
  - **Accept:**
    - On `tests/fixtures/fuel_plant/_capture-tuning-2026-09-23T142926` (the
      committed slice): every read is placed or counted under a named state,
      and the counts sum to the reads.
    - Weights sum to the eligible dwell time (± one window).
    - Building twice gives the same sha256. Changing one point changes it.
    - A session recorded after the base revision's successor flash is refused
      by name (use `tune_revisions.yaml`'s real flash times).
    - The grid design refuses unstated environment by name.
    - A design round-trips through `write_design` / `load_design`
      byte-identical.
  *Done 2026-10-15:*
  - **Module.** `src/analysis/sim_design.py` (layer 5). `SimDesign`,
    `DesignPoint`, `SessionCheck`, `GridSpec` and `GridEnvironment` are
    pydantic, `extra="forbid"`, frozen. `sha256()` hashes `canonical_json()`
    (sorted keys, no NaN). The model has no hash field. Refusals are
    `SimDesignRefusalError` with a stable `reason`.
    `design.operating_points(fill_bytes)` / `operating_point(i, fill_byte=)`
    give `paired_scenarios.OperatingPoint`s with `source: design:<sha256>`
    and `conditioned: {fuel_comp_load_byte: b}`. A byte outside the point's
    interval is refused (`fill_byte_outside_interval`), so E08.3 draws inside
    the interval and can never use a midpoint the design made up.
    `require_base(design, image_sha256)` is the run's
    `design_revision_mismatch` check, for E08.3/E08.5 to call.
  - **Drive design.** Each session is dated by `tune_revisions.
    session_dated_at` (its first sample). The revision at that start and at
    every pulse read (`revisions_at`) must be the base, so a session that
    runs on past the next flash is refused too. Sessions are ordered by that
    parsed time, never by name. Reads come from `build_samples` then
    `apply_manifold_refusals`, the same order `evaluate` uses. Counts are
    kept in two layers: `plant_states` covers every read and sums to
    `reads`; `placement` covers every eligible read and sums to
    `plant_states["eligible"]`. A placement is `placed`,
    `throttle_unpaired`, `throttle_closed_position_unestimated`,
    `fill_byte_unpaired`, `fill_byte:<N02 state:reason>` or
    `map_unresolved`, checked in that order. Each `SessionCheck` carries the
    session's own counts, `eligible_dwell_s`, `placed_weight` and N02's
    closed-position estimate.
    - **Throttle** (operator, 2026-10-15). It is PID 0x11 paired one-to-one
      within `pair_tolerance_s` (`throttle_pct_source: pid:0x11`). Otherwise
      it is DID 0x120F on the pulse DID's Mode 22 cycle
      (`did:0x120F`). The DID read is corrected by N02's own
      `estimate_closed_position` (rpm and the first captured coolant source,
      as N02 picks it) and `throttle_counts_did`. The counts interval goes
      through PID 0x11's own handler scaling, from the declaration's
      `throttle_pid` constants, into `throttle_pct_lo/hi`; `throttle_pct` is
      their centre. This is a contract boundary only, and nothing in the
      paired slice computes from it. A DID with no closed-position estimate
      is `throttle_closed_position_unestimated`. `throttle_unpaired` means
      neither route has a read.
    - **Battery** (operator, 2026-10-15). It is the module voltage within the
      plant's `battery_max_age_s`, as `build_samples` pairs it
      (`battery_v_source: measured`). Without one, the point is still
      placed, at the caller's `battery_v_when_unmeasured`
      (`battery_v_source: unmeasured`). That value is recorded in the design
      and the hash, and only when some point used it. A drive that needs it
      and was not given it is refused (`battery_v_unstated`). The rule lives
      in one function, `_battery_v`. `summary()` counts
      `battery_unmeasured_points`. E08.3 marks the absolute pulse
      unsupported for those points.
    - **Fill byte.** It comes from N02's closed-loop-route rows, one per
      STFT read, through `points_from_samples`, paired one-to-one with the
      pulse read within `pair_tolerance_s`. That route places a byte
      whatever the error route's gates say. A byte reconstructed on any
      image other than the base revision's registry hash is refused
      (`fill_image_mismatch`).
    - **Dwell and MAP.** Dwell is the capped gap to the next eligible read;
      the last eligible read gets the cap. The dwell of unplaced reads is
      kept by reason (`unplaced_dwell_s`), so placed weight plus unplaced
      dwell is `eligible_dwell_s` exactly. `map_kpa` is
      `predict(...).map_kpa` on the read's own plant inputs. Every point
      carries `map_kpa_source`, `rail_kpa_source`, `battery_v_source` and
      `throttle_pct_source`, all in the points table and the hash, so E08.3
      can tell model and working-assumption values apart.
    - **Further refusals:** `no_sessions`, `duplicate_session`,
      `session_missing`, `session_undated`, `base_revision_unknown`,
      `base_image_unknown`, `fuel_type_unknown`, and `synthetic_as_observed`
      (from `check_origin`).
  - **Grid design.** `load_edges` are in Fuel Comp's column axis's own
    decode (`fuel_comp_flow_axis`, %, found through
    `fuel_comp_fit.yaml` `rom.fuel_comp_kind`), refused outside 0–100 %
    (`load_outside_model`) and when not strictly rising
    (`grid_edges_invalid`). Every value is a keyword with no usable default:
    baro, IAT, coolant, battery, rail (a number or `"working_assumption"`),
    throttle and pedal flag. Any missing one is `environment_unstated`,
    naming the fields. Turning a fill-% load into airflow needs a stated
    `load_basis` (`load_basis_unstated`). It has one value,
    `fill_fraction_as_manifold_ratio` (kept as a stated, hashed assumption:
    operator, 2026-10-15): MAP = fill/100 × baro, and MAF is found by
    bisection on the plant's own `predict` until its MAP is that value.
    After solving, a cell whose MAP is not the target within
    `GRID_MAP_REL_TOLERANCE` (1e-9 relative) is counted, never placed: above
    the target is `load_below_model_floor`, fuel-mv-3's residual-gas floor
    (verifier finding: such cells had been placed at 4.17 kPa for a 2.0 kPa
    target, worst error 2.17 kPa); anything else is `map_unresolved`. A cell
    the manifold model refuses is counted (`outside_ve_fit`), not placed.
    The byte at a cell centre is the whole counts either side of it, never
    rounded.
  - **Storage.** `write_design` writes `<root>/<sha>/design.parquet` (an
    explicit pyarrow schema) and `design.json`, and refuses `inside_checkout`
    as `write_candidate` does. `load_design` refuses content whose hash
    differs from the recorded one or the directory name
    (`design_hash_mismatch`), and a directory missing either file
    (`design_missing`).
  - **Tests.** `tests/test_sim_design.py`, 36 passing (`pytest -n0`, about
    41 s). The bare committed slice logs no PID 0x11, DID 0x120F or module
    voltage, so the placing tests use a copy of it in `tmp_path` with stated
    reads added, labelled as scaffolding: a 20 % THROTTLE_POS and a 14.1 V
    CONTROL_MODULE_VOLTAGE read at each RPM read, and optionally a DID
    0x120F read on each pulse-DID cycle, raw 130 at idle and 300 off idle.
    N02 gets an invented Fuel Comp calibration under the base revision's
    registry hash. Adding the battery voltage changes the plant's dead-time
    source, so the copy's states differ from the bare slice's.
    - `test_every_read_of_the_committed_slice_is_placed_or_counted_by_name`:
      1,596 reads, `plant_states` equal to `build_samples`' value counts
      (eligible 152, fuel_cut 106, lambda_rail 16, missing_input 1,
      post_fuel_cut 462, transient 859). Placement is
      `{throttle_unpaired: 152}`, with 124.04 s of dwell under that reason.
    - `test_a_placed_design_accounts_for_every_eligible_read_and_its_dwell`
      and `test_weights_are_dwell_to_the_next_eligible_read_capped_at_the_window`.
      The second recomputes the dwell by brute force from the plant's own
      frame (equal to 1e-9), checks it lies within one window of the
      uncapped sum, checks each point's weight against its read, and checks
      that `map_kpa` equals the plant's MAP exactly.
      `test_a_slow_drive_and_a_fast_drive_weigh_the_same_driving_equally`
      checks that 2 Hz and 15 Hz reads over 60 s give equal dwell to within
      one cap.
    - `test_sessions_are_ordered_by_their_data_not_their_names`: two
      sessions whose names sort opposite to their data's start. It checks
      the time order of `sessions` and of the points, the per-session
      counts, dwell and weight against a one-session build, and that the
      design's counts are the doubled sums.
    - `test_the_hash_is_computed_stable_and_moves_with_one_point`: two
      builds give the same sha256. Moving one point's rpm by 12.5, or halving
      one weight, changes it. The grid case is checked too.
      `test_per_point_sources_are_in_the_points_table_and_the_hash`: the
      four `*_source` columns are in the parquet and in the canonical JSON,
      and changing any of `map_kpa_source`, `rail_kpa_source` or
      `battery_v_source` changes the hash.
    - Throttle:
      `test_without_pid_0x11_throttle_comes_from_did_0x120f_through_n02s_closed_position`
      (closed position [98, 107] from mode 130; each point's `lo/hi` equals
      the handler worked by hand),
      `test_pid_0x11_is_used_where_co_observed_and_the_did_elsewhere`, and
      `test_a_did_with_no_closed_position_estimate_is_named_apart_from_no_throttle`.
    - Battery:
      `test_an_unmeasured_battery_is_placed_at_the_stated_voltage_or_refused`
      covers `battery_v_unstated`, the placed `unmeasured` points, the
      summary count, the hash moving with the stated value, and no recorded
      value when every read was measured.
    - `test_a_drive_after_the_base_revisions_successor_flash_is_refused_by_name`
      runs the slice against base `ZS44A-k106`, whose successor
      ZS44A-20260920 was flashed 2026-09-21T03:08:45Z, and also against a
      later base. Both refuse `drive_not_on_base_revision`, naming the
      session and the revision it ran on.
      `test_a_drive_straddling_the_successor_flash_is_refused` shifts the
      slice to start at 2026-10-04T23:00Z, before the 23:02:33Z flash.
    - `test_the_grid_refuses_an_unstated_environment_by_name` (each field),
      `test_a_malformed_grid_is_refused_by_name` (5 cases),
      `test_grid_maf_is_solved_through_the_plants_own_manifold_model`
      (fuel-mv-1 and fuel-mv-3: MAP = load × baro to 1e-9, MAP recomputed
      from the solved MAF), and
      `test_a_grid_outside_the_fitted_ve_range_is_counted_not_placed`
      (fuel-mv-3 at 63 kPa gives `outside_ve_fit` 12).
      `test_a_grid_cell_below_the_manifold_models_floor_is_counted_not_placed`
      uses fuel-mv-3, baro 80 and load edges [0, 5, 25]: column 0 gives
      `load_below_model_floor` 2, and the placed cells sit at their target to
      1e-9. fuel-mv-1 places all 4.
    - `test_a_design_round_trips_byte_identical[drive|grid]`: the loaded
      design equals the original, its canonical JSON is identical, and
      writing it again gives byte-identical `design.json` and
      `design.parquet`.
      `test_writing_is_refused_inside_the_checkout_and_a_tampered_design_on_load`.
    - `test_design_sources_pass_the_recording_guard_and_a_session_source_is_still_refused`
      calls the unchanged `_refuse_recorded_sources` with drive and grid
      design points, then with one point whose source names
      `_capture-tuning-2026-09-23T142926`, which is refused as
      `recorded_value_refused`.
      `test_design_points_run_through_the_paired_comparison_unchanged` runs
      `run_paired` over a grid design's points.
    - `test_a_fill_byte_outside_the_points_interval_is_refused`,
      `test_a_run_on_another_base_image_is_refused`,
      `test_a_fill_byte_reconstructed_on_another_image_is_refused`,
      `test_drive_inputs_are_refused_by_name`,
      `test_without_the_image_every_point_is_unplaced_under_n02s_reason`.
      N02 names a row by its first failing state, so on the slice that is
      `input_missing:obd:FUEL_STATUS`, not `rom_image:*`.
    - `test_building_and_writing_designs_loads_no_ecu_facing_package`
      (clean process).
    - Private images:
      `test_the_real_base_image_reconstructs_fill_bytes_on_the_augmented_slice`
      uses `OPERATOR_ROMS["r20260920"]` and skips when it is absent. It ran
      here before the rework: 162 of 165 eligible reads placed (3
      `fill_byte_unpaired`), with 125.32 s of 130.50 s dwell placed and
      fill-byte widths of median 6 and max 8 counts. The fill byte needs an
      image only for its curves. Without one, the committed tests pass a
      synthetic calibration callable as `resolver=`.
  - **Real drives on ZS44A-20260920** (laptop, `~/canary-data`, real
    images; scratch only, after the rework).
    `_capture-tuning-2026-09-23T142926`, `_capture-tuning-2026-09-23T164007`
    and `_capture-ram-study-2026-10-04T204557` hold 4,693 reads: eligible
    590 (402.3 s of dwell), transient 1,827, post_fuel_cut 1,269, fuel_cut
    428, cold 334, not_co_observed 189 (the ram study polls no MAF),
    lambda_rail 45, missing_input 11. None of the three logs the module
    voltage.
    - Without `battery_v_when_unmeasured` the build is refused
      (`battery_v_unstated`, naming `_capture-tuning-2026-09-23T142926`).
    - With 14.0 V stated (22.8 s): **298 points, 210.0 s of the 402.3 s
      eligible dwell**. All 298 have `throttle_pct_source: did:0x120F` and
      `battery_v_source: unmeasured`. By regime the weight is closed_loop
      182.9 s, enrichment 3.0 s, unknown 24.1 s. Fill-byte width is median
      57 and max 69 counts on N02's DID route.
    - Unplaced: `fill_byte:unpaired:pid_0x11` 170 (115.5 s),
      `throttle_unpaired` 73 (39.5 s), `fill_byte:unpaired:did_0x120F` 32
      (21.7 s), `fill_byte_unpaired` 8 (10.2 s), and the bank-2 trim
      unpaired 9 (5.4 s).
    - Per session: 09-23T142926 places 298 of 358 (210.0 s of 251.9 s);
      N02's closed position is [93, 102] from mode 125 over 423 warm-idle
      reads. 09-23T164007 places 0 of 232. Its single PID 0x11 read makes
      N02 take the PID route for the fill byte, so 170 reads are
      `fill_byte:unpaired:pid_0x11`. 10-04 ram study: no eligible read.

- [x] **E08.3 — The Monte Carlo engine: paired draws over declared uncertainty.**
  - **Uncertainty declaration** `data/nissan-xterra-2008/sim_uncertainty.yaml`
    (schema in `sim_montecarlo.py`, pydantic, `extra="forbid"`):
    - `plant_version` (must match the run's plant).
    - Per plant-registry parameter that carries a bracket: `distribution`
      (`uniform` | `triangular` with `mode` | `truncated_normal` with `sigma`,
      truncated at the bracket), its bracket (copied from the registry and
      **checked equal**, `bracket_mismatch` otherwise), and `evidence`.
    - The lumped scale uses the frozen baseline's bootstrap interval, which
      is a measured interval, not a bracket. Declare its form with evidence
      too.
    - A registry parameter with a bracket but no entry here is refused
      (`distribution_undeclared`). So is an entry for a parameter the
      registry does not have (`parameter_unknown`).
    - Start the Xterra file with `uniform` everywhere, with evidence "bracket
      only: no measurement says where inside", except where E07.3 states
      otherwise.
  - **Input uncertainty:** the fill byte interval from the design is sampled
    uniformly over its whole counts, declared in the same file
    (`fill_byte: uniform_integer`) with evidence. The rpm word is
    deterministic.
  - **Sampling:** a seeded generator (`numpy.random.Generator(PCG64(seed))`);
    `draws` and `seed` are required arguments, never defaulted. Each draw is
    one parameter vector shared by **both** ROMs at **every** point (common
    random numbers). The seed, draw count and the uncertainty file's sha256
    go in the provenance.
  - **The paired hook:** add an optional `plant_overrides: Mapping[str,
    float]` to `paired_scenarios.run_paired` (passed to the plant's
    `resolve_parameters(..., overrides=)`), defaulting to none. Every existing
    caller and test is unchanged, and `tests/test_paired_scenarios.py` stays
    green unmodified. This is the only edit to `paired_scenarios.py`.
    Overrides of a parameter the plant does not declare are refused there
    (`override_unknown`).
  - **Speed, measured before optimising:**
    1. First measure `run_paired` per point on the laptop with a 1,000-point
       design. Record the number in this item.
    2. The ECU side (BFS, X, Target AFR, pulse words) does not depend on plant
       parameters. Compute it once per (point, fill byte value) and cache it.
       There are at most 256 byte values per point.
    3. Then vectorise the plant over draws. A test asserts the cached and
       vectorised path equals the plain `run_paired` loop exactly on a small
       design and a few draws.
    4. Target, recorded here once measured: 1,000 draws × 2,000 points in
       under 60 s on the laptop. Rust is out of scope until this is measured
       and missed (E06).
  - **Output:** `MonteCarloResult` with, per point × draw × output: base,
    candidate, delta, relative delta, and the per-point support status and
    reasons from `run_paired` (support does not vary by draw: assert it). It
    also carries the provenance (both image hashes, design sha, uncertainty
    sha, plant version and baseline sha, contract sha, seed, draws, tool
    version) and the standing caveats (`structural_error_not_sampled`, the
    plant's qualification reason, every `assumptions` string from
    `run_paired`). Traces use a `_sim-*` session name
    (`src.session.is_synthetic_session`) and stay out of evidence and
    training as E07.6's do.
  - **Accept:**
    - Negative control: an identical candidate gives every delta exactly
      0.0 at every point and draw.
    - Scale cancellation: draws that vary only `stoich_air_per_open_ms` give
      a relative Δλ spread of exactly 0 (to 1e-12) for a Fuel Comp edit in
      open loop. A parameter that does not cancel, e.g. a dead-time offset
      on a short pulse, gives a nonzero spread. Both are asserted, so the
      cancellation claim can fail.
    - Same seed, same result, byte-identical. A different seed changes the
      draws but not the negative control.
    - `distribution_undeclared`, `bracket_mismatch`, `parameter_unknown`
      and `override_unknown` are asserted by name.
    - Cached and vectorised equals plain loop exactly.
    - The timing measurement is recorded here.
  *Done 2026-10-15 (reworked the same day after verification):*
  - **Module.** `src/analysis/sim_montecarlo.py` (layer 5).
    `run_monte_carlo(kit, template, design, base_image, candidate_image,
    uncertainty, *, draws, seed, vary=None)`. `template` is a
    `PairedScenario` with no points: ROM identities, plant identity, learned
    state, events. The points come from a `MonteCarloDesign`: `sha256`,
    `base_image_sha256`, an optional `plant_version`, and `DesignPoint`s, each
    an `OperatingPoint` plus `fill_byte_lo` / `fill_byte_hi` and `weight`.
    `MonteCarloDesign.from_sim_design(design)` adapts E08.2's `SimDesign`. It
    was written against `sim_design.py` as it stood on 2026-10-15, while E08.2
    was in rework.
    - Each point is `operating_point(i, fill_byte=lo)` with `conditioned`
      emptied, carrying the interval, the weight, the design sha, the base
      image sha and the plant version.
    - Sources are mapped explicitly, never by fallback:
      - a `map_kpa_source` naming a manifold model becomes `plant_model`;
      - `measured` or a `read:<channel>` source becomes `measured`;
      - `stated` stays `stated`;
      - anything else is `map_source_unknown`.
    - Rail and battery sources must be one of the known labels, else
      `rail_source_unknown` / `battery_source_unknown`.
    - A design built on another plant is `design_plant_mismatch`.
  - **Declaration.** `data/nissan-xterra-2008/sim_uncertainty.yaml`
    (`fuel-mv-1`) declares 7 parameters: 6 bracketed working assumptions (the
    λ delay among them) and the lumped scale.
    - The 6 working assumptions are `uniform`, evidence "Bracket only: …".
    - `stoich_air_per_open_ms` is `bracket_from: baseline_interval` [0.050591,
      0.051501], `truncated_normal` mean 0.051063, σ 0.0002766. E07.3 states
      this as a 90 % bootstrap interval, a measured interval, so σ makes the
      normal's central 90 % span the interval's mean half-width, and the
      normal is truncated at the interval.
    - `inputs.fill_byte: uniform_integer`.
    - Every entry needs `bracket_from` (`working_assumption` |
      `baseline_interval`). Both it and the bracket are checked against the
      kit's plant (`bracket_mismatch`).
    - Each form takes exactly its own shape fields: `triangular` needs
      `mode`, and `truncated_normal` needs `mean` as well as `sigma`.
    - Also refused: `parameter_not_bracketed` (an entry for a known
      parameter) and `uncertainty_plant_mismatch`.
  - **The paired hook** (`src/analysis/paired_scenarios.py`).
    `run_paired(..., plant_overrides=None)` resolves through
    `plant_parameters(kit, overrides)`.
    - Refusals: `override_unknown` (a parameter the plant does not read) and
      `override_outside_bracket`.
    - Working assumptions go to `resolve_parameters(overrides=)` unchanged.
    - `resolve_parameters` refuses an override of an identified value, so the
      lumped scale is applied in `plant_parameters`, inside the frozen
      baseline's interval.
    - Sensitivity cases run on top of the draw (`{**overrides, **case}`), and
      `parameter_band` uses the drawn s.
    - A run with overrides folds them into `scenario_sha256`, so it has its
      own `_sim-*` session. Two draws never share one, and neither does a
      draw and the frozen plant.
    - `rank_as_improvement` refuses a result with overrides as
      `plant_overrides_not_frozen`.
    - With no overrides and default point sources the bytes are HEAD's: the
      verifier's 10-case `to_dict()` comparison against HEAD's module is
      equal, with no overrides and with `plant_overrides={}`.
      `tests/test_paired_scenarios.py` 16 pass, unmodified.
  - **MAP and rail reach the spread.**
    - **Point sources.** `OperatingPoint` gains three defaulted sources:
      `map_kpa_source` (`stated` | `measured` | `plant_model`),
      `rail_kpa_source` (`stated` | `measured` | `working_assumption`) and
      `battery_v_source` (`stated` | `measured` | `unmeasured`). A
      non-default source is folded into the scenario hash.
    - **What the plant gets.** `_fuel_chain` and the fast path build the plant
      input the same way:
      - `plant_model` passes `map_kpa=None`, so the plant's manifold model sets
        MAP from the airflow at each draw's VE;
      - `working_assumption` passes rail NaN (`plant_rail_kpa`), so the drawn
        rail applies;
      - stated and measured values stay held boundaries.
    - **Contract** (`engine_sim_contract.yaml`, `sim_contract.py`). The
      `Assignment` gains `unstated: internal_model`, allowed only on a
      scenario-prescribed boundary.
      - `conditional_rom_comparison.manifold_pressure` declares it. A
        `plant_model` point leaves MAP out of its contract scenario.
      - `_check_unstated` requires an internal model of the quantity for that
        mode and the contract's plant version. fuel-mv-1's MAP internal model
        now lists `conditional_rom_comparison` beside `plant_replay`.
      - `check_scenario` does not ask for an unstated boundary. A stated
        (measured) MAP is still a boundary.
      - `tests/test_engine_dynamics.py`'s internal-model assertion is updated
        to match.
    - **Refusing manifold models.** A plant whose manifold model refuses MAP
      per sample, run with `plant_model` points, is
      `manifold_refusal_varies_by_draw`. Its refusals would vary with the VE
      draw. `_fuel_chain` names such a refusal as `plant:<reason>`.
    - **The rail caveat.** `resolve_parameters().effects` leaves out the rail
      effect, so when any point's `rail_kpa_source` is `working_assumption`,
      `run_paired` and `run_monte_carlo` both add
      `plant:rail_pressure_unmeasured` to `unresolved`. With the default
      `stated` it is absent, and HEAD equality holds.
  - **Battery (operator decision).** At a point with
    `battery_v_source: unmeasured`, only `injector_pulse_ms` is unsupported,
    with `battery_voltage_unmeasured`, in `run_paired` and the fast path
    alike. λ, fuel and their deltas stay supported: the dead time cancels out
    of the metered part.
  - **Speed** (laptop, `.venv`):
    1. **`run_paired` alone.** Fuel Comp +3 edit, random open- and closed-loop
       points: **5.15 ms/point** at 1,000 points (5.15 s). About 31 ms is
       fixed per call (`compare_roms`). Each point runs `_pair_point` 11
       times: the main run plus 10 sensitivity cases.
    2. **The ECU-side cache.** It is computed once per (point, drawn fill
       byte):
       - `_ecu_chain` runs per combination for each ROM;
       - the integer pulse chain (`predict_pulse`) runs once per ROM over
         every combination, vectorised;
       - `_pair_point`'s loop logic (open loop, the closed-loop hold, a moved
         setpoint) and `run_paired`'s per-output reasons are rebuilt from the
         same helpers.
    3. **The plant.** Each draw runs one vectorised `fuel_plant.predict`
       call per MAP route (stated or model), over every point and both
       ROMs. The draws loop in Python with scalar parameters: the first
       measurement put the cost in the ECU cache (about 2 s of 18.7 s was
       the plant), so vectorising the cache across combinations was the
       exact route that mattered.
    4. **1,000 draws × 2,000 points**, half the points at `plant_model` MAP
       and working-assumption rail:

       | Fill interval | Combinations | Time |
       |---|---|---|
       | 14 counts | 28,000 | **5.0 s** |
       | 45 counts | 90,000 | **14.1–14.8 s** (2 runs) |
       | 69 counts | 138,000 | **21.3 s** in a fresh process; 39.9 s run third in one process, with two earlier 1,000 × 2,000 results still in memory |

       All are under 60 s. The first version, `_pair_point` per combination,
       took 18.7 s and 53.6 s for 14 and 45 counts.
  - **What the runtime self-check covers, and what it does not.** After
    assembly, each run re-runs a set of (point, draw) pairs through
    `run_paired` itself and compares the **assembled** arrays exactly at
    those pairs: base, candidate, the reasons via the support index, and λ
    relative delta. Any difference refuses with `fast_path_diverged`.
    - **Which pairs.** First one combination per loop kind, then one per
      distinct support signature, then any, up to 8 combinations. Each is
      taken at the first draw that visits it, plus the last draw at point 0.
      `run_paired` runs once per distinct draw, at that draw's parameters
      and drawn bytes.
    - **Not covered.** It does not check (point, draw) pairs outside that
      set. A fault confined to other draws is caught only by
      `test_cached_and_vectorised_equals_the_plain_run_paired_loop_exactly`,
      which compares every pair.
    - **Mutations tried.** Shifting the cached dead time by one count trips
      it. So does swapping base and candidate in the plant pass
      (`test_a_wrong_assembly_is_refused_by_the_run_paired_check`).
  - **Support** is kept per (point, draw), as an index into `reason_sets`.
    - It can vary with the drawn fill byte, which is an input.
    - It cannot vary with the plant draw: a supported output that is not
      finite at some draw is `support_varies_by_draw`.
  - **Result contents.**
    - Per point × draw × output: `base` and `candidate` (NaN where
      `run_paired` gives None).
    - Accessors: `delta()`, `relative_delta()`, `supported()`, `reasons()`,
      `point_support()`.
    - Also carried: `weights`, `fill_bytes`, the drawn `parameters`, `loop`,
      `coverage` (over pairs) and `digest()`. `traces()` is a long frame
      under `_sim-<digest>`.
    - Caveats: `structural_error_not_sampled`, the qualification reason,
      `parameter_uncertainty_only`, `parameter_held:<id>`,
      `parameter_inert:<id>`, then every `run_paired` assumption.
    - `parameter_inert` is measured, not listed: a drawn parameter that moves
      no plant output across its bracket anywhere in the design.
      - With stated or measured MAP and rail, it names
        `lambda_observation_delay`, `manifold_pressure` and `rail_pressure`.
      - With `plant_model` MAP and working-assumption rail, it names only
        `lambda_observation_delay`.
      - At WOT on fuel-mv-1, the VE draw cannot move λ: speed-density MAP is
        capped at baro across the whole VE bracket. Rail still moves it.
    - Provenance holds:
      - both image hashes, the design and uncertainty sha256s;
      - the plant version, the baseline and contract shas;
      - the seed, the draws, `tool_version` 1 and the RNG order;
      - the varied and held parameters;
      - the self-checked pairs, and the per-source point counts.
  - **Tests.** `tests/test_sim_montecarlo.py`, 25 pass. The kit is E07.6's
    synthetic image.
    - `test_the_negative_control_is_exactly_zero_at_every_point_and_draw`:
      every output's delta is 0.0 at every point and draw, at seeds 1 and 2,
      which draw different bytes and scales.
    - `test_the_same_seed_gives_a_byte_identical_result`: the digest, the
      array bytes and the traces are equal; seed 7 differs.
    - `test_the_lumped_scale_cancels_in_relative_delta_lambda_and_dead_time_does_not`
      (40 draws, fill bytes fixed, both points open loop):
      - varying only s, base λ moves (ptp > 1e-4) while the ptp of relative
        Δλ is ≤ 1e-12;
      - varying dead time instead, the short-pulse spread is > 1e-3 and more
        than 10 × the WOT spread, which is > 0.
    - `test_cached_and_vectorised_equals_the_plain_run_paired_loop_exactly`
      runs the plain loop, `run_paired(..., plant_overrides=draw)` at the
      drawn bytes, for every draw.
      - Edits: Fuel Comp, Target AFR (moved setpoint), a gated `fill_curve_low`
        edit, and K.
      - Points (8): WOT, cruise, a short pulse, a pulse at minimum G
        (`short_pulse_undecoded`), cold, `plant_model`-MAP WOT and cruise,
        and unmeasured battery.
      - Equal: every value, delta, reason, status and λ `relative_delta`.
        Support at a fixed byte is identical across plant draws.
    - `test_support_does_not_vary_by_plant_draw`.
    - `test_the_fill_byte_is_drawn_over_its_whole_interval`: every count of
      [190, 194] is drawn; on a column-ramped Fuel Comp, BFS moves with the
      byte.
    - Rework 1, overrides:
      `test_a_run_on_overridden_parameters_is_its_own_run_and_never_ranks`.
      Three distinct shas and sessions. With a qualified plant and a passing
      verdict, the frozen run ranks; an overridden one is refused only as
      `plant_overrides_not_frozen`.
    - Rework 2, assembly:
      `test_a_wrong_assembly_is_refused_by_the_run_paired_check`.
    - Rework 3, MAP and rail:
      - `test_map_and_rail_reach_the_spread_where_the_plant_computes_them`:
        with model MAP and working rail, the VE and rail draws each move part-load λ
        (ptp > 1e-4) and stay supported, and neither is inert. With stated or
        measured sources both are held.
      - `test_an_unstated_map_is_the_contracts_declared_internal_model`: no
        MAP boundary in the contract scenario. With `unstated` deleted from
        the contract, the same point is `boundary_missing`, and a stated MAP
        still runs.
    - Rework 4, battery:
      `test_an_unmeasured_battery_voltage_unsupports_only_the_absolute_pulse`.
      Only the pulse is unsupported. Relative Δλ is the same at 14.0 V and
      12.5 V to 1e-12, and the pulse is not.
    - Rework 5, the adapter: `test_an_e08_2_design_adapts_with_its_sources`
      (an E08.2 grid design) and
      `test_a_design_built_on_another_plant_is_refused`.
    - Second re-verify:
      - `test_an_e08_2_design_with_an_unknown_source_is_refused_by_name`: a
        bogus MAP, rail or battery source on an E08.2 grid point is refused by
        name; `read:…` and `measured` map to `measured`, and `stated` to
        `stated`.
      - `test_the_rail_working_assumption_is_named_in_every_result_that_rests_on_it`:
        present for `run_paired` and the Monte Carlo on working-assumption
        rail, absent on stated rail.
      - `test_a_refusing_manifold_model_names_its_refusal_and_cannot_be_drawn_over`:
        on fuel-mv-2, `_fuel_chain` gives `plant:manifold_above_ambient`
        with NaN λ at 150 g/s and no reason at 20 g/s.
        - A model-MAP design is `manifold_refusal_varies_by_draw`.
        - A stated-MAP design reaches the contract's
          `plant_version_mismatch` instead.
    - Refusals by name:
      - `test_an_undeclared_mismatched_or_unknown_parameter_is_refused_by_name`
        (also the `extra="forbid"` and shape validators);
      - `test_the_paired_hook_refuses_unknown_and_outside_overrides`;
      - `test_the_design_and_template_are_refused_by_name`.
    - Also: `test_provenance_caveats_and_synthetic_traces`,
      `test_draws_and_seed_are_required_never_defaulted`,
      `test_a_named_plant_version_must_match_its_declaration_and_the_contract`
      (with a matching mv-3 declaration, the contract still refuses
      `plant_version_mismatch`),
      `test_a_monte_carlo_run_imports_nothing_that_talks_to_the_ecu` (clean
      process), and
      `test_the_declaration_file_is_plain_yaml_with_every_form_explained`.
  - **Gate.** `ruff check .`, `ruff format --check .` and `mypy src` are
    clean. `pytest tests/test_sim_montecarlo.py tests/test_paired_scenarios.py
    tests/test_fuel_plant.py tests/test_sim_contract.py tests/test_layering.py
    tests/test_engine_dynamics.py`: 141 passed. The verifier's 10-case HEAD
    comparison (`cmp_head.py`) is still equal.

- [x] **E08.4 — Per-cell aggregation that keeps each kind of empty distinct.**
  `aggregate_cells(result, design, axes)` → one row per cell of a stated grid.
  Default: the **Fuel Comp table's own rpm × load breakpoints on the base
  image**, so a result lines up with the table being edited; Target AFR's
  axes are the other built-in choice.
  - **Per cell and output:**
    - n points and dwell weight;
    - dwell-weighted p05/p50/p95 of delta and relative delta over all
      (point, draw) pairs. The quantiles are over pairs, each weighted by its
      point's dwell; this is a distribution of per-sample changes, not a
      comparison of two aggregates;
    - the share of weight per regime;
    - the support breakdown, with reasons.
  - **Status per cell, one of:**
    - `changed`: supported, and |p50| above zero;
    - `unchanged`: supported, and every pair's delta is exactly 0;
    - `unsupported`: points exist but none supported, with the reasons;
    - `partly_supported`: report both shares, and quantiles over supported
      pairs only, labelled;
    - `no_points`: the design never visited it;
    - `not_reached`: `compare_roms` says the change cannot reach this
      output.

    `no_points` and `unchanged` must never collapse. A cell with points but
    no reachable change is `not_reached`, not `unchanged`.
  - **Closed-loop cells** report `fuel_trim_total_pct` (the trim the
    candidate needs at the fixed point), not λ, with the run's
    `closed_loop_trim_authority_undecoded` reason carried as given.
  - **Affected cells overlay:** the edited cells from E08.1's provenance
    (`paired_scenarios.affected_cells`) are marked on the same grid, so
    "edited here, effect shows there" is visible. That includes the axis
    edits that move lookups (`moved_lookups`).
  - **Accept:**
    - Hand-built results exercise every status, including `no_points`
      versus `unchanged` versus `not_reached` on the same grid.
    - Quantiles are verified against a brute-force weighted quantile.
    - Moving one point across a breakpoint moves it between cells and
      nothing else.
    - An aggregate-first path cannot be written: the function takes the
      `MonteCarloResult`, never a pre-reduced frame (type-checked).
  *Done 2026-10-15 (`474c0267`):*
  - **Module.** `src/analysis/sim_cells.py` (layer 5).
    - **Grids:** `fuel_comp_axes(kit, base_image, slot=)` (the default) and
      `target_afr_axes(kit, base_image, slot=)`. `Grid` is `fuel_comp` |
      `target_afr`.
    - **Aggregation:** `aggregate_cells(result, design, axes, effects=)` →
      `CellTable`, a pydantic model with `schema_version: 1`
      (`cell_table_schema()`, `to_dict()` / `to_json()`). Every cell is
      present, row-major, and the validator checks each one against its
      axes.
  - **Departures from the text, each a constraint:**
    - **It takes the E08.2 `SimDesign`**, for the regimes, plus the run's
      `compare_roms` trace as `effects` (for the moved-lookup overlay).
      Inputs that are not the run's are refused: `design_mismatch`,
      `axes_image_mismatch`, `effects_mismatch`.
    - **Membership is the nearest breakpoint in the lookup's own comparison
      domain,** through the firmware's `search_axis`, with ties low. Grid cell
      (r, c) is therefore table cell (r, c), the one the bilinear lookup weighs
      most.
    - **Each (point, draw) pair is placed by the fill byte that draw used,**
      never by the interval's midpoint. On the Target AFR grid, the column is
      the base ROM's X, taken from that pair's base BFS. A pair with no base
      BFS is counted as `unplaced`, never dropped.
    - **`paired_scenarios.rpm_word`** is now the single rpm-word conversion
      the run and the cells share.
    - **`changed`** means any supported pair moved, even where the median is
      zero.
    - **Reach is checked, not trusted:** a nonzero delta on an output
      `compare_roms` calls unreached refuses the table
      (`reach_contradicted`).
    - **The headline follows the loop** (`headline_by_loop`): open loop
      `delivered_lambda`, closed loop `fuel_trim_total_pct` with
      `closed_loop_trim_authority_undecoded` as given, setpoint moved
      `target_phi`.
  - **Tests.** `tests/test_sim_cells.py`, 28 passing (re-run 2026-10-15,
    `pytest -n0`, 3.2 s). The Accept list by name:
    - `test_every_status_on_one_grid_and_no_kind_of_empty_collapses`
    - `test_quantiles_are_dwell_weighted_over_pairs_against_brute_force`
    - `test_moving_one_point_across_a_breakpoint_moves_only_that_point`
    - `test_an_aggregate_first_path_cannot_be_written`

    Others:
    - E08.7's negative control:
      `test_an_identical_candidate_is_unchanged_in_every_visited_cell`.
    - Overlay, Target AFR placement and closed-loop trim tests.
    - The clean-process no-ECU-import test.
  - **Real images:** a 20260920 → 20261004 run visited 53 of 256 cells,
    with the B08.6 pattern. That figure is from the commit message. It was
    not re-run when this box was ticked: the machine that ticked it has no
    ROM images.
    *Re-run 2026-10-15 on the laptop that has the images, through E08.5's
    CLI (200 draws, seed 1, design `322e4cab…`): 53 of 256 cells visited;
    ΔBFS 46 changed, 6 unchanged, 1 unsupported, 203 no_points; result
    digest `956d7bbed7cb…`, stable across runs. The full-load point at
    800 rpm, cell (1, 15), is real reads paired across a tip-in: E07.3's
    steady-rule pairing leak, recorded there.*

- [x] **E08.5 — CLI: `tools/rom_sim.py`.** Offline, laptop or desk, prints
  JSON and writes artifacts under a caller-given `--out`, refusing
  `inside_checkout` (scratch or `/data/canary/sim/…` later in E08.8).
  - `design drives --base-revision R --sessions S... [--sessions-root DIR] --out D`
  - `design grid --rpm-edges ... --load-edges ... --baro --iat --coolant ... --out D`
  - `run --base (--revision R | --rom PATH --sha256 H) (--edits SPEC.yaml | --candidate-rom PATH) --design DIR --draws N --seed S [--plant-version V] --out D`
    writes `result.json` (provenance, caveats, cells), `traces.parquet`
    (`_sim-*`), and `report.html` (E08.6).
  - `inspect --edits SPEC.yaml --base ...`: the resolved cell list (old →
    new raw, ±% per cell) and `compare_roms`'s reached outputs, without
    running anything. This is the "what did I just edit" check.
  - ROM directories come from `--rom-dir` or `$CANARY_ROM_DIRS`, as
    `tools.fuel_comp_restore_reproduction` does. Images never enter git. The
    CLI copies nothing into the checkout.
  - **Accept:**
    - An end-to-end test with a synthetic image pair and a grid design writes
      all three files.
    - Each refusal surfaces with its reason and exit code 2.
    - The clean-process no-ECU-import test covers the CLI.
    - `--help` documents every subcommand.
  *Done 2026-10-15:*
  - **Tool.** `tools/rom_sim.py` (`.venv/bin/python -m tools.rom_sim`), one
    argparse tree: `design drives`, `design grid`, `inspect`, `run`. It reads
    through `load_kit`, `RevisionImageResolver` (`--rom-dir` or
    `$CANARY_ROM_DIRS`), `resolve_base`, `build_candidate` /
    `load_candidate_file`, `load_design`, `run_monte_carlo`, `compare_roms`,
    `aggregate_cells` and `render_report`; it implements nothing of its own
    but argument handling, the run mapping and the file writing. Every
    refusal of any layer prints `{"refused": <reason>, "message": ...}` and
    exits 2. No `src/` module was edited.
  - **Required, no default.** `--draws`, `--seed`, `--design`, `--fuel-type`
    (drives), `--out`, and the grid environment (the library's own
    `environment_unstated` / `load_basis_unstated`) were asked for. Missing,
    these are `argument_missing` naming the flag (argparse is permissive so
    the refusal is data). **Beyond the text, because `PairedScenario` needs
    them and the CLI must not invent them:** `--stft-pct`, `--ltft-pct` (the
    base's learned trims) and `--candidate-events` (`ecu_reflash`, or `none`
    as a stated answer; `none` with others is `candidate_events_ambiguous`).
    The contract's other learned boundaries are held at 0.0 under both ROMs
    and recorded in `result.json` (`inputs.learned.held`). `--grid
    fuel_comp|target_afr` picks E08.4's grid (default Fuel Comp's own).
    `--base` is an accepted marker flag (the checklist writes `run --base
    (--revision R | ...)`); `--edits` alone takes the spec's own base.
  - **`run` outputs**, all under `--out`, which is refused inside or around
    the checkout (`inside_checkout`, checked before any work, `design` too):
    `result.json` holds the tool and stage versions, `result_digest`,
    `inputs`, `caveats`, `unresolved`, `assumptions`, `inert_parameters`,
    `coverage`, the Monte Carlo provenance, E08.1's candidate provenance
    (cells old -> new, ranges, effect), the `run` mapping and the whole
    `cell_table`. `traces.parquet` is `MonteCarloResult.traces()` (session
    `_sim-<digest>`). `report.html` is `render_report(cell_table, run=run)`,
    and `render_report(result['cell_table'], run=result['run'])` rebuilds it
    byte for byte (asserted). The `run` mapping closes E08.6's gaps: label,
    seed, both image labels and plain sha256s, the route, `spec_sha256`, the
    edit rows (spec route: selector and operation as written, cells changed
    and left alone; file route: one row per table the image changed),
    `summary(design)`, plant version and uncertainty sha256.
  - **`inspect`** prints the changed cells (table, slot, row, col, address,
    old, new, delta, % of the old value, on `raw & mask` for a masked trim),
    the provenance's changed ranges, and two `compare_roms` traces: the
    structural reach and the same under the run's contract mode. It builds no
    design, draws nothing and writes nothing.
  - **Tests.** `tests/test_rom_sim_cli.py`, 33 passing (`pytest -n0`; about
    20 s on a quiet laptop, 80 s under the load of three other sessions, so
    the file will land in the slow tier). The world is E07.6's synthetic
    image written beside a copy of the Xterra profile in `tmp_path` whose
    application model is re-pinned to it and whose `tune_revisions.yaml` has a
    `SYNTH-base` revision with its sha256. **Coupling:**
    `tests.test_sim_montecarlo.build_kit_and_image` (and through it
    `tests.test_paired_scenarios`), and the shape of `program.identity.sha256`
    and `revisions` in two profile files, edited as data.
    - `test_a_synthetic_pair_and_a_grid_design_write_all_three_files`: `design
      grid` (rpm 2000 / 2500 / 3000 x load 28.5 / 46 / 65 %, nine points,
      design sha in the path) then `run --edits` with a (4, 4) 128 -> 148
      edit. The three files exist and nothing else; the cell table validates
      through `CellTable` and carries the design, base and candidate shas; the
      caveat block precedes the first `<svg>`; the page equals the re-render;
      the first centre is the edited cell, so ΔBFS is `changed` there and
      `unchanged` in the other eight, with relative p05 above 5 %;
      traces carry one `_sim-*` session and `points x draws x outputs` rows.
    - The run mapping (`test_the_run_facts_...`), the ROM-file route making the
      same cell table as the spec route with its table-grouped edit row
      (`test_the_rom_file_route_...`), byte-identical results for one seed and
      a different file for another, and a spec run with no base flags.
    - `inspect`: (4, 4) 128 -> 148, delta 20, `pct` 15.625 (20 / 128), the
      candidate sha, no file written, BFS reached and `fuel_comp_lookup`
      among the changed nodes; a masked `timing_trim_a` edit is judged on
      `raw & 0x7F`, reaches `ignition_trim_selected` and leaves
      `base_fuel_schedule_word` unchanged.
    - **Refusals, each exit 2 with its reason:** `argument_missing` for each
      of `--draws`, `--seed`, `--stft-pct`, `--ltft-pct`,
      `--candidate-events`, `--design` and `--fuel-type`;
      `environment_unstated` for each of the seven grid values and
      `load_basis_unstated`; `inside_checkout` for `run`, `design grid` and
      `design drives` (the checkout moved to scratch so a regression cannot
      write into the repo, plus the real paths on `_out_dir`, including the
      checkout's parent); `drive_not_on_base_revision`, `session_missing`,
      `base_revision_unknown`; `design_revision_mismatch` (a design on the
      real registry's 20260920 image, a run on the synthetic one),
      `design_missing`, `design_hash_mismatch`; `candidate_unstated` (both and
      neither), `base_unstated`, `base_ambiguous`, `candidate_events_ambiguous`;
      `image_hash_mismatch`, `base_mismatch`, `no_op_edit`,
      `edit_spec_not_found`, `image_unreadable` (the UpRev backup lookalike),
      `image_not_found` (candidate file, and a revision with no image in the
      ROM dirs or `$CANARY_ROM_DIRS`), `profile_not_found`.
    - `test_the_command_line_imports_nothing_that_talks_to_the_ecu`: in a
      fresh interpreter, `design grid`, both `inspect` routes and `run`
      through `tools.rom_sim.main`; no `src.control`, daemon, dashboard, MCP,
      `can`, `serial` or non-allowed `src.collection` module is loaded.
    - `--help`: the top level lists all four subcommands (`design drives` and
      `design grid` included) and the refusal and checkout rules; each
      subcommand's help names every flag, and a parser walk requires help text
      on every option (`%` in help had crashed `--help` once).
    - Mutation checks, each reverted: the checkout refusal off fails both
      checkout tests; the `argument_missing` check off fails the missing
      argument test; `render_report` without `run=` fails the end-to-end test.
  - **Review fixes (verifier, same day).**
    - `--out` is refused inside, around or through a symlink to any
      `--rom-dir` or `$CANARY_ROM_DIRS` entry, as `inside_rom_library`
      (`write_candidate`'s name), in all three writing subcommands; the
      verifier had written `result.json` into the operator's `Tuned ROMs`
      directory with an unguarded `--out`.
    - A non-finite number (`nan`, `inf`) in any numeric argument is
      `argument_invalid`. `run` writes its three files to `.tmp` names and
      moves them into place only when all are staged, so a failure leaves
      nothing of the run (a result that cannot be serialised is
      `result_not_finite`; a move that fails takes back the earlier ones).
    - `--out`, `--base-revision`, `--sessions`, `--rpm-edges` and
      `--load-edges` are no longer argparse-required: a missing one is the
      JSON `argument_missing`, naming all that are missing.
    - A file-route `run.edits` also lists changed bytes outside any table
      (the checksum words, unmapped bytes) as a row `checksum bytes
      0x<start>-0x<end>`, so the report says the image changed there too.
    - The clean-process test also runs `design drives` on the committed
      slice. New tests: `test_the_out_directory_is_refused_inside_or_around_a_rom_directory`
      (flag, environment, parent, symlink; nothing written),
      `test_a_non_finite_number_is_refused_...` (both trims and two grid
      arguments), `test_a_failure_while_publishing_leaves_no_partial_outputs`,
      `test_a_missing_required_argument_is_json_not_usage_text` and
      `test_a_file_route_lists_the_changed_bytes_outside_any_table`.
      Mutations (library check off, finite check off, range row off) each
      fail their test.
  - **Gate.** `ruff check .`, `ruff format --check .`, `mypy src
    tools/rom_sim.py` clean; `pytest tests/test_rom_sim_cli.py
    tests/test_layering.py`: 44 passed.
  - **Real case** (laptop, scratch only, `.../scratchpad/e085/`; ROM dir =
    the parent of `OPERATOR_ROMS["r20260920"]`):
    - `design drives --base-revision ZS44A-20260920 --sessions
      _capture-tuning-2026-09-23T142926 --fuel-type gasoline
      --battery-v-when-unmeasured 14.0`: design `322e4cab...` (the same
      sha256 E08.2 and E08.4 recorded), 298 points, 10.9 s on a quiet laptop.
    - `inspect --edits tests/fixtures/rom_edit_spec/fuel_comp_light_load.yaml`:
      5 cells, e.g. (2, 2) 130 -> 133 (+2.31 %), (3, 3) 128 -> 131
      (+2.34 %), (0, 0) 128 -> 126 (-1.56 %), 2 s. All four traced outputs
      are `unevaluable` (reached, not computable), as E08.4 recorded.
    - `run --base --revision ZS44A-20260920 --candidate-rom
      ZS44A-Tuned-20261004.OsirisROM --design 322e4cab... --draws 200 --seed 1
      --stft-pct 0 --ltft-pct 0 --candidate-events ecu_reflash`: 7.7 s of work
      (9.5 s wall), ΔBFS 46 `changed`, 6 `unchanged`, 1 `unsupported`, 203
      `no_points` and 210.01 s of weight, the counts E08.4 recorded at 50
      draws; `result.json` 0.9 MB, `traces.parquet` 357,600 rows, one
      `_sim-956d7bbed7cb` session, `report.html` 0.8 MB; the run's edit rows
      are 195 cells of `fuel_compensation` and 15 of `fuel_comp_flow_axis`.
      The trims 0 / 0 with `ecu_reflash` were chosen for this case from the
      note that the 2026-09-23 drives ran with LTFT 0.00; they are the
      operator's to confirm.

- [x] **E08.6 — Report page: self-contained HTML.**
  `render_report(result_json) -> str`, no network, inline CSS/SVG, light and
  dark.
  - **At the top, before any chart:** the caveats (plant not qualified and
    why; structural error not sampled; parameter uncertainty only), the two
    image identities, the edit list, design kind and coverage, draws and
    seed.
  - **Heatmaps** on the E08.4 grid for relative Δλ p50 (diverging scale
    centred at 0), the p05–p95 width, ΔBFS and Δpulse. Each status that is
    not `changed`/`unchanged` gets a distinct hatch or label, never a colour
    that reads as a value. Edited cells are outlined.
  - **Per-cell detail** (hover or table): n, weight, quantiles, regime mix,
    and reasons.
  - **Accept:**
    - Rendering a hand-built result contains each status's label and the
      caveat block before the first `<svg>`.
    - No external URL appears in the output (asserted).
    - Rendering is a pure function of the JSON: same JSON, same bytes.
  *Done 2026-10-15:*
  - **Module.** `src/analysis/sim_report.py` (layer 5).
    `render_report(cell_table_json, *, run=None) -> str`.
    - **Input.** The input is E08.4's cell table (`CellTable.to_json()`, its
      bytes, or the parsed mapping). It is validated through `CellTable`, so
      `schema_version` must be 1 and NaN is refused (`cell_table_invalid`).
    - **Standing caveats.** A table without `structural_error_not_sampled`
      and `parameter_uncertainty_only` is refused (`standing_caveat_missing`).
      E08.3 always writes both.
    - **Run facts.** The table does not carry the seed, the image labels, the
      edit list or the design summary. They come in `run` (`ReportRun`,
      `extra="forbid"`). `RUN_KEYS` lists its keys: `label`, `seed`, `base` /
      `candidate` (`{label, sha256}`), `edit_route`, `spec_sha256`, `edits`,
      `design`, `plant_version` and `uncertainty_sha256`.
      - `edits` rows are `{table, slot, selector, operation, changed,
        unchanged}`, with every key stated.
      - `design` is `sim_design.summary()`, key for key.
      - An unknown key is `run_key_unknown`, and a bad value is `run_invalid`.
      - An image sha, design sha, kind, point count or weight that is not the
        table's is `run_identity_mismatch`.
      - Every key is optional. A missing one is printed as "not stated by the
        caller (run.seed)", never guessed.
    - **Purity.** Mappings are walked in sorted order, and nothing reads a
      clock or the environment.
  - **Page order.** The order is fixed:
    1. the caveat block: the plant's qualification and why, the two
       standing caveats explained, every other caveat as given, and every
       `unresolved` string;
    2. both image identities and `rom_outcome`;
    3. the edits: the route, the spec sha, the `run.edits` table, the
       table's overlay (edited cells, moved breakpoints, off-grid tables,
       moved lookups) and the reach per output;
    4. the design and coverage: kind, sha, points, cells visited, weight
       placed and unplaced, and the `summary()` counts when given;
    5. draws and seed, plant version, uncertainty sha, result digest, the
       quantile method and the membership rule;
    6. the maps;
    7. the per-cell detail.
  - **Maps.** Each map is the table's own grid. Ticks are the decoded
    breakpoint values, and axis titles are the axis table and unit
    (`fuel_comp_engine_speed_axis (rpm)`, `fuel_comp_flow_axis (%)`).
    - **Headline maps.** There is one map per loop in `headline_by_loop`,
      drawn from each cell's `by_loop` block:
      - open loop: relative Δλ p50;
      - its p05–p95 width, unsigned, on a single-hue ramp;
      - closed loop: Δ total trim p50, in % points.
      A loop with no pairs in the run gets no map, and the page says so.
    - **Output maps.** Relative ΔBFS p50 and Δpulse p50 (ms), over all
      pairs.
    - **Colour.** The diverging ramp is the dashboard's
      (`lib/colorScale.ts`: blue, pale neutral, red), symmetric about 0, and
      the same in both themes.
    - **Status marks.** Only `changed` / `unchanged` are coloured. Every
      other state has its own pattern and label:
      - `unsupported`: diagonal hatch, `U`;
      - `partly_supported`: a cross-hatch with no fill colour (verifier,
        2026-10-15: a colour reads as the cell's value). The label is the
        supported pairs' value and the supported share, `P+1.23` over
        `· 50%`;
      - `no_points`: dots;
      - `not_reached`: horizontal hatch, `NR`;
      - `other_loop` (pairs only in another loop): a grid, labelled with
        the loop it does have (`CL` / `OL`);
      - `undefined` (relative delta with every base 0): a cross, `∅`.
      An unsupported cell with computed pairs (the closed-loop trim) shows
      the number as text on the hatch, never as a colour. A map with nothing
      to colour says so instead of drawing a scale.
    - **Refusals added after verification:**
      - **Loop names.** A loop name the producer does not write
        (`HEADLINE_BY_LOOP`'s keys) in `headline_by_loop`, a `by_loop` block
        or `loop_shares` is `loop_unknown`, and a headline output that is not
        the producer's for that loop is `headline_unknown`. Every element id
        and `url(#…)` is also built from `_ident(key)`
        (`[^A-Za-z0-9_-]` → `_`); the ids of a real page are unchanged.
      - **Quantile basis.** A cell whose quantiles are over other pairs than
        its status names (changed / unchanged over `all_pairs`,
        partly_supported over `supported_pairs`, unsupported over none or
        `computed_unsupported_pairs`) is `quantile_basis_mismatch`, so a
        forged table cannot colour pairs it calls unsupported.
    - **Overlay.** Edited cells are outlined on every map. Moved
      breakpoints get a ▲ and a highlighted tick, whose hover gives raw
      old → new.
  - **Detail.** Every cell's hover also has a "same cell, other outputs
    (all pairs)" line: ΔBFS p50, Δλ p50, and Δtarget φ p50 (absolute and
    relative) when any of its quantiles is non-zero, so a λ effect that is
    really a target-φ column shift is visible from the hover. "regimes" is
    glossed as the design's recorded label of each point and "loops" as the
    simulated loop of each pair. Every cell's `<title>` gives the cell and its breakpoints, n
    points, pairs, weight and its share of the run, the regime and loop mix,
    the edit, and that map's output: status, quantile block (`over`), Δ and
    relative p05/p50/p95, the support shares and the reasons with their
    shares. A table holds one row per visited or edited cell, with every
    output and every loop headline. Weight is in s for a drive design and
    "grid weight" for a grid.
  - **Themes and network.** Light and dark come from `prefers-color-scheme`.
    The page has inline CSS and inline SVG with no `xmlns`. Patterns are
    referenced by fragment only (`url(#m-…)`). There is no link, script,
    image or font. Every JSON string goes through `html.escape`. At phone
    width a map scrolls sideways (min-width 560 px) instead of shrinking its
    text.
  - **Tests.** `tests/test_sim_report.py`, 19 passing (`pytest -n0`, 4 s).
    The hand-built table is a 3 × 4 Fuel Comp grid validated through
    `CellTable`. It holds changed (0, 0), edited 128 → 131; unchanged (0, 1);
    unsupported (0, 2); partly supported (1, 0) at 0.5/0.5; closed loop
    (1, 1); a pulse output that is `not_reached`; a moved column breakpoint
    2 (67 → 70); and markup in a caveat, a reason, an axis domain and the
    labels.
    - `test_every_status_has_its_label_and_the_caveats_come_before_the_first_svg`:
      - Before the first `<svg>`, in order: caveats, images, edits, design,
        sampling. That block holds the qualification reason (escaped), both
        standing caveats, every unresolved string (including
        `battery_voltage_unmeasured` and `plant:rail_pressure_unmeasured`),
        both shas, the design sha, the edit, the seed, the draws and "5 of
        12" cells visited.
      - All six statuses are drawn. Each non-value mark's fill is its own
        `url(#…)` pattern, and each value mark's fill is a colour.
      - Labels are asserted: `CL`, `NR`, `U`, `P+1.23`, `-1.58`, `0`.
      - Each legend line is drawn.
    - `test_closed_loop_cells_show_the_trim_headline_as_text_on_a_hatch`
      (−2.19 on the unsupported hatch, `computed_unsupported_pairs`, with the
      reason; open-loop cells are `OL`; no setpoint-moved map, named).
    - `test_the_width_map_is_the_p05_to_p95_span_unsigned` (−3.1 … +0.2 %
      gives `3.30`).
    - `test_axes_carry_decoded_breakpoints_and_units_and_moved_breakpoints_are_marked`.
    - `test_edited_cells_are_outlined_on_every_map`: the outline coordinates
      in all 5 maps equal the overlay's edited cells, and there is none when
      nothing is edited.
    - `test_per_cell_detail_is_in_the_hover_and_the_table`.
    - `test_rendering_is_a_pure_function_of_the_json`: str, bytes, mapping
      and key-reversed JSON give identical bytes, and one changed p50 changes
      them.
    - `test_every_string_from_the_json_is_escaped`: no `<script`, `<b>` or
      `<stock>` survives, and the escaped forms are present.
    - `test_no_external_url_appears_in_the_output`: no `http(s):`, `//host`,
      `src=` / `href=` / `action=`, `@import`, non-fragment `url(`, or
      `<link|script|img|iframe|object|embed|base`.
    - `test_run_keys_are_named_and_checked_against_the_table`: every
      refusal by name, and the "not stated" lines without `run`.
    - `test_a_real_aggregated_run_renders_with_its_design_summary`: E07.6's
      synthetic image, a Fuel Comp (4, 4) +20 edit, 4 points × 6 draws,
      through `aggregate_cells`. `run.design = summary(design)` is accepted.
      The outline is at (4, 4) on every map, and the BFS mark there is
      `changed`, as in the table.
    - `test_rendering_a_report_imports_nothing_that_talks_to_the_ecu`
      (clean process).
    - **Verifier fixes (2026-10-15):**
      - `test_a_loop_name_the_producer_does_not_write_never_reaches_an_id`:
        the hostile loop `x"><svg onload=alert(9)>` in `headline_by_loop`, a
        `by_loop` block and `loop_shares` is `loop_unknown`; a wrong headline
        output is `headline_unknown`.
      - `test_every_id_and_fragment_reference_is_sanitised_even_for_a_hostile_panel_key`:
        a panel with that key, drawn directly, gives one `<svg>` and no
        `on*` attribute (read with an HTML parser, not a regex), and every id
        and `url(#…)` matches `[A-Za-z0-9_-]+`; so does every id on a real
        page.
      - `test_a_forged_quantile_basis_cannot_colour_unsupported_pairs`: 4
        cases, `quantile_basis_mismatch`.
      - `test_partly_supported_is_a_hatch_with_value_and_share_never_a_fill`:
        the cell's one `fill` is its pattern, and a partly-supported value
        far above the changed cells does not move the scale.
      - `test_an_undefined_relative_change_and_a_partly_supported_pulse_have_their_own_marks`:
        `∅` on its pattern, and the `_delta_pick` branch (`P+0.40` over
        `· 25%`).
      - `test_a_cell_hover_names_what_else_moves_in_the_same_cell`.
      - `test_run_identity_checks_each_catch_their_own_mismatch`:
        `design.sha256`, `kind`, `points` and `weight`, one case each, each
        named in the refusal.
    - **Mutation checks**, each reverted, each failing at least one test:
      - no escaping: 4 fail;
      - caveat block dropped: 2;
      - unsupported cells coloured: 1;
      - outlines dropped: 2;
      - headline maps read without `by_loop`: 2;
      - unsorted share dicts: 2;
      - whole identity block off: 2; each of its four checks alone: 1 each;
      - `_ident` not applied: 1; loop check off: 1; quantile-basis check
        off: 1;
      - `partly_supported` coloured: 3; other-outputs line dropped: 1;
        undefined mark dropped: 1.
  - **Real report** (laptop, scratch only): E08.4's end-to-end table
    (ZS44A-20260920 → ZS44A-20261004, 298 points, 50 draws, seed 1). `run`
    is built from the pickled result's provenance, `summary(design)` and its
    affected cells grouped by table (`candidate_file`: 195 cells of
    `fuel_compensation`, 15 of `fuel_comp_flow_axis`).
    - The page is 833,969 bytes, rendered in 0.02 s. The caveat block is at
      byte 3,656 and the first `<svg>` at 13,794.
    - ΔBFS: 46 changed, 6 unchanged, 1 unsupported, 203 no points, the same
      counts as E08.4. The open-loop λ map has 4 changed, 1 unsupported and
      48 `CL`. The closed-loop trim map has 48 unsupported with computed
      values and 5 `OL`. The pulse is unsupported in all 53.
    - 195 outlines and 15 moved ticks on each map. The hover of cell (7, 15),
      whose open-loop λ is +4.99 % against ΔBFS −1.53 %, now carries "ΔBFS p50
      −1.534 % · Δtarget φ p50 −0.03125 (−2.899 %)": the target-φ column shift.
    - Headless Chromium (light at 1,000 px, dark at 390 px) made no request
      other than the file itself.

- [~] **E08.7 — Known answers before novel ones.** The workbench is trusted
  only after it reproduces answers obtained independently.
  1. **Negative control:** identical base and candidate gives `unchanged` in
     every visited cell, at every draw.
  2. **K +5 % moves BFS +5 %** (E07.6's known answer) through the CLI and an
     edit spec, per point, not on average.
  3. **The Fuel Comp restore, on the real images** (private, skips by name
     when absent, as `tests/test_fuel_comp_restore_private_rom.py`). Run
     20260920 → 20261004 over a drive design. At every open-loop point the
     simulated ΔBFS ratio must equal that point's Fuel Comp multiplier ratio
     from `fuel_comp_restore.multiplier_change`, until BFS meets its ceiling.
     These are two code paths that share no BFS code. The per-cell pattern
     must show B08.6's 2026-10-15 result: idle leaner, light load off idle
     richer, full throttle +2.3 to +4.6 % rising with rpm.
  4. **A Target AFR edit** of +4 counts reproduces E07.6's WOT figures (φ
     +0.03125, pulse +0.41 ms, λ −2.5 %).
  5. **Physical** (truck, later): the post-flash drives of E07.4's case
     scored against this workbench's per-cell prediction made **before**
     they are looked at. A prediction artifact written before the drive is
     the order evidence; do not re-date it.
  - **Accept:** 1–4 pass in the suite (3 in the private-ROM file); 5 stays
    open, with the prediction artifact's path recorded here.
  *Items 1–4 done 2026-10-15, and item 5's prediction artifact written
  (below). Scoring the post-flash drives is still owed, so the item is `[~]`:*
  - **Route.** Every answer goes in through `tools/rom_sim.py` (`design
    grid` / `design drives`, then `run` with `--edits` or `--candidate-rom`)
    and is read back from what the run wrote: `traces.parquet` per point ×
    draw × output, and `result.json`'s cell table. Every numeric assertion is
    per (point, draw). The synthetic world is E07.6's image
    (`tests.test_sim_montecarlo.build_kit_and_image`) beside a profile copy in
    `tmp_path`, built the way `tests/test_rom_sim_cli.py` builds it but
    without importing that file.
  - **1. Negative control** (`tests/test_rom_sim_known_answers.py`). The grid
    is 5 rpm × 10 load cells (50 points, 2,000–4,800 rpm, 24–96 % fill), so
    it has closed-loop cells and 11 open-loop ones.
    - `test_an_identical_candidate_file_is_unchanged_in_every_visited_cell_at_every_draw`:
      an identical `--candidate-rom`, at seeds 1 and 2 with 4 draws each.
      Provenance has no cells and no ranges, and every reach is `no_change`.
      Every computed value is equal on both sides, with delta exactly 0.0.
      Every visited cell is `unchanged` for every supported output. Outputs
      with no supported pair are `unsupported` (closed-loop λ, fuel and trim:
      `closed_loop_trim_authority_undecoded`), and their computed quantiles
      are 0.0 too. λ is `unchanged` in the open-loop cells.
    - The spec route cannot express an identical candidate:
      `test_the_spec_route_cannot_express_an_identical_candidate` (`set_raw`
      to the cell's own value) exits 2 with `no_op_edit`. Its control is
      `test_a_spec_edit_no_point_reads_moves_no_ecu_output_at_any_draw`, Fuel
      Comp (13, 8) 128 → 148 at 5,600 rpm × byte 128, which no point's
      bilinear corners read. BFS is `reached` structurally, yet BFS is
      computed at every (point, draw) with Δ exactly 0. So is every other
      computed output, and every visited cell's BFS is `unchanged`.
    - **Off the diagonal on purpose** (verifier finding: the first version
      edited (15, 15), so a row/column swap was invisible). The transpose
      (8, 13), 3,600 rpm × byte 208, is read by the 3,900 rpm points; a
      scratch copy editing (8, 13) fails.
  - **2. K +5 %**:
    `test_a_k_edit_of_five_percent_moves_bfs_five_percent_at_every_point_until_the_ceiling`.
    - **Setup.** The spec is `k_constant` `cells [[0, 0]]`, `scale 1.05`,
      `rounding floor`: 28715 → 30150, E07.6's own `28715 * 105 // 100`
      (ratio 1.049974). The base is the synthetic image with `bfs_ceiling`
      set flat at 90 (clamp 90 << 8 = 23,040). The synthetic image's 255 is
      never reached by a steady grid point: base BFS spans 6,632–26,560 on
      this grid. A flat curve makes the clamp a constant the test states
      without the twin's interpolation.
    - **Tolerance, derived** (verifier finding: it had been a chosen 8
      counts). Tp's quotient is q = ⌊K·A⌋ with A shared, so
      q_new − r·q_old ∈ (−1, r), r = K_new/K_old. Tp = ⌊q·c⌋ with
      c = 9163/65536 (`TP_SCALE`), so Tp_new − r·Tp_old ∈ (−1−c, r(1+c)).
      Fuel Comp is flat 128, and BFS = 4·⌊Tp·FC/128⌋ = 4·Tp, so the
      candidate lies within (−4.56, +4.80) counts of r × base. The test uses
      4·r·(1+c) + 0.2 = 4.99 counts, the 0.2 stated slack for q's own
      sub-count truncation (the low byte's `× m >> 8`, divided by d·N ≥ 192).
      The verifier's sweep of 32,861 kernel inputs found +4.54 / −4.25 at
      worst.
    - **Expectation.** Per (point, draw), with 50 points × 3 draws: the
      candidate is base × K ratio capped at the ceiling, within that bound
      (the measured worst is 3.48). Of the 150 pairs, 105 are
      free: ratio 1.04973–1.05027, inside one quantum of that point's base.
      45 are clamped and equal 23,040 exactly; 30 of them are clamped on the
      base too. BFS is the same at every draw of a point.
  - **3. Fuel Comp restore on the real images**
    (`tests/test_rom_sim_private_rom.py`, `pytestmark = slow`).
    - **Skips.** It skips by name when an image is absent
      (`image_not_found`) or the drive is absent.
    - **Drive.** It runs `_capture-tuning-2026-09-23T142926` from
      `~/canary-data/sessions/nissan-xterra-2008`, not a committed slice.
      The committed `tests/fixtures/fuel_plant/` slice of that drive has no
      throttle read (PID 0x11 or DID 0x120F) and no FUEL_STATUS, so E08.2
      places none of its 152 eligible reads (`throttle_unpaired`). The
      `tests/fixtures/fuel_comp/` ram-study slice polls no MAF and has no
      eligible read.
    - **Run.** `design drives` (ZS44A-20260920, `--battery-v-when-unmeasured
      14.0`) gives 298 points. Then `run --base --revision ZS44A-20260920
      --candidate-rom ZS44A-Tuned-20261004.OsirisROM --draws 3 --seed 1
      --stft-pct 0 --ltft-pct 0 --candidate-events ecu_reflash`.
    - `test_simulated_bfs_moves_by_each_points_fuel_comp_multiplier_ratio`:
      - **Method.** For each of the 894 (point, draw) pairs it calls
        `multiplier_change(old, new, InputPoint(...))` at that point's rpm
        word (N02's `fuel_comp_inputs.rpm_word`, asserted equal to
        `paired_scenarios.rpm_word`) and the pair's drawn fill byte.
      - **Bound.** `|BFS_new − BFS_old × FC_new/FC_old| < 4 × max(1,
        FC_new/FC_old)` holds at every pair. This is exact, not fitted: K,
        airflow and speed are equal (`check_comparable`), so
        BFS = 4 × ⌊Tp × FC / 128⌋ (`fuel_comp_product` `>> 7`, then
        `steady_state_bfs` `<< 2`) with one Tp. The measured worst is 3.97
        counts.
      - **Ceiling.** Both images' `bfs_ceiling` is flat 255, read directly:
        clamp 65,280, against a highest BFS of 52,396. No pair is clamped, so
        the identity is asserted on all of them, enrichment-labelled points
        included. The multiplier moves at more than half the pairs, and BFS
        is unchanged exactly where it does not.
    - **"Share no BFS code", checked by reading both.**
      - `multiplier_change` reads cells and axes through `fuel_comp_rom` and
        computes no Tp, product or ceiling.
      - The workbench's BFS is `air_charge_twin` → `src.decode.air_charge`.
      - Both interpolate through `src.decode.ecm_lookup.lookup_2d` (the twin
        via `air_charge.rpm_load_lookup`). A fault inside that lookup would
        move both sides alike, and the identity cannot see it. Everything
        else is on one side only.
      - So the lookup is checked on its own:
        `test_the_shared_lookup_returns_the_raw_cell_at_every_node`. Each
        image's axes are read straight from the image bytes through `rom_map`
        (rpm as byte << 2) and must equal `FuelCompCalibration`'s. Then every
        node (16 × 16 per image) is evaluated through `multiplier_change`.
        Each must equal the raw byte at the `fuel_compensation` slot-1
        address + r·16 + c.
      - No drawn pair of this run lands exactly on a node (0 of 894), which
        is why the nodes are evaluated directly. The between-node
        interpolation stays shared and unchecked. A scratch copy reading
        `c·16 + r` fails at (0, 2), 126 against 128.
    - `test_the_cell_pattern_is_b08_6s_restore_result`, per pair and then on
      the cell table's relative ΔBFS p50:
      - **Idle** (≤ 800 rpm, bytes 48–57, 107 pairs): every pair −2.39 to
        −1.56 %, median −1.607 % (one count, 126/128 = −1.5625 %). Cells
        (800, 48) p50 −1.57 % and (800, 56) −2.27 %.
      - **Light load off idle** (1,000–3,200 rpm, bytes 20–60): every pair
        0 to +2.32 %, never leaner, most ≥ +0.75 %. Cells (1,600, 48)
        +2.32 % and (2,400, 32) +2.31 %.
        - **This evidence is thin:** 15 pairs from 5 points (1,600,
          2,212.5, 2,287.5, 2,312.5 and 2,550 rpm).
        - B08.6's own window (2,000–3,000 rpm, bytes 32–41) holds 3 pairs, one
          draw each of 3 points (+1.54, +1.59 and +2.31 %). The verifier
          counted 1 point by its own definition.
        - So this agrees with B08.6's sign and size; it is not an
          independent confirmation of that row.
      - **Full throttle** (bytes ≥ 190, ≥ 4,000 rpm): +2.29 % at 4,400 rpm;
        +3.82 to +4.62 % at 5,200–5,600 rpm, every pair of the upper band
        above the lower. Cells (4,400, 144) +2.29 % < (5,200, 144) +3.85 %.
      - **Also seen, not asserted.** At 800 rpm, bytes 28–39 go +0.8 %
        (richer at idle speed on a light byte). At 3,200 rpm, bytes ≥ 190 go
        −0.77 to −1.53 %, B08.6's part-load −0.8 to −2.3 %.
  - **4. Target AFR +4 counts**:
    `test_a_target_afr_edit_of_four_counts_reproduces_e076s_wot_figures`.
    - **Spec.** `fuel_targets` slot 1, `all: true`, `add_raw: 4`. The
      candidate sha256 equals E07.6's `_edit_all(..., 4)` candidate.
    - **Conditions.** E07.6's `LEARNED` (STFT 0, LTFT 2.34) and E07.6's
      `WOT` at fill byte 200, every value `stated`. A grid sets MAP from fill
      × baro and solves MAF, so it cannot hold MAF 150 g/s with MAP 79 kPa.
      The two points are written into the CLI-built grid design, with the
      hash recomputed by `write_design`.
    - **Did not reproduce as written; the cause is found.** E07.6's figures
      (commit 84061a65) were measured with BFS stated at 42,000. E07.6 now
      computes BFS (`paired_steady`) and refuses a stated one, so its own WOT
      point today gives BFS 39,604, pulse +0.39 ms and λ −2.531 %
      (−2.547…−2.515 %).
    - **Point 0 (MAF 150).** The E08 path reproduces today's E07.6
      exactly: BFS, φ and pulse are equal to `run_paired` at every draw.
      Relative Δλ over 200 draws is p05/p50/p95 −2.545 / −2.531 / −2.517 %,
      against run_paired's central −2.531 % and range −2.547…−2.515 %.
    - **Point 1** (MAF 159.06 g/s, PID word 15906, where the steady BFS is
      42,000; 15905 gives 41,996 and a 0.40 ms pulse) reproduces the
      written figures. φ is +0.03125 at every draw and the pulse
      +0.41 ms (16.40 → 16.81) at every draw. run_paired's λ is −2.509 %
      (range −2.5246…−2.4939 %, i.e. −2.52…−2.49). Monte Carlo p05/p50/p95
      is −2.523 / −2.510 / −2.496 %, inside the range. All 200 draws happen
      to fall inside it too (−2.5246…−2.4944 %). This is not asserted: a
      joint draw may legitimately leave a one-at-a-time range.
    - **Assertions.** p50 is within 2e-4 of the central value and p05–p95
      lies inside the range, at both points. The pulse step is one 0.01 ms
      quantum, so the written +0.41 ms holds only at BFS ≥ ~41,997 on this
      image.
  - **Mutation checks** (scratch copies under the session scratchpad, never
    the repo; each fails):
    - **Item 2.** Each of these fails the per-point check:
      - expected ratio 1.04 (worst error 200 counts);
      - expected ratio 1.051, a 0.1 % error (24 counts), and 1.0485 (31
        counts), both re-run against the derived 4.99-count bound;
      - kernel `air_charge_tp` given half the K change (499 counts);
      - kernel `steady_state_bfs` with the ceiling lifted (4,848 counts).
      A (K+1)/K ratio error (3.5e-5) is below the word and passes, as
      expected.
    - **Item 3.** Each of these fails the identity:
      - expected `(FC_new + 1)/FC_old`;
      - `multiplier_change(new, old, …)`;
      - the sim-side `rpm_load_lookup` reading byte + 8.
      An idle expectation of two counts (125/128) fails the pattern test
      (median −1.607 vs −2.34 %).
    - **Item 1.** A candidate file with every Fuel Comp cell +1, with the
      provenance check removed, fails on the traces (Δ 48 counts).
    - **Item 4.** Pulse +0.41 at point 0, φ 0.0234375 and λ −2.4 % each
      fail.
  - **Runtimes** (`pytest -n0`, laptop shared with other sessions):
    - `test_rom_sim_known_answers.py`: 5 tests, 6.8 s quiet and about 10 s
      loaded.
      - Over 1.5 s: the identical-file control, 1.6–3.9 s call plus
        1.1–3.1 s of module setup.
      - Near 1.5 s: the spec control (0.8–1.7 s).
      - None is marked slow, and `tests/slow_tests.txt` is not edited.
    - `test_rom_sim_private_rom.py`: 17–34 s for the drive design and the
      per-pair frame (module setup), then 2.1–2.5 s, 0.5 s, and 24–25 s for
      the node check (it decodes both `.OsirisROM` files). The whole file is
      marked `pytest.mark.slow`.
  - **Gate.** `ruff check .`, `ruff format --check .`, `mypy src` and mypy
    on both files are clean. `pytest tests/test_rom_sim_known_answers.py
    tests/test_rom_sim_private_rom.py tests/test_layering.py -n0`: 19
    passed, with the images and the drive present.
  - **Item 5: prediction artifact, written 2026-10-16T00:56Z (before any
    post-flash drive is scored).** A verifier finding corrected the first
    version of this evidence, which said no post-flash drive existed. That
    was false.
    - **Directory.**
      `~/canary-data/sim/predictions/fuel-comp-restore-2026-10-04/`. It is
      outside the checkout and the ROM dirs. The design is under `designs/`,
      the run under `run/`, and each command's stdout is in
      `design_stdout.json` / `run_stdout.json`.
    - **Commands** (the item-3 run, with the same flags):
      ```
      .venv/bin/python -m tools.rom_sim design drives \
        --base-revision ZS44A-20260920 --sessions _capture-tuning-2026-09-23T142926 \
        --sessions-root ~/canary-data/sessions/nissan-xterra-2008 --fuel-type gasoline \
        --battery-v-when-unmeasured 14.0 --rom-dir "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs" \
        --out ~/canary-data/sim/predictions/fuel-comp-restore-2026-10-04/designs
      .venv/bin/python -m tools.rom_sim run --base --revision ZS44A-20260920 \
        --candidate-rom "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs/ZS44A-Tuned-20261004.OsirisROM" \
        --design <out>/designs/322e4cab… --draws 3 --seed 1 --stft-pct 0 --ltft-pct 0 \
        --candidate-events ecu_reflash --rom-dir "/mnt/c/Users/thebo/Desktop/Uprev/Tuned ROMs" \
        --out ~/canary-data/sim/predictions/fuel-comp-restore-2026-10-04/run
      ```
    - **Identities:**
      - design sha256
        `322e4cabadfbdcffefa54662eff3b5a7b721586ebef51a30c9037e0e71517d6e`
        (298 points);
      - `result_digest`
        `389fa0beb846c95b3f4f44f94606ed0a6fa71b0feac859994d8be59e5a89bd98`
        (traces session `_sim-389fa0beb846`);
      - base image
        `a46baa248f16db8d5ed9854913e614dd7d77b7579bb6c36bddb792741531be50`,
        candidate
        `5587f7e1e6dbd0fcb9ffe0c857cc0cafebbe11bf306636c0f3265b903254d253`;
      - seed 1, 3 draws;
      - `result.json` sha256
        `0e1294711018511faa9ac4bc6cb258ccd35dacae2c9ae27093c9638aba8d34be`;
      - `report.html` sha256
        `cd99ebcb3ca013d4e9ed5d557961ce50a974dff0cf8a997a24a45ee0c8bf1477`;
      - `traces.parquet` sha256
        `92d35caa610b1f34b897fabc0398bc4eaf36f66b46307afe16b578505a772066`.
    - **A denser companion, also written before any scoring**
      (2026-10-16T01:04Z). Same design and flags with `--draws 200`,
      `--out …/fuel-comp-restore-2026-10-04/run-200draws`. Three draws give
      only a coarse per-cell spread, so cell quantiles should be read from
      this run:
      - `result_digest`
        `956d7bbed7cb053c544c2c1e0fb5d07542a5a157a56fdcc9d1564cbceec0e8c0`.
        This is the same digest as every earlier 200-draw run of this pair,
        so the prediction is deterministic.
      - `result.json` sha256
        `71267e1633b467603bdce31cf6d09b63f5595e8f8c8d5d5690e5ca0e4ee57917`;
      - `report.html` sha256
        `5173dadd56ec584fbcd15f120e12c1a0565d296ea12fbc9ea99c2457c0c67cf4`;
      - `traces.parquet` sha256
        `f05b5c2d3bfa7e94bc370f051670f6cdee2ae0de90f712a545a616b00e4cc5c5`.
    - **Post-flash recordings that exist now.** These are the sessions under
      `~/canary-data/sessions/nissan-xterra-2008` that
      `tune_revisions.for_session` (dated by first sample) puts on
      ZS44A-20261004:
      - `2026-10-08T172943`;
      - `2026-10-08T182550` (first sample 20:50:12Z);
      - `_capture-fuel-pulsation-2026-10-08T210012`;
      - `2026-10-10T153117`;
      - `2026-10-10T165941`.
      Only their start times were read. **None of them has been scored
      against this prediction.**
    - **Order evidence.** The order evidence is the git commit recording
      this block, never a re-dated file.
    - **Still owed (truck and E07.4).** Score those drives (and later ones)
      per rpm × byte cell against this prediction with E07.4's case
      machinery. Until then the item stays `[~]`.

- [ ] **E08.8 — Later: dashboard and Pi.** Deferred until E08.1–E08.7 pass.
  - An Engine Tuning page that takes an edit spec, runs a bounded job, and
    shows the E08.6 report.
  - On the Pi it is a bounded single-flight job after collection stops
    (AGENTS.md: no analysis on the Pi while it collects), with artifacts under
    `/data/canary/sim/`, declared in `tests/test_state_dir.py`.
  - Shares E07.7's Pi integration rather than a second runtime.

- [x] **E08.9 — Desk launcher: one script brings up the GUI on a laptop.**
  Requested 2026-10-15 by the operator: "a single python script that a user
  can run that will launch the gui tool", to edit a ROM and run the simulator
  without the truck. The pieces exist. `canary.sim.example.yaml` runs the
  whole daemon on a virtual CAN bus with OBD disabled, so it never transmits.
  The ROM Editor page (W09) reads base images from the ROM Library. What is
  missing is the glue, with state kept out of the checkout. Today the sim
  config writes control state under `data/sim/`, which is not git-ignored, and
  ROM Library uploads would put proprietary images in the repo tree.
  - **Script:** `scripts/canary_desk.py`, run as
    `.venv/bin/python scripts/canary_desk.py`. Standard library plus the repo's
    own modules; no new dependency.
  - **State outside the checkout.** `--state-dir`, default
    `~/canary-data/desk`, becomes `daemon.state_dir`, with
    `collection.storage_path` under it. A state dir inside the checkout is
    refused (`inside_checkout`). The generated config is written there too
    (`<state>/canary.desk.yaml`). It is built from `canary.sim.example.yaml`
    so the two cannot drift: load it, override only the named fields, and
    validate through `CanaryConfig`. Overrides:
    - `daemon.state_dir`, with every explicit `data/sim/...` daemon path
      dropped so S03's re-rooting applies;
    - storage path;
    - dashboard host `127.0.0.1`, never `0.0.0.0` (`--host` to widen);
    - `--port`, default 8000;
    - no replay session unless `--replay <session>` is given;
    - models stay disabled.
  - **Dashboard bundle.** If `src/dashboard/static/dist` is missing or older
    than the frontend sources, run `pnpm install --frozen-lockfile` (only when
    `node_modules` is missing) and `pnpm build`. Reuse S02's staleness rule if
    one exists. A missing `pnpm` is refused with what to install; never
    install it.
  - **Control token.** Generated once (`secrets.token_urlsafe`) and kept at
    `<state>/control/desk.token`, mode 0600. It is passed to the daemon child
    through `daemon.control_token_env`, never on a command line, and printed
    once with "paste it into the dashboard's token field". If the frontend
    already accepts a token some other way, use that instead and say so. Don't
    change the frontend for this item.
  - **ROMs.** `--rom-dir` (repeatable), or `$CANARY_ROM_DIRS`: each file is
    imported into the ROM Library with `RomLibrary.add_upload`, idempotent by
    sha256, before the daemon starts or through its own upload route. An UpRev
    encrypted backup `.bin` is reported by `rom_image`'s refusal name and
    skipped, not fatal. Print each image's library sha and `revision_match`,
    so the operator can see which bases can simulate (W09: only `matched`
    revisions).
  - **Launch.**
    - Start `.venv/bin/python -m src.main --config <generated> --profile
      nissan-xterra-2008` (`--profile` overridable) as a child process.
    - Wait for the dashboard to answer, with a stated timeout.
    - Open the ROM Editor's hash route in a browser. On WSL use the Windows
      browser (`cmd.exe /c start`, or `wslview` if present), elsewhere
      `webbrowser`; `--no-browser` skips this.
    - Ctrl+C stops the child cleanly.
    - A port already in use is refused by name before launch.
    - Never `sudo`; never touch `/etc/canary` or `/data/canary`.
  - **Accept:**
    - Unit tests of the config builder: the overrides are applied, it
      validates, nothing lands under the checkout, and `inside_checkout` is
      refused.
    - Token persistence and permissions.
    - ROM import idempotence on a synthetic image, and the `.bin` skip.
    - Browser-command selection (WSL vs not).
    - Port-in-use refusal.
    - A smoke test: launch with `--no-browser` on a free port, get the
      dashboard's index and a control route with the token, stop the child,
      and confirm `git status` shows nothing new in the checkout.
    - A real run on this laptop with the operator's ROM dir: the ROM Editor
      lists the images and a simulate job completes.
    - A README section "Desk mode (no truck)" with the one command.
  - **Evidence (2026-10-15).**
    - `scripts/canary_desk.py` (new), `tests/test_canary_desk.py` (new, 37
      tests: config builder, `inside_checkout`, token 0600 and reuse, ROM
      import idempotence and the encrypted-`.bin` skip, WSL vs other browser,
      port in use, bundle staleness, plus the `slow` smoke that launches the
      real daemon, fetches `/` and `/control/rom/library` with the token,
      stops it with SIGINT, checks the daemon pid is gone and that
      `git status --porcelain --ignored` outside the frontend tree is
      unchanged): `pytest tests/test_canary_desk.py` 37 passed. README
      "Desk mode (no truck)" added; `tests/fixtures/script_cli_snapshot.json`
      gained the script's entry (112 insertions, no deletions).
    - Real run, this laptop: `.venv/bin/python scripts/canary_desk.py
      --rom-dir ".../Uprev/Tuned ROMs" --no-browser --state-dir <scratch>
      --port <free>`. The ROM Library listed all three `.OsirisROM` images,
      each `matched`: `ZS44A-Tuned-20260920` sha `a46baa24...` (revision
      `ZS44A-20260920`), `ZS44A-Tuned-20261004` sha `5587f7e1...`
      (`ZS44A-20261004`), `ZS44A-Tuned` sha `e01cc0cb...` (`ZS44A-k106`). A
      second run on the same state said `present` for all three. The preview
      route answered 200 on the 20261004 image, and
      `POST .../simulate` (Fuel Comp slot 1, 1500-3000 rpm x 20-50 %, 4
      draws) returned 200 with a result (`outcome: unevaluable`, which is the
      simulator's verdict for that edit and not a launcher fault). Ctrl+C
      stopped launcher and daemon (exit 0); the ROM folder still held exactly
      its three files. The browser page itself was not driven.
    - Deviations from the item text: the stale-bundle rule is S02's, minus
      `*.tsbuildinfo` (tsc caches, rewritten by every `pnpm typecheck`, which
      kept a fresh build looking stale); ROMs go in through the daemon's own
      upload route after it answers, so the library written is the one the
      page shows; `--no-build` was added so the smoke test never runs `pnpm`;
      state dirs under `/etc/canary` and `/data/canary` are refused too
      (`reserved_state_dir`); the token variable is
      `CANARY_DESK_CONTROL_TOKEN`, so the operator's real
      `CANARY_CONTROL_TOKEN` is removed from the child's environment. The
      launcher opens `http://127.0.0.1:<port>/#wb/tuning-rom-editor?token=<quote(token,
      safe="")>` (aba2b1fe, `tokenHandoff.ts`): the `?` is inside the fragment,
      so the token never reaches a request line or the daemon log, and is
      never printed when a browser is opened. With `--no-browser` the full link
      is printed once. The token never reaches the daemon's argv (environment
      only). On WSL, `wslview` / `cmd.exe /c start` necessarily receive the
      link as an argument of that short-lived process; acceptable on a
      single-user laptop. `handoff_url` asserts the token is
      `[A-Za-z0-9_-]+` (`token_not_urlsafe` refusal) rather than escaping for
      `cmd.exe`. A failed browser launch names the bare URL, not the link.
    - Follow-up (verifier minors): the `try` starts right after `Popen`, so
      an interrupt cannot orphan the daemon; an exception during the health
      wait stops the child (tested); a second Ctrl+C inside `stop_child`
      escalates to kill with no traceback (tested). `--rom-dir` is optional;
      the README's one command is `scripts/canary_desk.py`, ROMs open from
      the GUI. `tests/test_canary_desk.py`: 45 passed.

- [ ] **E08.10 — Name every held working assumption; BFS stops inheriting
  the target-lookup gate.** Found 2026-10-15 by W09.6's real-image runs
  (`tests/test_rom_editor_private_rom.py`, `a00dfb99`; W09 doc, W09.6
  "Laptop half", logged under W09.7). The operator agreed to this plan.
  1. **The VE that sets the result is not named.**
     - The sim kit's plant is fuel-mv-1, the only version the contract admits
       (E08.3). Its VE is the registry's working assumption: 0.7, bracket
       [0.4, 1.0], `requires_measurement` (`fuel_plant.yaml` ~194-198).
     - A grid design solves MAF from the stated load at that VE (E08.2's
       `load_basis`). At 100 % fill that is ~103 g/s (BFS ~29k), against the
       truck's ~38–42k at WOT; fuel-mv-3 identifies 0.9055 at 4,000–5,000 rpm
       (~133 g/s).
     - So BFS and pulse deltas at the top of a grid are understated by ~30 %.
       Relative λ is roughly unaffected.
     - Nothing in the result names VE. The only plant caveat present is
       `plant:manifold_pressure_unmeasured`.
     - **Fix at source.** Every result carries, in `assumptions`, each plant
       working assumption a computed output rests on: parameter, value,
       source file and bracket, and whether this run drew it or held it.
       Derive this from the registry the run resolved, never from a
       hand-kept list.
     - A design's own `assumptions`, such as the grid's "MAF solved at VE
       0.7", flow into the result too. E08.6's report shows both in its
       caveat block, ahead of the first chart. That also closes E08.6's
       reported gap that the design's `assumptions` were not in `summary()`.
     - A grid's MAF depends on VE but is frozen when the design is built.
       Name that in the result too. The VE draw does not move a grid
       point's MAF.
  2. **BFS inherits a gate it does not depend on.**
     `paired_scenarios.run_paired` (~1017) adds the run-level `gate` to every
     output's reasons. A target-lookup gate (e.g.
     `target_lookup:enrichment_gate_state_unknown`) therefore marks
     `base_fuel_schedule` unsupported, although steady BFS is computed before
     the Target AFR lookup.
     - Scope each gate reason to the outputs downstream of the node it names,
       through the application model's `depends_on`, not a hand list.
     - The real 20260920 → 20261004 run's one unsupported ΔBFS cell, (8, 15),
       is this case.
     - The W09.6 test marks it `KNOWN OVER-REFUSAL`. Flip that one assertion
       deliberately, and tell the W09 session.
  - **Accept:**
    - A grid run on fuel-mv-1 names VE 0.7 [0.4, 1.0] (held or drawn) and
      the frozen-MAF fact in `assumptions` and in the report's caveat block.
      A test fails if the registry entry changes and the result does not
      follow.
    - The ΔBFS cell (8, 15) of the real run becomes supported. Its pulse, λ
      and target φ keep the gate reason.
    - `cmp_head.py`'s HEAD equality is re-stated: the cases where only these
      reason or assumption strings changed are listed; values are unchanged
      everywhere.
  - **Not here (needs an operator decision):** switching the kit to an
    identified VE (fuel-mv-2/3). The contract admits only fuel-mv-1, and
    fuel-mv-2/3's refusing MAP conflicts with drawing VE over plant-model
    MAP (`manifold_refusal_varies_by_draw`). It is a new plant choice for
    the simulator, recorded here so it is not implied.

## Not in scope (named so they are not implied)

- **Spark, torque, knock, power:** `compare_roms` traces ignition edits but
  no plant computes torque (E07.5 stage 3 needs spark sweeps and the H07
  dyno). The report shows them `unsupported`.
- **Transients, tip-in, wall wetting, fuel cut:** steady points only (E07.5).
- **Closed-loop trim authority and settling:** the clamp is undecoded; such
  outputs are computed but unsupported (E07.6).
- **Optimising a ROM against the simulator:** `paired_scenarios.optimise`
  stays refused until effect predictions qualify (E07.6).
- **Writing anything to the ECU.** Candidates are `flashable: false` with
  stale checksums. A flash stays the operator's explicit, per-operation act
  (AGENTS.md).
