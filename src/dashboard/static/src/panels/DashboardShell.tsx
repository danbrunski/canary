import type { ReactNode } from "react";

import { ConnectionIndicator } from "./ConnectionIndicator";
import { OperatingModeIndicator } from "./OperatingModeIndicator";
import { buttonClass, cn, headerClass, mainClass, shellClass } from "../styles";
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
    <div className={shellClass}>
      <header className={headerClass}>
        <div className="flex min-w-[6.5rem] flex-col">
          <div className="bg-[linear-gradient(95deg,var(--color-accent),#ffd9a0_70%)] bg-clip-text text-[1.05rem] font-extrabold tracking-[0.14em] text-transparent">
            CANary
          </div>
          <div className="text-[0.62rem] uppercase tracking-[0.16em] text-fg-faint">
            truck telemetry
          </div>
        </div>
        <OperatingModeIndicator events={events} />
        <nav
          className="flex items-center gap-1 rounded-full border border-border-soft bg-black/30 p-1 max-[900px]:order-4 max-[900px]:w-full max-[900px]:overflow-x-auto"
          aria-label="Dashboard views"
        >
          {views.map((view) => (
            <button
              key={view.id}
              type="button"
              className={cn(
                "min-h-8 flex-1 rounded-full border-0 bg-transparent px-3.5 py-1.5 text-[0.82rem] font-semibold tracking-[0.02em] text-fg-dim transition-colors hover:text-fg max-[900px]:flex-[1_0_auto]",
                activeView === view.id &&
                  "bg-[linear-gradient(180deg,var(--color-elev-3),var(--color-elev-2))] text-fg shadow-[inset_0_1px_0_rgba(255,255,255,0.05),0_2px_8px_-4px_rgba(0,0,0,0.7)]",
              )}
              onClick={() => onViewChange(view.id)}
            >
              {view.label}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex gap-2 text-[0.78rem] text-fg-dim max-[900px]:ml-0">
          <ConnectionIndicator label="tel" state={telemetryState} />
          <ConnectionIndicator label="anom" state={anomalyState} />
          <ConnectionIndicator
            label={controlReady ? "ctrl" : "token"}
            state={controlReady ? controlState : "closed"}
          />
        </div>
        <button
          type="button"
          className={cn(
            buttonClass,
            "rounded-full px-4 text-[0.78rem] font-semibold uppercase tracking-[0.04em]",
            appMode === "parked" && "border-accent bg-accent/15 text-accent",
          )}
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
      <main className={mainClass}>{children}</main>
    </div>
  );
}
