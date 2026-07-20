import { useEffect, useState } from "react";

import {
  deleteSession,
  downloadSession,
  pinSession,
  syncSessionToCompanion,
  unpinSession,
  updateSessionMetadata,
} from "../api/http";
import type { RunAction } from "../hooks/useAsyncAction";
import type { CompanionSettings } from "../hooks/useCompanionSettings";
import {
  buttonClass,
  cn,
  dangerButtonClass,
  inputClass,
  panelClass,
  panelCountClass,
  panelHeaderClass,
  panelTitleClass,
} from "../styles";
import type { SessionSummary } from "../types";
import { currentPiHost } from "../util/browser";
import { parseJsonObject } from "../util/json";

interface SessionsCardProps {
  token: string;
  run: RunAction;
  sessions: SessionSummary[];
  companion: CompanionSettings;
}

/** Sessions list + per-session actions, extracted from ControlPanel's
 *  ~8-concerns god-panel (Task 3.2). Owns its own selection/edit state —
 *  the parent only feeds it the polled session list. */
export function SessionsCard({ token, run, sessions, companion }: SessionsCardProps) {
  const [selectedSessionId, setSelectedSessionId] = useState("");
  const [metadataText, setMetadataText] = useState("{}");
  const [pinReason, setPinReason] = useState("");
  const [syncPin, setSyncPin] = useState(true);

  // Keep the selection valid as the polled list changes; default to the
  // latest session once one becomes available.
  useEffect(() => {
    setSelectedSessionId((current) => {
      if (current && sessions.some((s) => s.session_id === current)) return current;
      return sessions.at(-1)?.session_id ?? "";
    });
  }, [sessions]);

  const selectedSession = sessions.find((s) => s.session_id === selectedSessionId) ?? null;

  const saveMetadata = () =>
    run("metadata update", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      await updateSessionMetadata(token, selectedSessionId, parseJsonObject(metadataText), true);
    });

  const pinSelected = () =>
    run("session pin", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      await pinSession(token, selectedSessionId, pinReason || undefined);
    });

  const unpinSelected = () =>
    run("session unpin", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      await unpinSession(token, selectedSessionId);
    });

  const deleteSelected = () =>
    run("session delete", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      if (!window.confirm(`Delete session ${selectedSessionId}?`)) return;
      await deleteSession(token, selectedSessionId, selectedSession?.pinned ?? false);
    });

  const exportSelected = () =>
    run("session export", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      const blob = await downloadSession(token, selectedSessionId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${selectedSessionId}.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
    });

  const syncSelectedToLaptop = () =>
    run("session sync", async () => {
      if (!selectedSessionId) throw new Error("Select a session first.");
      if (!companion.baseUrl) throw new Error("Enter the companion API URL.");
      await syncSessionToCompanion(companion.baseUrl, companion.token, selectedSessionId, {
        pi_host: currentPiHost(),
        token: token || null,
        pin: syncPin,
        index_with_config: true,
      });
    });

  return (
    <section className={cn(panelClass, "col-span-2 max-[1100px]:col-span-1")}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Sessions</h2>
        <span className={panelCountClass}>{sessions.length}</span>
      </header>
      <div className="flex max-h-80 flex-col gap-1.5 overflow-y-auto">
        {sessions.slice().reverse().map((session) => (
          <button
            key={session.session_id}
            type="button"
            className={cn(
              "grid w-full grid-cols-[1fr_auto] gap-2 rounded-[8px] border border-border bg-elev-2 px-2.5 py-2 text-left text-fg transition-colors hover:border-border-soft max-[1100px]:grid-cols-1",
              session.session_id === selectedSessionId && "border-accent",
            )}
            onClick={() => setSelectedSessionId(session.session_id)}
          >
            <span>{session.session_id}</span>
            <small className="text-fg-dim">
              {session.telemetry_chunks} chunks / {session.tag_count} tags
              {session.pinned ? " / pinned" : ""}
            </small>
          </button>
        ))}
      </div>
      {selectedSession && (
        <div className="mt-3 flex flex-col gap-2">
          <textarea
            className={cn(inputClass, "min-h-20 w-full resize-y font-num text-[0.8rem]")}
            value={metadataText}
            onChange={(event) => setMetadataText(event.target.value)}
            rows={3}
            spellCheck={false}
          />
          <div className="flex flex-wrap gap-1.5">
            <button className={buttonClass} type="button" onClick={saveMetadata}>Metadata</button>
            <button className={buttonClass} type="button" onClick={exportSelected}>Export</button>
            <button className={buttonClass} type="button" onClick={syncSelectedToLaptop}>Sync to laptop</button>
            <button className={buttonClass} type="button" onClick={pinSelected}>Pin</button>
            <button className={buttonClass} type="button" onClick={unpinSelected}>Unpin</button>
            <button type="button" className={dangerButtonClass} onClick={deleteSelected}>
              Delete
            </button>
          </div>
          <label className="flex min-h-9 flex-row items-center gap-2 text-[0.75rem] text-fg-dim">
            <input
              type="checkbox"
              checked={syncPin}
              onChange={(event) => setSyncPin(event.target.checked)}
            />
            pin on Pi during sync
          </label>
          <input className={inputClass} value={pinReason} onChange={(event) => setPinReason(event.target.value)} placeholder="pin reason" />
        </div>
      )}
    </section>
  );
}
