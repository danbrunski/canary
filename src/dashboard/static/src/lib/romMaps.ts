import type { ChannelSnapshot } from "../hooks/useTelemetryStream";

export interface RomProvider {
  spec: string;
  transform: "identity" | "scale" | "equiv_ratio_afr";
  factor: number | null;
}

export interface RomTable {
  slot: number;
  kind: "fuel_targets" | "fuel_compensation";
  title: string;
  x_label: string;
  x_unit: string;
  y_label: string;
  y_unit: string;
  unit: string;
  status: "available" | "tbd";
  reason: string | null;
  x_axis: number[];
  y_axis: number[];
  values: number[][];
  raw: number[][];
}

export interface RomInspection {
  source_sha256: string;
  revision: string;
  recorded_revision: string | null;
  matches_recorded_revision: boolean;
  map_slots: number[];
  tables: RomTable[];
  live: Record<"rpm" | "base_fuel_schedule" | "commanded_afr", RomProvider | null>;
  warnings: string[];
  afr_stoich: number;
}

// Display freshness, not a collector or diagnostic threshold. Never leave a
// highlighted cell frozen on the last sample when the stream disconnects.
export const TRACE_MAX_AGE_MS = 2500;
export const TRACE_MAX_SKEW_MS = 750;

export function liveValue(
  channels: Map<string, ChannelSnapshot>, provider: RomProvider | null,
  now: number, stoich: number,
): { value: number; timestamp: number } | null {
  if (!provider) return null;
  const row = channels.get(provider.spec)?.latest;
  if (!row || typeof row.value !== "number" || !Number.isFinite(row.value) || !row.timestamp) return null;
  const timestamp = Date.parse(row.timestamp);
  if (!Number.isFinite(timestamp) || now - timestamp > TRACE_MAX_AGE_MS || timestamp - now > 1000) return null;
  let value = row.value;
  if (provider.transform === "scale") {
    if (provider.factor === null || !Number.isFinite(provider.factor)) return null;
    value *= provider.factor;
  } else if (provider.transform === "equiv_ratio_afr") {
    const sentinel = channels.get(`${provider.spec}_SENTINEL`)?.latest;
    if (sentinel?.timestamp && Date.parse(sentinel.timestamp) >= timestamp) return null;
    if (value <= 0) return null;
    value = stoich / value;
  } else if (provider.transform !== "identity") return null;
  return Number.isFinite(value) ? { value, timestamp } : null;
}

export function axisBracket(axis: number[], value: number): number[] | null {
  if (!axis.length || !Number.isFinite(value) || value < axis[0] || value > axis[axis.length - 1]) return null;
  const hi = axis.findIndex((point) => point >= value);
  return axis[hi] === value ? [hi] : [hi - 1, hi];
}

export function tracePosition(table: RomTable, rpm: number, bfs: number) {
  if (table.kind !== "fuel_targets" || table.status !== "available") return null;
  const rows = axisBracket(table.y_axis, rpm);
  const columns = axisBracket(table.x_axis, bfs);
  if (!rows || !columns) return null;
  const nearest = (indices: number[], axis: number[], value: number) =>
    indices.reduce((a, b) => Math.abs(axis[a] - value) <= Math.abs(axis[b] - value) ? a : b);
  return { rows, columns, row: nearest(rows, table.y_axis, rpm), column: nearest(columns, table.x_axis, bfs) };
}

/** Numeric scale only: green is low, red is high, not safe/dangerous. */
export function mapColor(value: number, min: number, max: number): string {
  const stops = [[80, 190, 110], [245, 210, 65], [248, 155, 55], [230, 80, 65]];
  const fraction = max > min ? Math.min(1, Math.max(0, (value - min) / (max - min))) : 0;
  const step = fraction * (stops.length - 1);
  const low = Math.min(stops.length - 2, Math.floor(step));
  const blend = step - low;
  return `rgb(${stops[low].map((v, i) => Math.round(v + (stops[low + 1][i] - v) * blend)).join(", ")})`;
}
