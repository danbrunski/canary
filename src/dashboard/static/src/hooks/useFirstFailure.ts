import { useCallback, useEffect, useRef } from "react";

/** A read for a poll whose first failure is shown and whose re-reads stay
 *  quiet (a 5 s refresh of a panel must not repaint its notice every time the
 *  daemon hiccups).
 *
 *  Returns a fetcher for `usePolling`/`useControlResource` that calls
 *  `onFirstFailure` once, for the first read of each `read` identity that
 *  fails, and rethrows so the poll still sees every failure. An aborted read
 *  (unmount, a new `read`) reports nothing and leaves the next read first.
 *  Keep `read` and `onFirstFailure` stable (`useCallback`). */
export function useFirstFailure<T>(
  read: (signal: AbortSignal) => Promise<T>,
  onFirstFailure: (err: unknown) => void,
): (signal: AbortSignal) => Promise<T> {
  const first = useRef(true);
  useEffect(() => {
    first.current = true;
  }, [read]);
  return useCallback(
    async (signal: AbortSignal): Promise<T> => {
      try {
        const value = await read(signal);
        if (!signal.aborted) first.current = false;
        return value;
      } catch (err) {
        if (!signal.aborted) {
          const report = first.current;
          first.current = false;
          if (report) onFirstFailure(err);
        }
        throw err;
      }
    },
    [read, onFirstFailure],
  );
}
