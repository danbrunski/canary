// Numeric display formatting. Previously three `formatNumber` copies (with
// different small-value precision), two `formatPercent` (one .toFixed(0),
// one .toFixed(1)), and two identical `toneClass`. Parameterized so each
// call site keeps its exact prior output, with one definition.

export type Tone = "ok" | "warn" | "bad";

/** Magnitude-aware: integers ≥100, one decimal ≥10, `smallDigits` below.
 *  `smallDigits` defaults to 2 (Telemetry/Training); evidence cards pass 3. */
export function formatNumber(value: number, smallDigits = 2): string {
  const abs = Math.abs(value);
  if (abs >= 100) return value.toFixed(0);
  if (abs >= 10) return value.toFixed(1);
  return value.toFixed(smallDigits);
}

/** Fraction → percent. `null`/`undefined` → "-". `digits` defaults to 1
 *  (Training); ModelHealth passes 0. */
export function formatPercent(
  value: number | null | undefined,
  digits = 1,
): string {
  return value == null ? "-" : `${(value * 100).toFixed(digits)}%`;
}

/** Always-signed, two decimals (e.g. delta vs. previous run). */
export function formatSigned(value: number): string {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
}

/** Always-signed percent, one decimal. */
export function formatSignedPercent(value: number): string {
  return `${value >= 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
}

/** Tailwind text-tone class for an optional ok/warn/bad tone. */
export function toneClass(tone: Tone | undefined): string {
  if (tone === "ok") return "text-ok";
  if (tone === "warn") return "text-warn";
  if (tone === "bad") return "text-bad";
  return "";
}
