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

/** h:mm:ss / mm:ss elapsed since an ISO start, clamped at zero. */
export function formatElapsed(startedAt: string, now: number): string {
  const start = new Date(startedAt).getTime();
  if (Number.isNaN(start)) return "—";
  const secs = Math.max(0, Math.floor((now - start) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Relative age of a timestamp: "now" / "Ns" / "-" for missing/unparseable. */
export function formatAge(timestamp: string | null): string {
  if (!timestamp) return "-";
  const ageMs = Date.now() - Date.parse(timestamp);
  if (!Number.isFinite(ageMs)) return "-";
  if (ageMs < 1000) return "now";
  return `${Math.round(ageMs / 1000)}s`;
}
