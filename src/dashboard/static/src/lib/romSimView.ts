// What the Simulate results show, as pure functions of the cell table (W09.4).
// A port of `src/analysis/sim_report.py` (E08.6's HTML report) to the DOM, so
// the page and the report read one cell table the same way: same status
// words, same marks, same choice of which pairs a map cell speaks for.
//
// What this makes impossible rather than documented:
// - only a computed value gets a colour: `changed` and `unchanged` cells are
//   coloured on the diverging ramp (`lib/colorScale.ts`), centred at zero;
//   `unsupported`, `partly_supported`, `no_points` and `not_reached` each
//   have their own hatch and label, and `partly_supported` is never a fill;
// - a closed-loop pair is never drawn as a lambda effect: each map follows
//   `headline_by_loop`, so a loop kind's pairs show that loop's output (open
//   loop relative lambda, closed loop the total trim) from the cell's
//   `by_loop` block;
// - a cell whose pairs are all in another loop is its own mark, naming the
//   loop it does have.

import type { SimCell, SimCellTable, SimOutputCell, SimStatus } from "../types/romEditor";
import { divergingAround0, sequentialIn } from "./colorScale";

/** `sim_cells.STATUSES`, in that order. */
export const STATUSES: readonly SimStatus[] = [
  "changed",
  "unchanged",
  "unsupported",
  "partly_supported",
  "no_points",
  "not_reached",
];

/** A map cell's mark: one of the table's statuses, or `other_loop` (the cell
 *  has pairs, none in this map's loop) or `undefined` (the relative delta has
 *  no defined pair: every base is 0). */
export type Mark = SimStatus | "other_loop" | "undefined";

export const MARKS: readonly Mark[] = [...STATUSES, "other_loop", "undefined"];

/** The label printed in a cell of each non-value mark. */
export const MARK_LABELS: Record<Exclude<Mark, "changed" | "unchanged">, string> = {
  unsupported: "U",
  partly_supported: "P",
  no_points: "–",
  not_reached: "NR",
  other_loop: "XL",
  undefined: "∅",
};

/** The mark in words (cell descriptions, the legend, tests). */
export const MARK_WORDS: Record<Mark, string> = {
  changed: "changed",
  unchanged: "unchanged",
  unsupported: "unsupported",
  partly_supported: "partly supported",
  no_points: "no points",
  not_reached: "not reached",
  other_loop: "other loop",
  undefined: "relative change undefined",
};

export const MARK_MEANING: Record<Mark | "edited" | "axis_moved", string> = {
  changed: "coloured by the value",
  unchanged: "every supported pair exactly 0",
  unsupported: "no supported pair (hatched; a computed value is text only)",
  partly_supported: "hatched, never coloured; label = supported value, then supported share",
  no_points: "the design never visited the cell",
  not_reached: "compare_roms says the change cannot reach this output",
  other_loop:
    "another loop: every pair of the cell is in the loop named in it (OL open loop, CL closed loop, SP setpoint moved)",
  undefined: "every pair's base is 0",
  edited: "edited cell (raw old → new in its detail)",
  axis_moved: "a breakpoint the candidate moves (▲)",
};

export const LOOP_ABBREVIATIONS: Record<string, string> = {
  open_loop: "OL",
  closed_loop: "CL",
  closed_loop_setpoint_moved: "SP",
};

/** How each output is named and the unit of its delta. */
export const OUTPUT_LABELS: Record<string, { name: string; unit: string }> = {
  delivered_lambda: { name: "λ", unit: "" },
  base_fuel_schedule: { name: "BFS", unit: "word" },
  injector_pulse_ms: { name: "pulse", unit: "ms" },
  fuel_trim_total_pct: { name: "total fuel trim", unit: "% points" },
  target_phi: { name: "target φ", unit: "" },
  fuel_per_event_g: { name: "fuel per event", unit: "g" },
};

/** `sim_cells.NO_RELATIVE_DELTA`: the trim is already a percentage of fuel. */
const NO_RELATIVE_DELTA = new Set(["fuel_trim_total_pct"]);

/** The text of a `changed` cell whose median is exactly 0, and what it means
 *  (the legend prints both). */
export const CHANGED_ZERO_LABEL = "0*";
export const CHANGED_ZERO_MEANING =
  "some pairs move but the middle value is exactly 0 (not unchanged)";

/** Whether a map has a `changed` cell with a median of exactly 0. */
export function hasChangedZero(picks: readonly { mark: Mark; value: number | null }[]): boolean {
  return picks.some((pick) => pick.mark === "changed" && pick.value === 0);
}

export const COLOURED_MARKS: readonly Mark[] = ["changed", "unchanged"];

export const outputName = (output: string) => OUTPUT_LABELS[output]?.name ?? output;
export const outputUnit = (output: string) => OUTPUT_LABELS[output]?.unit ?? "";
export const isRelative = (output: string) => !NO_RELATIVE_DELTA.has(output);

// -- formatting -----------------------------------------------------------------------------

/** Four significant digits, no padding (`12350`, `0.001234`). */
export function g(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  return String(Number(value.toPrecision(digits)));
}

export function signed(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "0";
  return `${value > 0 ? "+" : ""}${g(value, digits)}`;
}

export function pct(share: number | null | undefined): string {
  return share === null || share === undefined ? "—" : `${(share * 100).toFixed(1)} %`;
}

/** A value short enough to sit in a cell. */
export function inCell(value: number, withSign = true): string {
  if (value === 0) return "0";
  const sign = withSign && value > 0 ? "+" : "";
  const mag = Math.abs(value);
  const digits = mag < 10 ? 2 : mag < 100 ? 1 : 0;
  return `${sign}${value.toFixed(digits)}`;
}

export function tick(value: number | null, breakpoint: number): string {
  if (value === null) return String(breakpoint);
  return Number.isInteger(value) ? String(value) : String(Number(value.toPrecision(4)));
}

export function weightUnit(table: SimCellTable): string {
  return table.design_kind === "drive" ? "s" : "grid weight";
}

export function loopsText(cell: SimCell): string {
  return [...cell.by_loop]
    .sort((a, b) => a.loop.localeCompare(b.loop))
    .map((block) => LOOP_ABBREVIATIONS[block.loop] ?? block.loop.slice(0, 2).toUpperCase())
    .join("/");
}

export function sharesText(shares: Record<string, number>): string {
  const entries = Object.entries(shares).sort(([a], [b]) => a.localeCompare(b));
  return entries.length === 0
    ? "—"
    : entries.map(([name, share]) => `${name} ${pct(share)}`).join(", ");
}

// -- what a map cell shows ------------------------------------------------------------------

export interface Pick {
  mark: Mark;
  /** The value the colour encodes (`changed` / `unchanged`), or the label's
   *  (`partly_supported`). */
  value: number | null;
  /** A computed value shown as text only (an unsupported cell's computed pairs). */
  textValue: number | null;
  /** The output cell the pick came from, for the detail. */
  source: SimOutputCell | null;
  output: string;
}

export type Quantity = "p50" | "width";

/** p50, or the p05-p95 width, of the relative delta in % (the delta itself
 *  for an output with no relative delta); null when undefined. */
function valueOf(oc: SimOutputCell, output: string, quantity: Quantity): number | null {
  const block = oc.quantiles;
  if (!block) return null;
  const relative = isRelative(output);
  const q = relative ? block.relative_delta : block.delta;
  if (!q) return null;
  const scale = relative ? 100 : 1;
  return quantity === "width" ? (q.p95 - q.p05) * scale : q.p50 * scale;
}

function pickOutput(oc: SimOutputCell, output: string, quantity: Quantity): Pick {
  const status = oc.status;
  if (status === "no_points" || status === "not_reached") {
    return { mark: status, value: null, textValue: null, source: oc, output };
  }
  const value = valueOf(oc, output, quantity);
  if (status === "unsupported") {
    return { mark: "unsupported", value: null, textValue: value, source: oc, output };
  }
  if (value === null)
    return { mark: "undefined", value: null, textValue: null, source: oc, output };
  return { mark: status, value, textValue: null, source: oc, output };
}

const noPoints = (output: string): Pick => ({
  mark: "no_points",
  value: null,
  textValue: null,
  source: null,
  output,
});

type Picker = (cell: SimCell) => Pick;

function headlinePick(loop: string, output: string, quantity: Quantity): Picker {
  return (cell) => {
    if (cell.pairs === 0) return noPoints(output);
    const block = cell.by_loop.find((item) => item.loop === loop);
    if (!block) {
      return { mark: "other_loop", value: null, textValue: null, source: null, output };
    }
    return pickOutput(block.cell, output, quantity);
  };
}

function outputPick(output: string, quantity: Quantity): Picker {
  return (cell) => {
    const oc = cell.outputs[output];
    return oc ? pickOutput(oc, output, quantity) : noPoints(output);
  };
}

/** The absolute delta's p50 (the pulse map is in ms, not relative). */
function deltaPick(output: string): Picker {
  return (cell) => {
    const oc = cell.outputs[output];
    if (!oc) return noPoints(output);
    const base = pickOutput(oc, output, "p50");
    if (base.mark === "no_points" || base.mark === "not_reached") return base;
    const delta = oc.quantiles?.delta.p50 ?? null;
    if (base.mark === "unsupported") return { ...base, value: null, textValue: delta };
    if (delta === null) return { ...base, mark: "undefined", value: null };
    return { mark: oc.status, value: delta, textValue: null, source: oc, output };
  };
}

export type ViewId = "lambda" | "width" | "bfs" | "pulse";

export const VIEWS: readonly { id: ViewId; label: string }[] = [
  { id: "lambda", label: "Relative Δλ p50" },
  { id: "width", label: "p05–p95 width" },
  { id: "bfs", label: "ΔBFS" },
  { id: "pulse", label: "Δpulse" },
];

export interface MapSpec {
  key: string;
  title: string;
  note: string;
  unit: string;
  scale: "diverging" | "sequential";
  pick: Picker;
}

const loopOrder = (loop: string): [number, string] => [
  loop === "open_loop" ? 0 : loop === "closed_loop" ? 1 : 2,
  loop,
];

function compareLoops(a: string, b: string): number {
  const [ra, na] = loopOrder(a);
  const [rb, nb] = loopOrder(b);
  return ra - rb || na.localeCompare(nb);
}

const quantityUnit = (output: string) => (isRelative(output) ? "%" : outputUnit(output));

/** The maps one view shows, and the reasons a map the view would show is
 *  absent. The headline view has one map per loop kind that has pairs. */
export function mapsForView(
  table: SimCellTable,
  view: ViewId,
): { maps: MapSpec[]; absent: string[] } {
  const maps: MapSpec[] = [];
  const absent: string[] = [];
  const present = new Set<string>();
  for (const cell of table.cells) for (const block of cell.by_loop) present.add(block.loop);

  if (view === "lambda" || view === "width") {
    const loops = Object.entries(table.headline_by_loop)
      .filter(([loop]) => view === "lambda" || loop === "open_loop")
      .sort(([a], [b]) => compareLoops(a, b));
    for (const [loop, output] of loops) {
      const name = outputName(output);
      if (!present.has(loop)) {
        absent.push(`no ${loop} pairs in this run, so no ${name} map for it`);
        continue;
      }
      const relative = isRelative(output);
      const what = relative ? `relative Δ${name}` : `Δ${name}`;
      if (view === "lambda") {
        maps.push({
          key: `headline-${loop}`,
          title: `${what} p50, ${loop} pairs (${output}, the table's headline for ${loop})`,
          note:
            output === "delivered_lambda"
              ? "candidate − base over base; negative is richer"
              : `candidate − base${relative ? "" : "; this output has no relative change"}`,
          unit: quantityUnit(output),
          scale: "diverging",
          pick: headlinePick(loop, output, "p50"),
        });
      } else {
        maps.push({
          key: `width-${loop}`,
          title: `p05–p95 width of ${what}, ${loop} pairs`,
          note: "the spread over (point, draw) pairs: parameter uncertainty and the cell's operating-point mix, never structural error",
          unit: quantityUnit(output),
          scale: "sequential",
          pick: headlinePick(loop, output, "width"),
        });
      }
    }
    if (view === "width" && !table.headline_by_loop.open_loop) {
      absent.push("the table has no open_loop headline, so no width map");
    }
    return { maps, absent };
  }

  const output = view === "bfs" ? "base_fuel_schedule" : "injector_pulse_ms";
  if (!table.outputs.includes(output)) {
    absent.push(`the table has no ${output}`);
    return { maps, absent };
  }
  const name = outputName(output);
  if (view === "bfs") {
    maps.push({
      key: `output-${output}`,
      title: `relative Δ${name} p50, all pairs (${output})`,
      note: "candidate − base over base; positive is more fuel",
      unit: "%",
      scale: "diverging",
      pick: outputPick(output, "p50"),
    });
  } else {
    maps.push({
      key: `output-${output}`,
      title: `Δ${name} p50, all pairs (${output})`,
      note: "candidate − base, milliseconds",
      unit: outputUnit(output),
      scale: "diverging",
      pick: deltaPick(output),
    });
  }
  return { maps, absent };
}

/** The text in a map cell, one or two short lines. */
export function cellLines(pick: Pick, cell: SimCell, withSign: boolean): string[] {
  switch (pick.mark) {
    case "changed":
      // A changed cell whose middle value is exactly 0 (some pairs move, most
      // do not) must not read as `unchanged`'s "0": the star says so.
      return [pick.value === 0 ? CHANGED_ZERO_LABEL : inCell(pick.value ?? 0, withSign)];
    case "unchanged":
      return ["0"];
    case "partly_supported": {
      const share = pick.source?.supported_share;
      return [
        `P${inCell(pick.value ?? 0, withSign)}`,
        `· ${share === null || share === undefined ? "?" : `${(share * 100).toFixed(0)}%`}`,
      ];
    }
    case "unsupported":
      // The hatch and the label say unsupported; a computed number is text.
      return pick.textValue === null
        ? [MARK_LABELS.unsupported]
        : [MARK_LABELS.unsupported, inCell(pick.textValue, withSign)];
    case "other_loop":
      return [loopsText(cell)];
    default:
      return [MARK_LABELS[pick.mark]];
  }
}

/** One cell in a sentence (its accessible name, its hover). */
export function describePick(table: SimCellTable, cell: SimCell, pick: Pick, unit: string): string {
  const rows = table.rows.unit ?? "raw";
  const cols = table.cols.unit ?? "raw";
  const where = `Row ${cell.row + 1}, column ${cell.col + 1} (${tick(cell.row_value, cell.row_breakpoint)} ${rows}, ${tick(cell.col_value, cell.col_breakpoint)} ${cols})`;
  let what = MARK_WORDS[pick.mark];
  if (pick.mark === "changed" && pick.value === 0) {
    what += `, median exactly 0${unit ? ` ${unit}` : ""} (some pairs move)`;
  } else if (pick.mark === "changed" || pick.mark === "unchanged") {
    what += pick.value !== null ? ` ${signed(pick.value)}${unit ? ` ${unit}` : ""}` : "";
  } else if (pick.mark === "partly_supported") {
    what += ` ${signed(pick.value)}${unit ? ` ${unit}` : ""}, ${pct(pick.source?.supported_share)} supported`;
  } else if (pick.mark === "unsupported" && pick.textValue !== null) {
    what += ` (computed only: ${signed(pick.textValue)}${unit ? ` ${unit}` : ""})`;
  } else if (pick.mark === "other_loop") {
    what += `: its pairs are ${loopsText(cell)}`;
  }
  const extras = [
    cell.edited ? `edited, raw ${cell.edited.old} to ${cell.edited.new}` : null,
    cell.row_axis_moved || cell.col_axis_moved ? "a breakpoint moves" : null,
  ].filter(Boolean);
  return `${where}: ${what}${extras.length ? `; ${extras.join("; ")}` : ""}`;
}

// -- colour ---------------------------------------------------------------------------------

const PATTERN_INK = "#59636e";
const HATCH_BASE = "#e3e6ea";

/** The hatches, fixed colours: data, not chrome, the same in both themes. */
const HATCH: Record<
  Exclude<Mark, "changed" | "unchanged">,
  { backgroundColor: string; backgroundImage: string; backgroundSize?: string }
> = {
  unsupported: {
    backgroundColor: HATCH_BASE,
    backgroundImage: `repeating-linear-gradient(45deg, ${PATTERN_INK} 0 1.6px, transparent 1.6px 6px)`,
  },
  not_reached: {
    backgroundColor: HATCH_BASE,
    backgroundImage: `repeating-linear-gradient(0deg, ${PATTERN_INK} 0 1.2px, transparent 1.2px 5px)`,
  },
  no_points: {
    backgroundColor: "#f7f8fa",
    backgroundImage: "radial-gradient(circle, #8a96a3 0.9px, transparent 1.2px)",
    backgroundSize: "6px 6px",
  },
  partly_supported: {
    backgroundColor: "#f4f1e6",
    backgroundImage: `repeating-linear-gradient(-45deg, ${PATTERN_INK} 0 1px, transparent 1px 5px), repeating-linear-gradient(45deg, ${PATTERN_INK} 0 1px, transparent 1px 5px)`,
  },
  other_loop: {
    backgroundColor: "#eef0f3",
    backgroundImage:
      "repeating-linear-gradient(0deg, #9aa5b1 0 1px, transparent 1px 8px), repeating-linear-gradient(90deg, #9aa5b1 0 1px, transparent 1px 8px)",
  },
  undefined: {
    backgroundColor: "#eef0f3",
    backgroundImage:
      "repeating-linear-gradient(45deg, #9aa5b1 0 1px, transparent 1px 7px), repeating-linear-gradient(-45deg, #9aa5b1 0 1px, transparent 1px 7px)",
  },
};

export interface MarkStyle {
  backgroundColor: string;
  backgroundImage?: string;
  backgroundSize?: string;
}

/** The largest magnitude among the coloured cells: the ramp's extent. */
export function extentOf(picks: readonly Pick[]): number {
  let extent = 0;
  for (const pick of picks) {
    if (COLOURED_MARKS.includes(pick.mark) && pick.value !== null) {
      extent = Math.max(extent, Math.abs(pick.value));
    }
  }
  return extent;
}

/** A value's colour on the map's ramp: diverging is centred at zero (the
 *  neutral), sequential runs from 0 to the extent. */
export function rampColour(scale: MapSpec["scale"], value: number, extent: number): string {
  return scale === "diverging" ? divergingAround0(value, extent) : sequentialIn(value, 0, extent);
}

/** A coloured mark's fill from its value; every other mark is a hatch. */
export function markStyle(pick: Pick, scale: MapSpec["scale"], extent: number): MarkStyle {
  if (COLOURED_MARKS.includes(pick.mark) && pick.value !== null) {
    return { backgroundColor: rampColour(scale, pick.value, extent) };
  }
  return HATCH[pick.mark as Exclude<Mark, "changed" | "unchanged">];
}

/** The legend bar's CSS gradient for a map. */
export function rampGradient(scale: MapSpec["scale"], extent: number): string {
  const stops =
    scale === "diverging"
      ? [-extent, 0, extent].map((value) => rampColour(scale, value, extent))
      : [0, extent].map((value) => rampColour(scale, value, extent));
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

/** The hatch of a mark, for the legend swatches. */
export function hatchStyle(mark: Exclude<Mark, "changed" | "unchanged">): MarkStyle {
  return HATCH[mark];
}

/** Marks the map uses, in the table's order, for its legend. */
export function marksInUse(picks: readonly Pick[]): Mark[] {
  const used = new Set(picks.map((pick) => pick.mark));
  return MARKS.filter((mark) => used.has(mark));
}

// -- one cell's detail ----------------------------------------------------------------------

export function quantileLines(oc: SimOutputCell, output: string): string[] {
  const q = oc.quantiles;
  if (!q) return [];
  const unit = outputUnit(output);
  const lines = [
    `over ${q.over}: ${q.pairs} pairs, weight ${g(q.weight)}`,
    `Δ p05/p50/p95 ${signed(q.delta.p05)} / ${signed(q.delta.p50)} / ${signed(q.delta.p95)}${unit ? ` ${unit}` : ""}`,
  ];
  if (q.relative_delta) {
    const r = q.relative_delta;
    lines.push(
      `relative p05/p50/p95 ${signed(r.p05 * 100)} / ${signed(r.p50 * 100)} / ${signed(r.p95 * 100)} %`,
    );
  } else if (isRelative(output)) {
    lines.push("relative: undefined");
  }
  if (q.relative_undefined_pairs) {
    lines.push(`relative undefined at ${q.relative_undefined_pairs} pairs (base 0)`);
  }
  return lines;
}

export function supportLines(oc: SimOutputCell): string[] {
  const lines: string[] = [];
  if (oc.supported_share !== null) {
    lines.push(`supported ${pct(oc.supported_share)}, unsupported ${pct(oc.unsupported_share)}`);
  }
  if (oc.changed_share !== null) {
    lines.push(`changed share of supported weight ${pct(oc.changed_share)}`);
  }
  if (oc.supported_effect) lines.push(`supported pairs show: ${oc.supported_effect}`);
  for (const entry of oc.support) {
    if (entry.reasons.length > 0) {
      lines.push(
        `reasons (${pct(entry.weight_share)}, ${entry.pairs} pairs): ${entry.reasons.join(", ")}`,
      );
    }
  }
  return lines;
}

/** The table's moved axes, keyed `rows:3` / `cols:5`. */
export function movedAxes(table: SimCellTable): Set<string> {
  return new Set(table.overlay.axis_edits.map((edit) => `${edit.axis}:${edit.index}`));
}

/** `paired_scenarios.PLANT_ASSUMPTION_PREFIX` and `sim_montecarlo.DESIGN_ASSUMPTION_PREFIX`:
 *  how the result's `assumptions` say whose they are (E08.10). */
export const PLANT_ASSUMPTION_PREFIX = "plant working assumption: ";
export const DESIGN_ASSUMPTION_PREFIX = "design: ";

/** The result's `assumptions` in groups, derived from the prefixes: the plant's
 *  working assumptions, the design's own, and anything unprefixed. Each string
 *  once, whole (the prefix stays), in the order given. */
export function groupAssumptions(assumptions: readonly string[]): {
  plant: string[];
  design: string[];
  other: string[];
} {
  const unique = [...new Set(assumptions)];
  return {
    plant: unique.filter((item) => item.startsWith(PLANT_ASSUMPTION_PREFIX)),
    design: unique.filter((item) => item.startsWith(DESIGN_ASSUMPTION_PREFIX)),
    other: unique.filter(
      (item) =>
        !item.startsWith(PLANT_ASSUMPTION_PREFIX) && !item.startsWith(DESIGN_ASSUMPTION_PREFIX),
    ),
  };
}
