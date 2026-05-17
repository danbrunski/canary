import { useState } from "react";

import { useAnomalyStream } from "./hooks/useAnomalyStream";
import { useBehaviorModels } from "./hooks/useBehaviorModels";
import { useBindings } from "./hooks/useBindings";
import { useControlToken } from "./hooks/useControlToken";
import { useStatus } from "./hooks/useStatus";
import { useTelemetryStream } from "./hooks/useTelemetryStream";
import { DEFAULT_ITEM } from "./nav";
import { DashboardShell } from "./panels/DashboardShell";
import { DriveScreen } from "./panels/DriveScreen";
import { Workbench, type DashboardData } from "./panels/Workbench";
import type { AppMode, WorkbenchItem } from "./types";

/** Top-level app: owns the Drive↔Workbench context and the active Workbench
 *  view, wires hooks into the shell. The mode drives the whole layout (see
 *  DashboardShell); the Workbench owns its grouped navigation. */
export default function App() {
  const [mode, setMode] = useState<AppMode>("driving");
  const [item, setItem] = useState<WorkbenchItem>(DEFAULT_ITEM);
  const telemetry = useTelemetryStream();
  const anomalies = useAnomalyStream();
  const { status, error: statusError } = useStatus();
  const behaviorModels = useBehaviorModels();
  const bindings = useBindings();
  const controlToken = useControlToken();

  const data: DashboardData = {
    mode,
    channels: telemetry.channels,
    bindings,
    events: anomalies.events,
    status,
    statusError,
    behaviorModels: behaviorModels.data,
    behaviorModelsError: behaviorModels.error,
    token: controlToken.token,
    onTokenChange: controlToken.setToken,
  };

  return (
    <DashboardShell
      appMode={mode}
      onToggleMode={() => setMode(mode === "driving" ? "parked" : "driving")}
      telemetryState={telemetry.connection}
      anomalyState={anomalies.connection}
      controlState={controlToken.hasToken ? "open" : "closed"}
      controlReady={controlToken.hasToken}
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
