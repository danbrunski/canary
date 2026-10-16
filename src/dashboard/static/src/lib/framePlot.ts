// The pure side of the Logs plots (R19.3): ranges, the shared time axis,
// strip polylines, markers, scatter positions and heat-map bins. The
// components keep React state, colours and the SVG. Nothing here draws across
// a gap or interpolates: a missing sample is skipped, and its neighbours are
// joined (the strip) or it is left out (scatter, heat).

import type { FrameResponse } from "../types/workbench";

export type Extent = readonly [number, number];

/** [min, max] of the finite values, or null when there are none. */
export function extent(values: ReadonlyArray<number | null>): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (v === null || !Number.isFinite(v)) continue;
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  return Number.isFinite(lo) ? [lo, hi] : null;
}

/** Where `value` sits in `range`, 0 at the minimum and 1 at the maximum. A
 *  range with no width (a constant channel) puts everything at 0. */
export function fraction(value: number, range: Extent): number {
  return (value - range[0]) / (range[1] - range[0] || 1);
}

// ---------------------------------------------------------------- strips

export interface TimeAxis {
  /** Epoch milliseconds of each row (NaN for an unparseable stamp). */
  times: number[];
  t0: number;
  t1: number;
}

/** The axis every strip shares: first row to last row, as the frame is ordered. */
export function timeAxis(timestamps: string[]): TimeAxis {
  const times = timestamps.map((t) => Date.parse(t));
  const t0 = times[0] ?? 0;
  const t1 = times[times.length - 1] ?? t0 + 1;
  return { times, t0, t1 };
}

/** `t` along the axis, 0 at the first row and `width` at the last. */
export function timeX(axis: TimeAxis, t: number, width: number): number {
  return ((t - axis.t0) / (axis.t1 - axis.t0 || 1)) * width;
}

/** The `points` of a strip's polyline: x on the shared time axis, y inside a
 *  4-unit margin, one decimal. A null sample is skipped, so the line joins
 *  its neighbours across the gap. */
export function stripPoints(
  values: Array<number | null>,
  axis: TimeAxis,
  range: Extent | null,
  width: number,
  height: number,
): string {
  const ty = (v: number) => (range ? height - 4 - fraction(v, range) * (height - 8) : 0);
  return values
    .map((v, i) =>
      v === null ? null : `${timeX(axis, axis.times[i], width).toFixed(1)},${ty(v).toFixed(1)}`,
    )
    .filter(Boolean)
    .join(" ");
}

export interface PlotMarker {
  t: number;
  label: string;
  kind: "tag" | "flash";
}

/** Operator tags, then calibration flashes, that fall inside the axis (both
 *  ends included). */
export function stripMarkers(
  frame: Pick<FrameResponse, "tags" | "revision_boundaries">,
  axis: TimeAxis,
): PlotMarker[] {
  return [
    ...frame.tags.map((tag) => ({ t: Date.parse(tag.t), label: tag.label, kind: "tag" as const })),
    ...frame.revision_boundaries.map((b) => ({
      t: Date.parse(b.flashed_at),
      label: `flash ${b.revision}`,
      kind: "flash" as const,
    })),
  ].filter((m) => m.t >= axis.t0 && m.t <= axis.t1);
}

// ---------------------------------------------------------------- scatter

export interface ScatterPoint {
  /** Row index, so the caller can look up a third channel for colour. */
  index: number;
  cx: number;
  cy: number;
}

/** One point per row that has both axes, in a `width` x `height` box with a
 *  5-unit margin and y growing upward. A row missing either axis is skipped. */
export function scatterPoints(
  xs: ReadonlyArray<number | null>,
  ys: ReadonlyArray<number | null>,
  xr: Extent | null,
  yr: Extent | null,
  width: number,
  height: number,
): ScatterPoint[] {
  const px = (v: number) => (xr ? fraction(v, xr) * (width - 10) + 5 : 0);
  const py = (v: number) => (yr ? height - 5 - fraction(v, yr) * (height - 10) : 0);
  const points: ScatterPoint[] = [];
  xs.forEach((xv, index) => {
    const yv = ys[index];
    if (xv === null || yv === null || yv === undefined) return;
    points.push({ index, cx: px(xv), cy: py(yv) });
  });
  return points;
}

// ---------------------------------------------------------------- heat map

/** Cells per side of the heat map. */
export const HEAT_BINS = 12;

export interface HeatCell {
  bx: number;
  by: number;
  /** Mean of the colour channel, or the sample count when there is none. */
  value: number;
  n: number;
}

/** Bin the samples into a `HEAT_BINS` x `HEAT_BINS` grid over the axes'
 *  ranges; the maximum falls in the last cell. With a colour channel (`zs`)
 *  the cell value is its mean over the samples that have it, and a sample
 *  missing it is not counted; with none the value is the sample count. A
 *  sample missing either axis is skipped. Cells are in first-seen order. */
export function heatCells(
  xs: ReadonlyArray<number | null>,
  ys: ReadonlyArray<number | null>,
  zs: ReadonlyArray<number | null> | null,
  xr: Extent | null,
  yr: Extent | null,
): HeatCell[] {
  if (!xr || !yr) return [];
  const sums = new Map<string, { sum: number; n: number }>();
  xs.forEach((xv, i) => {
    const yv = ys[i];
    const zv = zs ? zs[i] : 1;
    if (xv === null || yv === null || zv === null || zv === undefined) return;
    const bx = Math.min(HEAT_BINS - 1, Math.floor(fraction(xv, xr) * HEAT_BINS));
    const by = Math.min(HEAT_BINS - 1, Math.floor(fraction(yv, yr) * HEAT_BINS));
    const key = `${bx}:${by}`;
    const cell = sums.get(key) ?? { sum: 0, n: 0 };
    cell.sum += zv;
    cell.n += 1;
    sums.set(key, cell);
  });
  return [...sums.entries()].map(([key, cell]) => {
    const [bx, by] = key.split(":").map(Number);
    return { bx, by, value: zs ? cell.sum / cell.n : cell.n, n: cell.n };
  });
}

/** A cell's place on the colour ramp, 0 to 1 across the cells' own range;
 *  mid-ramp when there are no cells to range over. */
export function heatShade(value: number, range: Extent | null): number {
  return range ? fraction(value, range) : 0.5;
}
