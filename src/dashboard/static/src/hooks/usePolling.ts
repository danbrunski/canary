import { useCallback, useEffect, useRef, useState } from "react";

/** The one poll loop (R08.9). `useStatus`, `useBehaviorModels`,
 *  `useBehaviorLive` and `useControlResource` each ran their own; the last
 *  used `setInterval`, which fired again while the previous request was still
 *  in flight, stacking requests on a Pi that was already slow to answer.
 *
 *  Scheduling is completion-chained: the next fetch is armed `intervalMs`
 *  after the previous one *settles*, so two requests from one hook never
 *  overlap. `intervalMs: null` fetches once (and again on `refresh`).
 *
 *  A change of `intervalMs` (or `retryMs`) never restarts the loop: the request
 *  in flight is kept and only the pending timer is re-armed, `delay` after the
 *  last fetch settled. A slower cadence therefore fetches nothing, a faster one
 *  fetches at once only if the last result is already older than the new
 *  interval, and a cadence that flaps (a panel mounting and unmounting) cannot
 *  abort and reissue a request each time, which on the Pi's 1.3-2.6 s `/status`
 *  would queue them up. Only a new `fetcher`, `enabled`, `refresh`, or
 *  `intervalMs` going to or from `null` restarts it. */
export interface PollingOptions {
  /** Fetch only while true. Turning it off clears the state. */
  enabled?: boolean;
  /** Delay between one fetch settling and the next starting; `null` means
   *  fetch once. */
  intervalMs: number | null;
  /** Delay before the next fetch when the last one failed; omit to keep
   *  `intervalMs`. A slow poll (a 30 s one) would otherwise leave a transient
   *  failure on screen for a whole interval. */
  retryMs?: number;
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
  { enabled = true, intervalMs, retryMs }: PollingOptions,
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

  // The cadence the running loop reads each time it arms its timer, and the
  // loop's re-arm hook (`null` while no loop runs). Declared before the loop's
  // effect so a loop that starts in the same commit reads the new cadence.
  const cadence = useRef({ intervalMs, retryMs });
  const rearm = useRef<(() => void) | null>(null);
  useEffect(() => {
    cadence.current = { intervalMs, retryMs };
    rearm.current?.();
  }, [intervalMs, retryMs]);

  const repeating = intervalMs !== null;
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let timer: number | null = null;
    let cancelled = false;
    let inFlight = false;
    let failed = false;
    let settledAt = Date.now();

    // Arm the next fetch `delay` after the last one settled (now, if that is
    // already past). Never while one is in flight: its completion arms.
    const arm = (): void => {
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
      const { intervalMs: interval, retryMs: retry } = cadence.current;
      if (cancelled || inFlight || interval === null) return;
      const delay = failed ? (retry ?? interval) : interval;
      timer = window.setTimeout(() => void load(), Math.max(0, settledAt + delay - Date.now()));
    };

    const load = async (): Promise<void> => {
      timer = null;
      inFlight = true;
      failed = false;
      try {
        const next = await fetcher(controller.signal);
        if (cancelled) return;
        setSnapshot({ data: next, error: null, loading: false, source: fetcher });
      } catch (err) {
        if (cancelled || controller.signal.aborted || isAbort(err)) return;
        failed = true;
        setSnapshot((prev) => ({ data: prev.data, error: err, loading: false, source: fetcher }));
      }
      inFlight = false;
      settledAt = Date.now();
      arm();
    };
    rearm.current = arm;
    void load();

    return () => {
      cancelled = true;
      rearm.current = null;
      controller.abort();
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [fetcher, enabled, repeating, tick]);

  const { data, error, loading, source } = snapshot;
  const stale = enabled && !loading && source !== fetcher;
  return { data, error, loading, stale, refresh };
}
