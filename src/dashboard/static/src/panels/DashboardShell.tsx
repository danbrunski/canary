import type { ReactNode } from "react";

import { ConnectionIndicator } from "./ConnectionIndicator";
import { OperatingModeIndicator } from "./OperatingModeIndicator";
import type { AnomalyEvent, AppMode, ConnectionState } from "../types";

interface DashboardShellProps {
  appMode: AppMode;
  onToggleMode: () => void;
  telemetryState: ConnectionState;
  anomalyState: ConnectionState;
  events: AnomalyEvent[];
  children: ReactNode;
}

/** The page chrome — header (vehicle/mode/connection state), main panel
 *  grid, parked/driving toggle. Layout is landscape-first; on portrait
 *  the grid stacks via CSS media queries. */
export function DashboardShell({
  appMode,
  onToggleMode,
  telemetryState,
  anomalyState,
  events,
  children,
}: DashboardShellProps) {
  return (
    <div className={`shell shell-${appMode}`}>
      <header className="shell-header">
        <div className="brand">CANary</div>
        <OperatingModeIndicator events={events} />
        <div className="connections">
          <ConnectionIndicator label="telemetry" state={telemetryState} />
          <ConnectionIndicator label="anomalies" state={anomalyState} />
        </div>
        <button
          type="button"
          className={`mode-toggle ${appMode}`}
          onClick={onToggleMode}
          aria-label={
            appMode === "driving"
              ? "switch to parked / maintenance mode"
              : "switch to driving mode"
          }
        >
          {appMode === "driving" ? "→ parked" : "→ driving"}
        </button>
      </header>
      <main>{children}</main>
    </div>
  );
}
