// A hand-built simulate result (W09.4): a 3 x 4 Fuel Comp grid whose cells
// cover EVERY status of `sim_cells.STATUSES` in the open-loop lambda map, plus
// a closed-loop-only cell, a mixed cell, a moved-setpoint cell, a relative
// delta with no defined pair, edited cells and a moved breakpoint. The shape
// is `sim_cells.CellTable.to_dict()` and `rom_editor.simulate_edits`; the
// numbers are invented, not a simulation.

import type { RomTableAxis, RomTableValues, RomTablesResponse } from "../types/romLibrary";
import type {
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
    assumptions: [
      "the pedal-enrichment flag is stated, not logged",
      "rail pressure is the plant's working assumption",
    ],
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
      assumptions: ["grid load: the fill fraction is taken as MAP / baro"],
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
    spec_yaml: "schema_version: 1\nlabel: light load 2 % richer\n",
    spec_sha256: "5".repeat(64),
  };
}

/** A request with every one of the 21 fields stated. */
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
    tables: [...TABLES.tables, fuelCompWithAxes(swap), fuelCompTable(2)],
  };
}
