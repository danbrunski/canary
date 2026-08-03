import { useEffect, useState } from "react";

import { HttpError, NetworkError } from "../api/http";

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

/** FastAPI errors are `{"detail": "..."}`; show the sentence, not the JSON
 *  wrapper. Non-JSON bodies (an HTML 500 page, a proxy's error) fall back to
 *  the raw text so nothing is hidden. */
function serverDetail(body: string): string {
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = (parsed as { detail: unknown }).detail;
      if (typeof detail === "string") return detail.slice(0, 160);
    }
  } catch {
    /* not JSON — fall through to the raw body */
  }
  return body.slice(0, 160);
}

export function describeError(err: unknown): string {
  if (err instanceof NetworkError) {
    // The page itself can be served from the service-worker cache, so
    // "the dashboard loads" is no evidence the daemon is up — say so.
    return (
      `Can't reach the daemon (${err.url}) — the request got no response. ` +
      "Check the Pi is powered and on this network, and that its hostname " +
      "still resolves. The dashboard shell is cached offline, so this page " +
      "keeps loading even when the daemon is gone."
    );
  }
  if (err instanceof HttpError) {
    if (err.status === 401 || err.status === 403)
      return "Not authorized — enter or check the control token.";
    if (err.status === 503)
      return "This API isn't available on the daemon (feature not wired).";
    if (err.status === 400 || err.status === 422)
      return `Rejected by the server: ${serverDetail(err.body)}`;
    // 404/409 carry the daemon's own reason ("no capture is active", "a
    // capture profile is already active"); collapsing them to the bare
    // status was throwing away the only actionable part.
    if (err.status === 404 || err.status === 409)
      return `Refused by the server: ${serverDetail(err.body)}`;
    return `Server error ${err.status}.`;
  }
  return err instanceof Error ? err.message : String(err);
}
