import { useCallback, useEffect, useState } from "react";

import { Panel } from "../components/Panel";
import { HttpError, getReplay, startReplay, stopReplay } from "../api/http";
import type { RunAction } from "../hooks/useAsyncAction";
import { describeError, useControlResource } from "../hooks/useControlResource";
import { buttonClass, inputClass } from "../styles";
import type { ReplayStatus, SessionSummary } from "../types";
import { formatElapsed } from "../util/time";

interface ReplayCardProps {
  token: string;
  run: RunAction;
  sessions: SessionSummary[];
  /** Selected session in the Sessions list (Task 3.2: "prefilled from the
   *  selected session") — re-synced below whenever it changes, unless the
   *  operator already picked a different one for the replay itself. */
  selectedSessionId: string;
}

/** A `startReplay`/`stopReplay` `HttpError`'s `body` is the FastAPI
 *  `{"detail": "..."}` JSON the daemon raised — including the 409
 *  refuse-while-driving text (7.2, operator-approved) that must render
 *  verbatim, not paraphrased through `describeError`'s generic "Server
 *  error 409." bucket. */
function replayErrorMessage(err: unknown): string {
  if (err instanceof HttpError) {
    try {
      const parsed = JSON.parse(err.body) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      // Not JSON — fall through to the raw body.
    }
    return err.body || err.message;
  }
  // Anything that isn't an HTTP answer — notably an unreachable daemon —
  // reads better through the shared formatter than as a raw browser string.
  return describeError(err);
}

/** Start/stop/inspect runtime replay from the dashboard (Task 2.2), hosted
 *  in the Sessions panel beside the session it replays (3.2). Replay is a
 *  bench/desk tool — the 409 the daemon raises while `vehicle_state ==
 *  "engine_running"` is shown exactly as the daemon phrased it. */
export function ReplayCard({ token, run, sessions, selectedSessionId }: ReplayCardProps) {
  const [sessionId, setSessionId] = useState(selectedSessionId);
  const [speed, setSpeed] = useState("1");
  const [loop, setLoop] = useState(false);

  // Follow the Sessions list's selection until the operator picks a
  // different one for the replay itself (or the selection disappears from
  // the polled list, e.g. after a delete).
  useEffect(() => {
    setSessionId((current) => {
      if (current && sessions.some((s) => s.session_id === current)) return current;
      return selectedSessionId || sessions.at(-1)?.session_id || "";
    });
  }, [selectedSessionId, sessions]);

  const fetcher = useCallback((signal: AbortSignal) => getReplay(token, signal), [token]);
  const replayRes = useControlResource(fetcher, { enabled: Boolean(token), intervalMs: 3000 });
  // The 3 s poll is the source of truth once it's landed at least once; a
  // mutation's own response updates this immediately in between polls so
  // Start/Stop don't wait out a stale poll to reflect in the UI.
  const [replay, setReplay] = useState<ReplayStatus | null>(null);
  useEffect(() => {
    if (replayRes.data) setReplay(replayRes.data.replay);
  }, [replayRes.data]);

  const start = () =>
    run("replay start", async () => {
      if (!sessionId) throw new Error("Select a session first.");
      let result;
      try {
        result = await startReplay(token, {
          session_id: sessionId,
          speed: Number(speed) || 1,
          loop,
        });
      } catch (err) {
        throw new Error(replayErrorMessage(err));
      }
      setReplay(result.replay);
    });

  const stop = () =>
    run("replay stop", async () => {
      let result;
      try {
        result = await stopReplay(token);
      } catch (err) {
        throw new Error(replayErrorMessage(err));
      }
      setReplay(result.replay);
    });

  return (
    <Panel
      title="Replay"
      actions={
        <span className={replay?.running ? "text-[0.8rem] text-warn" : "text-fg-dim"}>
          {replay?.running ? "running" : "idle"}
        </span>
      }
    >
      <div className="grid grid-cols-[1fr_5rem_auto] gap-2 max-[1100px]:grid-cols-1">
        <select
          className={inputClass}
          value={sessionId}
          onChange={(event) => setSessionId(event.target.value)}
          disabled={Boolean(replay?.running)}
        >
          <option value="">select a session…</option>
          {sessions
            .slice()
            .reverse()
            .map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {s.session_id}
              </option>
            ))}
        </select>
        <input
          className={inputClass}
          value={speed}
          onChange={(event) => setSpeed(event.target.value)}
          inputMode="decimal"
          placeholder="speed"
          disabled={Boolean(replay?.running)}
        />
        {replay?.running ? (
          <button className={buttonClass} type="button" onClick={stop} disabled={!token}>
            Stop
          </button>
        ) : (
          <button
            className={buttonClass}
            type="button"
            onClick={start}
            disabled={!token || !sessionId}
          >
            Start
          </button>
        )}
      </div>
      <label className="mt-2 flex min-h-9 flex-row items-center gap-2 text-[0.75rem] text-fg-dim">
        <input
          type="checkbox"
          checked={loop}
          onChange={(event) => setLoop(event.target.checked)}
          disabled={Boolean(replay?.running)}
        />
        loop
      </label>
      {replay?.running && (
        <div className="mt-3 flex flex-col gap-1 rounded-[8px] border border-warn/35 bg-warn/10 p-2.5 text-[0.82rem]">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <b>{replay.source_session_id}</b>
            {replay.started_at && (
              <span className="font-num tabular-nums text-fg-dim">
                elapsed <ReplayElapsed startedAt={replay.started_at} />
              </span>
            )}
          </div>
          <span
            className="truncate text-fg-dim"
            title="isolated _replay-* output — never mixed into the training corpus"
          >
            → {replay.output_session_dir}
          </span>
        </div>
      )}
    </Panel>
  );
}

/** Ticks its own clock rather than the parent's — the parent only re-renders
 *  on the 3 s replay-status poll, which would otherwise make "elapsed" jump
 *  in 3 s steps instead of counting smoothly. */
function ReplayElapsed({ startedAt }: { startedAt: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  return <>{formatElapsed(startedAt, now)}</>;
}
