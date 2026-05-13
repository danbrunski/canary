import { useState } from "react";

import { useAnomalyStream } from "./hooks/useAnomalyStream";
import { useBehaviorModels } from "./hooks/useBehaviorModels";
import { useBindings } from "./hooks/useBindings";
import { useStatus } from "./hooks/useStatus";
import { useTelemetryStream } from "./hooks/useTelemetryStream";
import { AnomalyPanel } from "./panels/AnomalyPanel";
import { BehaviorModelsPanel } from "./panels/BehaviorModelsPanel";
import { DashboardShell } from "./panels/DashboardShell";
import { DiagnosticsPanel } from "./panels/DiagnosticsPanel";
import { ModelPanel } from "./panels/ModelPanel";
import { SystemPanel } from "./panels/SystemPanel";
import { TelemetryPanel } from "./panels/TelemetryPanel";
import { TpmsPanel } from "./panels/TpmsPanel";
import type { AppMode } from "./types";

/** Top-level app: owns the parked/driving toggle, wires hooks into panels. */
export default function App() {
  const [mode, setMode] = useState<AppMode>("driving");
  const telemetry = useTelemetryStream();
  const anomalies = useAnomalyStream();
  const { status, error: statusError } = useStatus();
  const behaviorModels = useBehaviorModels();
  const bindings = useBindings();

  return (
    <DashboardShell
      appMode={mode}
      onToggleMode={() => setMode(mode === "driving" ? "parked" : "driving")}
      telemetryState={telemetry.connection}
      anomalyState={anomalies.connection}
      events={anomalies.events}
    >
      <div className="panel-grid">
        <TelemetryPanel channels={telemetry.channels} bindings={bindings} />
        <AnomalyPanel events={anomalies.events} />
        <ModelPanel status={status} events={anomalies.events} />
        <SystemPanel
          status={status}
          channels={telemetry.channels}
          statusError={statusError}
        />
        <BehaviorModelsPanel data={behaviorModels.data} error={behaviorModels.error} />
        <TpmsPanel channels={telemetry.channels} />
        <DiagnosticsPanel mode={mode} />
      </div>
    </DashboardShell>
  );
}
