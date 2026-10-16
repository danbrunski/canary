// The pure side of the Logs table (R19.3): row order, the virtualized window,
// value formatting and the CSV export. `FrameTable` keeps the React state
// (sort choice, scroll position) and the rendering.

import type { FrameResponse } from "../types/workbench";
import { toCsv } from "../util/csv";
import { formatReading } from "../util/format";

export const ROW_HEIGHT = 24;
export const VIEWPORT_ROWS = 18;
export const OVERSCAN = 20;

/** The sort key of the time column; every other key is a channel spec. */
export const TIME_KEY = "timestamp";

export interface FrameSort {
  key: string;
  dir: 1 | -1;
}

/** One logged value as the table and the plot captions show it: three
 *  places with trailing zeros dropped, whole numbers from 1000, blank when
 *  missing. */
export function formatFrameValue(value: number | null | undefined): string {
  return formatReading(value, { digits: 3, trimZeros: true, bigAt: 1000, missing: "" });
}

/** Row indices in display order. The time column is the frame's own order,
 *  or its reverse. A channel sorts by value, and a missing value sorts last
 *  in either direction. */
export function sortedRowOrder(
  frame: Pick<FrameResponse, "timestamps" | "values">,
  sort: FrameSort,
): number[] {
  const index = frame.timestamps.map((_, i) => i);
  if (sort.key === TIME_KEY) return sort.dir === 1 ? index : index.reverse();
  const column = frame.values[sort.key] ?? [];
  return index.sort((a, b) => {
    const va = column[a];
    const vb = column[b];
    if (va === null || va === undefined) return 1;
    if (vb === null || vb === undefined) return -1;
    return (va - vb) * sort.dir;
  });
}

export interface RowWindow {
  /** First rendered row (inclusive). */
  first: number;
  /** One past the last rendered row. */
  last: number;
}

/** The rows to render for a scroll position: those in view plus `OVERSCAN`
 *  above and below, with spacer rows standing in for the rest. */
export function visibleRowWindow(scrollTop: number, total: number): RowWindow {
  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const last = Math.min(total, first + VIEWPORT_ROWS + OVERSCAN * 2);
  return { first, last };
}

/** The CSV of every row in `order` (not only the window), channels headed by
 *  spec and unit. */
export function frameCsv(frame: FrameResponse, order: number[]): string {
  const specs = frame.columns.map((c) => c.spec);
  const header = [
    TIME_KEY,
    ...frame.columns.map((c) => (c.unit ? `${c.spec} (${c.unit})` : c.spec)),
  ];
  const rows = order.map((i) => [
    frame.timestamps[i],
    ...specs.map((s) => frame.values[s]?.[i] ?? null),
  ]);
  return toCsv(header, rows);
}

export function frameCsvName(frame: FrameResponse): string {
  return `${frame.session_id}-${frame.regime_report.regime}.csv`;
}
