// Shared helpers for the Models▸Behavior live overlay. Kept out of the
// canvas components so the panel can reuse the exact same severity colors
// and bin math the charts draw with (no drift between the marker color and
// the chip, or between the heatmap outline and the "n=" caption).

import type { BehaviorSeverity } from "../types";

/** Canvas hex per severity — mirrors the `--color-ok/warn/bad` theme tokens
 *  (index.css). Canvas can't read CSS custom properties cheaply, so the
 *  values are duplicated here with that file named as the source of truth. */
export const SEVERITY_HEX: Record<BehaviorSeverity, string> = {
  ok: "#5fe3b3",
  warning: "#ffd166",
  critical: "#ff6f6f",
};

/** Tailwind text class per severity for the chip / readout. */
export const SEVERITY_TEXT: Record<BehaviorSeverity, string> = {
  ok: "text-ok",
  warning: "text-warn",
  critical: "text-bad",
};

/** Index of the bin ``value`` falls into for a strictly-increasing
 *  ``edges`` array, or ``null`` when out of the grid extent. Inclusive on
 *  both ends, matching ``src/analysis/surface_2d._bin_index`` so the UI
 *  highlights exactly the cell the decoder evaluated. */
export function binIndex(value: number, edges: number[]): number | null {
  const n = edges.length;
  if (n < 2) return null;
  if (value < edges[0] || value > edges[n - 1]) return null;
  for (let k = 0; k < n - 1; k++) {
    if (value >= edges[k] && value <= edges[k + 1]) return k;
  }
  return n - 2;
}

/** Compact numeric formatter for the readout row; ``"—"`` for null/NaN. */
export function fmt(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return value.toFixed(digits);
}
