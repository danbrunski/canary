import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { TelemetryRow } from "../types";
import type { RomProvider, RomTable } from "../types/workbench";
import { sequentialIn } from "./colorScale";

export type { RomInspection, RomProvider, RomTable } from "../types/workbench";

// Display freshness, not a collector or diagnostic threshold. Never leave a
// highlighted cell frozen on the last sample when the stream disconnects.
export const TRACE_MAX_AGE_MS = 2500;
export const TRACE_MAX_SKEW_MS = 750;

/** The live-stream key for a stored memory-channel spec (R08.5): memory rows
 *  are stored as source `obd`, channel `mem:<NAME>`, so `mem:X` streams as
 *  `obd:mem:X`. Any other spec already names its stream. The only place that
 *  rule is written; never hand-build `obd:mem:` keys. */
export function streamKey(spec: string): string {
  return spec.startsWith("mem:") ? `obd:${spec}` : spec;
}

/** A stored channel spec's row on the live stream. Stream keys are
 *  `<source>:<channel>`, and memory rows are stored as source `obd`, channel
 *  `mem:<NAME>` — so the spec `mem:X` arrives keyed `obd:mem:X`. */
export function streamRow(
  channels: Map<string, ChannelSnapshot>,
  spec: string,
): TelemetryRow | null {
  return (channels.get(spec) ?? channels.get(`obd:${spec}`))?.latest ?? null;
}

export function liveValue(
  channels: Map<string, ChannelSnapshot>,
  provider: RomProvider | null,
  now: number,
  /** The ECM's AFR at lambda = 1 (`afr_scale`), never the fuel's stoich. */
  afrScale: number,
): { value: number; timestamp: number } | null {
  if (!provider) return null;
  const row = streamRow(channels, provider.spec);
  if (!row || typeof row.value !== "number" || !Number.isFinite(row.value) || !row.timestamp)
    return null;
  const timestamp = Date.parse(row.timestamp);
  if (!Number.isFinite(timestamp) || now - timestamp > TRACE_MAX_AGE_MS || timestamp - now > 1000)
    return null;
  let value = row.value;
  if (provider.transform === "scale") {
    if (provider.factor === null || !Number.isFinite(provider.factor)) return null;
    value *= provider.factor;
  } else if (provider.transform === "equiv_ratio_afr") {
    const sentinel = streamRow(channels, `${provider.spec}_SENTINEL`);
    if (sentinel?.timestamp && Date.parse(sentinel.timestamp) >= timestamp) return null;
    if (value <= 0) return null;
    value = afrScale / value;
  } else if (provider.transform !== "identity") return null;
  return Number.isFinite(value) ? { value, timestamp } : null;
}

export function axisBracket(axis: number[], value: number): number[] | null {
  if (!axis.length || !Number.isFinite(value) || value < axis[0] || value > axis[axis.length - 1])
    return null;
  const hi = axis.findIndex((point) => point >= value);
  return axis[hi] === value ? [hi] : [hi - 1, hi];
}

export function tracePosition(table: RomTable, rpm: number, bfs: number) {
  if (table.kind !== "fuel_targets" || table.status !== "available") return null;
  const rows = axisBracket(table.y_axis, rpm);
  const columns = axisBracket(table.x_axis, bfs);
  if (!rows || !columns) return null;
  const nearest = (indices: number[], axis: number[], value: number) =>
    indices.reduce((a, b) => (Math.abs(axis[a] - value) <= Math.abs(axis[b] - value) ? a : b));
  return {
    rows,
    columns,
    row: nearest(rows, table.y_axis, rpm),
    column: nearest(columns, table.x_axis, bfs),
  };
}

/** Numeric scale only: green is low, red is high, not safe/dangerous. The
 *  shared sequential ramp (R08.7). */
export function mapColor(value: number, min: number, max: number): string {
  return sequentialIn(value, min, max);
}
