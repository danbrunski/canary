// Time formatting. Previously re-implemented in TestSessionPanel,
// ControlPanel, AnomalyPanel, ModelEvidencePanel and TelemetryPanel — with
// subtly different invalid-input handling and locale options. Each variant
// is preserved verbatim under one name so call sites are unchanged.

/** Wall-clock h:mm:ss from Unix *seconds* (operator tap log). */
export function formatClockTime(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Wall-clock h:mm:ss from an ISO/parseable string; an unparseable value
 *  falls back to the raw input (capture start, control-event log). */
export function formatTimeOfDay(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/** Locale-default time-of-day. A string is an ISO/parseable stamp; a number is
 *  epoch *milliseconds* (a `Date` argument). Unparseable input falls back to
 *  the raw value as text (anomaly & evidence timelines, the alarm log). The
 *  fallback used to sit in a `catch` that never ran, since an invalid `Date`
 *  formats as "Invalid Date" rather than throwing. */
export function formatTimestamp(ts: string | number): string {
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? String(ts) : date.toLocaleTimeString();
}

export interface DateTimeOptions {
  /** What a number means: epoch "seconds" (the daemon's Unix stamps) or
   *  "milliseconds". A string is always an ISO/parseable stamp. Default
   *  "seconds". */
  unit?: "seconds" | "milliseconds";
  /** "default" is the locale's full date and time; "medium" is the short
   *  paired-device form ("Nov 15, 2026, 3:04 PM"). Default "default". */
  style?: "default" | "medium";
  /** Text for null, undefined or an empty string. Default "—". */
  missing?: string;
}

/** A local date and time. Null/empty reads `missing`; an unparseable value
 *  reads as its own text rather than "Invalid Date" (the bare
 *  `toLocaleString()` copies printed that, the Pi-link card alone fell back). */
export function formatDateTime(
  value: string | number | null | undefined,
  { unit = "seconds", style = "default", missing = "—" }: DateTimeOptions = {},
): string {
  if (value === null || value === undefined || value === "") return missing;
  const date = new Date(typeof value === "number" && unit === "seconds" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return String(value);
  return style === "medium"
    ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
    : date.toLocaleString();
}

export interface ClockDurationOptions {
  /** How fractional seconds collapse. Default "floor" (a running clock never
   *  shows a second that has not passed). */
  rounding?: "floor" | "round";
  /** "auto" shows hours only when nonzero; "always" pads them to two digits
   *  (`00:01:00`). Default "auto". */
  hours?: "auto" | "always";
  /** Pad the minutes when there is no hours field (`01:05` vs `1:05`).
   *  Default true. */
  padMinutes?: boolean;
  /** Text for a null or non-finite duration. Default "—". */
  missing?: string;
}

/** A duration in seconds as a clock: `h:mm:ss`, `mm:ss` or `m:ss`, clamped at
 *  zero (R08.5). The one clock-style formatter: the workbench's log durations,
 *  the tuning bar's capture clock and `formatElapsed` are its options. Not
 *  `formatDuration`, which reads "Ym Zs". */
export function formatClockDuration(
  seconds: number | null | undefined,
  {
    rounding = "floor",
    hours = "auto",
    padMinutes = true,
    missing = "—",
  }: ClockDurationOptions = {},
): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return missing;
  const total = Math.max(0, rounding === "round" ? Math.round(seconds) : Math.floor(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (hours === "always") return `${pad(h)}:${pad(m)}:${pad(s)}`;
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${padMinutes ? pad(m) : m}:${pad(s)}`;
}

/** h:mm:ss / mm:ss elapsed since an ISO start, clamped at zero. */
export function formatElapsed(startedAt: string, now: number): string {
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "—";
  return formatClockDuration((now - start) / 1000);
}

/** Relative age of a timestamp: "now" / "Ns" / "-" for missing/unparseable. */
export function formatAge(timestamp: string | null): string {
  if (!timestamp) return "-";
  const ageMs = Date.now() - Date.parse(timestamp);
  if (!Number.isFinite(ageMs)) return "-";
  if (ageMs < 1000) return "now";
  return `${Math.round(ageMs / 1000)}s`;
}

/** "Ym Zs" from a duration in seconds. `collapseZero` (PowerPanel) rounds,
 *  drops the "0m" prefix, and reads "0s" for null/non-positive; the default
 *  (TripPanel) floors, always shows minutes, and reads "—" for null/NaN. */
export function formatDuration(
  seconds: number | null,
  { collapseZero = false }: { collapseZero?: boolean } = {},
): string {
  if (collapseZero) {
    if (seconds == null || !(seconds > 0)) return "0s"; // also NaN, which was "NaNs"
    const total = Math.round(seconds);
    const m = Math.floor(total / 60);
    const s = total % 60;
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  }
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}m ${s}s`;
}
