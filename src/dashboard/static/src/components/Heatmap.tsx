import { useEffect, useRef } from "react";

import type { GridCell } from "../types";

interface HeatmapProps {
  /** Row-major flat list of cells. ``rows`` is the outer index. */
  cells: GridCell[];
  rows: number;
  cols: number;
  width?: number;
  height?: number;
  /** What attribute of each cell to render. ``mean`` shows the fitted value;
   *  ``count`` shows operating-envelope coverage (dark cell = "we've never
   *  observed this corner of the surface"). */
  value?: "mean" | "count";
  /** Optional caption rendered above the canvas. */
  caption?: string;
}

/** Canvas grid renderer for 2D behavior-model surfaces.
 *
 *  Empty cells (``count === 0``) render as dark/transparent so an
 *  operator immediately sees which corners of the operating envelope
 *  haven't been driven yet. The color ramp is a hot-cold gradient on
 *  the populated cells' value range; the legend below the chart gives
 *  the actual scale.
 */
export function Heatmap({
  cells,
  rows,
  cols,
  width = 240,
  height = 140,
  value = "mean",
  caption,
}: HeatmapProps) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    if (cells.length === 0 || rows === 0 || cols === 0) return;

    const populated = cells.filter((c) => c.count > 0);
    if (populated.length === 0) {
      ctx.fillStyle = "#94a3ad";
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillText("no cells populated yet", 8, height / 2);
      return;
    }

    const values = populated.map((c) => (value === "count" ? c.count : c.mean));
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const span = hi - lo || 1;

    const cellW = width / cols;
    const cellH = height / rows;

    cells.forEach((cell, i) => {
      const row = Math.floor(i / cols);
      const col = i % cols;
      const x = col * cellW;
      // Row 0 = bottom of the chart so larger axis-A values render up,
      // matching reader intuition for RPM × LOAD-style maps.
      const y = (rows - 1 - row) * cellH;
      if (cell.count === 0) {
        ctx.fillStyle = "#1f262d"; // bg-elev-2 — visible-but-empty
      } else {
        const v = value === "count" ? cell.count : cell.mean;
        const t = (v - lo) / span;
        ctx.fillStyle = ramp(t);
      }
      ctx.fillRect(x, y, cellW + 0.5, cellH + 0.5);
    });
  }, [cells, rows, cols, width, height, value]);

  return (
    <div className="heatmap-wrap">
      {caption && <div className="heatmap-caption">{caption}</div>}
      <canvas ref={ref} width={width} height={height} />
    </div>
  );
}

/** Hot-cold ramp: dark blue (cold/low) → amber (mid) → bright white (hot/high).
 *  Picked to be readable on the dark dashboard theme; ``t`` is in [0, 1]. */
function ramp(t: number): string {
  const clamped = Math.max(0, Math.min(1, t));
  if (clamped < 0.5) {
    // dark teal → amber
    const k = clamped * 2;
    const r = Math.round(28 + (255 - 28) * k);
    const g = Math.round(80 + (184 - 80) * k);
    const b = Math.round(120 + (74 - 120) * k);
    return `rgb(${r}, ${g}, ${b})`;
  }
  // amber → near-white
  const k = (clamped - 0.5) * 2;
  const r = Math.round(255);
  const g = Math.round(184 + (240 - 184) * k);
  const b = Math.round(74 + (220 - 74) * k);
  return `rgb(${r}, ${g}, ${b})`;
}
