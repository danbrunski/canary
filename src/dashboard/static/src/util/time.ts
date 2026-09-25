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

/** Locale-default time-of-day; unparseable input falls back to the raw
 *  string (anomaly & evidence timelines). */
export function formatTimestamp(ts: string): string {
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return ts;
  }
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
    if (seconds == null || seconds <= 0) return "0s";
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
