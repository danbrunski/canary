import { useEffect, useState } from "react";

import { getStatus } from "../api/http";
import type { StatusResponse } from "../types";

/** Polls `/status` at ``intervalMs`` (default 5 s). Returns the latest snapshot,
 *  the load error (if any), and a `loading` flag for the first request. */
export function useStatus(intervalMs = 5000) {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const tick = async () => {
      try {
        const next = await getStatus(controller.signal);
        if (!cancelled) {
          setStatus(next);
          setError(null);
        }
      } catch (err) {
        if (!cancelled && err instanceof Error && err.name !== "AbortError") {
          setError(err);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
      if (!cancelled) {
        timer = setTimeout(tick, intervalMs);
      }
    };
    tick();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) clearTimeout(timer);
    };
  }, [intervalMs]);

  return { status, error, loading };
}
