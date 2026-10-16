import { useState } from "react";

import { Panel } from "../../components/Panel";
import { useLogs } from "../../hooks/useWorkbench";
import { buttonClass, cn } from "../../styles";
import type { SessionChoice } from "../../types/workbench";
import { RefusalNotice } from "./WorkbenchNotices";

/** Which sessions an analysis pools: ticked sessions, or every session on a
 *  calibration revision (the running one is offered). The choice itself lives
 *  in the page; this owns the session listing it is made from. */
export function SessionChooser({
  token,
  choice,
  onChoice,
  runningRevision,
  selectedLogId,
}: {
  token: string;
  choice: SessionChoice;
  onChoice: (choice: SessionChoice) => void;
  runningRevision: string | null;
  selectedLogId: string | null;
}) {
  const [includeSessions, setIncludeSessions] = useState(false);
  const { data: logs, error: refusal } = useLogs(token, includeSessions);

  const toggle = (id: string) => {
    const sessions = choice.sessions.includes(id)
      ? choice.sessions.filter((s) => s !== id)
      : [...choice.sessions, id];
    onChoice({ sessions, revision: null });
  };
  const empty = choice.sessions.length === 0 && !choice.revision;
  return (
    <Panel
      title="Sessions"
      count={choice.revision ? `all on ${choice.revision}` : `${choice.sessions.length} chosen`}
      actions={
        <label className="flex items-center gap-1.5 text-xs text-fg-dim">
          <input
            type="checkbox"
            checked={includeSessions}
            onChange={(e) => setIncludeSessions(e.target.checked)}
          />
          Include ordinary sessions
        </label>
      }
    >
      {empty && (
        <p className="m-0 mb-2 text-sm">
          Choose sessions or a revision.{" "}
          <button
            type="button"
            className={buttonClass}
            disabled={!runningRevision}
            title={runningRevision ? undefined : "The running calibration is not known yet"}
            onClick={() => runningRevision && onChoice({ sessions: [], revision: runningRevision })}
          >
            All on the running calibration{runningRevision ? ` (${runningRevision})` : ""}
          </button>
          {selectedLogId && (
            <button
              type="button"
              className={cn(buttonClass, "ml-2")}
              onClick={() => onChoice({ sessions: [selectedLogId], revision: null })}
            >
              Use the selected log
            </button>
          )}
        </p>
      )}
      {!empty && (
        <button
          type="button"
          className={cn(buttonClass, "mb-2")}
          onClick={() => onChoice({ sessions: [], revision: null })}
        >
          Clear selection
        </button>
      )}
      <RefusalNotice refusal={refusal} />
      {logs && (
        <ul className="max-h-48 space-y-0.5 overflow-auto text-xs">
          {logs.logs.map((log) => (
            <li key={log.session_id}>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={choice.sessions.includes(log.session_id)}
                  onChange={() => toggle(log.session_id)}
                />
                <span>{log.session_id}</span>
                <span className="text-fg-faint">
                  {log.profile ?? "session"} · {log.revision.revision ?? "unknown revision"}
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
