// A hand-built simulate result (W09.4): a 3 x 4 Fuel Comp grid whose cells
// cover EVERY status of `sim_cells.STATUSES` in the open-loop lambda map, plus
// a closed-loop-only cell, a mixed cell, a moved-setpoint cell, a relative
// delta with no defined pair, edited cells and a moved breakpoint. The shape
// is `sim_cells.CellTable.to_dict()` and `rom_editor.simulate_edits`; the
// numbers are invented, not a simulation.

import type { RomTableAxis, RomTableValues, RomTablesResponse } from "../types/romLibrary";
import type {
  IgnitionTimingBlock,
  RomSimRequest,
  RomSimResult,
  SimCell,
  SimCellTable,
  SimLoopBlock,
  SimOutputCell,
  SimQuantileBlock,
  SimRunContext,
  SimStatus,
} from "../types/romEditor";

export const BASE_SHA = "a".repeat(64);
export const CANDIDATE_SHA = "c".repeat(64);
export const DESIGN_SHA = "d".repeat(64);

/** `paired_scenarios.format_plant_assumption`'s string for the held VE (E08.10). */
export const VE_ASSUMPTION =
  "plant working assumption: manifold_pressure (volumetric_efficiency) = 0.7, bracket [0.4, 1.0], held at that value (xterra/fuel_plant.yaml registry, plant fuel-mv-1; unresolved effect manifold_pressure_unmeasured)";
const RAIL_ASSUMPTION =
  "plant working assumption: rail_pressure (kPa) = 350.0, bracket [305.0, 371.0], drawn inside its bracket in this run (xterra/fuel_plant.yaml registry, plant fuel-mv-1; unresolved effect rail_pressure_unmeasured)";
const DESIGN_ASSUMPTION = "design: grid load: the fill fraction is taken as MAP / baro";
/** The result's `assumptions`: unprefixed, plant working, design. */
export const SIM_ASSUMPTIONS = [
  "the pedal-enrichment flag is stated, not logged",
  VE_ASSUMPTION,
  RAIL_ASSUMPTION,
  DESIGN_ASSUMPTION,
];

import { TABLES, TABLES_WITH_FUEL_COMP, fuelCompTable } from "./romLibraryFixtures";

type Over = SimQuantileBlock["over"];

function quantiles(
  over: Over,
  delta: [number, number, number],
  relative: [number, number, number] | null,
  pairs = 40,
): SimQuantileBlock {
  const [p05, p50, p95] = delta;
  return {
    over,
    pairs,
    weight: 0.25,
    delta: { p05, p50, p95 },
    relative_delta: relative ? { p05: relative[0], p50: relative[1], p95: relative[2] } : null,
    relative_undefined_pairs: 0,
  };
}

interface OutputOptions {
  delta?: [number, number, number];
  relative?: [number, number, number] | null;
  share?: number;
  reasons?: string[];
}

/** One output cell of the given status, with the quantile basis the status
 *  names (`sim_report._QUANTILES_OVER`). */
export function output(status: SimStatus, options: OutputOptions = {}): SimOutputCell {
  const delta = options.delta ?? [0, 0, 0];
  // A lambda-like base of 1: the relative change is the delta itself.
  const relative = options.relative === undefined ? delta : options.relative;
  const reasons = options.reasons ?? [];
  switch (status) {
    case "no_points":
    case "not_reached":
      return {
        status,
        pairs: 0,
        supported_share: null,
        unsupported_share: null,
        support: [],
        quantiles: null,
        changed_share: null,
      };
    case "unsupported":
      return {
        status,
        pairs: 40,
        supported_share: 0,
        unsupported_share: 1,
        support: [
          {
            status: "unsupported",
            reasons: reasons.length ? reasons : ["closed_loop_trim_authority_undecoded"],
            pairs: 40,
            weight_share: 1,
          },
        ],
        quantiles: options.delta ? quantiles("computed_unsupported_pairs", delta, relative) : null,
        changed_share: null,
      };
    case "partly_supported":
      return {
        status,
        pairs: 40,
        supported_share: options.share ?? 0.6,
        unsupported_share: 1 - (options.share ?? 0.6),
        support: [
          { status: "supported", reasons: [], pairs: 24, weight_share: options.share ?? 0.6 },
          {
            status: "unsupported",
            reasons: reasons.length ? reasons : ["fill_byte_outside_plant_range"],
            pairs: 16,
            weight_share: 1 - (options.share ?? 0.6),
          },
        ],
        quantiles: quantiles("supported_pairs", delta, relative, 24),
        changed_share: 1,
        supported_effect: "changed",
      };
    case "unchanged":
      return {
        status,
        pairs: 40,
        supported_share: 1,
        unsupported_share: 0,
        support: [{ status: "supported", reasons: [], pairs: 40, weight_share: 1 }],
        quantiles: quantiles("all_pairs", [0, 0, 0], [0, 0, 0]),
        changed_share: 0,
      };
    case "changed":
      return {
        status,
        pairs: 40,
        supported_share: 1,
        unsupported_share: 0,
        support: [{ status: "supported", reasons: [], pairs: 40, weight_share: 1 }],
        quantiles: quantiles("all_pairs", delta, relative),
        changed_share: 1,
      };
  }
}

const OUTPUTS = [
  "base_fuel_schedule",
  "delivered_lambda",
  "fuel_trim_total_pct",
  "injector_pulse_ms",
  "target_phi",
];

function loopBlock(loop: string, outputName: string, cell: SimOutputCell, share = 1): SimLoopBlock {
  return { loop, weight_share: share, pairs: cell.pairs || 40, output: outputName, cell };
}

interface CellOptions {
  pairs?: number;
  loops?: Record<string, number>;
  outputs?: Partial<Record<string, SimOutputCell>>;
  byLoop?: SimLoopBlock[];
  edited?: { old: number; new: number };
  rowMoved?: boolean;
}

/** A visited cell. Every output defaults to `unchanged`; override per test. */
function cell(row: number, col: number, options: CellOptions = {}): SimCell {
  const rows = [400, 800, 1200];
  const cols = [7.1, 14.3, 21.4, 28.6];
  const pairs = options.pairs ?? 40;
  const visited = pairs > 0;
  const none = (status: SimStatus) => output(status);
  const outputs: Record<string, SimOutputCell> = {};
  for (const name of OUTPUTS) {
    outputs[name] = options.outputs?.[name] ?? (visited ? output("unchanged") : none("no_points"));
  }
  return {
    row,
    col,
    row_breakpoint: [8, 16, 24][row] ?? 0,
    col_breakpoint: [16, 32, 48, 64][col] ?? 0,
    row_value: rows[row] ?? null,
    col_value: cols[col] ?? null,
    n_points: visited ? 1 : 0,
    points: visited ? [row * 4 + col] : [],
    pairs,
    weight: visited ? 0.25 : 0,
    regime_shares: visited ? { lean_target: 0.75, closed_loop: 0.25 } : {},
    loop_shares: visited ? (options.loops ?? { open_loop: 1 }) : {},
    outputs,
    by_loop: visited ? (options.byLoop ?? []) : [],
    edited: options.edited ?? null,
    row_axis_moved: options.rowMoved ?? false,
    col_axis_moved: false,
  };
}

const LAMBDA = "delivered_lambda";
const TRIM = "fuel_trim_total_pct";
const PHI = "target_phi";

export function buildCells(): SimCell[] {
  const richer = output("changed", {
    delta: [-0.02, -0.0152, -0.01],
    relative: [-0.02, -0.0152, -0.01],
  });
  const leaner = output("changed", { delta: [0.0, 0.0031, 0.006], relative: [0.0, 0.0031, 0.006] });
  const bfsUp = output("changed", { delta: [30, 36, 40], relative: [0.019, 0.0201, 0.0212] });
  const pulse = output("changed", { delta: [0.04, 0.052, 0.066] });
  const trimComputed = output("unsupported", { delta: [-1.2, -0.8, -0.4], relative: null });
  const trimSupported = output("changed", { delta: [0.2, 0.9, 1.6], relative: null });
  const undefinedRel = {
    ...output("changed", { delta: [0.001, 0.002, 0.003] }),
    quantiles: {
      ...quantiles("all_pairs", [0.001, 0.002, 0.003], null),
      relative_undefined_pairs: 40,
    },
  };
  return [
    // (0,0): changed, edited.
    cell(0, 0, {
      edited: { old: 120, new: 122 },
      outputs: { [LAMBDA]: richer, base_fuel_schedule: bfsUp, injector_pulse_ms: pulse },
      byLoop: [loopBlock("open_loop", LAMBDA, richer)],
    }),
    // (0,1): unchanged.
    cell(0, 1, { byLoop: [loopBlock("open_loop", LAMBDA, output("unchanged"))] }),
    // (0,2): unsupported, with computed values (text only).
    cell(0, 2, {
      outputs: {
        [LAMBDA]: output("unsupported", {
          delta: [-0.01, -0.006, -0.002],
          reasons: ["plant_not_qualified"],
        }),
        injector_pulse_ms: output("unsupported", { delta: [0.01, 0.02, 0.03] }),
      },
      byLoop: [
        loopBlock(
          "open_loop",
          LAMBDA,
          output("unsupported", {
            delta: [-0.01, -0.006, -0.002],
            reasons: ["plant_not_qualified"],
          }),
        ),
      ],
    }),
    // (0,3): partly supported.
    cell(0, 3, {
      outputs: {
        [LAMBDA]: output("partly_supported", { delta: [0.0, 0.004, 0.009], share: 0.6 }),
        injector_pulse_ms: output("partly_supported", { delta: [0.01, 0.02, 0.03], share: 0.6 }),
      },
      byLoop: [
        loopBlock(
          "open_loop",
          LAMBDA,
          output("partly_supported", { delta: [0.0, 0.004, 0.009], share: 0.6 }),
        ),
      ],
    }),
    // (1,0): never visited.
    cell(1, 0, { pairs: 0 }),
    // (1,1): the change cannot reach lambda here.
    cell(1, 1, {
      outputs: { [LAMBDA]: output("not_reached"), injector_pulse_ms: output("not_reached") },
      byLoop: [loopBlock("open_loop", LAMBDA, output("not_reached"))],
    }),
    // (1,2): every pair closed loop: no open-loop mark, a computed-only trim.
    cell(1, 2, {
      loops: { closed_loop: 1 },
      outputs: { [TRIM]: trimComputed },
      byLoop: [loopBlock("closed_loop", TRIM, trimComputed)],
    }),
    // (1,3): both loops in one cell; the closed-loop trim is supported here.
    cell(1, 3, {
      loops: { open_loop: 0.7, closed_loop: 0.3 },
      outputs: { [LAMBDA]: leaner, base_fuel_schedule: bfsUp, injector_pulse_ms: pulse },
      byLoop: [
        loopBlock("open_loop", LAMBDA, leaner, 0.7),
        loopBlock("closed_loop", TRIM, trimSupported, 0.3),
      ],
    }),
    // (2,0): a relative change with no defined pair (every base 0).
    cell(2, 0, {
      outputs: { [LAMBDA]: undefinedRel },
      byLoop: [loopBlock("open_loop", LAMBDA, undefinedRel)],
    }),
    // (2,1): the setpoint moved: what changed is the commanded phi.
    cell(2, 1, {
      loops: { closed_loop_setpoint_moved: 1 },
      outputs: {
        [PHI]: output("changed", {
          delta: [0.03, 0.03125, 0.032],
          relative: [0.029, 0.031, 0.033],
        }),
      },
      byLoop: [
        loopBlock(
          "closed_loop_setpoint_moved",
          PHI,
          output("changed", { delta: [0.03, 0.03125, 0.032], relative: [0.029, 0.031, 0.033] }),
        ),
      ],
    }),
    // (2,2): changed, edited, a breakpoint of its row moves.
    cell(2, 2, {
      edited: { old: 128, new: 131 },
      rowMoved: true,
      outputs: { [LAMBDA]: richer, base_fuel_schedule: bfsUp, injector_pulse_ms: pulse },
      byLoop: [loopBlock("open_loop", LAMBDA, richer)],
    }),
    // (2,3): never visited.
    cell(2, 3, { pairs: 0 }),
  ];
}

function statusCounts(cells: SimCell[]): Record<string, Record<string, number>> {
  const counts: Record<string, Record<string, number>> = {};
  for (const name of OUTPUTS) {
    counts[name] = {};
    for (const item of cells) {
      const status = item.outputs[name]?.status ?? "no_points";
      counts[name][status] = (counts[name][status] ?? 0) + 1;
    }
  }
  return counts;
}

export function buildCellTable(): SimCellTable {
  const cells = buildCells();
  return {
    schema_version: 1,
    tool: "sim_cells",
    tool_version: "1",
    grid: "fuel_comp",
    table: "fuel_compensation",
    slot: 1,
    membership: "nearest_breakpoint",
    membership_rule: "nearest breakpoint in the lookup's comparison domain",
    quantiles: [0.05, 0.5, 0.95],
    quantile_method: "inverted_cdf",
    weight_basis: "point weight / draws",
    base_image_sha256: BASE_SHA,
    candidate_image_sha256: CANDIDATE_SHA,
    design_sha256: DESIGN_SHA,
    design_kind: "grid",
    result_digest: "e".repeat(64),
    draws: 200,
    n_points: 10,
    outputs: OUTPUTS,
    headline_by_loop: {
      open_loop: LAMBDA,
      closed_loop: TRIM,
      closed_loop_setpoint_moved: PHI,
    },
    rows: {
      table: "fuel_comp_engine_speed_axis",
      coordinate: "engine_speed_word",
      raw: [8, 16, 24],
      breakpoints: [8, 16, 24],
      values: [400, 800, 1200],
      unit: "rpm",
      domain: "engine speed word",
      cell_ranges: [
        [null, 11],
        [12, 19],
        [20, null],
      ],
    },
    cols: {
      table: "fuel_comp_flow_axis",
      coordinate: "fuel_comp_load_byte",
      raw: [16, 32, 48, 64],
      breakpoints: [16, 32, 48, 64],
      values: [7.1, 14.3, 21.4, 28.6],
      unit: "%",
      domain: "fuel comp load byte",
      cell_ranges: [
        [null, 23],
        [24, 39],
        [40, 55],
        [56, null],
      ],
    },
    reach: Object.fromEntries(
      OUTPUTS.map((name) => [name, { status: "reached" as const, basis: ["fuel_comp_lookup"] }]),
    ),
    caveats: [
      "plant_not_qualified: no qualification verdict yet (baseline validation pending)",
      "structural_error_not_sampled",
      "parameter_uncertainty_only",
      "synthetic_example_not_a_measurement",
      // As the run states them: `caveats` repeats every assumption (E08.10).
      ...SIM_ASSUMPTIONS,
    ],
    unresolved: ["closed_loop_trim_authority_undecoded", "plant:rail_pressure_working_assumption"],
    rom_outcome: "traced",
    weight_total: 2.75,
    unplaced: { load_below_model_floor: { pairs: 40, weight: 0.25 } },
    status_counts: statusCounts(cells),
    overlay: {
      edited_cells: [
        { row: 0, col: 0, old: 120, new: 122 },
        { row: 2, col: 2, old: 128, new: 131 },
      ],
      axis_edits: [
        { axis: "rows", table: "fuel_comp_engine_speed_axis", index: 2, old: 24, new: 26 },
      ],
      other_edits: [],
      moved_lookups: [{ node: "target_afr_lookup", tables: ["fuel_targets"], on_this_grid: false }],
    },
    cells,
  };
}

export function buildSimResult(): RomSimResult {
  return {
    tool: "rom_editor",
    tool_version: "1",
    label: "simulation",
    synthetic: false,
    caveats: buildCellTable().caveats,
    assumptions: [...SIM_ASSUMPTIONS],
    unresolved: buildCellTable().unresolved,
    inert_parameters: ["injector_dead_time_offset"],
    coverage: { delivered_lambda: 0.82, injector_pulse_ms: 0.4 },
    outcome: "traced",
    learned_state: { stft_pct: 0, ltft_pct: 0, source: "stated: ROM Editor" },
    provenance: {
      tool: "sim_montecarlo",
      base_image_sha256: BASE_SHA,
      candidate_image_sha256: CANDIDATE_SHA,
      candidate_events: ["ecu_reflash"],
      design_sha256: DESIGN_SHA,
      uncertainty_sha256: "9".repeat(64),
      plant_version: "fuel-mv-3",
      baseline_sha256: null,
      contract_sha256: "7".repeat(64),
      application_model_sha256: "8".repeat(64),
      seed: 20261015,
      draws: 200,
      rng: "numpy.random.Generator(PCG64(seed))",
    },
    design: {
      sha256: DESIGN_SHA,
      kind: "grid",
      base_revision: "ZS44A-20260920",
      points: 10,
      weight: 2.5,
      weight_basis: "grid_uniform",
      eligible_dwell_s: 0,
      reads: 0,
      plant_states: { eligible: 9 },
      placement: { placed: 9, load_below_model_floor: 3 },
      unplaced_dwell_s: {},
      weight_by_regime: { closed_loop: 0.5, lean_target: 1.5 },
      points_by_throttle_source: { stated: 9 },
      points_by_battery_source: { stated: 9 },
      battery_unmeasured_points: 0,
      battery_v_when_unmeasured: null,
      fill_byte_width: { max: 1, median: 1 },
      grid: {
        rpm_edges: [200, 600, 1000, 1400],
        load_edges: [3.5, 10.7, 17.8, 25, 32.2],
        load_axis: "fuel_comp_flow_axis",
        load_unit: "%",
        load_basis: "fill_fraction_as_manifold_ratio",
        environment: {
          baro_kpa: 80,
          iat_c: 25,
          coolant_c: 88,
          battery_v: 14,
          rail_kpa: 380,
          rail_kpa_source: "working_assumption",
          throttle_pct: 20,
          pedal_flag: 0,
        },
      },
    },
    candidate: {
      kind: "simulation_candidate",
      flashable: false,
      label: "light load 2 % richer",
      base_sha256: BASE_SHA,
      candidate_sha256: CANDIDATE_SHA,
      spec_sha256: "5".repeat(64),
      cells: [
        {
          table: "fuel_compensation",
          slot: 1,
          row: 0,
          col: 0,
          address: 919136,
          old: 120,
          new: 122,
        },
        {
          table: "fuel_compensation",
          slot: 1,
          row: 2,
          col: 2,
          address: 919186,
          old: 128,
          new: 131,
        },
      ],
      ranges: [],
      effect: {
        outcome: "traced",
        slot: 1,
        slot_source: "default_slot",
        changed_bytes: 2,
        changed_nodes: ["fuel_comp_lookup"],
        moved_lookups: [{ node: "target_afr_lookup", tables: ["fuel_targets"] }],
      },
    },
    cells: buildCellTable(),
    // A Fuel Comp edit reaches ignition timing outside the trims.
    ignition_timing: buildTimingBlock("fuel_comp"),
    spec_yaml: "schema_version: 1\nlabel: light load 2 % richer\n",
    spec_sha256: "5".repeat(64),
  };
}

// -- ignition timing (E09.7) --------------------------------------------------------------

/** The trims' axes in the fixture: rpm = raw x 50 (400..6,400), X = raw (8..128). The
 *  ECM's rpm word is the axis byte x 4 (`paired_scenarios.TRIM_RPM_AXIS_SCALE`). */
export const TRIM_AXIS_RAW = Array.from({ length: 16 }, (_, i) => 8 * (i + 1));

const TRIM_DECODE = {
  formula: "timing trim = (raw & 0x7F) × 1",
  kind: "linear" as const,
  quantity: "timing trim",
  unit: "count",
  scale: 1,
  offset: 0,
  numerator: null,
  mask: 0x7f,
  confidence: "verified" as const,
  evidence: "The trim interpolator masks every cell with 0x7F.",
};

/** One timing trim in Map 1, as the daemon lists it: a u8 grid whose bit 7 is a
 *  flag (set from column 8 on here), decoded on the low seven bits. Trim B reads
 *  three counts above A. */
export function timingTrimTable(which: "a" | "b"): RomTableValues {
  const lift = which === "b" ? 3 : 0;
  const raw = TRIM_AXIS_RAW.map((_, r) =>
    TRIM_AXIS_RAW.map((_, c) => (c >= 8 ? 0x80 : 0) | (40 + r + lift)),
  );
  const letter = which.toUpperCase();
  return {
    id: `timing_trim_${which}`,
    name: `Ignition timing trim ${letter}`,
    label: `Ignition timing trim ${letter} (Map 1)`,
    slot: 1,
    kind: `timing_trim_${which}`,
    role: "table",
    decode: TRIM_DECODE,
    offset: which === "a" ? 0xe0040 : 0xe0140,
    end: which === "a" ? 0xe013f : 0xe023f,
    rows: 16,
    cols: 16,
    dtype: "u8",
    unit: "count",
    raw,
    decoded: raw.map((row) => row.map((value) => value & 0x7f)),
    axes: {
      rows: {
        id: "timing_trim_rpm_axis",
        name: "Timing trim rpm axis",
        offset: 0xcc93,
        raw: TRIM_AXIS_RAW,
        decoded: TRIM_AXIS_RAW.map((value) => value * 50),
        unit: "rpm",
        decode: null,
        confidence: "verified",
      },
      cols: {
        id: "timing_trim_load_axis",
        name: "Timing trim load axis (BFS X counts)",
        offset: 0xcc83,
        raw: TRIM_AXIS_RAW,
        decoded: null,
        unit: null,
        decode: null,
        confidence: "verified",
      },
    },
    confidence: "verified",
    evidence: "Read from its RAM copy by 0x04A18C.",
  };
}

const CONDITIONS = [
  "every chain term but the trim term 0xFFFF2793 is equal on both ROMs: unidentified, never given a value",
  "trim path: 0xFFFF2833 bit 4 and 0xFFFF4785 bit 6 clear, no 0xFFFF28B4 bit 1 bypass",
  "rate limit at its fixed point with a nonzero rise allowance",
  "intermediate signed-byte clamps (0xFFFF1A58, 0xFFFF1346) not binding",
];
const TORQUE_REFUSALS = [
  "spark_response_unmeasured: spark has no measured torque response",
  "torque_plant_not_validated",
];
const KNOCK_REFUSALS = ["knock_unmodelled: no knock model"];
const TIMING_STATE = {
  high_det: true,
  knock_control: "fallback" as const,
  source: "stated: ROM Editor",
};

function trimCell(
  row: number,
  col: number,
  delta: number | null,
  reasons: string[],
  tableRead: "a" | "b" | null = "b",
) {
  return {
    row,
    col,
    rpm_word: (TRIM_AXIS_RAW[row] ?? 0) * 4,
    x: TRIM_AXIS_RAW[col] ?? 0,
    base: null,
    candidate: null,
    delta,
    trim_term_delta: delta,
    status: delta === null ? ("unevaluable" as const) : ("delta_if_no_clamp" as const),
    reasons,
    // A grid cell is read at a fixed X: 1B24's inputs do not move.
    charge_word_routes: [] as string[],
    table_read: tableRead,
  };
}

/** A cell whose change is known only as `lo`..`hi` (the 1B24 charge word swept over
 *  its band): `delta` null, the ends carrying the answer, as the backend gives it. */
function rangeCell(row: number, col: number, lo: number, hi: number, reasons: string[]) {
  return {
    ...trimCell(row, col, null, [
      "clamp_headroom_unknown",
      "charge_word_1b24_unresolved",
      ...reasons,
    ]),
    status: "range" as const,
    delta_min: lo,
    delta_max: hi,
    candidate_min: null,
    candidate_max: null,
    term_27c6_delta: [lo, hi] as [number, number],
    term_279a: "zero" as const,
  };
}

function summary(cells: IgnitionTimingBlock["cells"]): IgnitionTimingBlock["summary"] {
  const listed = cells ?? [];
  return {
    cells: 256,
    // The backend's rule (`paired_scenarios.timing_cell_summary`): a range counts as changed.
    changed: listed.filter(
      (cell) => cell.status === "range" || (cell.delta !== null && cell.delta !== 0),
    ).length,
    ranged: listed.filter((cell) => cell.status === "range").length,
    unevaluable: listed.filter((cell) => cell.status === "unevaluable").length,
    // The backend's rule (`paired_scenarios.timing_cell_summary`): only the
    // `trim_[ab]_not_read:` reasons, never `trim_term_floor_may_bind` (a trim the ECM reads).
    changed_trim_not_read: listed.filter((cell) =>
      cell.reasons.some((reason) => /^trim_[ab]_not_read:/.test(reason)),
    ).length,
    reasons: [...new Set(listed.flatMap((cell) => cell.reasons))].sort(),
  };
}

/** The charge word inputs a Fuel Comp / K edit moves (`charge_word_routes`). */
const FUEL_COMP_ROUTES = ["0xFFFF1736 via X", "0xFFFF175C via BFS"];

type PointOutcome = NonNullable<IgnitionTimingBlock["points"]>[number]["outcomes"][number];

/** One design point with the outcomes given (x_base / x_candidate filled in). */
function pointRow(
  point: number,
  rpm: number,
  outcomes: (Omit<PointOutcome, "trim_term_delta" | "x_base" | "x_candidate"> & {
    trim_term_delta?: number | null;
    x_candidate?: number | null;
  })[],
): NonNullable<IgnitionTimingBlock["points"]>[number] {
  return {
    point,
    rpm,
    rpm_word: rpm / 12.5,
    coolant_c: 88,
    pedal_flag: false,
    outcomes: outcomes.map((outcome) => ({
      trim_term_delta: 0,
      x_base: 33,
      x_candidate: 33,
      ...outcome,
    })),
  };
}

function points(reasons: string[], delta: number | null = null): IgnitionTimingBlock["points"] {
  return [
    {
      point: 0,
      rpm: 1200,
      rpm_word: 96,
      coolant_c: 88,
      pedal_flag: false,
      outcomes: [
        {
          status: delta === null ? "unevaluable" : "delta_if_no_clamp",
          delta,
          trim_term_delta: delta ?? 0,
          charge_word_routes: [],
          reasons,
          x_base: 33,
          x_candidate: 33,
          draws: 150,
        },
      ],
    },
    {
      point: 1,
      rpm: 1600,
      rpm_word: 128,
      coolant_c: 88,
      pedal_flag: false,
      outcomes: [
        {
          status: delta === null ? "unevaluable" : "delta_if_no_clamp",
          delta,
          trim_term_delta: delta ?? 0,
          charge_word_routes: [],
          reasons,
          x_base: 41,
          x_candidate: 41,
          draws: 50,
        },
      ],
    },
  ];
}

/** The `ignition_timing` block for the cases the page shows:
 *  `trim_b` -3 counts on two cells of trim B with the latch set (a number per cell),
 *  `trim_a` the same edit to trim A with the latch set (read nowhere: `NR`),
 *  `both` -2 counts on eight cells of row 8 (columns 4-11, across the flag edge) of
 *    BOTH trims with the latch set: trim B is read, so each cell shows -2 and names
 *    trim A as edited but not read (real ZS44A-20261004, the verifier's repro),
 *  `floor` trim B set to 3 counts (below the floor of 7) with the latch set: the
 *    trim IS read, the cell is unevaluable and nothing is "edited but not read",
 *  `fuel_comp` an edit outside the trims (`timing_input_changed`, no cells): its design
 *    points are a range (150 pairs, 1B24 swept independently on the two ROMs) and an
 *    unevaluable one (50 pairs, 279A not modelled),
 *  `range` a base-timing table edit computed on the trim grid: one cell with a change
 *    known only as -3..+1, one exact -2 cell, and points that are a range (150 pairs)
 *    and a nonzero exact change (50 pairs). */
export function buildTimingBlock(
  kind: "trim_b" | "trim_a" | "both" | "floor" | "fuel_comp" | "range",
): IgnitionTimingBlock {
  const base = {
    output: "ignition_timing_deg",
    model_output: "ignition_timing",
    units: "deg_btdc",
    side: "ecu: the timing byte's chain, no plant",
    conditions: CONDITIONS,
    torque: {
      status: "unsupported" as const,
      reason: TORQUE_REFUSALS[0],
      reasons: TORQUE_REFUSALS,
    },
    knock: { status: "unsupported" as const, reasons: KNOCK_REFUSALS },
  };
  if (kind === "fuel_comp") {
    const reasons = ["timing_input_changed:fuel_comp_lookup"];
    return {
      ...base,
      trims_changed: false,
      other_inputs_changed: reasons,
      status: "unevaluable",
      reasons,
      state: TIMING_STATE,
      points: [
        // A Fuel Comp edit moves X, and with it BFS: 1B24 is swept on each ROM.
        pointRow(0, 1200, [
          {
            status: "range",
            delta: null,
            delta_min: -2,
            delta_max: 1,
            term_27c6_delta: [-2, 1],
            term_279a: "zero",
            charge_word_routes: FUEL_COMP_ROUTES,
            reasons: [
              "clamp_headroom_unknown",
              "charge_word_1b24_unresolved",
              "charge_word_inputs_moved",
            ],
            draws: 150,
            x_candidate: 31,
          },
        ]),
        // 279A is unmodelled only where BFS moved (cold coolant here).
        pointRow(1, 1600, [
          {
            status: "unevaluable",
            delta: null,
            delta_min: null,
            delta_max: null,
            term_27c6_delta: [-1, 2],
            term_279a: "unmodelled",
            charge_word_routes: FUEL_COMP_ROUTES,
            reasons: ["term_279a_unmodelled", "charge_word_inputs_moved"],
            draws: 50,
            x_candidate: 39,
          },
        ]),
      ],
    };
  }
  if (kind === "range") {
    const cells = [
      rangeCell(3, 3, -3, 1, []),
      {
        ...trimCell(4, 9, -2, ["clamp_headroom_unknown"]),
        term_27c6_delta: [0, 0] as [number, number],
      },
    ];
    return {
      ...base,
      trims_changed: false,
      base_timing_tables_changed: true,
      other_inputs_changed: [],
      status: "evaluated",
      state: TIMING_STATE,
      cells_at: {
        coolant_c: 88,
        pedal_flag: false,
        grid: "the trims' own nodes: rpm word = axis byte x 4, X = load axis byte",
      },
      summary: summary(cells),
      cells,
      points: [
        pointRow(0, 1200, [
          {
            status: "range",
            delta: null,
            delta_min: -3,
            delta_max: 1,
            term_27c6_delta: [-3, 1],
            term_279a: "zero",
            charge_word_routes: [],
            reasons: ["clamp_headroom_unknown", "charge_word_1b24_unresolved"],
            draws: 150,
          },
        ]),
        pointRow(1, 1600, [
          {
            status: "computed",
            delta: -2,
            delta_min: -2,
            delta_max: -2,
            term_27c6_delta: [-2, -2],
            term_279a: "zero",
            charge_word_routes: [],
            reasons: [],
            draws: 50,
          },
        ]),
      ],
    };
  }
  const cells = {
    trim_b: [
      trimCell(3, 3, -3, ["clamp_headroom_unknown"]),
      trimCell(4, 9, -3, ["clamp_headroom_unknown"]),
    ],
    trim_a: [
      trimCell(3, 3, 0, ["trim_a_not_read:high_det_latch_set"]),
      trimCell(4, 9, 0, ["trim_a_not_read:high_det_latch_set"]),
    ],
    both: Array.from({ length: 8 }, (_, i) =>
      trimCell(7, 3 + i, -2, ["clamp_headroom_unknown", "trim_a_not_read:high_det_latch_set"]),
    ),
    floor: [trimCell(3, 5, null, ["trim_term_floor_may_bind"])],
  }[kind];
  return {
    ...base,
    trims_changed: true,
    other_inputs_changed: [],
    status: "evaluated",
    state: TIMING_STATE,
    cells_at: {
      coolant_c: 88,
      pedal_flag: false,
      grid: "the trims' own nodes: rpm word = axis byte x 4, X = load axis byte",
    },
    summary: summary(cells),
    cells,
    points: kind === "floor" ? points(["trim_term_floor_may_bind"]) : points([], 0),
  };
}

/** A request with every one of the 23 fields stated. */
export function buildRequest(): RomSimRequest {
  return {
    label: "light load 2 % richer",
    edits: [
      { table: "fuel_compensation", slot: 1, cells: [[0, 0]], scale: 1.02, rounding: "nearest" },
    ],
    grid: "fuel_comp",
    rpm_edges: [200, 600, 1000, 1400],
    load_edges: [3.5, 10.7, 17.8, 25, 32.2],
    load_basis: "fill_fraction_as_manifold_ratio",
    baro_kpa: 80,
    iat_c: 25,
    coolant_c: 88,
    battery_v: 14,
    rail_kpa: "working_assumption",
    throttle_pct: 20,
    pedal_flag: 0,
    stft_pct: 0,
    ltft_pct: 0,
    throttle_closed_learned: 0,
    idle_air_learned: 0,
    evap_purge_area: 0,
    high_det: "set",
    knock_control: "fallback",
    candidate_events: "ecu_reflash",
    draws: 200,
    seed: 20261015,
  };
}

export function buildRunContext(): SimRunContext {
  return {
    request: buildRequest(),
    edits: [
      {
        tableName: "Fuel Compensation",
        summary: "scale raw ×1.02 (+2 %, nearest) on 1 cell",
        chain: null,
      },
    ],
    base: { sha256: BASE_SHA, label: "Running tune", revision: "ZS44A-20260920" },
    wotColumnEdited: false,
    elapsedS: 12,
  };
}

// -- the base image's Fuel Comp breakpoints -----------------------------------------------

const RPM_RAW = Array.from({ length: 16 }, (_, i) => 8 * (i + 1));
/** 14 counts per step is 6.25 % of the model's 224 ceiling: 6.25 ... 100. */
const LOAD_RAW = Array.from({ length: 16 }, (_, i) => 14 * (i + 1));

function rpmAxis(): RomTableAxis {
  return {
    id: "fuel_comp_engine_speed_axis",
    name: "Fuel Compensation rpm axis (as the ECM reads it)",
    offset: 0xcacd,
    raw: RPM_RAW,
    decoded: RPM_RAW.map((value) => value * 50),
    unit: "rpm",
    decode: { quantity: "rpm", unit: "rpm", kind: "linear", confidence: "verified" },
    confidence: "verified",
  };
}

function loadAxis(): RomTableAxis {
  return {
    id: "fuel_comp_flow_axis",
    name: "Fuel Compensation load axis (throttle-model fill, alpha-N)",
    offset: 0xcb29,
    raw: LOAD_RAW,
    decoded: LOAD_RAW.map((value) => (value * 100) / 224),
    unit: "%",
    decode: { quantity: "throttle-model fill", unit: "%", kind: "linear", confidence: "verified" },
    confidence: "verified",
  };
}

/** Fuel Compensation (Map 1) with its two axes decoded, as the daemon sends
 *  them for a mapped image. `swap` puts the load axis in `rows` and the rpm
 *  axis in `cols`: the suggestion reads decode quantities, so it must not
 *  care which is which. */
export function fuelCompWithAxes(swap = false): RomTableValues {
  const table = fuelCompTable(1);
  return {
    ...table,
    axes: swap ? { rows: loadAxis(), cols: rpmAxis() } : { rows: rpmAxis(), cols: loadAxis() },
  };
}

/** The library's tables for an image whose Fuel Comp axes are decoded. */
export function tablesWithAxes(swap = false): RomTablesResponse {
  return {
    ...TABLES_WITH_FUEL_COMP,
    tables: [
      ...TABLES.tables,
      fuelCompWithAxes(swap),
      fuelCompTable(2),
      timingTrimTable("a"),
      timingTrimTable("b"),
    ],
  };
}
