// The dashboard's value-to-colour ramps (R08.7). There were five unrelated
// ones — Heatmap, the trim-surface grid, the ROM map, ROM coverage and an HSL
// rainbow on Logs — so the trim surface and the ROM map, adjacent tuning
// pages, coloured the same idea differently. Three named ramps now:
//
// - `sequential`: low → high magnitude (map values, surfaces, densities).
// - `diverging`: a signed delta around zero (trim corrections, revision
//   comparisons), so "no change" is always the same neutral.
// - `coverage`: sample counts, where never-visited is not "low".
//
// None is a safety scale. Cells carry dark text (`text-bg`), so every stop is
// light enough to read it on. Ramps encode data, not chrome, and are the same
// in both themes.

type Rgb = readonly [number, number, number];

function clamp01(t: number): number {
  return Number.isFinite(t) ? Math.max(0, Math.min(1, t)) : 0;
}

function interpolate(stops: readonly Rgb[], t: number): string {
  const step = clamp01(t) * (stops.length - 1);
  const low = Math.min(stops.length - 2, Math.floor(step));
  const blend = step - low;
  const rgb = stops[low].map((v, i) => Math.round(v + (stops[low + 1][i] - v) * blend));
  return `rgb(${rgb.join(", ")})`;
}

/** Green → yellow → orange → red. The ROM maps' ramp, now everyone's. */
const SEQUENTIAL: readonly Rgb[] = [
  [80, 190, 110],
  [245, 210, 65],
  [248, 155, 55],
  [230, 80, 65],
];

/** Blue (negative) → pale neutral (zero) → red (positive). */
const DIVERGING: readonly Rgb[] = [
  [96, 150, 235],
  [224, 229, 234],
  [230, 80, 65],
];

/** `t` in [0, 1] (clamped) on the sequential ramp. */
export function sequential(t: number): string {
  return interpolate(SEQUENTIAL, t);
}

/** `value` within [min, max] on the sequential ramp. */
export function sequentialIn(value: number, min: number, max: number): string {
  return sequential(max > min ? (value - min) / (max - min) : 0);
}

/** `t` in [-1, 1] (clamped): -1 blue, 0 neutral, +1 red. */
export function diverging(t: number): string {
  return interpolate(DIVERGING, (Math.max(-1, Math.min(1, Number.isFinite(t) ? t : 0)) + 1) / 2);
}

/** `value` on the diverging ramp, scaled symmetrically by the largest
 *  magnitude so zero is always the neutral midpoint. */
export function divergingAround0(value: number, maxAbs: number): string {
  return diverging(maxAbs > 0 ? value / maxAbs : 0);
}

/** Coverage cells under this many samples are "thin", distinct from none. */
export const THIN_COVERAGE_SAMPLES = 5;

export type CoverageClass = "unvisited" | "thin" | "visited";

export function coverageClass(count: number): CoverageClass {
  return count <= 0 ? "unvisited" : count < THIN_COVERAGE_SAMPLES ? "thin" : "visited";
}

/** Sample-count shading: never-visited is a flat neutral, thin is pale, and
 *  visited cells deepen with count. Not a value scale and not a safety scale. */
export function coverage(count: number, max: number): string {
  const kind = coverageClass(count);
  if (kind === "unvisited") return "rgb(226, 232, 240)";
  if (kind === "thin") return "rgb(191, 219, 254)";
  const fraction =
    max > THIN_COVERAGE_SAMPLES
      ? Math.min(1, (count - THIN_COVERAGE_SAMPLES) / (max - THIN_COVERAGE_SAMPLES))
      : 1;
  return interpolate(
    [
      [96, 165, 250],
      [30, 64, 175],
    ],
    fraction,
  );
}
