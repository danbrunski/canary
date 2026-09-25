import type { AnomalyEvent } from "../types";
import { cn } from "../styles";

interface OperatingModeIndicatorProps {
  events: AnomalyEvent[];
  /** Live mode from `/models/behavior/live`, refreshed every inference tick.
   *  Preferred source: it stays current even when no anomalies fire — e.g.
   *  engine-off `koeo`, where models don't score so the anomaly stream goes
   *  silent and its last `operating_mode` would otherwise stick. */
  liveMode?: string | null;
}

/** Current operating mode badge (Task 5.11). Prefers the live behavior-stream
 *  mode; falls back to the most recent anomaly event's `operating_mode` if the
 *  live feed isn't up yet, then "unknown". */
export function OperatingModeIndicator({ events, liveMode }: OperatingModeIndicatorProps) {
  const recent = events.find((e) => e.operating_mode !== null);
  const mode = liveMode ?? recent?.operating_mode ?? "unknown";
  return (
    <div className="inline-flex min-w-[6.5rem] flex-col items-center rounded-full border border-border-soft bg-elev-2 px-4 py-1">
      <span className="text-[0.56rem] uppercase tracking-[0.18em] text-fg-faint">mode</span>
      <span
        className={cn(
          "text-base font-semibold uppercase tracking-[0.06em]",
          mode === "idle" && "text-ok",
          (mode === "city" || mode === "highway") && "text-accent",
          (mode === "koeo" || mode === "unknown") && "text-fg-dim",
        )}
      >
        {mode}
      </span>
    </div>
  );
}
