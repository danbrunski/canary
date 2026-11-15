import { useEffect, useState } from "react";

/** The wall clock as render state, re-read every `intervalMs` (R08.13).
 *
 *  A component that needs "now" — an age, a "last minute" window — must not
 *  call `Date.now()` while rendering: the value changes between renders of
 *  the same props, and it only moves when something else happens to
 *  re-render. Reading it from state makes the tick explicit.
 *
 *  `enabled: false` stops the clock (no timer runs, the value holds) for a
 *  panel that only needs it while something is open or running. */
export function useNow(intervalMs = 1000, enabled = true): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, enabled]);
  return now;
}
