import { useState } from "react";

import { useAnomalyStream } from "./hooks/useAnomalyStream";
import { useBehaviorModels } from "./hooks/useBehaviorModels";
import { useBindings } from "./hooks/useBindings";
import { useControlToken } from "./hooks/useControlToken";
import { useStatus } from "./hooks/useStatus";
import { useTelemetryStream } from "./hooks/useTelemetryStream";
import { AnomalyPanel } from "./panels/AnomalyPanel";
import { BehaviorModelsPanel } from "./panels/BehaviorModelsPanel";
import { ControlPanel } from "./panels/ControlPanel";
import { DashboardShell } from "./panels/DashboardShell";
import { DiagnosticsPanel } from "./panels/DiagnosticsPanel";
import { ModelPanel } from "./panels/ModelPanel";
import { SystemPanel } from "./panels/SystemPanel";
import { TelemetryPanel } from "./panels/TelemetryPanel";
import { TpmsPanel } from "./panels/TpmsPanel";
import type { AppMode, DashboardView } from "./types";

/** Top-level app: owns the parked/driving toggle, wires hooks into panels. */
export default function App() {
  const [mode, setMode] = useState<AppMode>("driving");
  const [view, setView] = useState<DashboardView>("drive");
  const telemetry = useTelemetryStream();
  const anomalies = useAnomalyStream();
  const { status, error: statusError } = useStatus();
  const behaviorModels = useBehaviorModels();
  const bindings = useBindings();
  const controlToken = useControlToken();

  return (
    <DashboardShell
      appMode={mode}
      activeView={view}
      onToggleMode={() => setMode(mode === "driving" ? "parked" : "driving")}
      onViewChange={setView}
      telemetryState={telemetry.connection}
      anomalyState={anomalies.connection}
      controlState={controlToken.hasToken ? "open" : "closed"}
      controlReady={controlToken.hasToken}
      events={anomalies.events}
    >
      {view === "drive" && (
        <div className="panel-grid drive-grid">
          <TelemetryPanel channels={telemetry.channels} bindings={bindings} compact />
          <AnomalyPanel events={anomalies.events} />
          <SystemPanel
            status={status}
            channels={telemetry.channels}
            statusError={statusError}
          />
          <TpmsPanel channels={telemetry.channels} />
        </div>
      )}
      {view === "charts" && (
        <div className="panel-grid single-grid">
          <TelemetryPanel channels={telemetry.channels} bindings={bindings} />
        </div>
      )}
      {view === "control" && (
        <ControlPanel token={controlToken.token} onTokenChange={controlToken.setToken} />
      )}
      {view === "models" && (
        <div className="panel-grid models-grid">
          <ModelPanel status={status} events={anomalies.events} />
          <BehaviorModelsPanel data={behaviorModels.data} error={behaviorModels.error} />
          <AnomalyPanel events={anomalies.events} />
        </div>
      )}
      {view === "system" && (
        <div className="panel-grid system-grid">
          <SystemPanel
            status={status}
            channels={telemetry.channels}
            statusError={statusError}
          />
          <DiagnosticsPanel mode={mode} />
          <TpmsPanel channels={telemetry.channels} />
        </div>
      )}
    </DashboardShell>
  );
}
