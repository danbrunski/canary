import type { ReactNode } from "react";

import { TokenGate } from "../components/TokenGate";
import type { ConnectivityMode } from "../hooks/useConnectivityMode";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { buttonClass, cn, headerClass, mainClass, shellClass } from "../styles";
import type { Theme } from "../hooks/useTheme";
import type {
  AnomalyEvent,
  AppMode,
  ConnectionState,
  StatusResponse,
} from "../types";
import { ConnectionIndicator } from "./ConnectionIndicator";
import { OperatingModeIndicator } from "./OperatingModeIndicator";
import { ReplayBanner } from "./ReplayBanner";
import { StatusStrip } from "./StatusStrip";
import { VehicleStateBadge } from "./VehicleStateBadge";

interface DashboardShellProps {
  appMode: AppMode;
  onToggleMode: () => void;
  theme: Theme;
  onToggleTheme: () => void;
  telemetryState: ConnectionState;
  anomalyState: ConnectionState;
  controlState: ConnectionState;
  controlReady: boolean;
  /** Task 6.35 — `Live Pi` / `Local Library` / `Queued Offline`. */
  connectivity: ConnectivityMode;
  events: AnomalyEvent[];
  liveMode?: string | null;
  status: StatusResponse | null;
  channels: Map<string, ChannelSnapshot>;
  /** Task 5.1 — the one control-token entry point, rendered here instead of
   *  per-panel. */
  token: string;
  onTokenChange: (token: string) => void;
  children: ReactNode;
}

const CONNECTIVITY_LABEL: Record<ConnectivityMode, string> = {
  "live-pi": "live pi",
  "local-library": "local library",
  "queued-offline": "queued offline",
};

const CONNECTIVITY_CLASS: Record<ConnectivityMode, string> = {
  "live-pi": "border-ok/40 text-ok",
  "local-library": "border-accent-2/40 text-accent-2",
  "queued-offline": "border-warn/40 text-warn",
};

/** Page chrome: header (brand, operating mode, connection state, the
 *  Drive↔Workbench toggle) + the always-visible StatusStrip + the active
 *  body. The mode toggle now switches the *whole* context — a locked
 *  glanceable Drive screen vs. the grouped Workbench — instead of hiding a
 *  single panel. Grouped navigation lives in the Workbench sidebar, not a
 *  flat top tab bar, so it scales to every Phase-6 panel. */
export function DashboardShell({
  appMode,
  onToggleMode,
  theme,
  onToggleTheme,
  telemetryState,
  anomalyState,
  controlState,
  controlReady,
  connectivity,
  events,
  liveMode,
  status,
  channels,
  token,
  onTokenChange,
  children,
}: DashboardShellProps) {
  const driving = appMode === "driving";
  return (
    <div className={shellClass}>
      <header className={headerClass}>
        <div className="flex min-w-[6.5rem] flex-col">
          <div className="bg-[linear-gradient(95deg,var(--color-accent),#ffd9a0_70%)] bg-clip-text text-[1.05rem] font-extrabold tracking-[0.14em] text-transparent">
            CANary
          </div>
          <div className="text-[0.62rem] uppercase tracking-[0.16em] text-fg-faint">
            {driving ? "drive" : "workbench"}
          </div>
        </div>
        <OperatingModeIndicator events={events} liveMode={liveMode} />
        <VehicleStateBadge vehicleState={status?.vehicle_state} />
        <TokenGate token={token} onTokenChange={onTokenChange} />
        <div className="ml-auto flex gap-2 text-[0.78rem] text-fg-dim">
          <span
            className={cn(
              "inline-flex min-h-6 items-center rounded-full border bg-black/25 px-2 text-[0.68rem] uppercase tracking-[0.06em]",
              CONNECTIVITY_CLASS[connectivity],
            )}
            title="Task 6.35 — Live Pi / Local Library / Queued Offline"
          >
            {CONNECTIVITY_LABEL[connectivity]}
          </span>
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
            "rounded-full px-3 text-[0.78rem]",
            theme === "night" && "border-accent-2 text-accent-2",
          )}
          onClick={onToggleTheme}
          aria-label={
            theme === "dark"
              ? "switch to the night (low-glare) theme"
              : "switch to the day theme"
          }
          title="Toggle day / night theme"
        >
          {theme === "dark" ? "◐ day" : "● night"}
        </button>
        <button
          type="button"
          className={cn(
            buttonClass,
            "rounded-full px-4 text-[0.78rem] font-semibold uppercase tracking-[0.04em]",
            !driving && "border-accent bg-accent/15 text-accent",
          )}
          onClick={onToggleMode}
          aria-label={
            driving
              ? "switch to the parked workbench"
              : "switch to the driving screen"
          }
        >
          {driving ? "→ workbench" : "→ drive"}
        </button>
      </header>
      <ReplayBanner replay={status?.replay} />
      <StatusStrip status={status} channels={channels} events={events} />
      <main className={mainClass}>{children}</main>
    </div>
  );
}
