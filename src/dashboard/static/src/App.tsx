import { PairingApprovalPrompt } from "./components/PairingApprovalPrompt";
import { PairingCard } from "./components/PairingCard";
import { PanelErrorBoundary } from "./components/PanelErrorBoundary";
import { PhoneNav } from "./components/PhoneNav";
import { useAnomalyStream } from "./hooks/useAnomalyStream";
import { useBehaviorLive } from "./hooks/useBehaviorLive";
import { useBehaviorModels } from "./hooks/useBehaviorModels";
import { useCompanionSettings } from "./hooks/useCompanionSettings";
import { useBindings } from "./hooks/useBindings";
import { useConnectivityMode } from "./hooks/useConnectivityMode";
import { useControlToken } from "./hooks/useControlToken";
import { useHashRoute } from "./hooks/useHashRoute";
import { useIsPhone } from "./hooks/useMediaQuery";
import { useScoreStream } from "./hooks/useScoreStream";
import { useStatus } from "./hooks/useStatus";
import { useTelemetryStream } from "./hooks/useTelemetryStream";
import { useTheme } from "./hooks/useTheme";
import { DashboardShell } from "./panels/DashboardShell";
import { DriveScreen } from "./panels/DriveScreen";
import { Workbench, type DashboardData } from "./panels/Workbench";
import type { WorkbenchItem } from "./types";

/** Top-level app: owns the Drive↔Workbench context and the active Workbench
 *  view (mirrored to the URL hash for kiosk reload), wires hooks into the
 *  shell. The mode drives the whole layout (see DashboardShell); the
 *  Workbench owns its grouped navigation. */
export default function App() {
  const { mode, item, setMode, setItem } = useHashRoute();
  const theme = useTheme();
  const telemetry = useTelemetryStream();
  const anomalies = useAnomalyStream();
  const liveScores = useScoreStream();
  const { status, error: statusError } = useStatus();
  const connectivity = useConnectivityMode(status !== null && statusError === null);
  const behaviorModels = useBehaviorModels();
  const behaviorLive = useBehaviorLive();
  const bindings = useBindings();
  const controlToken = useControlToken();
  const companion = useCompanionSettings();
  const phone = useIsPhone();
  // A Workbench page opened from the phone bottom bar or More sheet leaves
  // the driving context, as the desktop mode toggle does.
  const openItem = (next: WorkbenchItem) => {
    setItem(next);
    setMode("parked");
  };

  const data: DashboardData = {
    mode,
    channels: telemetry.channels,
    bindings,
    events: anomalies.events,
    liveScores: liveScores.scores,
    status,
    statusError,
    behaviorModels: behaviorModels.data,
    behaviorModelsError: behaviorModels.error,
    behaviorLive,
    token: controlToken.token,
    companion,
  };

  return (
    <DashboardShell
      appMode={mode}
      onToggleMode={() => setMode(mode === "driving" ? "parked" : "driving")}
      theme={theme.theme}
      onToggleTheme={theme.toggle}
      telemetryState={telemetry.connection}
      anomalyState={anomalies.connection}
      controlState={controlToken.hasToken ? "open" : "closed"}
      controlReady={controlToken.hasToken}
      connectivity={connectivity}
      events={anomalies.events}
      liveMode={behaviorLive?.operating_mode ?? null}
      status={status}
      channels={telemetry.channels}
      token={controlToken.token}
      onTokenChange={controlToken.setToken}
      phone={phone}
      bottomNav={
        <PhoneNav
          mode={mode}
          item={item}
          onDrive={() => setMode("driving")}
          onNavigate={openItem}
        />
      }
    >
      {!controlToken.hasToken && connectivity === "live-pi" && (
        <PairingCard onPaired={controlToken.setToken} />
      )}
      {controlToken.hasToken && <PairingApprovalPrompt token={controlToken.token} />}
      {mode === "driving" ? (
        <PanelErrorBoundary resetKey="drive">
          <DriveScreen
            channels={telemetry.channels}
            bindings={bindings}
            scheduler={status?.health?.obd_scheduler}
          />
        </PanelErrorBoundary>
      ) : (
        <Workbench active={item} onNavigate={setItem} data={data} phone={phone} />
      )}
    </DashboardShell>
  );
}
