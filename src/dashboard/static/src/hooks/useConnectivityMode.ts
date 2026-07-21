import { useEffect, useState } from "react";

import { getCompanionStatus } from "../api/http";

export type ConnectivityMode = "live-pi" | "local-library" | "queued-offline";

/** Task 6.35 — the 3-state indicator the offline dashboard/library mode
 *  needs: `Live Pi`, `Local Library`, or `Queued Offline`.
 *
 * The same React build is served by two different backends (the live Pi
 * daemon and the local companion app — see `src/companion/api.py` mounting
 * `dist/`), and only one of `/status` (daemon-only) / `/companion/status`
 * (companion-only) exists on whichever origin actually served this page.
 * That mutual exclusivity *is* the signal:
 *   - `/status` reachable → `"live-pi"` (the real-time Pi dashboard).
 *   - `/status` unreachable but `/companion/status` reachable →
 *     `"local-library"` (running against the offline companion).
 *   - neither reachable → `"queued-offline"` (this tab has no live backend
 *     at all right now — actions queue locally until one comes back).
 *
 * `statusOk` is passed in rather than re-fetched — `useStatus()` already
 * polls `/status` for the rest of the dashboard; this hook only adds the
 * one poll `/status` doesn't cover.
 */
export function useConnectivityMode(statusOk: boolean, intervalMs = 5000): ConnectivityMode {
  const [companionReachable, setCompanionReachable] = useState(false);

  useEffect(() => {
    // No need to poll `/companion/status` once we know the daemon itself
    // answers `/status` - that already settles the mode as "live-pi".
    if (statusOk) return undefined;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const tick = async () => {
      try {
        await getCompanionStatus(controller.signal);
        if (!cancelled) setCompanionReachable(true);
      } catch (err) {
        if (!cancelled && !(err instanceof Error && err.name === "AbortError")) {
          setCompanionReachable(false);
        }
      }
      if (!cancelled) timer = setTimeout(tick, intervalMs);
    };
    tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [statusOk, intervalMs]);

  if (statusOk) return "live-pi";
  return companionReachable ? "local-library" : "queued-offline";
}
