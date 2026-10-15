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

- [ ] **E08.1 — Edit specification: what the operator writes.**
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

- [ ] **E08.2 — Operating-point designs: where the simulated engine runs.**
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

- [ ] **E08.3 — The Monte Carlo engine: paired draws over declared uncertainty.**
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

- [ ] **E08.4 — Per-cell aggregation that keeps each kind of empty distinct.**
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
