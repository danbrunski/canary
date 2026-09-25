import { useCallback, useEffect, useState } from "react";

import { listSessions } from "../api/http";
import { Notice } from "../components/Notice";
import { useAsyncAction } from "../hooks/useAsyncAction";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import { describeError } from "../hooks/useControlResource";
import type { SessionSummary } from "../types";
import { ReplayCard } from "./ReplayCard";
import { SessionsCard } from "./SessionsCard";

const TOKEN_GATE = "Enter the control token first.";

interface SessionsPanelProps {
  token: string;
  companion: CompanionSettings;
}

/** Sessions panel (Task 3.1): promotes the session operations already
 *  extracted into `SessionsCard` (refactoring 3.2's god-panel split, which
 *  landed before this task) from ControlPanel's grid into their own
 *  top-level `system-sessions` view, and hosts `ReplayCard` (2.2/3.2) beside
 *  it — replay is a per-session action, prefilled from whichever session is
 *  selected here. Owns the session poll and the shared selection so both
 *  cards read the same list/session without a second fetch. */
export function SessionsPanel({ token, companion }: SessionsPanelProps) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [selectedSessionId, setSelectedSessionId] = useState("");
  const [showCaptures, setShowCaptures] = useState(false);
  const { notice, setNotice, busy, run: runAction } = useAsyncAction();

  const refreshSessions = useCallback(async () => {
    if (!token) return;
    const next = await listSessions(token, undefined, undefined, showCaptures);
    setSessions(next.sessions);
  }, [token, showCaptures]);

  useEffect(() => {
    if (!token) {
      setSessions([]);
      return;
    }
    refreshSessions().catch((err: unknown) => {
      setNotice({ kind: "error", text: describeError(err) });
    });
    const timer = window.setInterval(() => {
      refreshSessions().catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [refreshSessions, token, setNotice]);

  // Keep the selection valid as the polled list changes; default to the
  // latest session once one becomes available (same rule `SessionsCard`
  // used to own privately, moved up here so `ReplayCard` shares it).
  useEffect(() => {
    setSelectedSessionId((current) => {
      if (current && sessions.some((s) => s.session_id === current)) return current;
      return sessions.at(-1)?.session_id ?? "";
    });
  }, [sessions]);

  const run = useCallback(
    (label: string, action: () => Promise<void>) =>
      runAction(label, action, {
        precheck: () => (token ? null : TOKEN_GATE),
        then: refreshSessions,
      }),
    [runAction, token, refreshSessions],
  );

  return (
    <section className="flex flex-col gap-3">
      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />
      <label className="flex items-center gap-2 text-sm text-fg-dim">
        <input
          type="checkbox"
          checked={showCaptures}
          onChange={(event) => setShowCaptures(event.target.checked)}
        />
        captures ({showCaptures ? "isolated capture sessions" : "drive sessions"})
      </label>
      <div className="grid grid-cols-3 gap-4 max-[1100px]:grid-cols-1">
        <SessionsCard
          token={token}
          run={run}
          sessions={sessions}
          companion={companion}
          selectedSessionId={selectedSessionId}
          onSelectSessionId={setSelectedSessionId}
        />
        <ReplayCard
          token={token}
          run={run}
          sessions={sessions}
          selectedSessionId={selectedSessionId}
        />
      </div>
    </section>
  );
}
