import type { AnomalyEvent } from "../types";

interface OperatingModeIndicatorProps {
  events: AnomalyEvent[];
}

/** Current operating mode badge (Task 5.11). Reads the most recent anomaly
 *  event's `operating_mode` — that's the freshest cue the daemon publishes.
 *  When inference hasn't emitted anything yet, falls back to "unknown".
 *
 *  In a future revision, the daemon can publish operating-mode transitions
 *  on the telemetry broadcaster as a synthesized "system:mode" channel; for
 *  now the anomaly stream is sufficient and avoids a second producer. */
export function OperatingModeIndicator({ events }: OperatingModeIndicatorProps) {
  const recent = events.find((e) => e.operating_mode !== null);
  const mode = recent?.operating_mode ?? "unknown";
  return (
    <div className={`mode-badge mode-${mode}`}>
      <span className="mode-label">mode</span>
      <span className="mode-value">{mode}</span>
    </div>
  );
}
