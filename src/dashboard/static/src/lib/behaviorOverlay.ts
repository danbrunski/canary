// Shared helpers for the Models▸Behavior live overlay. Kept out of the
// canvas components so the panel can reuse the exact same severity colors
// and bin math the charts draw with (no drift between the marker color and
// the chip, or between the heatmap outline and the "n=" caption).

import type { BehaviorSeverity } from "../types";
import { formatReading } from "../util/format";
import type { ColorToken } from "./chartTheme";

/** Theme token per severity, for canvas marks (`cssColor(token)`) and SVG
 *  (`cssVar(token)`). Was `SEVERITY_HEX`, a copy of the day palette that
 *  ignored the night theme (R08.7). */
export const SEVERITY_TOKEN: Record<BehaviorSeverity, ColorToken> = {
  ok: "ok",
  warning: "warn",
  critical: "bad",
};

/** Tailwind text class per severity for the chip / readout. */
export const SEVERITY_TEXT: Record<BehaviorSeverity, string> = {
  ok: "text-ok",
  warning: "text-warn",
  critical: "text-bad",
};

/** Theme token for a severity that may be absent (B03.9: an unscored tick
 *  carries `null`); unscored draws in the faint neutral, never the ok green. */
export function severityToken(severity: BehaviorSeverity | null): ColorToken {
  return severity === null ? "fg-faint" : SEVERITY_TOKEN[severity];
}

/** Tailwind text class for a severity that may be absent (see `severityToken`). */
export function severityText(severity: BehaviorSeverity | null): string {
  return severity === null ? "text-fg-dim" : SEVERITY_TEXT[severity];
}

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
  return formatReading(value, { digits });
}
