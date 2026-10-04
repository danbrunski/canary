/**
 * The ECM's own coordinates on a ROM table (W05), beside the inferred lookup.
 *
 * The inferred cell (`tracePosition` from RPM and BFS) is geometry this page
 * computes; the ECM cell comes from the map-trace memory channels, which are
 * the coordinates the ECM says it is reading. Where the two differ is the
 * finding — a wrong axis decode, a wrong BFS scale, or interpolation the
 * inference does not model — so both are drawn and the difference is stated.
 *
 * How a trace value lands on a table's breakpoints is a measured constraint
 * held in the vehicle profile (`rom_viewer.yaml` → `trace.*.axis_mapping`).
 * Nothing here places a cell on an unverified axis. A verified axis names the
 * breakpoints it is compared with (`axis_units`): a `raw` axis undoes the
 * catalog decode its channel is stored under and lands on the ROM's raw axis
 * bytes, so a scaled value is never compared with raw bytes (W05.1). Past an
 * axis end the ECM cell is the edge cell only where the profile declares the
 * ECM clamps (`trace.out_of_range`).
 */
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { CaptureProfile, StatusResponse } from "../types";
import type {
  AxisTrace,
  CellResidency,
  RomInspection,
  RomTable,
  TableTrace,
} from "../types/workbench";
import { coverage } from "./colorScale";
import { streamRow, TRACE_MAX_SKEW_MS, tracePosition } from "./romMaps";

export type { AxisTrace, CellResidency, TableTrace } from "../types/workbench";

/** W05.4: a trace channel older than this greys the ECM trace out. */
export const TRACE_CHANNEL_STALE_MS = 3000;
/** The two traces of one table are separate memory reads; beyond this apart
 *  they are not one operating point. */
export const ECM_TRACE_MAX_SKEW_MS = 750;

export function traceSample(
  channels: Map<string, ChannelSnapshot>,
  spec: string,
  now: number,
): { value: number; timestamp: number } | null {
  const row = streamRow(channels, spec);
  if (!row || typeof row.value !== "number" || !Number.isFinite(row.value) || !row.timestamp)
    return null;
  const timestamp = Date.parse(row.timestamp);
  if (
    !Number.isFinite(timestamp) ||
    now - timestamp > TRACE_CHANNEL_STALE_MS ||
    timestamp - now > 1000
  )
    return null;
  return { value: row.value, timestamp };
}

type Axis = "x" | "y";
const AXES = ["x", "y"] as const;

/** A value undone by its catalog decode is a whole count up to float
 *  round-off, never more (`rom_trace.WHOLE_COUNT_TOLERANCE`). */
const WHOLE_COUNT_TOLERANCE = 1e-6;

/** The breakpoints a trace axis is compared with on `table`: the ROM's raw
 *  axis bytes for a `raw` mapping, the displayed axis for `display`, or null
 *  when the table does not carry them. */
export function traceBreakpoints(table: RomTable, axis: Axis): number[] | null {
  const mapping = table.trace?.[axis];
  if (!mapping) return null;
  const display = axis === "x" ? table.x_axis : table.y_axis;
  if (mapping.axis_units === "display") return display;
  if (mapping.axis_units !== "raw") return null;
  const raw = axis === "x" ? table.x_axis_raw : table.y_axis_raw;
  return raw && raw.length > 0 && raw.length === display.length ? raw : null;
}

/** Why no ECM cell may be placed on this table, or null when it may. Pass
 *  the table to also check it carries the breakpoints a mapping lands on. */
export function mappingRefusal(
  trace: TableTrace | null | undefined,
  table?: RomTable,
): string | null {
  if (!trace)
    return "This vehicle's rom_viewer.yaml maps no trace channels to this table; no ECM cell is drawn.";
  const unverified = AXES.filter((axis) => trace[axis].axis_mapping !== "verified");
  if (unverified.length)
    return `ECM cell not drawn: how the ${unverified.map((axis) => `${axis.toUpperCase()} trace (${trace[axis].channel})`).join(" and ")} lands on this table's breakpoints is not verified. ${unverified.map((axis) => trace[axis].evidence).join(" ")}`;
  for (const axis of AXES) {
    const mapping = trace[axis];
    const name = `${axis.toUpperCase()} trace (${mapping.channel})`;
    if (mapping.axis_units !== "display" && mapping.axis_units !== "raw")
      return `ECM cell not drawn: the ${name} mapping names no axis units, so which breakpoints it lands on is unknown.`;
    if (mapping.axis_units === "raw" && !mapping.stored_decode)
      return `ECM cell not drawn: the ${name} lands on raw axis bytes, but the DID catalog names no decode to undo for it.`;
    if (table && !traceBreakpoints(table, axis))
      return `ECM cell not drawn: the ${name} lands on this table's raw axis bytes, which this ROM inspection does not carry.`;
  }
  return null;
}

/** A stored trace value in the units of the breakpoints its axis is compared
 *  with, or null (unverified, or not a whole raw count under the decode). */
function toAxis(axis: AxisTrace, stored: number): number | null {
  if (axis.axis_mapping !== "verified" || axis.scale === null || axis.offset === null) return null;
  let input = stored;
  if (axis.axis_units === "raw") {
    if (!axis.stored_decode) return null;
    const raw = (stored - axis.stored_decode.offset) / axis.stored_decode.scale;
    const whole = Math.round(raw);
    if (!Number.isFinite(raw) || Math.abs(raw - whole) > WHOLE_COUNT_TOLERANCE) return null;
    input = whole;
  } else if (axis.axis_units !== "display") return null;
  const mapped = input * axis.scale + axis.offset;
  return Number.isFinite(mapped) ? mapped : null;
}

export interface EcmPoint {
  /** In the units of the breakpoints each axis is compared with: raw counts
   *  on a `raw` axis, displayed units on a `display` one. */
  x: number;
  y: number;
  xTimestamp: number;
  yTimestamp: number;
}

function freshPair(trace: TableTrace, channels: Map<string, ChannelSnapshot>, now: number) {
  const xs = traceSample(channels, trace.x.channel, now);
  const ys = traceSample(channels, trace.y.channel, now);
  if (!xs || !ys || Math.abs(xs.timestamp - ys.timestamp) > ECM_TRACE_MAX_SKEW_MS) return null;
  return { xs, ys };
}

/** The ECM's coordinates, when fresh, paired and verified. */
export function ecmPoint(
  trace: TableTrace | null | undefined,
  channels: Map<string, ChannelSnapshot>,
  now: number,
): EcmPoint | null {
  if (!trace || mappingRefusal(trace)) return null;
  const pair = freshPair(trace, channels, now);
  if (!pair) return null;
  const x = toAxis(trace.x, pair.xs.value);
  const y = toAxis(trace.y, pair.ys.value);
  return x === null || y === null
    ? null
    : { x, y, xTimestamp: pair.xs.timestamp, yTimestamp: pair.ys.timestamp };
}

/** Why a fresh, paired ECM sample has no coordinates: a raw axis whose value
 *  does not come back to a whole count under the decode the page loaded. */
export function ecmValueRefusal(
  trace: TableTrace | null | undefined,
  channels: Map<string, ChannelSnapshot>,
  now: number,
): string | null {
  if (!trace || mappingRefusal(trace)) return null;
  const pair = freshPair(trace, channels, now);
  if (!pair) return null;
  const bad = AXES.filter(
    (axis) => toAxis(trace[axis], (axis === "x" ? pair.xs : pair.ys).value) === null,
  );
  if (!bad.length) return null;
  return `ECM cell not drawn: ${bad.map((axis) => trace[axis].channel).join(" and ")} does not return to a whole raw count under the catalog decode this ROM inspection carries. Load the ROM again after a catalog change.`;
}

export type AxisEdge = "below" | "above";

/** Where `value` sits on `breakpoints` as a fractional index: `i + t` is `t`
 *  of the way from breakpoint `i` to `i + 1`, the weight the ECM's linear
 *  lookup gives the upper one. Past an end it is that end, naming the edge,
 *  when `clamp`; otherwise null. */
export function axisPosition(
  breakpoints: number[],
  value: number,
  clamp: boolean,
): { position: number; edge: AxisEdge | null } | null {
  if (!breakpoints.length || !Number.isFinite(value)) return null;
  if (value < breakpoints[0]) return clamp ? { position: 0, edge: "below" } : null;
  const last = breakpoints.length - 1;
  if (value > breakpoints[last]) return clamp ? { position: last, edge: "above" } : null;
  const upper = breakpoints.findIndex((point) => point >= value);
  if (breakpoints[upper] === value) return { position: upper, edge: null };
  const lower = upper - 1;
  return {
    position: lower + (value - breakpoints[lower]) / (breakpoints[upper] - breakpoints[lower]),
    edge: null,
  };
}

/** Nearest breakpoint to a fractional position; a tie goes low, as the
 *  inferred lookup does. */
function nearestIndex(position: number): number {
  const lower = Math.floor(position);
  return position - lower <= 0.5 ? lower : lower + 1;
}

export type CellPosition = NonNullable<ReturnType<typeof tracePosition>>;

/** The ECM cell, and the edge each coordinate was clamped from (null when
 *  it is on the axis). */
export type EcmPosition = CellPosition & { edges: Record<Axis, AxisEdge | null> };

/** The cell the ECM coordinates point at, by the same nearest-breakpoint rule
 *  as the inferred lookup so the two are comparable cell for cell, compared
 *  on the breakpoints each mapping names. */
export function ecmPosition(table: RomTable, point: EcmPoint | null): EcmPosition | null {
  if (!point || table.kind !== "fuel_targets" || table.status !== "available") return null;
  if (mappingRefusal(table.trace, table)) return null;
  const clamp = table.trace?.out_of_range?.behaviour === "clamp";
  const xAxis = traceBreakpoints(table, "x");
  const yAxis = traceBreakpoints(table, "y");
  if (!xAxis || !yAxis) return null;
  const x = axisPosition(xAxis, point.x, clamp);
  const y = axisPosition(yAxis, point.y, clamp);
  if (!x || !y) return null;
  const bracket = (position: number) =>
    Number.isInteger(position) ? [position] : [Math.floor(position), Math.ceil(position)];
  return {
    rows: bracket(y.position),
    columns: bracket(x.position),
    row: nearestIndex(y.position),
    column: nearestIndex(x.position),
    edges: { x: x.edge, y: y.edge },
  };
}

export interface EcmDisplay {
  x: number;
  y: number;
  /** A raw-axis coordinate past an end has no displayed value: it is shown
   *  as "at or below" / "at or above" that end. */
  edges: Record<Axis, AxisEdge | null>;
}

/** The ECM coordinates in the table's displayed units, for messages. A
 *  `display` axis is its own value; a `raw` axis is placed by its fractional
 *  position between the displayed breakpoints of the same cell. */
export function ecmDisplay(table: RomTable, point: EcmPoint): EcmDisplay | null {
  const out: EcmDisplay = { x: point.x, y: point.y, edges: { x: null, y: null } };
  for (const axis of AXES) {
    if (table.trace?.[axis].axis_units !== "raw") continue;
    const breakpoints = traceBreakpoints(table, axis);
    const display = axis === "x" ? table.x_axis : table.y_axis;
    const at = breakpoints ? axisPosition(breakpoints, point[axis], true) : null;
    if (!at) return null;
    const lower = Math.floor(at.position);
    const upper = Math.min(lower + 1, display.length - 1);
    out[axis] = display[lower] + (display[upper] - display[lower]) * (at.position - lower);
    out.edges[axis] = at.edge;
  }
  return out;
}

export interface Disagreement {
  rows: number;
  columns: number;
  x: { ecm: number; inferred: number; ecmEdge: AxisEdge | null };
  y: { ecm: number; inferred: number; ecmEdge: AxisEdge | null };
}

/** How far the ECM cell is from the inferred one, or null when they agree. */
export function cellDisagreement(
  ecm: CellPosition | null,
  inferred: CellPosition | null,
  ecmAt: { x: number; y: number; edges?: Record<Axis, AxisEdge | null> },
  inferredAt: { x: number; y: number },
): Disagreement | null {
  if (!ecm || !inferred) return null;
  const rows = ecm.row - inferred.row;
  const columns = ecm.column - inferred.column;
  if (!rows && !columns) return null;
  return {
    rows,
    columns,
    x: { ecm: ecmAt.x, inferred: inferredAt.x, ecmEdge: ecmAt.edges?.x ?? null },
    y: { ecm: ecmAt.y, inferred: inferredAt.y, ecmEdge: ecmAt.edges?.y ?? null },
  };
}

const signed = (n: number) => (n > 0 ? `+${n}` : `${n}`);
const edgeMark = (edge: AxisEdge | null) => (edge === "below" ? "≤" : edge === "above" ? "≥" : "");

export function describeDisagreement(d: Disagreement, table: RomTable): string {
  const parts: string[] = [];
  if (d.columns)
    parts.push(
      `${signed(d.columns)} column${Math.abs(d.columns) === 1 ? "" : "s"} (ECM ${edgeMark(d.x.ecmEdge)}${d.x.ecm.toFixed(2)} vs inferred ${d.x.inferred.toFixed(2)} ${table.x_unit})`,
    );
  if (d.rows)
    parts.push(
      `${signed(d.rows)} row${Math.abs(d.rows) === 1 ? "" : "s"} (ECM ${edgeMark(d.y.ecmEdge)}${d.y.ecm.toFixed(0)} vs inferred ${d.y.inferred.toFixed(0)} ${table.y_unit})`,
    );
  return `ECM cell differs from the inferred cell by ${parts.join(" and ")}.`;
}

/** Says when the ECM cell is an edge cell because a coordinate is past the
 *  axis (the lookup clamps), or "" when both are on the axes. */
export function describeClamp(position: EcmPosition, table: RomTable): string {
  const parts = AXES.filter((axis) => position.edges[axis]).map((axis) => {
    const edge = position.edges[axis];
    const what = axis === "x" ? `${table.x_label} coordinate` : `${table.y_label} coordinate`;
    const end = edge === "below" ? "first" : "last";
    return `the ECM's ${what} is ${edge} the ${end} breakpoint`;
  });
  return parts.length
    ? `Edge cell: ${parts.join(" and ")}, and the ECM's lookup reads the edge there.`
    : "";
}

export type TraceLiveness =
  | { state: "live" }
  | { state: "no_ecm_data"; message: string }
  | { state: "not_polled"; message: string; profiles: string[]; note: string | null }
  | { state: "not_answering"; message: string };

const bare = (spec: string) => spec.split(":").slice(1).join(":").toUpperCase();

/** Profiles in the vehicle's catalog that poll every trace channel. */
export function profilesCarrying(trace: TableTrace, profiles: CaptureProfile[] | null): string[] {
  const needed = [trace.x.channel, trace.y.channel].map(bare);
  return (profiles ?? [])
    .filter((profile) => profile.mode22_enabled !== false)
    .filter((profile) => {
      const selected = new Set((profile.memory ?? []).map((name) => name.toUpperCase()));
      return needed.every((name) => selected.has(name));
    })
    .map((profile) => profile.name);
}

/** W05.4: is the ECM trace live, and if not, which of the three reasons. */
export function traceLiveness(
  trace: TableTrace,
  channels: Map<string, ChannelSnapshot>,
  now: number,
  status: StatusResponse | null,
  profiles: CaptureProfile[] | null,
): TraceLiveness {
  const specs = [...new Set([trace.x.channel, trace.y.channel])];
  const stale = specs.filter((spec) => !traceSample(channels, spec, now));
  if (!stale.length) return { state: "live" };
  const vehicle = status?.vehicle_state?.state;
  if (!status || (vehicle !== "engine_running" && vehicle !== "ignition_only")) {
    return {
      state: "no_ecm_data",
      message: `Trace greyed: no ECM data (vehicle state ${vehicle ?? "unknown"}). The trace channels answer only with the key on.`,
    };
  }
  const profile = status.capture_profile ?? null;
  const selected = new Set(
    profile && profile.mode22_enabled !== false
      ? (profile.memory ?? []).map((n) => n.toUpperCase())
      : [],
  );
  const unpolled = stale.filter((spec) => !selected.has(bare(spec)));
  if (unpolled.length) {
    const carrying = profilesCarrying(trace, profiles);
    const offer = carrying.length
      ? `${carrying.map((n) => `“${n}”`).join(", ")} carries them — start it on Logging.`
      : `No capture profile in this vehicle's catalog polls them.${trace.unavailable_note ? ` ${trace.unavailable_note}` : ""}`;
    return {
      state: "not_polled",
      message: `Trace greyed: ${unpolled.join(", ")} not polled by the active profile “${profile?.name ?? "unknown"}”. ${offer}`,
      profiles: carrying,
      note: trace.unavailable_note,
    };
  }
  const requests = status.health?.obd_scheduler?.requests ?? [];
  const counters = stale.map((spec) => {
    const axis = spec === trace.x.channel ? trace.x : trace.y;
    const row = requests.find((r) => r.kind === "memory" && r.channel === axis.request_channel);
    if (!row) return `${spec}: no scheduler request ${axis.request_channel ?? ""} found`.trim();
    return `${spec} (${row.channel}): sent ${row.requests_sent ?? 0}, responses ${row.responses ?? 0}, timeouts ${row.timeouts ?? 0}, NRCs ${row.negative_responses ?? 0}`;
  });
  return {
    state: "not_answering",
    message: `Trace greyed: polled but not answering in the last ${TRACE_CHANNEL_STALE_MS / 1000} s — ${counters.join("; ")}.`,
  };
}

/** A per-cell count grid shaped like `table.values`. */
export type CoverageGrid = number[][];

export function emptyCoverage(table: RomTable): CoverageGrid {
  return table.values.map((row) => row.map(() => 0));
}

export function addCoverage(grid: CoverageGrid, position: CellPosition): CoverageGrid {
  return grid.map((row, r) =>
    r === position.row ? row.map((count, c) => (c === position.column ? count + 1 : count)) : row,
  );
}

export { coverageClass, THIN_COVERAGE_SAMPLES, type CoverageClass } from "./colorScale";

/** Sample-count shading (moved to `colorScale.coverage`, R08.7). */
export function coverageColor(count: number, max: number): string {
  return coverage(count, max);
}

/** A residency grid can shade a displayed table only if it was binned on the
 *  same breakpoints — a log from another revision may have other axes. */
export function residencyMatchesTable(residency: CellResidency, table: RomTable): boolean {
  if (!residency.available) return false;
  const same = (a: number[], b: number[]) =>
    a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < 1e-6);
  return same(residency.x_axis, table.x_axis) && same(residency.y_axis, table.y_axis);
}

/** How old `/status.vehicle_state` may be before the trace stops trusting
 *  it, and how far in the future (clock skew) it may claim to be. */
const VEHICLE_STATE_MAX_AGE_MS = 15000;
const VEHICLE_STATE_MAX_LEAD_MS = 1000;

export interface TraceGateInput {
  /** The operator asserted this ROM and slot are running. */
  confirmed: boolean;
  rom: Pick<RomInspection, "matches_recorded_revision" | "recorded_revision"> | null;
  status: StatusResponse | null;
  now: number;
  /** The commanded-AFR fuel-cut sentinel is fresh. */
  fuelCut: boolean;
  rpm: { timestamp: number } | null;
  bfs: { timestamp: number } | null;
}

export interface TraceGate {
  /** Why no trace (inferred or ECM) is called active; "" when none. */
  gate: string;
  /** Why the inferred lookup is not drawn: the gate, or a live-input
   *  reason (fuel cut, missing or skewed RPM/BFS); "" when it is. */
  trace: string;
}

/** The ROM-maps trace gates (W05.5, R08.8), first failing reason wins.
 *  Nothing is called an active cell unless the operator asserts this ROM and
 *  slot are running and nothing the daemon knows contradicts it. */
export function traceGate({
  confirmed,
  rom,
  status,
  now,
  fuelCut,
  rpm,
  bfs,
}: TraceGateInput): TraceGate {
  const vehicle = status?.vehicle_state;
  const stateAge = vehicle?.derived_at ? now - Date.parse(vehicle.derived_at) : NaN;
  const stateFresh =
    Number.isFinite(stateAge) &&
    stateAge >= -VEHICLE_STATE_MAX_LEAD_MS &&
    stateAge <= VEHICLE_STATE_MAX_AGE_MS;
  let gate = "";
  if (!confirmed) gate = "Confirm the selected ROM and map slot before tracing.";
  else if (!rom?.matches_recorded_revision)
    gate = "Selected ROM differs from the recorded active revision; viewing only.";
  else if (status?.calibration_matches_registry === false)
    gate = `ECU reports calibration id ${status.ecu_calibration_id ?? "unknown"}, which does not match the flash history's ${status.registry_revision ?? "running revision"} (${status.registry_calibration_id ?? "no recorded id"}). The ECU is not running the recorded ROM, so no trace is called active.`;
  else if (
    status?.registry_revision &&
    rom.recorded_revision &&
    status.registry_revision !== rom.recorded_revision
  )
    gate = `The flash history's running revision is now ${status.registry_revision}, not this ROM's ${rom.recorded_revision}; no trace is called active.`;
  else if (vehicle?.state === "replay") gate = "Replay active — live-map tracing is disabled.";
  else if (!stateFresh || vehicle?.state !== "engine_running")
    gate = "Waiting for fresh engine-running status.";
  if (gate) return { gate, trace: gate };
  if (fuelCut) return { gate, trace: "Fuel-cut sentinel — no active fueling cell inferred." };
  if (!rpm || !bfs)
    return {
      gate,
      trace:
        "Waiting for fresh RPM and BFS. Missing/stale channels are not substituted with load or throttle.",
    };
  if (Math.abs(rpm.timestamp - bfs.timestamp) > TRACE_MAX_SKEW_MS)
    return { gate, trace: "RPM/BFS sample skew exceeds 750 ms; trace paused." };
  return { gate, trace: "" };
}
