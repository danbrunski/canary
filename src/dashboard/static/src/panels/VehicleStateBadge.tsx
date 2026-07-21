import { cn } from "../styles";
import type { VehicleState } from "../types";

interface VehicleStateBadgeProps {
  /** `/status.vehicle_state`; null before the first `useStatus` poll lands
   *  or on a bare daemon that never computed it. */
  vehicleState: VehicleState | null | undefined;
}

const LABEL: Record<VehicleState["state"], string> = {
  engine_running: "engine running",
  ignition_only: "ignition only",
  engine_off: "engine off",
  no_data: "no data",
  replay: "replay",
  unknown: "unknown",
};

/** `status.vehicle_state` badge (Task 1.1), next to `OperatingModeIndicator`
 *  in the shell header. AGENTS.md is explicit this is the field to read
 *  *first* — "detectors quiet" reads very differently under `engine_off`
 *  vs `engine_running` vs `replay` — so it gets equal header billing to the
 *  ML operating mode, not buried in capture metadata. `no_data`/`unknown`
 *  get the alert treatment: both mean "don't trust the silence". */
export function VehicleStateBadge({ vehicleState }: VehicleStateBadgeProps) {
  const state = vehicleState?.state ?? null;
  return (
    <div
      className="inline-flex min-w-[6.5rem] flex-col items-center rounded-full border border-border-soft bg-elev-2 px-4 py-1"
      title={state === null ? "vehicle_state unavailable" : `vehicle_state: ${state}`}
    >
      <span className="text-[0.56rem] uppercase tracking-[0.18em] text-fg-faint">
        vehicle
      </span>
      <span
        className={cn(
          "text-base font-semibold uppercase tracking-[0.06em]",
          state === "engine_running" && "text-ok",
          state === "ignition_only" && "text-accent",
          state === "engine_off" && "text-fg-dim",
          state === "replay" && "text-accent-2",
          (state === "no_data" || state === "unknown" || state === null) && "text-bad",
        )}
      >
        {state === null ? "—" : LABEL[state]}
      </span>
    </div>
  );
}
