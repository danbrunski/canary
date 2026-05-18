import { useEffect, useState } from "react";

import { getBehaviorLive } from "../api/http";
import type { BehaviorLiveResponse } from "../types";

/** Poll `/models/behavior/live` at ``intervalMs`` (default 2 s).
 *
 *  Deliberately decoupled from `useBehaviorModels` (60 s): the static
 *  surfaces only change on a manual refit, but the operating point moves
 *  every inference tick. The rolling trail lives server-side, so even this
 *  2 s cadence yields a smooth trajectory rather than the sparse points a
 *  client-side sampler would collect. Errors are swallowed — the overlay is
 *  additive, the static surfaces must keep rendering if the daemon is down. */
export function useBehaviorLive(intervalMs = 2_000) {
  const [data, setData] = useState<BehaviorLiveResponse | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const tick = async () => {
      try {
        const next = await getBehaviorLive(controller.signal);
        if (!cancelled) setData(next);
      } catch {
        /* additive overlay — keep last good data, retry next tick */
      }
      if (!cancelled) timer = setTimeout(tick, intervalMs);
    };
    tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [intervalMs]);

  return data;
}
