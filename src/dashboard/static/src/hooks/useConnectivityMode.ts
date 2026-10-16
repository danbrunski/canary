import { useEffect, useState } from "react";

import { getCompanionStatus, getIdentity } from "../api/http";
import { piDisplayName } from "../lib/piLink";

export type ConnectivityMode = "live-pi" | "desk" | "local-library" | "queued-offline";

export interface Connectivity {
  mode: ConnectivityMode;
  /** Display name of the Pi a `desk` daemon is linked to (companion mode);
   *  null when none is linked or companion mode is off. */
  linkedPi: string | null;
}

function isAbort(err: unknown): boolean {
  return err instanceof Error && err.name === "AbortError";
}

/** Task 6.35 — the indicator for where this dashboard is reading from:
 *  `Live Pi`, `Desk`, `Local Library` or `Queued Offline`.
 *
 * The same build is served by a Pi daemon, by a laptop running the whole
 * daemon (`scripts/canary_desk.py`, which with `companion.enabled` also
 * serves the companion routes same-origin), and by the offline local
 * library. Companion routes can therefore sit beside `/status`; they no
 * longer exclude it. What tells the first two apart is `/identity`:
 *   - `/status` answers and `/identity` says `raspberry_pi` → `"live-pi"`.
 *   - `/status` answers and `/identity` says anything else → `"desk"`.
 *     An unreadable `/identity` (a daemon older than the endpoint) reads as
 *     `"live-pi"`, which is what every daemon was before it existed.
 *   - `/status` is silent but `/companion/status` answers →
 *     `"local-library"`.
 *   - neither answers → `"queued-offline"` (actions queue locally until a
 *     backend comes back).
 *
 * `statusOk` is passed in rather than re-fetched — `useStatus()` already
 * polls `/status` for the rest of the dashboard. `/identity` is asked once
 * per time `/status` comes up; `/companion/status` is polled only when it
 * can change what is shown (desk, or no `/status`).
 */
export function useConnectivity(statusOk: boolean, intervalMs = 5000): Connectivity {
  const [hostKind, setHostKind] = useState<string | null>(null);
  const [companionReachable, setCompanionReachable] = useState(false);
  const [linkedPi, setLinkedPi] = useState<string | null>(null);

  useEffect(() => {
    if (!statusOk) return undefined;
    const controller = new AbortController();
    getIdentity(controller.signal).then(
      (identity) => setHostKind(identity.host_kind),
      (err: unknown) => {
        if (!isAbort(err)) setHostKind(null);
      },
    );
    return () => controller.abort();
  }, [statusOk]);

  const desk = statusOk && hostKind !== null && hostKind !== "raspberry_pi";

  useEffect(() => {
    // A Pi daemon answering `/status` settles the mode as "live-pi"; there
    // is nothing for `/companion/status` to add.
    if (statusOk && !desk) return undefined;

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const tick = async () => {
      try {
        const status = await getCompanionStatus(controller.signal);
        if (!cancelled) {
          setCompanionReachable(true);
          setLinkedPi(status.pi ? piDisplayName(status.pi) : null);
        }
      } catch (err) {
        if (!cancelled && !isAbort(err)) {
          setCompanionReachable(false);
          setLinkedPi(null);
        }
      }
      if (!cancelled) timer = setTimeout(() => void tick(), intervalMs);
    };
    // `tick` catches everything it awaits, so it never rejects.
    void tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [statusOk, desk, intervalMs]);

  if (statusOk) return { mode: desk ? "desk" : "live-pi", linkedPi: desk ? linkedPi : null };
  return { mode: companionReachable ? "local-library" : "queued-offline", linkedPi: null };
}

export function useConnectivityMode(statusOk: boolean, intervalMs = 5000): ConnectivityMode {
  return useConnectivity(statusOk, intervalMs).mode;
}
