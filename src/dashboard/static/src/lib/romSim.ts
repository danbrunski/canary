// Pure helpers for the ROM Editor's Simulate form (W09.4): the form's values
// as text (so empty is distinguishable from 0), turning them into the
// simulate route's request, the grid edges the base image suggests, what the
// browser remembers between visits, and the route's failures by name.
//
// What this makes impossible rather than documented:
// - nothing is defaulted: an empty field is `missing` and blocks the request,
//   and every request field is named (`SIM_REQUEST_FIELDS`, 21 of them, the
//   whole of `rom_editor.SimRequest`);
// - a remembered value is never applied without being shown: the form seeds
//   its fields from `loadRemembered` and marks them, and `candidate_events`,
//   `grid` and the edges are never remembered at all (a choice about *this*
//   experiment, or derived from *this* base image);
// - the suggested edges are derived from the base image's Fuel Comp
//   breakpoints by decode quantity, never by "rows" (UpRev labels them the
//   other way round), and a suggestion that does not put each grid point on
//   its breakpoint says so.
//
// The server (`rom_editor.SimRequest`, `sim_design.build_grid_design`) stays
// the authority: these checks only stop a request that cannot be valid.

import { HttpError } from "../api/http";
import { describeError } from "../hooks/useControlResource";
import { isFuelTargets } from "./romLibrary";
import type {
  RomEdit,
  RomSimRequest,
  SimCandidateEvents,
  SimFailure,
  SimGrid,
  SimLoadBasis,
} from "../types/romEditor";
import type { RomTableAxis, RomTableValues } from "../types/romLibrary";

/** `rom_editor.MAX_GRID_POINTS` and `MAX_DRAWS`: refused server-side before
 *  any work (`grid_too_large`, `draws_too_many`). */
export const MAX_GRID_POINTS = 1024;
export const MAX_DRAWS = 2000;
/** Fuel Comp's load axis spans 0-100 % of the fill model's ceiling
 *  (`sim_design.build_grid_design`: `load_outside_model`). */
export const LOAD_MIN = 0;
export const LOAD_MAX = 100;

/** `sim_design.LoadBasis`: today's one value, and what it means
 *  (`sim_design._LOAD_BASIS_MEANING`). */
export const LOAD_BASIS: SimLoadBasis = "fill_fraction_as_manifold_ratio";
export const LOAD_BASIS_MEANING =
  "The throttle-model fill fraction (Fuel Comp's load axis, % of the model's ceiling) is taken as MAP / baro, and MAF is the airflow at which the plant's manifold model gives that MAP. The fill byte has no absolute unit in code (rom_map.yaml fuel_comp_flow_axis); this mapping is stated, not decoded.";

/** Every field of the simulate body. The test pins this to the 21 in
 *  `docs/headless-api.md`. */
const REQUEST_FIELD_MAP: Record<keyof RomSimRequest, true> = {
  label: true,
  edits: true,
  grid: true,
  rpm_edges: true,
  load_edges: true,
  load_basis: true,
  baro_kpa: true,
  iat_c: true,
  coolant_c: true,
  battery_v: true,
  rail_kpa: true,
  throttle_pct: true,
  pedal_flag: true,
  stft_pct: true,
  ltft_pct: true,
  throttle_closed_learned: true,
  idle_air_learned: true,
  evap_purge_area: true,
  candidate_events: true,
  draws: true,
  seed: true,
};
export const SIM_REQUEST_FIELDS = Object.keys(REQUEST_FIELD_MAP) as (keyof RomSimRequest)[];

// -- the form's values ----------------------------------------------------------------------

export type NumberKey =
  | "baro"
  | "iat"
  | "coolant"
  | "battery"
  | "railKpa"
  | "throttle"
  | "pedalFlag"
  | "stft"
  | "ltft"
  | "throttleClosedLearned"
  | "idleAirLearned"
  | "evapPurgeArea"
  | "draws"
  | "seed";

/** Everything the operator states, as text. `""` is "not stated". */
export interface SimFormValues extends Record<NumberKey, string> {
  grid: "" | SimGrid;
  rpmEdges: string;
  loadEdges: string;
  /** `stated` takes `railKpa`; `working_assumption` is the plant's own value,
   *  said so. */
  rail: "" | "working_assumption" | "stated";
  candidateEvents: "" | SimCandidateEvents;
}

export type FormKey = keyof SimFormValues;

export const EMPTY_FORM: SimFormValues = {
  grid: "",
  rpmEdges: "",
  loadEdges: "",
  baro: "",
  iat: "",
  coolant: "",
  battery: "",
  rail: "",
  railKpa: "",
  throttle: "",
  pedalFlag: "",
  stft: "",
  ltft: "",
  throttleClosedLearned: "",
  idleAirLearned: "",
  evapPurgeArea: "",
  candidateEvents: "",
  draws: "",
  seed: "",
};

export interface NumberFieldSpec {
  key: NumberKey;
  request: keyof RomSimRequest;
  label: string;
  unit: string;
  integer: boolean;
  min: number | null;
  max: number | null;
  hint?: string;
}

const field = (
  key: NumberKey,
  request: keyof RomSimRequest,
  label: string,
  unit: string,
  extra: Partial<Pick<NumberFieldSpec, "integer" | "min" | "max" | "hint">> = {},
): NumberFieldSpec => ({
  key,
  request,
  label,
  unit,
  integer: extra.integer ?? false,
  min: extra.min ?? null,
  max: extra.max ?? null,
  hint: extra.hint,
});

/** The stated environment (`rail_kpa` has its own control: it is a number or
 *  `working_assumption`). */
/** What bit 0 of the pedal flag does (`target_phi_twin.yaml`
 *  `forced_column_flag`; `rom_map.yaml` `fuel_targets`): the flag is not
 *  logged, so the run states it, and a stated 0 means no simulated point
 *  reads the WOT column. */
export const PEDAL_FLAG_HINT =
  "not logged, so you state it. Bit 0 forces the last Target AFR column (the WOT column): " +
  "with bit 0 clear, no simulated point reads that column";

export const ENVIRONMENT_FIELDS: NumberFieldSpec[] = [
  field("baro", "baro_kpa", "Baro", "kPa", {
    hint: "home is about 80 kPa, trips reach 63 kPa: no default",
  }),
  field("iat", "iat_c", "IAT", "°C"),
  field("coolant", "coolant_c", "Coolant", "°C"),
  field("battery", "battery_v", "Battery", "V"),
  field("throttle", "throttle_pct", "Throttle", "%"),
  field("pedalFlag", "pedal_flag", "Pedal flag", "0-255", {
    integer: true,
    min: 0,
    max: 255,
    hint: PEDAL_FLAG_HINT,
  }),
];

export const RAIL_FIELD = field("railKpa", "rail_kpa", "Rail pressure", "kPa");

/** The learned state both ROMs start from (`paired_scenarios.LearnedState`). */
export const LEARNED_FIELDS: NumberFieldSpec[] = [
  field("stft", "stft_pct", "STFT", "%"),
  field("ltft", "ltft_pct", "LTFT", "%"),
  field("throttleClosedLearned", "throttle_closed_learned", "Throttle closed learned", "counts", {
    hint: "the learned closed throttle position, raw counts",
  }),
  field("idleAirLearned", "idle_air_learned", "Idle air learned", "counts", {
    hint: "raw counts",
  }),
  field("evapPurgeArea", "evap_purge_area", "EVAP purge area", "counts", { hint: "raw counts" }),
];

export const DRAWS_FIELD = field("draws", "draws", "Draws", "", {
  integer: true,
  min: 1,
  max: MAX_DRAWS,
});
export const SEED_FIELD = field("seed", "seed", "Seed", "", { integer: true, min: 0 });

export const CANDIDATE_EVENT_CHOICES: {
  id: SimCandidateEvents;
  label: string;
  meaning: string;
}[] = [
  {
    id: "none",
    label: "Same learned state on both",
    meaning:
      "Base and candidate start from the same learned state: the change alone, as if the trims carried over.",
  },
  {
    id: "ecu_reflash",
    label: "Fresh flash resets trims",
    meaning:
      "The candidate starts from what a flash resets (the contract says what), as the truck would after the flash; the base keeps its own.",
  },
];

export const GRID_CHOICES: { id: SimGrid; label: string; meaning: string }[] = [
  {
    id: "fuel_comp",
    label: "Fuel Comp axes",
    meaning: "Cells are Fuel Compensation's own cells: grid cell (r, c) is table cell (r, c).",
  },
  {
    id: "target_afr",
    label: "Fuel Targets axes",
    meaning: "Cells are Fuel Targets' (Target AFR's) cells, read on that table's own axes.",
  },
];

// -- parsing --------------------------------------------------------------------------------

const DECIMAL = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$/i;

export type Parsed =
  { kind: "missing" } | { kind: "invalid"; message: string } | { kind: "ok"; value: number };

/** One number as typed. Only plain decimals: no hex, no `Infinity`, no blank. */
export function parseNumber(
  text: string,
  spec: Pick<NumberFieldSpec, "integer" | "min" | "max">,
): Parsed {
  const trimmed = text.trim();
  if (trimmed === "") return { kind: "missing" };
  if (!DECIMAL.test(trimmed)) return { kind: "invalid", message: `"${trimmed}" is not a number` };
  const value = Number(trimmed);
  if (!Number.isFinite(value)) return { kind: "invalid", message: `"${trimmed}" is not finite` };
  if (spec.integer && !Number.isInteger(value)) {
    return { kind: "invalid", message: `${trimmed} is not a whole number` };
  }
  // Past 2^53 - 1 a double cannot hold every whole number: `Number()` would
  // round what was typed, and the request would carry a different seed.
  if (spec.integer && !Number.isSafeInteger(value)) {
    return {
      kind: "invalid",
      message: `${trimmed} is beyond ${Number.MAX_SAFE_INTEGER} (2^53 − 1), the largest whole number the page can send exactly`,
    };
  }
  if (spec.min !== null && value < spec.min) {
    return { kind: "invalid", message: `${trimmed} is below ${spec.min}` };
  }
  if (spec.max !== null && value > spec.max) {
    return { kind: "invalid", message: `${trimmed} is above ${spec.max}` };
  }
  return { kind: "ok", value };
}

export interface ParsedEdges {
  edges: number[];
  /** Why the list is not a usable set of edges; null when it is. */
  error: string | null;
  empty: boolean;
}

/** A comma, space or newline separated list of cell edges: at least two,
 *  finite, strictly rising. */
export function parseEdges(text: string): ParsedEdges {
  const tokens = text.split(/[\s,;]+/).filter((token) => token !== "");
  if (tokens.length === 0) return { edges: [], error: null, empty: true };
  const edges: number[] = [];
  for (const token of tokens) {
    if (!DECIMAL.test(token)) {
      return { edges, error: `"${token}" is not a number`, empty: false };
    }
    edges.push(Number(token));
  }
  if (edges.length < 2) {
    return { edges, error: "needs at least two edges (one cell)", empty: false };
  }
  for (let i = 1; i < edges.length; i += 1) {
    if (!((edges[i] ?? 0) > (edges[i - 1] ?? 0))) {
      return {
        edges,
        error: `edges must rise strictly: ${edges[i - 1]} then ${edges[i]} at position ${i + 1}`,
        empty: false,
      };
    }
  }
  return { edges, error: null, empty: false };
}

/** The load edges' extra rule: the fill model spans 0-100 %. */
export function loadEdgesError(edges: number[]): string | null {
  const lo = edges[0];
  const hi = edges[edges.length - 1];
  if (lo !== undefined && lo < LOAD_MIN) {
    return `edge ${lo} is below ${LOAD_MIN} %: the fill model spans ${LOAD_MIN}-${LOAD_MAX} % (load_outside_model)`;
  }
  if (hi !== undefined && hi > LOAD_MAX) {
    return `edge ${hi} is above ${LOAD_MAX} %: the fill model spans ${LOAD_MIN}-${LOAD_MAX} % (load_outside_model)`;
  }
  return null;
}

/** Cells in a grid with these edges: (rpm edges - 1) x (load edges - 1). */
export function gridPoints(rpm: number[], load: number[]): number {
  return Math.max(0, rpm.length - 1) * Math.max(0, load.length - 1);
}

// -- the request ----------------------------------------------------------------------------

export interface SimProblem {
  /** The form key, or `edits` / `label` for the pending edits. */
  field: string;
  label: string;
  kind: "missing" | "invalid";
  message: string;
}

export interface SimCheck {
  request: RomSimRequest | null;
  problems: SimProblem[];
}

/** The request the form describes, or every reason there is none. Missing
 *  fields are named (`Baro (kPa)`), never filled in. */
export function checkSimForm(values: SimFormValues, label: string, edits: RomEdit[]): SimCheck {
  const problems: SimProblem[] = [];
  const missing = (key: string, name: string, message: string) =>
    problems.push({ field: key, label: name, kind: "missing", message });
  const invalid = (key: string, name: string, message: string) =>
    problems.push({ field: key, label: name, kind: "invalid", message });

  if (edits.length === 0) missing("edits", "Edits", "apply at least one edit above");
  if (!label.trim()) missing("label", "Label", "name the edit set above");

  if (values.grid === "") missing("grid", "Grid", "choose which table's axes the cells use");

  const rpm = parseEdges(values.rpmEdges);
  if (rpm.empty) missing("rpmEdges", "RPM edges", "state the rpm cell edges");
  else if (rpm.error) invalid("rpmEdges", "RPM edges", rpm.error);
  const load = parseEdges(values.loadEdges);
  if (load.empty) missing("loadEdges", "Load edges", "state the load cell edges (% fill)");
  else if (load.error) invalid("loadEdges", "Load edges", load.error);
  else {
    const range = loadEdgesError(load.edges);
    if (range) invalid("loadEdges", "Load edges", range);
  }
  const cells = gridPoints(rpm.error ? [] : rpm.edges, load.error ? [] : load.edges);
  if (cells > MAX_GRID_POINTS) {
    invalid(
      "gridPoints",
      "Grid size",
      `${cells} cells; at most ${MAX_GRID_POINTS} (grid_too_large)`,
    );
  }

  const numbers: Partial<Record<NumberKey, number>> = {};
  for (const spec of [...ENVIRONMENT_FIELDS, ...LEARNED_FIELDS, DRAWS_FIELD, SEED_FIELD]) {
    const parsed = parseNumber(values[spec.key], spec);
    const name = spec.unit ? `${spec.label} (${spec.unit})` : spec.label;
    if (parsed.kind === "missing") missing(spec.key, name, "state a value");
    else if (parsed.kind === "invalid") invalid(spec.key, name, parsed.message);
    else numbers[spec.key] = parsed.value;
  }

  let rail: number | "working_assumption" | null = null;
  if (values.rail === "") {
    missing(
      "rail",
      "Rail pressure",
      "state a rail pressure or choose the plant's working assumption",
    );
  } else if (values.rail === "working_assumption") {
    rail = "working_assumption";
  } else {
    const parsed = parseNumber(values.railKpa, RAIL_FIELD);
    if (parsed.kind === "missing") missing("railKpa", "Rail pressure (kPa)", "state a value");
    else if (parsed.kind === "invalid") invalid("railKpa", "Rail pressure (kPa)", parsed.message);
    else rail = parsed.value;
  }

  if (values.candidateEvents === "") {
    missing(
      "candidateEvents",
      "Candidate events",
      "choose: same learned state on both, or a fresh flash",
    );
  }
  if ((numbers.draws ?? 0) > MAX_DRAWS) {
    invalid("draws", "Draws", `${numbers.draws} draws; at most ${MAX_DRAWS} (draws_too_many)`);
  }

  if (problems.length > 0) return { request: null, problems };
  const n = numbers as Record<NumberKey, number>;
  const request: RomSimRequest = {
    label: label.trim(),
    edits,
    grid: values.grid as SimGrid,
    rpm_edges: rpm.edges,
    load_edges: load.edges,
    load_basis: LOAD_BASIS,
    baro_kpa: n.baro,
    iat_c: n.iat,
    coolant_c: n.coolant,
    battery_v: n.battery,
    rail_kpa: rail as number | "working_assumption",
    throttle_pct: n.throttle,
    pedal_flag: n.pedalFlag,
    stft_pct: n.stft,
    ltft_pct: n.ltft,
    throttle_closed_learned: n.throttleClosedLearned,
    idle_air_learned: n.idleAirLearned,
    evap_purge_area: n.evapPurgeArea,
    candidate_events: values.candidateEvents as SimCandidateEvents,
    draws: n.draws,
    seed: n.seed,
  };
  return { request, problems };
}

/** A fresh seed, a whole number the form then shows (never sent unseen). */
export function newSeed(random: () => number = Math.random): number {
  return Math.floor(random() * 2 ** 31);
}

// -- grid edges from the base image ---------------------------------------------------------

/** Cell edges that put one cell on each breakpoint: the midpoints between
 *  breakpoints, extended half a step at each end. Null when there are fewer
 *  than two breakpoints or they do not rise strictly. */
export function edgesFromBreakpoints(breakpoints: number[]): number[] | null {
  if (breakpoints.length < 2) return null;
  for (let i = 1; i < breakpoints.length; i += 1) {
    if (!((breakpoints[i] ?? 0) > (breakpoints[i - 1] ?? 0))) return null;
  }
  const first = breakpoints[0] ?? 0;
  const second = breakpoints[1] ?? 0;
  const last = breakpoints[breakpoints.length - 1] ?? 0;
  const before = breakpoints[breakpoints.length - 2] ?? 0;
  const edges = [first - (second - first) / 2];
  for (let i = 0; i < breakpoints.length - 1; i += 1) {
    edges.push(((breakpoints[i] ?? 0) + (breakpoints[i + 1] ?? 0)) / 2);
  }
  edges.push(last + (last - before) / 2);
  return edges.map((edge) => Number(edge.toPrecision(7)));
}

/** The largest distance of a cell's centre from its breakpoint, as a share
 *  of that cell's width: 0 on a uniform axis, where the edges put each grid
 *  point exactly on a breakpoint. */
export function centreOffset(edges: number[], breakpoints: number[]): number {
  let worst = 0;
  for (let i = 0; i < breakpoints.length; i += 1) {
    const lo = edges[i];
    const hi = edges[i + 1];
    const at = breakpoints[i];
    if (lo === undefined || hi === undefined || at === undefined || hi <= lo) continue;
    worst = Math.max(worst, Math.abs((lo + hi) / 2 - at) / (hi - lo));
  }
  return worst;
}

/** Edges with the parts outside the fill model's 0-100 % dropped and the
 *  bounds put in their place. An explicit operator action, never applied
 *  unasked. */
export function trimLoadEdges(edges: number[]): number[] {
  const out = edges.filter((edge) => edge >= LOAD_MIN && edge <= LOAD_MAX);
  if (edges.some((edge) => edge < LOAD_MIN) && (out[0] ?? 1) > LOAD_MIN) out.unshift(LOAD_MIN);
  if (edges.some((edge) => edge > LOAD_MAX) && (out[out.length - 1] ?? 0) < LOAD_MAX) {
    out.push(LOAD_MAX);
  }
  return out;
}

export function formatEdges(edges: number[]): string {
  return edges.map((edge) => String(edge)).join(", ");
}

type AxisKind = "rpm" | "load";

function axisKind(axis: RomTableAxis): AxisKind | null {
  const quantity = (axis.decode?.quantity ?? "").toLowerCase();
  if (/\brpm\b|engine.?speed/.test(quantity)) return "rpm";
  if (/fill|load|flow/.test(quantity)) return "load";
  if ((axis.decode?.unit ?? axis.unit) === "rpm") return "rpm";
  return null;
}

function numericAxis(axis: RomTableAxis | null | undefined): number[] | null {
  const values = axis?.decoded;
  if (!values || values.length === 0) return null;
  return values.every(
    (value): value is number => typeof value === "number" && Number.isFinite(value),
  )
    ? values
    : null;
}

export interface BreakpointSuggestion {
  /** Fuel Comp's breakpoints in the units the form's edges use. */
  rpm: number[] | null;
  load: number[] | null;
  rpmEdges: number[] | null;
  loadEdges: number[] | null;
  /** Why an axis could not be suggested (shown, so a blank is explained). */
  notes: string[];
  /** Share of a cell's width the worst grid point sits off its breakpoint. */
  offCentre: { rpm: number; load: number };
}

/** The suggested edges from the base image's Fuel Comp (Map 1) breakpoints.
 *  Each axis is classified by its decode quantity (rpm, throttle-model fill),
 *  never by whether it is the table's rows or columns: UpRev labels them
 *  the other way round. */
export function suggestEdges(tables: RomTableValues[]): BreakpointSuggestion {
  const none: BreakpointSuggestion = {
    rpm: null,
    load: null,
    rpmEdges: null,
    loadEdges: null,
    notes: [],
    offCentre: { rpm: 0, load: 0 },
  };
  const table = tables.find(
    (item) => item.kind === "fuel_compensation" && item.slot === 1 && item.role !== "axis",
  );
  if (!table) {
    return {
      ...none,
      notes: ["this image has no Fuel Compensation (Map 1) table to read breakpoints from"],
    };
  }
  const axes = [table.axes.rows, table.axes.cols].filter((axis): axis is RomTableAxis => !!axis);
  const kinds = axes.map(axisKind);
  const rpmAxis = axes.find((_, index) => kinds[index] === "rpm");
  // The axis that is not rpm is the load axis; a decode that names load says so too.
  const loadAxis =
    axes.find((_, index) => kinds[index] === "load") ??
    (rpmAxis ? axes.find((axis) => axis !== rpmAxis) : undefined);
  const notes: string[] = [];
  const rpm = rpmAxis ? numericAxis(rpmAxis) : null;
  const load = loadAxis ? numericAxis(loadAxis) : null;
  if (!rpmAxis)
    notes.push("no Fuel Comp axis declares an rpm decode, so no rpm edges are suggested");
  else if (!rpm) notes.push(`${rpmAxis.name} has no decoded values, so no rpm edges are suggested`);
  if (!loadAxis) notes.push("no Fuel Comp load axis was found, so no load edges are suggested");
  else if (!load) {
    notes.push(`${loadAxis.name} has no decoded values, so no load edges are suggested`);
  }
  const rpmEdges = rpm ? edgesFromBreakpoints(rpm) : null;
  const loadEdges = load ? edgesFromBreakpoints(load) : null;
  if (rpm && !rpmEdges)
    notes.push("the rpm breakpoints do not rise strictly, so no edges are suggested");
  if (load && !loadEdges)
    notes.push("the load breakpoints do not rise strictly, so no edges are suggested");
  return {
    rpm,
    load,
    rpmEdges,
    loadEdges,
    notes,
    offCentre: {
      rpm: rpm && rpmEdges ? centreOffset(rpmEdges, rpm) : 0,
      load: load && loadEdges ? centreOffset(loadEdges, load) : 0,
    },
  };
}

// -- what the browser remembers -------------------------------------------------------------

/** The last-used values that come back, shown and marked in the form. Not
 *  the grid, the edges or the candidate events: those describe *this*
 *  experiment or *this* base image, so they are stated every time. */
export const REMEMBERED_KEYS: readonly FormKey[] = [
  "baro",
  "iat",
  "coolant",
  "battery",
  "rail",
  "railKpa",
  "throttle",
  "pedalFlag",
  "stft",
  "ltft",
  "throttleClosedLearned",
  "idleAirLearned",
  "evapPurgeArea",
  "draws",
  "seed",
];

export const SIM_STORAGE_KEY = "canary.romSim.lastUsed";

export interface Remembered {
  values: Partial<SimFormValues>;
  savedAt: string | null;
}

const NOTHING: Remembered = { values: {}, savedAt: null };

/** The remembered values; nothing when absent, unreadable or shaped wrongly
 *  (`localStorage` may be blocked or hold another version). Empty entries are
 *  not values. */
export function loadRemembered(): Remembered {
  try {
    const text = window.localStorage.getItem(SIM_STORAGE_KEY);
    if (!text) return NOTHING;
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null) return NOTHING;
    const record = parsed as Record<string, unknown>;
    const stored = record.values;
    if (record.version !== 1 || typeof stored !== "object" || stored === null) return NOTHING;
    const values: Record<string, string> = {};
    for (const key of REMEMBERED_KEYS) {
      const value = (stored as Record<string, unknown>)[key];
      if (typeof value !== "string" || value.trim() === "") continue;
      if (key === "rail" && value !== "working_assumption" && value !== "stated") continue;
      values[key] = value;
    }
    return {
      values: values,
      savedAt: typeof record.savedAt === "string" ? record.savedAt : null,
    };
  } catch {
    return NOTHING;
  }
}

export function saveRemembered(values: SimFormValues, now: Date = new Date()): void {
  try {
    const kept: Record<string, string> = {};
    for (const key of REMEMBERED_KEYS) kept[key] = values[key];
    window.localStorage.setItem(
      SIM_STORAGE_KEY,
      JSON.stringify({ version: 1, savedAt: now.toISOString(), values: kept }),
    );
  } catch {
    // Storage blocked or full: nothing is remembered, nothing is lost.
  }
}

export function forgetRemembered(): void {
  try {
    window.localStorage.removeItem(SIM_STORAGE_KEY);
  } catch {
    // Blocked: nothing to forget.
  }
}

/** The form's values with the remembered ones laid over empty fields, and
 *  the keys that came from memory (so the form can mark them). */
export function seedForm(
  base: SimFormValues,
  remembered: Remembered,
): {
  values: SimFormValues;
  remembered: FormKey[];
} {
  const values = { ...base };
  const keys: FormKey[] = [];
  for (const key of REMEMBERED_KEYS) {
    const value = remembered.values[key];
    if (value === undefined || values[key] !== "") continue;
    (values as Record<FormKey, string>)[key] = value;
    keys.push(key);
  }
  return { values, remembered: keys };
}

// -- blocked, and failures ------------------------------------------------------------------

/** Why Simulate is off. `base_not_a_recorded_revision` is the server's own
 *  reason (`rom_editor.sim_base`); `preview_refused` is the page's. */
export const SIM_BLOCK_TEXT: Record<string, string> = {
  base_not_a_recorded_revision:
    "The simulator needs a base recorded in tune_revisions.yaml. This image is not one (a reference image, such as a tuner's base that was never flashed, is not a revision), so edits can be previewed but not simulated.",
  preview_refused:
    "The last preview was refused, so these edits cannot run. Fix or remove the edit that was refused, then preview again.",
};

export const FAILURE_TITLE: Record<SimFailure["kind"], string> = {
  refused: "Refused",
  deferred: "Deferred: a capture is recording",
  busy: "Busy: another analysis is running",
  unavailable: "Unavailable: the job did not finish",
  invalid: "Request rejected",
  not_found: "Not found",
  unauthorized: "Not authorized",
  error: "Failed",
};

/** What to do about a deferred run, whatever the server's own wording. */
export const DEFERRED_ADVICE =
  "A capture is recording, so the simulator does not run on the Pi (nothing may load it while it collects). Pull the images and run the edit spec on the laptop.";

function asString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** The simulate route's failure by its own name: `sim_refused` (400),
 *  `analysis_deferred` / `analysis_busy` (409), `analysis_unavailable` (503),
 *  pydantic's list (422), a plain string (404, 409 no map). Anything else is
 *  an ordinary error described as every other page does. */
export function simFailure(err: unknown): SimFailure {
  if (!(err instanceof HttpError)) {
    return { kind: "error", reason: null, message: describeError(err) };
  }
  let body: unknown;
  try {
    body = JSON.parse(err.body);
  } catch {
    body = null;
  }
  const record = typeof body === "object" && body !== null ? (body as Record<string, unknown>) : {};
  const detail = "detail" in record ? record.detail : body;
  if (Array.isArray(detail)) {
    const text = detail
      .map((item) => {
        const entry = (typeof item === "object" && item !== null ? item : {}) as Record<
          string,
          unknown
        >;
        const where = Array.isArray(entry.loc) ? entry.loc.slice(1).join(".") : "";
        return `${where ? `${where}: ` : ""}${asString(entry.msg) ?? "invalid"}`;
      })
      .join("; ");
    return { kind: "invalid", reason: "validation", message: text || describeError(err) };
  }
  if (typeof detail === "object" && detail !== null) {
    const d = detail as Record<string, unknown>;
    const reason = asString(d.reason);
    const message = asString(d.message) ?? describeError(err);
    switch (d.error) {
      case "sim_refused":
      case "edit_refused":
        return {
          kind: "refused",
          reason,
          message,
          refusalKind: asString(d.kind),
        };
      case "analysis_deferred":
        return {
          kind: "deferred",
          reason,
          message,
          laptopPath: asString(d.laptop_path),
          profile: asString(d.profile),
        };
      case "analysis_busy":
        return { kind: "busy", reason, message };
      case "analysis_unavailable":
        return { kind: "unavailable", reason, message };
      default:
        break;
    }
  }
  if (typeof detail === "string") {
    return {
      kind: err.status === 404 ? "not_found" : "error",
      reason: null,
      message: detail,
    };
  }
  if (err.status === 401 || err.status === 403) {
    return { kind: "unauthorized", reason: null, message: describeError(err) };
  }
  return { kind: "error", reason: null, message: describeError(err) };
}

/** `<label>.sim-result.json`, filesystem-safe. */
export function resultFileName(label: string): string {
  const stem = label
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${stem || "rom-sim"}.sim-result.json`;
}

/** Whether E08.7 (known answers before novel ones) is fully ticked in
 *  `docs/roadmap/tasks/E08-rom-edit-simulation.md`. On 2026-10-15 it was `[~]`:
 *  items 1-4 (no-op control, K +5 %, the Fuel Comp restore on the real images,
 *  Target AFR +4 counts) pass in the test suite through `tools/rom_sim.py`;
 *  item 5, scoring the post-flash truck drives against a prediction written
 *  before them, is still owed. W09.6 is where they are first run through this
 *  page. Flip this when E08.7's box is ticked. */
export const KNOWN_ANSWERS_REPRODUCED = false;

export const KNOWN_ANSWERS_LINE =
  "E08.7's known answers 1–4 (a no-op candidate, K +5 % per point, the Fuel Comp restore on the real images, a Target AFR +4 edit) are reproduced in the test suite through the CLI path (tools/rom_sim.py). They have not been run through this page on real images yet (W09.6 does that first), and scoring against post-flash truck drives (item 5) is still owed: E08.7 is partly done in the E08 roadmap (state of 2026-10-15).";

export function knownAnswersLine(): string {
  return KNOWN_ANSWERS_REPRODUCED
    ? "E08.7 is ticked in the E08 roadmap: the known answers (no-op control, K +5 % per point, the Fuel Comp restore, a Target AFR +4 edit) are reproduced and the post-flash truck drives were scored against a prediction written before them."
    : KNOWN_ANSWERS_LINE;
}

// -- the WOT column ---------------------------------------------------------------------

/** Whether any pending edit writes the last column of a Fuel Targets table.
 *  The last column is derived from the loaded table's own dimensions, never
 *  assumed. Only `cells` and `all` selectors resolve (the page builds
 *  nothing else); a `region` selector is in axis units and is not resolved
 *  here. */
export function editsTouchWotColumn(edits: readonly RomEdit[], tables: RomTableValues[]): boolean {
  return edits.some((edit) => {
    const table = tables.find(
      (item) => item.id === edit.table && (item.slot ?? null) === (edit.slot ?? null),
    );
    if (!table || !isFuelTargets(table)) return false;
    const cols = table.raw[0]?.length ?? 0;
    if (cols === 0) return false;
    if (edit.all) return true;
    return (edit.cells ?? []).some(([, col]) => col === cols - 1);
  });
}

/** The flag when it is a whole number with bit 0 clear, else null (empty,
 *  invalid, or bit 0 set: nothing to warn about). */
export function pedalFlagBit0Clear(text: string): number | null {
  const parsed = parseNumber(text, { integer: true, min: 0, max: 255 });
  return parsed.kind === "ok" && (parsed.value & 1) === 0 ? parsed.value : null;
}

/** The pre-Run note: the edit touches the WOT column and the stated flag
 *  never reads it. It does not offer to set the flag. */
export function wotColumnWarning(flag: number): string {
  return (
    `Your edit touches the WOT column. With pedal flag ${flag} the simulated points never ` +
    "read it, so this run will show no change from this edit. Set pedal flag to 1 (bit 0) " +
    "to simulate wide-open throttle."
  );
}

/** The result's caveat line for the same case. */
export function wotColumnCaveat(flag: number): string {
  return `The WOT column was edited with pedal flag ${flag}: this run cannot show that edit's effect.`;
}
