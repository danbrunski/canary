import { useEffect, useState } from "react";

import type { ReplayStatus } from "../types";
import { formatElapsed } from "../util/time";

interface ReplayBannerProps {
  /** `/status.replay`; null/undefined when no replay controller is wired
   *  (tests / bare app) or the replay block hasn't landed yet. */
  replay: ReplayStatus | null | undefined;
}

/** Full-width "you are looking at replayed data" banner (Task 1.2). Mounted
 *  once in `DashboardShell`, above `<main>`, so it's unmissable in *both*
 *  Drive and Workbench — re-emitted session data must never be mistaken for
 *  the live truck (AGENTS.md's replay-vs-live rule). Renders nothing when no
 *  replay is running; that's the "replay-block-absent" case the FE tests
 *  cover alongside active/inactive. */
export function ReplayBanner({ replay }: ReplayBannerProps) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!replay?.running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [replay?.running]);

  if (!replay?.running) return null;

  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-center gap-3 border-b border-warn/50 bg-warn/15 px-4 py-2 text-center text-[0.82rem] font-bold uppercase tracking-[0.08em] text-warn"
    >
      <span>REPLAY — session {replay.source_session_id ?? replay.replay_id ?? "unknown"}</span>
      {replay.started_at && (
        <span className="font-num normal-case tracking-normal text-warn/90">
          elapsed {formatElapsed(replay.started_at, now)}
        </span>
      )}
      {replay.speed != null && (
        <span className="font-num normal-case tracking-normal text-warn/90">
          {replay.speed}x{replay.loop ? " · looping" : ""}
        </span>
      )}
    </div>
  );
}
