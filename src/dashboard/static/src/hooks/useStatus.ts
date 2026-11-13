import { useEffect, useMemo, useRef } from "react";

import { getStatus, getStatusCompact } from "../api/http";
import { compactChangeKey, overlayCompact } from "../lib/statusView";
import { useLiveFullStatusDemand } from "./statusDemand";
import { usePolling } from "./usePolling";

/** The full `/status` costs 1.3-2.6 s on the Pi and carries the whole scheduler
 *  request table, so it is polled slowly, and at the fast cadence only while a
 *  panel that shows it is mounted (`useLiveFullStatus`, which may ask for a
 *  cadence of its own; the shortest asked for wins). A change in what the
 *  daemon is doing (`compactChangeKey`) fetches it at once. */
export const FULL_IDLE_INTERVAL_MS = 30_000;

/** Polls `/status?view=compact` every ``intervalMs`` (default 5 s) after the
 *  previous poll settles, and the full `/status` on the cadence above, and
 *  returns them as one `StatusResponse`: the last full payload with the
 *  compact fields laid over it (`lib/statusView`). `status` is `null` until the
 *  first full payload lands, so a panel never reads a field the compact view
 *  lacks from an object that has never had it. `loading` covers that first
 *  wait; `error` is the latest failure of either poll, and a failed full poll
 *  is retried after ``intervalMs``. */
export function useStatus(intervalMs = 5000) {
  const compact = usePolling(getStatusCompact, { intervalMs });
  const liveMs = useLiveFullStatusDemand();
  // A failed full fetch is retried at the compact cadence: left to the 30 s
  // interval it would keep `status` null (and the dashboard "offline") for 30 s.
  const full = usePolling(getStatus, {
    intervalMs: liveMs === null ? FULL_IDLE_INTERVAL_MS : Math.min(liveMs, FULL_IDLE_INTERVAL_MS),
    retryMs: intervalMs,
  });

  const key = compact.data === null ? null : compactChangeKey(compact.data);
  const lastKey = useRef(key);
  const { refresh: refreshFull } = full;
  useEffect(() => {
    if (key !== null && lastKey.current !== null && key !== lastKey.current) refreshFull();
    lastKey.current = key;
  }, [key, refreshFull]);

  const status = useMemo(() => {
    if (full.data === null) return null;
    return compact.data === null ? full.data : overlayCompact(full.data, compact.data);
  }, [full.data, compact.data]);

  const failure = compact.error ?? full.error;
  return {
    status,
    error: failure instanceof Error ? failure : null,
    loading: full.loading,
  };
}
