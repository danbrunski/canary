import type { ReactNode } from "react";

import { ConnectionIndicator } from "./ConnectionIndicator";
import { OperatingModeIndicator } from "./OperatingModeIndicator";
import type {
  AnomalyEvent,
  AppMode,
  ConnectionState,
  DashboardView,
} from "../types";

interface DashboardShellProps {
  appMode: AppMode;
  activeView: DashboardView;
  onToggleMode: () => void;
  onViewChange: (view: DashboardView) => void;
  telemetryState: ConnectionState;
  anomalyState: ConnectionState;
  controlState: ConnectionState;
  controlReady: boolean;
  events: AnomalyEvent[];
  children: ReactNode;
}

/** The page chrome — header (vehicle/mode/connection state), main panel
 *  grid, parked/driving toggle. Layout is landscape-first; on portrait
 *  the grid stacks via CSS media queries. */
export function DashboardShell({
  appMode,
  activeView,
  onToggleMode,
  onViewChange,
  telemetryState,
  anomalyState,
  controlState,
  controlReady,
  events,
  children,
}: DashboardShellProps) {
  const views: { id: DashboardView; label: string }[] = [
    { id: "drive", label: "Drive" },
    { id: "charts", label: "Charts" },
    { id: "control", label: "Control" },
    { id: "models", label: "Models" },
    { id: "system", label: "System" },
  ];

  return (
    <div className={`shell shell-${appMode}`}>
      <header className="shell-header">
        <div className="brand-block">
          <div className="brand">CANary</div>
          <div className="brand-subtitle">truck telemetry</div>
        </div>
        <OperatingModeIndicator events={events} />
        <nav className="view-tabs" aria-label="Dashboard views">
          {views.map((view) => (
            <button
              key={view.id}
              type="button"
              className={activeView === view.id ? "active" : ""}
              onClick={() => onViewChange(view.id)}
            >
              {view.label}
            </button>
          ))}
        </nav>
        <div className="connections">
          <ConnectionIndicator label="tel" state={telemetryState} />
          <ConnectionIndicator label="anom" state={anomalyState} />
          <ConnectionIndicator
            label={controlReady ? "ctrl" : "token"}
            state={controlReady ? controlState : "closed"}
          />
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
