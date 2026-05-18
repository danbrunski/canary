// Defensive coercion from `unknown` — the model `detail` blobs and several
// `/status` sub-objects are untyped JSON, so panels must narrow values
// before use. These were copy-pasted across TrainingPanel /
// ModelEvidencePanel / ModelHealthPanel (and had drifted: one `numberArray`
// copy let NaN/Infinity through). One definition now.

/** A finite number, or null for anything else (NaN/Infinity included). */
export function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A finite number, or `fallback` for anything else. */
export function numberOr(value: unknown, fallback: number): number {
  return numberOrNull(value) ?? fallback;
}

/** Finite numbers only — non-arrays and non-finite entries are dropped. */
export function numberArray(value: unknown): number[] {
  return Array.isArray(value)
    ? value.filter((v): v is number => typeof v === "number" && Number.isFinite(v))
    : [];
}

/** Every entry stringified; `[]` for non-arrays. */
export function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** The value if it is an array, else null. */
export function arrayOrNull(value: unknown): unknown[] | null {
  return Array.isArray(value) ? value : null;
}

/** A plain object (not null, not an array), else null. */
export function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** `Object.values` of a plain object, else null. */
export function objectValues(value: unknown): unknown[] | null {
  const obj = record(value);
  return obj ? Object.values(obj) : null;
}

/** `{ [k]: finite number }` keeping only numeric entries, or null when the
 *  input is not an object or has no numeric entries. */
export function numberMap(value: unknown): Record<string, number> | null {
  const obj = record(value);
  if (!obj) return null;
  const out: Record<string, number> = {};
  for (const [key, raw] of Object.entries(obj)) {
    const n = numberOrNull(raw);
    if (n !== null) out[key] = n;
  }
  return Object.keys(out).length > 0 ? out : null;
}

/** `{ [k]: number[] }` keeping only non-empty finite series, or null when
 *  the input is not an object or yields no series. */
export function numberSeriesMap(value: unknown): Record<string, number[]> | null {
  const obj = record(value);
  if (!obj) return null;
  const out: Record<string, number[]> = {};
  for (const [key, raw] of Object.entries(obj)) {
    const series = numberArray(raw);
    if (series.length > 0) out[key] = series;
  }
  return Object.keys(out).length > 0 ? out : null;
}
