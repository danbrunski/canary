import { useEffect, useState } from "react";

/** A counter that only goes up, for "is this answer still the one I asked
 *  for?". The ROM editor hand-rolled it four times (`request`, `counter`,
 *  `simRun`, `load`), each as a `useRef(0)` bumped and compared inline; a fifth
 *  copy that forgot the compare is how a stale answer lands over a newer one.
 *
 *  `next()` claims a number nobody has had (a new request's token, or the next
 *  id of a sequence); `isCurrent(token)` is true until anything claims or bumps
 *  after it; `bump()` supersedes whatever is in flight without starting
 *  anything. The returned object is the same for the life of the component, so
 *  it can sit in a callback's closure without going stale. */
export interface Generation {
  /** The latest number handed out (0 before the first). */
  current(): number;
  /** Claim the next number; every earlier token stops being current. */
  next(): number;
  /** Supersede every token handed out so far. */
  bump(): void;
  isCurrent(token: number): boolean;
}

function makeGeneration(): Generation {
  let value = 0;
  return {
    current: () => value,
    next: () => ++value,
    bump: () => {
      value += 1;
    },
    isCurrent: (token) => token === value,
  };
}

export function useGeneration(): Generation {
  return useState(makeGeneration)[0];
}

/** One request in flight at a time: a generation plus the `AbortController`
 *  that goes with it. `start()` aborts the previous request and returns the
 *  new one's token and signal; `cancel()` drops the wait (the result, if it
 *  ever comes, is not current) without starting another; leaving the component
 *  does the same. A request checks `isCurrent(token)` before it shows anything.
 *  A server job behind an aborted request is the server's own: nothing here
 *  stops it. */
export interface LatestRequest {
  start(): { token: number; signal: AbortSignal };
  cancel(): void;
  isCurrent(token: number): boolean;
}

function makeLatestRequest(generation: Generation): LatestRequest {
  let controller: AbortController | null = null;
  return {
    start() {
      controller?.abort();
      controller = new AbortController();
      return { token: generation.next(), signal: controller.signal };
    },
    cancel() {
      generation.bump();
      controller?.abort();
    },
    isCurrent: (token) => generation.isCurrent(token),
  };
}

export function useLatestRequest(): LatestRequest {
  const generation = useGeneration();
  const [latest] = useState(() => makeLatestRequest(generation));
  useEffect(() => () => latest.cancel(), [latest]);
  return latest;
}
