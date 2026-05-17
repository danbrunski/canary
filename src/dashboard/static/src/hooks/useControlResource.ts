import { useEffect, useState } from "react";

import { HttpError } from "../api/http";

export interface ResourceState<T> {
  data: T | null;
  error: string | null;
  /** True until the first fetch settles (false when disabled). */
  loading: boolean;
  /** HTTP status of the last failure, for auth-aware messaging. */
  status: number | null;
  refresh: () => void;
}

interface Options {
  /** Poll/fetch only when true (e.g. a control token is present). When
   *  false the panel shows its own "enter token" affordance instead of an
   *  error. */
  enabled?: boolean;
  intervalMs?: number;
}

/** Poll a fetcher on an interval with graceful, status-aware error
 *  handling (Task 6.14: 401/403/503 must read clearly, not as a generic
 *  crash). `fetcher` should be stable (wrap in `useCallback`); gate with
 *  `enabled` rather than passing a sometimes-null fetcher so generic
 *  inference stays clean. */
export function useControlResource<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { enabled = true, intervalMs = 8000 }: Options = {},
): ResourceState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState<boolean>(enabled);
  const [tick, setTick] = useState(0);

  const refresh = () => setTick((t) => t + 1);

  useEffect(() => {
    if (!enabled) {
      setData(null);
      setError(null);
      setStatus(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    const load = async () => {
      try {
        const next = await fetcher(controller.signal);
        if (cancelled) return;
        setData(next);
        setError(null);
        setStatus(null);
      } catch (err) {
        if (cancelled || controller.signal.aborted) return;
        setStatus(err instanceof HttpError ? err.status : null);
        setError(describeError(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    const timer = window.setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [fetcher, enabled, intervalMs, tick]);

  return { data, error, loading, status, refresh };
}

export function describeError(err: unknown): string {
  if (err instanceof HttpError) {
    if (err.status === 401 || err.status === 403)
      return "Not authorized — enter or check the control token.";
    if (err.status === 503)
      return "This API isn't available on the daemon (feature not wired).";
    if (err.status === 400 || err.status === 422)
      return `Rejected by the server: ${err.body.slice(0, 160)}`;
    return `Server error ${err.status}.`;
  }
  return err instanceof Error ? err.message : String(err);
}
