import { useMemo } from "react";

import { HttpError, NetworkError } from "../api/http";
import { usePolling } from "./usePolling";

export interface ResourceState<T, E = string> {
  data: T | null;
  error: E | null;
  /** True until the first fetch settles (false when disabled). */
  loading: boolean;
  /** HTTP status of the last failure, for auth-aware messaging. */
  status: number | null;
  /** The fetcher changed and its first fetch has not settled; `data` and
   *  `error` are the previous parameters'. See `usePolling`. */
  stale: boolean;
  refresh: () => void;
}

interface Options<E> {
  /** Poll/fetch only when true (e.g. a control token is present). When
   *  false the panel shows its own "enter token" affordance instead of an
   *  error. */
  enabled?: boolean;
  /** Delay after one fetch settles before the next; `null` fetches once
   *  (and again on `refresh`). Default 8 s. */
  intervalMs?: number | null;
  /** Turn a failure into the error the caller shows. Defaults to
   *  `describeError`; the workbench passes its structured `Refusal`
   *  mapper. Need not be stable: it runs at render, not in the loop. */
  describe?: (err: unknown) => E;
}

/** Poll a fetcher with graceful, status-aware error handling (Task 6.14:
 *  401/403/503 must read clearly, not as a generic crash). Built on
 *  `usePolling`, so a slow daemon never has two requests from one panel in
 *  flight. `fetcher` should be stable (wrap in `useCallback`); gate with
 *  `enabled` rather than passing a sometimes-null fetcher so generic
 *  inference stays clean. */
export function useControlResource<T, E = string>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { enabled = true, intervalMs = 8000, describe }: Options<E> = {},
): ResourceState<T, E> {
  const { data, error, loading, stale, refresh } = usePolling(fetcher, { enabled, intervalMs });
  const described = useMemo<E | null>(() => {
    if (error === null) return null;
    return describe ? describe(error) : (describeError(error) as E);
  }, [error, describe]);
  const status = error !== null && error instanceof HttpError ? error.status : null;
  return { data, error: described, loading, stale, status, refresh };
}

/** FastAPI errors are `{"detail": "..."}`; show the sentence, not the JSON
 *  wrapper. Non-JSON bodies (an HTML 500 page, a proxy's error) fall back to
 *  the raw text so nothing is hidden. */
function serverDetail(body: string): string {
  try {
    const parsed: unknown = JSON.parse(body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = parsed.detail;
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
    if (err.status === 503) return "This API isn't available on the daemon (feature not wired).";
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
