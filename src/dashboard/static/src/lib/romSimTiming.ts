// Pure helpers for the Simulate result's ignition timing section (E09.7): the
// stated latch and knock control in words, the trims' own 16 x 16 grid built
// from the block's cells and the base's trim axes, one mark per cell, and the
// reasons with what each means. The block is `sim_cells.ignition_timing_block`
// plus `ignition_timing_points`; nothing here computes a timing, it only
// reads one.
//
// What this makes impossible rather than documented:
// - a cell the block does not list is "no change" only because the block lists
//   every cell that changes or names a reason; a cell that names a reason is
//   never drawn as 0 (a trim the stated latch does not read, with no change in
//   the cell, is `NR`; an unevaluable cell is `U`; a range is its two ends), a number the block gives is
//   never drawn over by a note, and every reason on screen carries what it means;
// - the grid's labels come from the base's own trim axes and are checked
//   against the coordinates the cells carry: a mismatch is said, never drawn;
// - a reason this page has no sentence for still shows, as its code.

import type {
  IgnitionTimingBlock,
  SimKnockControl,
  TimingCell,
  TimingPointRow,
} from "../types/romEditor";
import type { RomTableValues } from "../types/romLibrary";
import { formatCell } from "./romLibrary";

/** The reason that carries `charge_word_routes`. */
const CHARGE_MOVED = "charge_word_inputs_moved";

/** Reasons the block can name, in words. The key is the whole reason, or its
 *  prefix up to the first `:` for the families below. */
const REASON_TEXT: Record<string, string> = {
  clamp_headroom_unknown:
    "A design has no logged timing, so the 50-degree cap and the -7 floor cannot be checked: the change holds if no clamp binds.",
  charge_word_1b24_unresolved:
    "The base timing term 0xFFFF27C6 reads the charge word 0xFFFF1B24, which is not computed. It is swept over its plausible band (0x4000 x 0.7 to 1.3), held equal on both ROMs when the edit leaves its inputs alone, so the change is known only as a range.",
  charge_word_inputs_moved:
    "The edit moves inputs of the charge word 0xFFFF1B24 (the inputs it moves are listed beside this reason), so 1B24 is not the same on both ROMs: it is swept over its plausible band independently on each.",
  term_279a_unmodelled:
    "The edit moves the inputs of the base timing term 0xFFFF279A, and 279A is not provably 0 here (for example at cold coolant) and is not modelled: no timing change is given.",
  term_1512_unmodelled:
    "Calibration 0x8538 is nonzero, which brings 0xFFFF26F2 (unidentified) into the base timing term 0xFFFF27C6 through 0xFFFF1512: that term is not computed, so no timing change is given.",
  high_det_forced_by_ks_fault:
    "Refused: knock control in fallback with the HI DET latch clear is not a running steady state. With the KS fault present the firmware sets HI DET on every pass while the engine runs (states 2 to 4 of 0x027CC4; engine running inferred).",
  enrichment_retard_unmodelled:
    "The edit moves the target phi, which moves the enrichment retard (0xFFFF174A, not computed): only the trim term's change is given, not a timing change.",
  trim_term_floor_may_bind:
    "A selected trim is below 7 counts, where the trim term's floor (0xFFFF11CB, 0 to 7) may bind: no change is claimed.",
  load_input_x_not_computed:
    "The load input X is not computed for this pair (its base fuel schedule is not a finite number), so the trims cannot be read.",
  coolant_blend_band:
    "Coolant is in the 56 to 60 °C band where the ECM blends the two trims: not modelled.",
  high_det_latch_unknown: "The HI DET latch was not stated, so the selected trim is not known.",
  pedal_flag_unknown: "The pedal flag is not known, so the load column is not known.",
  timing_state_unstated: "No HI DET latch or knock control state was stated.",
  unknown_bytes_changed:
    "A changed byte is in no node of the application model; nothing says it cannot reach ignition timing.",
};

const NOT_READ = /^trim_([ab])_not_read:(.+)$/;
const NOT_READ_WHY: Record<string, string> = {
  high_det_latch_set: "the HI DET latch is set, so the ECM reads trim B warm",
  high_det_latch_clear: "the HI DET latch is clear, so the ECM reads trim A warm",
  coolant_at_or_below_56c: "at or below 56 °C the ECM reads trim A only",
};

/** What a reason means, or null for one this page has no sentence for (it is
 *  then shown as its code alone). */
export function timingReasonText(reason: string): string | null {
  const known = REASON_TEXT[reason];
  if (known) return known;
  const notRead = NOT_READ.exec(reason);
  if (notRead?.[1] && notRead[2]) {
    const why = NOT_READ_WHY[notRead[2]] ?? notRead[2];
    return `Trim ${notRead[1].toUpperCase()} was edited here, but ${why}: that edit changes nothing the ECM reads at this cell.`;
  }
  const node = /^timing_input_changed:(.+)$/.exec(reason);
  if (node?.[1]) {
    return `An edited byte outside the trims reaches ignition timing through ${node[1]}. The trims' grid holds that change's terms equal, so no timing change is given for this edit; edit the trims on their own to see theirs.`;
  }
  const effects = /^effects_unevaluable:(.+)$/.exec(reason);
  if (effects?.[1]) return `The ROM comparison could not trace this edit (${effects[1]}).`;
  return null;
}

/** The stated state in words. */
export function stateText(state: NonNullable<IgnitionTimingBlock["state"]>): string {
  const knock: Record<SimKnockControl, string> = {
    active: "knock control active",
    fallback: "knock control in fallback (knock sensor fault)",
  };
  return `HI DET latch ${state.high_det ? "set (warm timing reads trim B)" : "clear (warm timing reads trim A)"}, ${knock[state.knock_control]}`;
}

// -- the grid ---------------------------------------------------------------------------------

export type MarkKind = "changed" | "range" | "unchanged" | "not_read" | "unevaluable";

export interface TimingMark {
  kind: MarkKind;
  /** What the cell shows: `-3`, `+2`, `-3..+1` (a range), `·`, `NR`, `U`. */
  text: string;
  /** The hover and screen-reader sentence. */
  title: string;
}

const NO_MARK: TimingMark = {
  kind: "unchanged",
  text: "·",
  title: "no change in timing",
};

/** The signed degrees a delta shows as. */
export function signed(delta: number): string {
  return delta > 0 ? `+${delta}` : String(delta);
}

/** The interval a `range` outcome carries, or null when it is not one (or carries
 *  no ends, which the page then does not invent). */
export function rangeEnds(item: {
  status: string;
  delta_min?: number | null;
  delta_max?: number | null;
}): [number, number] | null {
  if (item.status !== "range") return null;
  const lo = item.delta_min;
  const hi = item.delta_max;
  return typeof lo === "number" && typeof hi === "number" ? [lo, hi] : null;
}

/** `lo..hi` in signed degrees; an interval whose ends are equal is the value. */
export function rangeText([lo, hi]: [number, number]): string {
  return lo === hi ? signed(lo) : `${signed(lo)}..${signed(hi)}`;
}

/** One cell's mark. A range is drawn as its ends (`-3..+1`), never as `U`: the
 *  cell has an answer, an interval. A number the block gives is the cell's mark: a non-zero
 *  `delta` is shown as signed degrees whatever else the cell names (a trim edited
 *  beside the one the ECM reads is said in the hover text, never drawn over the
 *  number). A reason is never drawn as 0: a cell whose edited trim the stated
 *  latch does not read, and whose delta is 0, is `NR`; an unevaluable cell is
 *  `U`. A cell the block does not list is no change (the block lists every cell
 *  that changes or names a reason). */
export function timingMark(cell: TimingCell | null): TimingMark {
  if (cell === null) return NO_MARK;
  const ends = rangeEnds(cell);
  if (ends) {
    const [lo, hi] = ends;
    if (lo === hi && lo === 0) return { kind: "unchanged", text: "0", title: "0 deg" };
    const above =
      typeof cell.candidate_min === "number" && typeof cell.candidate_max === "number"
        ? `; candidate between ${cell.candidate_min} and ${cell.candidate_max} deg`
        : "";
    const moved = cell.reasons.includes(CHARGE_MOVED) ? (cell.charge_word_routes ?? []) : [];
    const via = moved.length > 0 ? `; charge word inputs moved: ${moved.join(", ")}` : "";
    return {
      kind: lo === hi ? "changed" : "range",
      text: rangeText(ends),
      title: `${lo === hi ? signed(lo) : `between ${signed(lo)} and ${signed(hi)}`} deg${above}; known only as a range: ${cell.reasons.join(", ") || "no reason given"}${via}`,
    };
  }
  if (cell.status === "unevaluable" || cell.delta === null) {
    return {
      kind: "unevaluable",
      text: "U",
      title: `unevaluable: ${cell.reasons.join(", ") || "no reason given"}`,
    };
  }
  const unread = cell.reasons.filter((reason) => NOT_READ.test(reason));
  if (cell.delta !== 0) {
    const noClamp = cell.reasons.includes("clamp_headroom_unknown");
    return {
      kind: "changed",
      text: signed(cell.delta),
      title:
        `${signed(cell.delta)} deg${noClamp ? " if no clamp binds" : ""}` +
        (unread.length > 0 ? `; also edited here but not read: ${unread.join(", ")}` : ""),
    };
  }
  if (unread.length > 0) {
    return {
      kind: "not_read",
      text: "NR",
      title: `edited trim not read: ${unread.join(", ")}`,
    };
  }
  return { kind: "unchanged", text: "0", title: "0 deg" };
}

export interface TimingGrid {
  /** One label per trim row (rpm), one per column (X counts). */
  rowLabels: string[];
  colLabels: string[];
  /** Where the labels came from; `cells` when the base's trim table was not given
   *  or did not match, and then they are the cells' own coordinates. */
  labelsFrom: "trim axes" | "cells";
  /** Row-major; null where the block lists no cell (unchanged). */
  cells: (TimingCell | null)[][];
  /** A sentence when the page's trim axes disagree with the result's cells. */
  mismatch: string | null;
}

const TRIM_ROWS = 16;
const TRIM_COLS = 16;

function trimTable(tables: readonly RomTableValues[]): RomTableValues | null {
  return (
    tables.find(
      (table) =>
        table.slot === 1 &&
        table.role !== "axis" &&
        (table.kind === "timing_trim_a" || table.kind === "timing_trim_b") &&
        (table.raw.length === TRIM_ROWS || table.raw[0]?.length === TRIM_COLS),
    ) ?? null
  );
}

/** The trims' 16 x 16 grid for `block`, labelled from the base's own trim axes
 *  (rpm down, X counts across) when `tables` has them and they agree with the
 *  coordinates the listed cells carry. Null when the block has no cells. */
export function timingGrid(
  block: IgnitionTimingBlock,
  tables: readonly RomTableValues[],
): TimingGrid | null {
  if (block.status !== "evaluated" || !block.cells) return null;
  const rows = TRIM_ROWS;
  const cols = TRIM_COLS;
  const cells: (TimingCell | null)[][] = Array.from({ length: rows }, () =>
    Array.from({ length: cols }, () => null),
  );
  for (const cell of block.cells) {
    const line = cells[cell.row];
    if (line && cell.col >= 0 && cell.col < cols) line[cell.col] = cell;
  }
  const trim = trimTable(tables);
  const rowAxis = trim?.axes.rows ?? null;
  const colAxis = trim?.axes.cols ?? null;
  const listed = block.cells;
  // A cell's rpm word is its row's axis byte times one constant for the whole table
  // (the ECM's word is a multiple of the byte); the constant is read off the cells,
  // never assumed. A cell whose X is not its column's axis byte is a mismatch too.
  const ratios = listed.map((cell) => cell.rpm_word / (rowAxis?.raw[cell.row] ?? Number.NaN));
  const scale = ratios[0];
  const mismatched = listed.filter(
    (cell, i) =>
      colAxis?.raw[cell.col] !== cell.x ||
      !(scale !== undefined && scale > 0 && Math.abs((ratios[i] ?? Number.NaN) - scale) < 1e-9),
  );
  if (rowAxis && colAxis && rowAxis.raw.length === rows && colAxis.raw.length === cols) {
    if (mismatched.length === 0) {
      return {
        rowLabels: rowAxis.raw.map((raw, r) => formatCell(rowAxis.decoded?.[r] ?? raw)),
        colLabels: colAxis.raw.map((raw, c) => formatCell(colAxis.decoded?.[c] ?? raw)),
        labelsFrom: "trim axes",
        cells,
        mismatch: null,
      };
    }
    return {
      ...fromCells(listed, rows, cols),
      cells,
      mismatch: `${mismatched.length} listed ${mismatched.length === 1 ? "cell is" : "cells are"} at coordinates the base's trim axes do not give (for example row ${(mismatched[0]?.row ?? 0) + 1}, column ${(mismatched[0]?.col ?? 0) + 1}); the labels below are the result's own.`,
    };
  }
  return { ...fromCells(listed, rows, cols), cells, mismatch: null };
}

/** Labels from the coordinates the listed cells carry; an unlisted row or
 *  column is only its number. */
function fromCells(
  listed: readonly TimingCell[],
  rows: number,
  cols: number,
): Pick<TimingGrid, "rowLabels" | "colLabels" | "labelsFrom"> {
  const rowLabels = Array.from({ length: rows }, (_, r) => {
    const found = listed.find((cell) => cell.row === r);
    return found ? `rpm word ${found.rpm_word}` : String(r + 1);
  });
  const colLabels = Array.from({ length: cols }, (_, c) => {
    const found = listed.find((cell) => cell.col === c);
    return found ? `X ${found.x}` : String(c + 1);
  });
  return { rowLabels, colLabels, labelsFrom: "cells" };
}

// -- the reasons ------------------------------------------------------------------------------

export interface ReasonCount {
  reason: string;
  /** Trim cells that name it. */
  cells: number;
  /** (point, draw) pairs at the design's points that name it. */
  pairs: number;
  /** What it means, or null when this page has no sentence for it. */
  text: string | null;
  /** For `charge_word_inputs_moved`: the charge word inputs the edit moves, as
   *  the cells and outcomes that name the reason give them (in first-seen order). */
  routes: string[];
}

/** Every reason on the block, with how many trim cells and design pairs name it. */
export function reasonCounts(block: IgnitionTimingBlock): ReasonCount[] {
  const cells = new Map<string, number>();
  const pairs = new Map<string, number>();
  const bump = (map: Map<string, number>, reason: string, by: number) =>
    map.set(reason, (map.get(reason) ?? 0) + by);
  for (const reason of block.reasons ?? []) bump(cells, reason, 0);
  for (const reason of block.other_inputs_changed) bump(cells, reason, 0);
  const routes = new Set<string>();
  const collect = (item: { reasons: string[]; charge_word_routes?: string[] | null }) => {
    if (item.reasons.includes(CHARGE_MOVED)) {
      for (const route of item.charge_word_routes ?? []) routes.add(route);
    }
  };
  for (const cell of block.cells ?? []) {
    collect(cell);
    for (const reason of cell.reasons) bump(cells, reason, 1);
  }
  for (const point of block.points ?? []) {
    for (const outcome of point.outcomes) {
      collect(outcome);
      for (const reason of outcome.reasons) bump(pairs, reason, outcome.draws);
    }
  }
  const all = new Set([...cells.keys(), ...pairs.keys()]);
  return [...all]
    .sort((a, b) => a.localeCompare(b))
    .map((reason) => ({
      reason,
      cells: cells.get(reason) ?? 0,
      pairs: pairs.get(reason) ?? 0,
      text: timingReasonText(reason),
      routes: reason === CHARGE_MOVED ? [...routes] : [],
    }));
}

export interface PointSummary {
  /** (point, draw) pairs in all. */
  pairs: number;
  points: number;
  byStatus: Record<string, number>;
  /** Pairs that carry a number for the timing change (0 included). With none, "how
   *  many show a change" has no answer, and the page says nothing about it. */
  numeric: number;
  /** Pairs whose timing change is a nonzero number of degrees. */
  changed: number;
  /** Pairs whose timing change is known only as an interval (`range`). */
  ranged: number;
}

/** The design's points, counted: pairs by status, and how many show a change. */
export function pointSummary(points: readonly TimingPointRow[] | undefined): PointSummary {
  const out: PointSummary = {
    pairs: 0,
    points: points?.length ?? 0,
    byStatus: {},
    numeric: 0,
    changed: 0,
    ranged: 0,
  };
  for (const point of points ?? []) {
    for (const outcome of point.outcomes) {
      out.pairs += outcome.draws;
      out.byStatus[outcome.status] = (out.byStatus[outcome.status] ?? 0) + outcome.draws;
      const ends = rangeEnds(outcome);
      if (ends && ends[0] !== ends[1]) {
        out.ranged += outcome.draws;
        continue;
      }
      const delta = ends ? ends[0] : outcome.delta;
      if (delta === null) continue;
      out.numeric += outcome.draws;
      if (delta !== 0) out.changed += outcome.draws;
    }
  }
  return out;
}

/** `unsupported (spark_response_unmeasured: ...)` for a torque or knock claim. */
export function claimLine(claim: { status: string; reason?: string | null; reasons: string[] }) {
  if (claim.status === "supported") return "supported";
  const first = claim.reason ?? claim.reasons[0] ?? null;
  const more = claim.reasons.length > 1 ? ` (+${claim.reasons.length - 1} more)` : "";
  return `unsupported${first ? `: ${first}${more}` : ""}`;
}
