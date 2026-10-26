import type { CSSProperties } from "react";

import { cssVar } from "../lib/chartTheme";
import { inkOn } from "../lib/colorScale";
import { mapColor } from "../lib/romMaps";
import {
  coverageClass,
  coverageColor,
  THIN_COVERAGE_SAMPLES,
  type CellPosition,
  type CoverageClass,
  type CoverageGrid,
} from "../lib/romTrace";

/** What a ROM grid draws on top of its values: the inferred operating-point
 *  cell (solid outline, dashed neighbours), the ECM's own cell (double ring)
 *  and log or live coverage (shading by sample count). Shared by `MapTable`
 *  (the ROM Library) and the ROM Editor's `CellGrid`, which is where ROM Maps'
 *  overlays went. */
export interface GridOverlay {
  /** The inferred lookup from live RPM and BFS. */
  position: CellPosition | null;
  /** The cell the ECM's own trace coordinates point at. */
  ecm: CellPosition | null;
  coverage: CoverageGrid | null;
  dwell: number[][] | null;
}

/** What the overlay says about one cell. */
export interface CellOverlayState {
  primary: boolean;
  nearby: boolean;
  ecmCell: boolean;
  /** Samples in the cell, or undefined without a coverage overlay. */
  count: number | undefined;
  covered: CoverageClass | null;
  dwellS: number | null;
}

export function overlayState(
  overlay: GridOverlay | null | undefined,
  row: number,
  col: number,
): CellOverlayState {
  const position = overlay?.position ?? null;
  const ecm = overlay?.ecm ?? null;
  const count = overlay?.coverage?.[row]?.[col];
  return {
    primary: position?.row === row && position.column === col,
    nearby: !!position?.rows.includes(row) && !!position.columns.includes(col),
    ecmCell: ecm?.row === row && ecm.column === col,
    count,
    covered: count === undefined ? null : coverageClass(count),
    dwellS: overlay?.dwell ? (overlay.dwell[row]?.[col] ?? 0) : null,
  };
}

/** The overlay's part of a cell's accessible name. */
export function overlayLabel(state: CellOverlayState): string {
  const trace = state.primary
    ? ", nearest inferred cell"
    : state.nearby
      ? ", surrounding cell"
      : "";
  const ecm = state.ecmCell ? ", ECM cell" : "";
  if (state.count === undefined) return `${trace}${ecm}`;
  const dwell = state.dwellS === null ? "" : `, ${state.dwellS.toFixed(1)} s dwell`;
  const note =
    state.covered === "unvisited" ? " (never visited)" : state.covered === "thin" ? " (thin)" : "";
  return `${trace}${ecm}, ${state.count} samples${dwell}${note}`;
}

/** The overlay's part of a cell's hover text. */
export function overlayTitle(state: CellOverlayState): string {
  if (state.count === undefined) return "";
  return ` · ${state.count} samples${state.dwellS === null ? "" : ` · ${state.dwellS.toFixed(1)} s`}`;
}

export interface OverlayStyleOptions {
  /** The cell's value colour (`mapColor`), or null when it has none. */
  heat: string | null;
  /** The busiest cell's count, which the coverage ramp scales to. */
  maxCount: number;
}

/** The cell's fill and overlay marks. A coverage overlay replaces the value
 *  colour (the shading is then sample count, and the page says so). Marks are
 *  drawn in the theme's dark ink (`bg`), which reads on every fill of both
 *  ramps; the ECM ring adds a series colour and a light ring around it. */
export function overlayStyle(
  state: CellOverlayState,
  { heat, maxCount }: OverlayStyleOptions,
): CSSProperties {
  const fill = state.count === undefined ? heat : coverageColor(state.count, maxCount);
  return {
    backgroundColor: fill ?? undefined,
    backgroundImage:
      state.covered === "unvisited"
        ? "repeating-linear-gradient(45deg, transparent 0 6px, rgba(100,116,139,0.25) 6px 8px)"
        : undefined,
    outline: state.nearby
      ? `${state.primary ? 3 : 2}px ${state.primary ? "solid" : "dashed"} ${cssVar("bg")}`
      : undefined,
    outlineOffset: state.nearby ? -3 : undefined,
    boxShadow: state.ecmCell
      ? `inset 0 0 0 4px ${cssVar("series-5")}, inset 0 0 0 6px ${cssVar("fg")}`
      : undefined,
    fontWeight: state.primary || state.ecmCell ? 800 : 500,
  };
}

/** The ink class for a cell on `fill`: dark on a light fill, light on a dark
 *  one (the deep end of the coverage ramp). */
export function inkClass(fill: string | undefined): string {
  return fill && inkOn(fill) === "light" ? "text-fg" : "text-bg";
}

/** The inferred-cell and ECM-cell glyphs, so neither mark is colour alone. */
export function OverlayMarks({ state }: { state: CellOverlayState }) {
  return (
    <>
      {state.primary && (
        <span aria-hidden="true" className="absolute left-1 top-0 text-[10px]">
          ●
        </span>
      )}
      {state.ecmCell && (
        <span aria-hidden="true" className="absolute right-1 top-0 text-[10px]">
          ECM
        </span>
      )}
    </>
  );
}

/** The key to a coverage shading. */
export function CoverageLegend({ maxCount }: { maxCount: number }) {
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs text-fg-dim">
      <span className="inline-flex items-center gap-1">
        <span
          aria-hidden="true"
          className="inline-block h-3 w-5 rounded"
          style={{
            backgroundColor: coverageColor(0, maxCount),
            backgroundImage:
              "repeating-linear-gradient(45deg, transparent 0 3px, rgba(100,116,139,0.35) 3px 4px)",
          }}
        />
        never visited
      </span>
      <span className="inline-flex items-center gap-1">
        <span
          aria-hidden="true"
          className="inline-block h-3 w-5 rounded"
          style={{ backgroundColor: coverageColor(1, maxCount) }}
        />
        thin (&lt; {THIN_COVERAGE_SAMPLES} samples)
      </span>
      <span className="inline-flex items-center gap-1">
        <span
          aria-hidden="true"
          className="inline-block h-3 w-5 rounded"
          style={{
            backgroundColor: coverageColor(Math.max(maxCount, THIN_COVERAGE_SAMPLES), maxCount),
          }}
        />
        visited (up to {maxCount})
      </span>
      <span>Shading is sample count; hover a cell for its count.</span>
    </div>
  );
}

/** The key to the value shading: low to high on the shared ramp. */
export function HeatLegend({
  min,
  max,
  unit,
  digits = 2,
}: {
  min: number;
  max: number;
  unit: string;
  digits?: number;
}) {
  return (
    <div className="flex items-center gap-3 text-xs text-fg-dim">
      <span>{min.toFixed(digits)}</span>
      <span
        aria-hidden="true"
        className="h-2 w-40 rounded"
        style={{
          background: `linear-gradient(to right, ${mapColor(min, min, max)}, ${mapColor(min + (max - min) / 3, min, max)}, ${mapColor(min + (2 * (max - min)) / 3, min, max)}, ${mapColor(max, min, max)})`,
        }}
      />
      <span>
        {max.toFixed(digits)} {unit}
      </span>
      <span>Low → high; not a safety rating</span>
    </div>
  );
}
