import { useEffect, useState } from "react";

import { getBehaviorModels } from "../api/http";
import type { BehaviorModelsResponse } from "../types";

/** Poll `/models/behavior` at ``intervalMs`` (default 60 s).
 *
 *  Behavior models change on manual `refit_behavior_models.py` runs, so a
 *  minute cadence is plenty — the alternative would be a WebSocket
 *  notification, but the data is small enough that polling stays free. */
export function useBehaviorModels(intervalMs = 60_000) {
  const [data, setData] = useState<BehaviorModelsResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const tick = async () => {
      try {
        const next = await getBehaviorModels(controller.signal);
        if (!cancelled) {
          setData(next);
          setError(null);
        }
      } catch (err) {
        if (!cancelled && err instanceof Error && err.name !== "AbortError") {
          setError(err);
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
  }, [intervalMs]);

  return { data, error };
}
