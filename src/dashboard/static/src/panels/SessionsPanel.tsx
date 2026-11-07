import { useCallback, useEffect, useState } from "react";

import { listSessions } from "../api/http";
import { Notice } from "../components/Notice";
import { useAsyncAction } from "../hooks/useAsyncAction";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import { describeError } from "../hooks/useControlResource";
import type { SessionSummary } from "../types";
import { ReplayCard } from "./ReplayCard";
import { SessionsCard } from "./SessionsCard";
import { TOKEN_FIRST } from "../lib/tokenMessages";

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

  // Clearing the token clears the list: on the change, while rendering,
  // rather than in the effect a commit later.
  const [listedToken, setListedToken] = useState(token);
  if (token !== listedToken) {
    setListedToken(token);
    if (!token) setSessions([]);
  }

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    // The first read reports its failure; the 5 s re-reads stay quiet.
    const load = async (report: boolean): Promise<void> => {
      try {
        const next = await listSessions(token, undefined, undefined, showCaptures);
        if (!cancelled) setSessions(next.sessions);
      } catch (err) {
        if (!cancelled && report) setNotice({ kind: "error", text: describeError(err) });
      }
    };
    void load(true);
    const timer = window.setInterval(() => void load(false), 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [token, showCaptures, setNotice]);

  // Keep the selection valid as the polled list changes; default to the
  // latest session once one becomes available (same rule `SessionsCard`
  // used to own privately, moved up here so `ReplayCard` shares it).
  // Adjusted while rendering, on a new list, so it lands in the same commit.
  const [selectionList, setSelectionList] = useState(sessions);
  if (selectionList !== sessions) {
    setSelectionList(sessions);
    const kept =
      selectedSessionId && sessions.some((s) => s.session_id === selectedSessionId)
        ? selectedSessionId
        : (sessions.at(-1)?.session_id ?? "");
    if (kept !== selectedSessionId) setSelectedSessionId(kept);
  }

  const run = useCallback(
    (label: string, action: () => Promise<void>) =>
      runAction(label, action, {
        precheck: () => (token ? null : TOKEN_FIRST),
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
