import type { TuningContext } from "../../hooks/useTuningContext";
import { LogBrowser } from "./LogBrowser";
import { LogViewer } from "./LogViewer";

// The views this page was split into (R19), still importable from here.
export { LogBrowser } from "./LogBrowser";
export { FrameTable } from "./FrameTable";
export { FrameVisual } from "./FrameVisual";
export { LogViewer } from "./LogViewer";

/** W04 Logs: the capture browser and, once a log is selected, that log in a
 *  table or visual presentation. Stands alone — the selected log comes from
 *  the group's persisted context, so `#wb/tuning-logs` opens it directly. */
export function LogsPage({ token, context }: { token: string; context: TuningContext }) {
  return (
    <div className="flex flex-col gap-4">
      <LogBrowser
        token={token}
        selectedId={context.selectedLogId}
        onSelect={(id) => context.setSelectedLogId(id)}
      />
      {context.selectedLogId && token && (
        <LogViewer token={token} sessionId={context.selectedLogId} />
      )}
    </div>
  );
}
