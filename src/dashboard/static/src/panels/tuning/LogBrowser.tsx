import { useState } from "react";

import { Panel } from "../../components/Panel";
import { TableScroll } from "../../components/TableScroll";
import { useLogs } from "../../hooks/useWorkbench";
import { cn } from "../../styles";
import type { LogSummary } from "../../types/workbench";
import { formatClockDuration } from "../../util/time";
import { RefusalNotice } from "./WorkbenchNotices";

/** The capture browser: one listing per "include ordinary sessions" choice,
 *  and the row clicked becomes the group's selected log. */
export function LogBrowser({
  token,
  selectedId,
  onSelect,
}: {
  token: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [includeSessions, setIncludeSessions] = useState(false);
  const { data, error: refusal } = useLogs(token, includeSessions);

  return (
    <Panel
      title="Captures"
      count={data ? `${data.logs.length}${data.capped ? ` of ${data.total}` : ""}` : undefined}
      actions={
        <label className="flex items-center gap-1.5 text-xs text-fg-dim">
          <input
            type="checkbox"
            checked={includeSessions}
            onChange={(event) => setIncludeSessions(event.target.checked)}
          />
          Include ordinary sessions
        </label>
      }
    >
      {!token && <p className="m-0 text-sm">Enter the control token to browse logs.</p>}
      <RefusalNotice refusal={refusal} />
      {data && data.logs.length === 0 && (
        <p className="m-0 text-sm text-fg-dim">
          {includeSessions
            ? "No sessions recorded yet."
            : "No captures yet — start one on Logging, or include ordinary sessions."}
        </p>
      )}
      {data && data.logs.length > 0 && (
        <TableScroll className="max-h-80 overflow-auto">
          <table aria-label="Capture browser" className="w-full text-left text-xs">
            <thead className="text-fg-faint">
              <tr>
                <th>Profile</th>
                <th>Start</th>
                <th>Duration</th>
                <th>Tune revision</th>
                <th>Quality</th>
                <th>End reason</th>
                <th className="text-right">Rows</th>
              </tr>
            </thead>
            <tbody>
              {data.logs.map((log) => (
                <LogRow
                  key={log.session_id}
                  log={log}
                  selected={log.session_id === selectedId}
                  onSelect={onSelect}
                />
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </Panel>
  );
}

function LogRow({
  log,
  selected,
  onSelect,
}: {
  log: LogSummary;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <tr
      className={cn(
        "cursor-pointer border-t border-border-soft hover:bg-elev-2",
        selected && "bg-elev-2 text-accent",
      )}
      aria-selected={selected}
      onClick={() => onSelect(log.session_id)}
    >
      <td className="py-1">{log.profile ?? (log.is_capture ? "unknown" : "session")}</td>
      <td title={log.session_id}>
        {log.started_at ? new Date(log.started_at).toLocaleString() : log.session_id}
      </td>
      <td title={log.duration_source ?? "unknown"}>
        {formatClockDuration(log.duration_s, {
          rounding: "round",
          padMinutes: false,
          missing: "unknown",
        })}
      </td>
      <td>
        {log.revision.revision ?? "unknown"}
        <span className="text-fg-faint"> · {log.revision.source.replace(/_/g, " ")}</span>
        {log.revision_boundaries.length > 0 && (
          <span className="ml-1 text-warn" title="A flash falls inside this log">
            ⚑ flash inside
          </span>
        )}
      </td>
      <td
        className={
          log.quality?.verdict === "pass" ? "text-ok" : log.quality ? "text-warn" : "text-fg-faint"
        }
      >
        {log.quality?.verdict ?? "unknown"}
      </td>
      <td>{log.end_reason?.replace(/_/g, " ") ?? (log.is_capture ? "open" : "—")}</td>
      <td className="text-right font-num">{log.row_count?.toLocaleString() ?? "unknown"}</td>
    </tr>
  );
}
