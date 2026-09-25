import { useCallback, useEffect, useState } from "react";

/** The one poll loop (R08.9). `useStatus`, `useBehaviorModels`,
 *  `useBehaviorLive` and `useControlResource` each ran their own; the last
 *  used `setInterval`, which fired again while the previous request was still
 *  in flight, stacking requests on a Pi that was already slow to answer.
 *
 *  Scheduling is completion-chained: the next fetch is armed `intervalMs`
 *  after the previous one *settles*, so two requests from one hook never
 *  overlap. `intervalMs: null` fetches once (and again on `refresh`). */
export interface PollingOptions {
  /** Fetch only while true. Turning it off clears the state. */
  enabled?: boolean;
  /** Delay between one fetch settling and the next starting; `null` means
   *  fetch once. */
  intervalMs: number | null;
}

export interface PollingState<T> {
  /** Last successful result; kept across later failures. */
  data: T | null;
  /** Last failure, raw (callers describe it); cleared by the next success. */
  error: unknown;
  /** True until the first fetch settles (false while disabled). */
  loading: boolean;
  /** True while `data`/`error` came from a previous `fetcher` (its
   *  parameters changed) and the new fetch has not settled yet. Lets a page
   *  show "Loading…" over the old view instead of passing it off as current. */
  stale: boolean;
  refresh: () => void;
}

interface Snapshot<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  /** The fetcher whose request produced `data`/`error`. */
  source: unknown;
}

function initial<T>(enabled: boolean): Snapshot<T> {
  return { data: null, error: null, loading: enabled, source: null };
}

/** An aborted fetch rejects with an `AbortError` that is not reliably an
 *  `Error` under jsdom; narrow on the name. */
function isAbort(err: unknown): boolean {
  return (
    typeof err === "object" && err !== null && (err as { name?: unknown }).name === "AbortError"
  );
}

/** Poll `fetcher` (keep it stable with `useCallback`; a new identity restarts
 *  the loop). An `AbortError` is cancellation, never reported as an error. */
export function usePolling<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  { enabled = true, intervalMs }: PollingOptions,
): PollingState<T> {
  const [snapshot, setSnapshot] = useState<Snapshot<T>>(() => initial<T>(enabled));
  const [wasEnabled, setWasEnabled] = useState(enabled);
  const [tick, setTick] = useState(0);

  // Reset on an enabled flip during render rather than in the effect, so a
  // disabled hook never shows the previous token's data for a frame.
  if (wasEnabled !== enabled) {
    setWasEnabled(enabled);
    setSnapshot(initial<T>(enabled));
  }

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let timer: number | null = null;
    let cancelled = false;

    const load = async (): Promise<void> => {
      try {
        const next = await fetcher(controller.signal);
        if (cancelled) return;
        setSnapshot({ data: next, error: null, loading: false, source: fetcher });
      } catch (err) {
        if (cancelled || controller.signal.aborted || isAbort(err)) return;
        setSnapshot((prev) => ({ data: prev.data, error: err, loading: false, source: fetcher }));
      }
      if (!cancelled && intervalMs !== null) {
        timer = window.setTimeout(() => void load(), intervalMs);
      }
    };
    void load();

    return () => {
      cancelled = true;
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [fetcher, enabled, intervalMs, tick]);

  const { data, error, loading, source } = snapshot;
  const stale = enabled && !loading && source !== fetcher;
  return { data, error, loading, stale, refresh };
}
