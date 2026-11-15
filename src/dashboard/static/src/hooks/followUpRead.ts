import { describeError } from "./useControlResource";
import type { RefreshOutcome } from "./usePolling";

/** The read an action waits on after it succeeded could not be loaded. The
 *  action went through; the panel is showing the old state. Loud on purpose
 *  (operator, 2026-11-15): a silently stale panel after a click reads as "it
 *  did nothing", and the next click repeats the action. Background poll
 *  re-reads stay quiet (`useFirstFailure`); only the read an action awaits
 *  comes through here. Its message is what the action's error slot shows. */
export class FollowUpReadError extends Error {
  constructor(cause: unknown) {
    super(`Done, but the refreshed view could not be loaded: ${describeError(cause)}`, { cause });
    this.name = "FollowUpReadError";
  }
}

/** One outcome for several reads: `failed` if any failed, else `abandoned` if
 *  any was (a read nobody took over is no evidence of a fresh view), else `ok`. */
export function combineOutcomes(...outcomes: RefreshOutcome[]): RefreshOutcome {
  const failed = outcomes.find((o) => o.status === "failed");
  if (failed) return failed;
  return outcomes.some((o) => o.status === "abandoned")
    ? { status: "abandoned" }
    : { status: "ok" };
}

/** Throw `FollowUpReadError` for a failed outcome; `ok` and `abandoned`
 *  (superseded, disabled, unmounted) return quietly. */
export function requireFresh(outcome: RefreshOutcome | void): void {
  if (outcome && outcome.status === "failed") throw new FollowUpReadError(outcome.error);
}

/** The follow-up of an action that is hand-rolled rather than run through
 *  `useAsyncAction`'s `then`: `await reloaded(refresh())` inside the try that
 *  already shows the action's error. A `then` needs none of this: the runner
 *  applies `requireFresh` to what it returns. */
export async function reloaded(...reads: Array<Promise<RefreshOutcome>>): Promise<void> {
  requireFresh(combineOutcomes(...(await Promise.all(reads))));
}

/** The notice text for a panel that shows the failure itself rather than
 *  throwing it (a `finally` that must still re-enable the controls); `null`
 *  when the read did not fail. */
export function followUpFailureText(outcome: RefreshOutcome): string | null {
  return outcome.status === "failed" ? new FollowUpReadError(outcome.error).message : null;
}
