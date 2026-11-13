import { useEffect, useSyncExternalStore } from "react";

/** The full-payload cadence while a live panel is mounted (`useStatus` falls
 *  back to its slower idle cadence when none is). */
export const FULL_LIVE_INTERVAL_MS = 5_000;

/** The shortest cadence a panel may ask for. */
export const MIN_LIVE_INTERVAL_MS = 1_000;

/** Which panels need the full `/status` fresh, and how fresh (R29.9).
 *
 *  `useStatus` fetches the full payload (1.3-2.6 s on the Pi) on a slow
 *  cadence. A panel whose content is in it and moves (the GPS fix, the
 *  scheduler's request table, drift and latency, rows written) calls
 *  `useLiveFullStatus()`, and the fast cadence applies while at least one such
 *  panel is mounted. A panel whose fields are slow averages may ask for a
 *  slower cadence than the default (`useLiveFullStatus(15_000)`); with several
 *  panels mounted the shortest cadence asked for wins, so a slow panel never
 *  slows another's. This is a module-level registry, not React context, so a
 *  panel rendered on its own in a test registers into nothing and works.
 *
 *  It is a separate module from `useStatus` so a panel can import it without
 *  pulling in `api/http`, which panel tests replace wholesale. */
const requests = new Map<symbol, number>();
let fastest: number | null = null;
const listeners = new Set<() => void>();

function recompute(): void {
  const next = requests.size === 0 ? null : Math.min(...requests.values());
  if (next === fastest) return;
  fastest = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Hold `useStatus` at a fast full-payload cadence (``intervalMs``, default
 *  5 s, never under 1 s) while this component is mounted. */
export function useLiveFullStatus(intervalMs: number = FULL_LIVE_INTERVAL_MS): void {
  // A floor of 1 s: a full `/status` takes seconds on the Pi, and a cadence of
  // 0 (or a negative one) would be a back-to-back loop. Not a number: the default.
  const asked = Number.isFinite(intervalMs)
    ? Math.max(intervalMs, MIN_LIVE_INTERVAL_MS)
    : FULL_LIVE_INTERVAL_MS;
  useEffect(() => {
    const id = Symbol("live-full-status");
    requests.set(id, asked);
    recompute();
    return () => {
      requests.delete(id);
      recompute();
    };
  }, [asked]);
}

/** The shortest cadence any mounted panel asks for, or `null` when none does
 *  (for `useStatus`). */
export function useLiveFullStatusDemand(): number | null {
  return useSyncExternalStore(
    subscribe,
    () => fastest,
    () => null,
  );
}
