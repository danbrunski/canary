import { useCallback, useState, type ReactNode } from "react";

import { Sheet } from "../components/Sheet";
import { TokenGate } from "../components/TokenGate";
import type { ConnectivityMode } from "../hooks/useConnectivityMode";
import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { buttonClass, cn, headerClass, mainClass, shellClass } from "../styles";
import type { Theme } from "../hooks/useTheme";
import type { AnomalyEvent, AppMode, ConnectionState, StatusResponse } from "../types";
import { ConnectionIndicator } from "./ConnectionIndicator";
import { OperatingModeIndicator } from "./OperatingModeIndicator";
import { ReplayBanner } from "./ReplayBanner";
import { StatusStrip } from "./StatusStrip";
import { VehicleStateBadge } from "./VehicleStateBadge";
import { CaptureProfileBanner } from "./CaptureProfileBanner";

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
  /** Phone layout (P2.2, below 600px): the header condenses to brand,
   *  connection status and one menu, and `bottomNav` replaces the mode
   *  toggle. */
  phone?: boolean;
  /** The phone bottom bar; rendered under the body only when `phone`. */
  bottomNav?: ReactNode;
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
 *  flat top tab bar, so it scales to every Phase-6 panel. On a phone the
 *  bottom bar (`bottomNav`) does that job instead. */
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
  phone = false,
  bottomNav,
  children,
}: DashboardShellProps) {
  const driving = appMode === "driving";
  const connectivityBadge = (
    <span
      className={cn(
        "inline-flex min-h-6 items-center rounded-full border bg-black/25 px-2 text-[0.68rem] uppercase tracking-[0.06em]",
        CONNECTIVITY_CLASS[connectivity],
      )}
      title="Task 6.35 — Live Pi / Local Library / Queued Offline"
    >
      {CONNECTIVITY_LABEL[connectivity]}
    </span>
  );
  const streams = (
    <>
      <ConnectionIndicator label="tel" state={telemetryState} />
      <ConnectionIndicator label="anom" state={anomalyState} />
      <ConnectionIndicator
        label={controlReady ? "ctrl" : "token"}
        state={controlReady ? controlState : "closed"}
      />
    </>
  );
  const themeToggle = (
    <button
      type="button"
      className={cn(
        buttonClass,
        "rounded-full px-3 text-[0.78rem]",
        theme === "night" && "border-accent-2 text-accent-2",
      )}
      onClick={onToggleTheme}
      aria-label={
        theme === "dark" ? "switch to the night (low-glare) theme" : "switch to the day theme"
      }
      title="Toggle day / night theme"
    >
      {theme === "dark" ? "◐ day" : "● night"}
    </button>
  );
  const brand = (
    <div className="flex min-w-[6.5rem] flex-col max-[600px]:min-w-0">
      <div className="bg-[linear-gradient(95deg,var(--color-accent),#ffd9a0_70%)] bg-clip-text text-[1.05rem] font-extrabold tracking-[0.14em] text-transparent">
        CANary
      </div>
      <div className="text-[0.62rem] uppercase tracking-[0.16em] text-fg-faint">
        {driving ? "drive" : "workbench"}
      </div>
    </div>
  );

  return (
    <div className={shellClass}>
      {phone ? (
        <header className={headerClass}>
          {brand}
          <PhoneHeaderMenu
            summary={
              <ConnectionSummary
                connectivity={connectivity}
                states={[telemetryState, anomalyState]}
              />
            }
          >
            <MenuSection title="Connection">
              <div className="flex flex-wrap items-center gap-2">
                {connectivityBadge}
                {streams}
              </div>
            </MenuSection>
            <MenuSection title="Vehicle">
              <div className="flex flex-wrap items-center gap-2">
                <VehicleStateBadge vehicleState={status?.vehicle_state} />
                <OperatingModeIndicator events={events} liveMode={liveMode} />
              </div>
            </MenuSection>
            <MenuSection title="Control token">
              {/* Right-aligned: the token popover opens leftwards from the
                  pill's right edge, so it stays on screen. */}
              <div className="flex justify-end">
                <TokenGate token={token} onTokenChange={onTokenChange} />
              </div>
            </MenuSection>
            <MenuSection title="Display">{themeToggle}</MenuSection>
          </PhoneHeaderMenu>
        </header>
      ) : (
        <header className={headerClass}>
          {brand}
          <OperatingModeIndicator events={events} liveMode={liveMode} />
          <VehicleStateBadge vehicleState={status?.vehicle_state} />
          <TokenGate token={token} onTokenChange={onTokenChange} />
          <div className="ml-auto flex gap-2 text-[0.78rem] text-fg-dim">
            {connectivityBadge}
            {streams}
          </div>
          {themeToggle}
          <button
            type="button"
            className={cn(
              buttonClass,
              "rounded-full px-4 text-[0.78rem] font-semibold uppercase tracking-[0.04em]",
              !driving && "border-accent bg-accent/15 text-accent",
            )}
            onClick={onToggleMode}
            aria-label={driving ? "switch to the parked workbench" : "switch to the driving screen"}
          >
            {driving ? "→ workbench" : "→ drive"}
          </button>
        </header>
      )}
      <ReplayBanner replay={status?.replay} />
      <CaptureProfileBanner state={status?.capture_profile} />
      <StatusStrip status={status} channels={channels} events={events} />
      <main className={mainClass}>{children}</main>
      {phone && bottomNav}
    </div>
  );
}

type Health = "ok" | "busy" | "down";

function streamHealth(states: ConnectionState[]): Health {
  if (states.some((state) => state === "error" || state === "closed")) return "down";
  if (states.some((state) => state === "connecting")) return "busy";
  return "ok";
}

const HEALTH_TEXT: Record<Health, string> = {
  ok: "streams open",
  busy: "connecting",
  down: "stream down",
};

const HEALTH_DOT: Record<Health, string> = {
  ok: "bg-ok shadow-[0_0_0_3px_rgba(95,227,179,0.18)]",
  busy: "animate-pulse bg-warn",
  down: "bg-bad shadow-[0_0_0_3px_rgba(255,111,111,0.2)]",
};

/** The phone header's one connection pill: where the dashboard is reading
 *  from, and a single dot for the worst of the live streams. The per-stream
 *  detail is in the menu. */
function ConnectionSummary({
  connectivity,
  states,
}: {
  connectivity: ConnectivityMode;
  states: ConnectionState[];
}) {
  const health = streamHealth(states);
  return (
    <span
      className={cn(
        "inline-flex min-h-7 min-w-0 items-center gap-1.5 rounded-full border bg-black/25 px-2.5 text-[0.66rem] font-semibold uppercase tracking-[0.05em]",
        CONNECTIVITY_CLASS[connectivity],
      )}
      role="status"
      aria-label={`connection: ${CONNECTIVITY_LABEL[connectivity]}, ${HEALTH_TEXT[health]}`}
    >
      <span className={cn("h-2 w-2 shrink-0 rounded-full", HEALTH_DOT[health])} />
      <span className="truncate">{CONNECTIVITY_LABEL[connectivity]}</span>
    </span>
  );
}

/** Connection status plus the one header menu (theme, control token,
 *  connectivity detail) on a phone. */
function PhoneHeaderMenu({ summary, children }: { summary: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  return (
    <div className="ml-auto flex min-w-0 items-center gap-2">
      {summary}
      <button
        type="button"
        className={cn(buttonClass, "min-h-11 min-w-11 rounded-full px-0 text-[1.1rem]")}
        aria-label="open menu"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        ☰
      </button>
      {open && (
        <Sheet title="Menu" onClose={close}>
          <div className="flex flex-col gap-4">{children}</div>
        </Sheet>
      )}
    </div>
  );
}

function MenuSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h3 className="m-0 text-[0.62rem] font-bold uppercase tracking-[0.16em] text-fg-faint">
        {title}
      </h3>
      {children}
    </section>
  );
}
