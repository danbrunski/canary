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
 * Nothing here places a cell on an unverified axis.
 */
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { CaptureProfile, StatusResponse } from "../types";
import { streamRow, tracePosition } from "./romMaps";
import type { RomTable } from "./romMaps";

export interface AxisTrace {
  channel: string;
  axis_mapping: "verified" | "unverified";
  scale: number | null;
  offset: number | null;
  evidence: string;
  /** Scheduler request carrying the channel (`mem:<window>`), or null. */
  request_channel: string | null;
}

export interface TableTrace {
  x: AxisTrace;
  y: AxisTrace;
  unavailable_note: string | null;
}

/** W05.4: a trace channel older than this greys the ECM trace out. */
export const TRACE_CHANNEL_STALE_MS = 3000;
/** The two traces of one table are separate memory reads; beyond this apart
 *  they are not one operating point. */
export const ECM_TRACE_MAX_SKEW_MS = 750;
/** Coverage cells under this many samples are "thin", distinct from none. */
export const THIN_COVERAGE_SAMPLES = 5;

export function traceSample(
  channels: Map<string, ChannelSnapshot>, spec: string, now: number,
): { value: number; timestamp: number } | null {
  const row = streamRow(channels, spec);
  if (!row || typeof row.value !== "number" || !Number.isFinite(row.value) || !row.timestamp) return null;
  const timestamp = Date.parse(row.timestamp);
  if (!Number.isFinite(timestamp) || now - timestamp > TRACE_CHANNEL_STALE_MS || timestamp - now > 1000) return null;
  return { value: row.value, timestamp };
}

/** Why no ECM cell may be placed on this table, or null when it may. */
export function mappingRefusal(trace: TableTrace | null | undefined): string | null {
  if (!trace) return "This vehicle's rom_viewer.yaml maps no trace channels to this table; no ECM cell is drawn.";
  const unverified = (["x", "y"] as const).filter((axis) => trace[axis].axis_mapping !== "verified");
  if (!unverified.length) return null;
  return `ECM cell not drawn: how the ${unverified.map((axis) => `${axis.toUpperCase()} trace (${trace[axis].channel})`).join(" and ")} lands on this table's breakpoints is not verified. ${unverified.map((axis) => trace[axis].evidence).join(" ")}`;
}

function toAxis(axis: AxisTrace, value: number): number | null {
  if (axis.axis_mapping !== "verified" || axis.scale === null || axis.offset === null) return null;
  const mapped = value * axis.scale + axis.offset;
  return Number.isFinite(mapped) ? mapped : null;
}

export interface EcmPoint { x: number; y: number; xTimestamp: number; yTimestamp: number }

/** The ECM's coordinates in axis units, when fresh, paired and verified. */
export function ecmPoint(trace: TableTrace | null | undefined, channels: Map<string, ChannelSnapshot>, now: number): EcmPoint | null {
  if (!trace || mappingRefusal(trace)) return null;
  const xs = traceSample(channels, trace.x.channel, now);
  const ys = traceSample(channels, trace.y.channel, now);
  if (!xs || !ys || Math.abs(xs.timestamp - ys.timestamp) > ECM_TRACE_MAX_SKEW_MS) return null;
  const x = toAxis(trace.x, xs.value);
  const y = toAxis(trace.y, ys.value);
  return x === null || y === null ? null : { x, y, xTimestamp: xs.timestamp, yTimestamp: ys.timestamp };
}

export type CellPosition = NonNullable<ReturnType<typeof tracePosition>>;

/** The cell the ECM coordinates point at, by the same nearest-breakpoint rule
 *  as the inferred lookup so the two are comparable cell for cell. */
export function ecmPosition(table: RomTable, point: EcmPoint | null): CellPosition | null {
  return point ? tracePosition(table, point.y, point.x) : null;
}

export interface Disagreement {
  rows: number;
  columns: number;
  x: { ecm: number; inferred: number };
  y: { ecm: number; inferred: number };
}

/** How far the ECM cell is from the inferred one, or null when they agree. */
export function cellDisagreement(
  ecm: CellPosition | null, inferred: CellPosition | null,
  ecmAt: { x: number; y: number }, inferredAt: { x: number; y: number },
): Disagreement | null {
  if (!ecm || !inferred) return null;
  const rows = ecm.row - inferred.row;
  const columns = ecm.column - inferred.column;
  if (!rows && !columns) return null;
  return { rows, columns, x: { ecm: ecmAt.x, inferred: inferredAt.x }, y: { ecm: ecmAt.y, inferred: inferredAt.y } };
}

const signed = (n: number) => (n > 0 ? `+${n}` : `${n}`);

export function describeDisagreement(d: Disagreement, table: RomTable): string {
  const parts: string[] = [];
  if (d.columns) parts.push(`${signed(d.columns)} column${Math.abs(d.columns) === 1 ? "" : "s"} (ECM ${d.x.ecm.toFixed(2)} vs inferred ${d.x.inferred.toFixed(2)} ${table.x_unit})`);
  if (d.rows) parts.push(`${signed(d.rows)} row${Math.abs(d.rows) === 1 ? "" : "s"} (ECM ${d.y.ecm.toFixed(0)} vs inferred ${d.y.inferred.toFixed(0)} ${table.y_unit})`);
  return `ECM cell differs from the inferred cell by ${parts.join(" and ")}.`;
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
  trace: TableTrace, channels: Map<string, ChannelSnapshot>, now: number,
  status: StatusResponse | null, profiles: CaptureProfile[] | null,
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
  const selected = new Set(profile && profile.mode22_enabled !== false ? (profile.memory ?? []).map((n) => n.toUpperCase()) : []);
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
  return grid.map((row, r) => (r === position.row ? row.map((count, c) => (c === position.column ? count + 1 : count)) : row));
}

export type CoverageClass = "unvisited" | "thin" | "visited";

export function coverageClass(count: number): CoverageClass {
  return count <= 0 ? "unvisited" : count < THIN_COVERAGE_SAMPLES ? "thin" : "visited";
}

/** Sample-count shading: never-visited is a flat neutral, thin is pale, and
 *  visited cells deepen with count. Not a value scale and not a safety scale. */
export function coverageColor(count: number, max: number): string {
  const kind = coverageClass(count);
  if (kind === "unvisited") return "rgb(226, 232, 240)";
  if (kind === "thin") return "rgb(191, 219, 254)";
  const fraction = max > THIN_COVERAGE_SAMPLES ? Math.min(1, (count - THIN_COVERAGE_SAMPLES) / (max - THIN_COVERAGE_SAMPLES)) : 1;
  const from = [96, 165, 250], to = [30, 64, 175];
  return `rgb(${from.map((v, i) => Math.round(v + (to[i] - v) * fraction)).join(", ")})`;
}

/** `GET /control/sessions/{id}/cell-residency` (W05.6). */
export type CellResidency =
  | {
    available: true; session_id: string; table: string; revision: string | null;
    x_axis: number[]; y_axis: number[]; counts: number[][]; dwell_s: number[][];
    samples: number; outside_axes: number; reason: null;
  }
  | {
    available: false; session_id: string; table: string; reason: string; message: string;
    missing?: string[];
  };

/** A residency grid can shade a displayed table only if it was binned on the
 *  same breakpoints — a log from another revision may have other axes. */
export function residencyMatchesTable(residency: CellResidency, table: RomTable): boolean {
  if (!residency.available) return false;
  const same = (a: number[], b: number[]) => a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < 1e-6);
  return same(residency.x_axis, table.x_axis) && same(residency.y_axis, table.y_axis);
}
