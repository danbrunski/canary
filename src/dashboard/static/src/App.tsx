import { useAnomalyStream } from "./hooks/useAnomalyStream";
import { useBehaviorLive } from "./hooks/useBehaviorLive";
import { useBehaviorModels } from "./hooks/useBehaviorModels";
import { useCompanionSettings } from "./hooks/useCompanionSettings";
import { useBindings } from "./hooks/useBindings";
import { useControlToken } from "./hooks/useControlToken";
import { useHashRoute } from "./hooks/useHashRoute";
import { useScoreStream } from "./hooks/useScoreStream";
import { useStatus } from "./hooks/useStatus";
import { useTelemetryStream } from "./hooks/useTelemetryStream";
import { useTheme } from "./hooks/useTheme";
import { DashboardShell } from "./panels/DashboardShell";
import { DriveScreen } from "./panels/DriveScreen";
import { Workbench, type DashboardData } from "./panels/Workbench";

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
  const behaviorModels = useBehaviorModels();
  const behaviorLive = useBehaviorLive();
  const bindings = useBindings();
  const controlToken = useControlToken();
  const companion = useCompanionSettings();

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
    onTokenChange: controlToken.setToken,
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
      sourceLabel={companion.baseUrl ? "live pi + laptop" : "live pi"}
      events={anomalies.events}
      status={status}
      channels={telemetry.channels}
    >
      {mode === "driving" ? (
        <DriveScreen channels={telemetry.channels} bindings={bindings} />
      ) : (
        <Workbench active={item} onNavigate={setItem} data={data} />
      )}
    </DashboardShell>
  );
}
