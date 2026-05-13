import { useState } from "react";

import { useAnomalyStream } from "./hooks/useAnomalyStream";
import { useStatus } from "./hooks/useStatus";
import { useTelemetryStream } from "./hooks/useTelemetryStream";
import { AnomalyPanel } from "./panels/AnomalyPanel";
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

  return (
    <DashboardShell
      appMode={mode}
      onToggleMode={() => setMode(mode === "driving" ? "parked" : "driving")}
      telemetryState={telemetry.connection}
      anomalyState={anomalies.connection}
      events={anomalies.events}
    >
      <div className="panel-grid">
        <TelemetryPanel channels={telemetry.channels} />
        <AnomalyPanel events={anomalies.events} />
        <ModelPanel status={status} events={anomalies.events} />
        <SystemPanel
          status={status}
          channels={telemetry.channels}
          statusError={statusError}
        />
        <TpmsPanel channels={telemetry.channels} />
        <DiagnosticsPanel mode={mode} />
      </div>
    </DashboardShell>
  );
}
