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

- [ ] **E08.5 — CLI: `tools/rom_sim.py`.** Offline, laptop or desk, prints
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

- [ ] **E08.6 — Report page: self-contained HTML.**
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

- [ ] **E08.7 — Known answers before novel ones.** The workbench is trusted
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

- [ ] **E08.8 — Later: dashboard and Pi.** Deferred until E08.1–E08.7 pass.
  - An Engine Tuning page that takes an edit spec, runs a bounded job, and
    shows the E08.6 report.
  - On the Pi it is a bounded single-flight job after collection stops
    (AGENTS.md: no analysis on the Pi while it collects), with artifacts under
    `/data/canary/sim/`, declared in `tests/test_state_dir.py`.
  - Shares E07.7's Pi integration rather than a second runtime.

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
