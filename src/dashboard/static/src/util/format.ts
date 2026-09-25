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
export function formatPercent(value: number | null | undefined, digits = 1): string {
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

const KM_PER_MILE = 1.609344;
const M_PER_FT = 0.3048;
const CARDINALS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];

/** Celsius, one decimal (host/CPU temps). */
export function formatTemp(c: number | null | undefined): string {
  return c == null ? "—" : `${c.toFixed(1)} °C`;
}

/** "lat, lon" at 5 decimal places, or "—" when either is missing (GPS fix). */
export function formatCoords(lat: number | null, lon: number | null): string {
  return lat == null || lon == null ? "—" : `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

/** Meters → feet (this US vehicle's display unit), whole numbers. */
export function formatAltitude(meters: number | null): string {
  return meters == null ? "—" : `${(meters / M_PER_FT).toFixed(0)} ft`;
}

/** GPS speed arrives in km/h; shown as mph for this US vehicle. */
export function formatGpsSpeed(kmh: number | null): string {
  return kmh == null ? "—" : `${(kmh / KM_PER_MILE).toFixed(0)} mph`;
}

/** Degrees + compass cardinal, e.g. "184° S". */
export function formatHeading(deg: number | null): string {
  return deg == null ? "—" : `${deg.toFixed(0)}° ${CARDINALS[Math.round(deg / 45) % 8]}`;
}
