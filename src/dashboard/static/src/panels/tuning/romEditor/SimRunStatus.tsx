import { useNow } from "../../../hooks/useNow";
import { FAILURE_TITLE, DEFERRED_ADVICE } from "../../../lib/romSim";
import { SERVER_JOB_LIMIT_S, cancelWording, formatDuration } from "../../../lib/romSimSize";
import { buttonClass } from "../../../styles";
import type { SimFailure } from "../../../types/romEditor";

/** Seconds since `startedAt`, ticking while the component is mounted. */
function useElapsedSeconds(startedAt: number): number {
  return Math.max(0, Math.floor((useNow(500) - startedAt) / 1000));
}

/** One request in flight: a spinner, the time it has taken, and Cancel. Cancel
 *  stops waiting and says so: the daemon keeps the job (see `cancelWording`). */
export function SimRunning({
  startedAt,
  expected,
  onCancel,
}: {
  startedAt: number;
  expected: { seconds: number; measured: boolean };
  onCancel: () => void;
}) {
  const elapsed = useElapsedSeconds(startedAt);
  return (
    <div role="status" className="space-y-2 rounded-lg border border-border p-3 text-sm">
      <div className="flex items-center gap-3">
        <span
          aria-hidden="true"
          className="inline-block size-4 animate-spin rounded-full border-2 border-fg-faint border-t-accent-2"
        />
        <span>
          Simulating… <span className="font-num">{elapsed} s</span> elapsed. One request, one job on
          the daemon; it can take minutes.
        </span>
        <button type="button" className={buttonClass} onClick={onCancel}>
          Cancel (stop waiting)
        </button>
      </div>
      <p className="text-xs text-fg-dim">{cancelWording(expected)}</p>
    </div>
  );
}

/** What the page tells you after Cancel: the request is dropped, the job is not. */
export function SimCancelled({
  startedAt,
  cancelledAt,
  expected,
}: {
  startedAt: number;
  cancelledAt: number;
  expected: { seconds: number; measured: boolean };
}) {
  const remaining = remainingSeconds(startedAt, expected.seconds, useNow(1000));
  return (
    <div role="status" className="space-y-1 rounded-lg border border-warn/60 p-3 text-sm">
      <p className="font-semibold">
        You stopped waiting after {Math.max(0, Math.round((cancelledAt - startedAt) / 1000))} s.
      </p>
      <p>
        The daemon is still running that simulation: nothing here can stop it, and it runs one at a
        time. {remaining}
      </p>
      <p className="text-xs text-fg-dim">
        Until it finishes, a different simulation is refused as busy (409 analysis_busy). Running
        the identical request again re-attaches to the job already running (the daemon shares a
        single-flight job by its request) instead of starting another.
      </p>
    </div>
  );
}

/** How long the abandoned run is still expected to take, in words. */
function remainingSeconds(startedAt: number, expectedS: number, now: number): string {
  const left = expectedS - (now - startedAt) / 1000;
  if (left > 1)
    return `About ${formatDuration(left)} more (estimated ${formatDuration(expectedS)} in all).`;
  return `Its estimate (${formatDuration(expectedS)}) has passed, so it may be about to finish; the daemon stops any such job at ${SERVER_JOB_LIMIT_S} s.`;
}

/** A failed run by its reason: a refusal names the reason, a deferred run says
 *  to take the images to the laptop. */
export function SimFailureAlert({
  failure,
  cancelled,
}: {
  failure: SimFailure;
  /** A run this page stopped waiting for: the busy refusal is that run. */
  cancelled?: { startedAt: number; expectedS: number } | null;
}) {
  const now = useNow(1000);
  return (
    <div role="alert" className="space-y-1 rounded-lg border border-bad/55 p-3 text-sm">
      <p className="font-semibold text-bad">
        {FAILURE_TITLE[failure.kind]}
        {failure.reason ? `: ${failure.reason}` : ""}
      </p>
      {failure.kind === "deferred" && <p>{DEFERRED_ADVICE}</p>}
      <p>{failure.message}</p>
      {failure.kind === "deferred" && failure.laptopPath && (
        <p className="text-fg-dim">{failure.laptopPath}</p>
      )}
      {failure.kind === "busy" && (
        <p className="text-fg-dim">
          Another analysis or digest holds the single workbench slot. Run again when it is done.
        </p>
      )}
      {failure.kind === "busy" && cancelled && (
        <p className="text-fg-dim">
          That is probably the run you cancelled: Cancel stops waiting, the daemon kept running it.{" "}
          {remainingSeconds(cancelled.startedAt, cancelled.expectedS, now)}
        </p>
      )}
      {failure.refusalKind && (
        <p className="text-xs text-fg-faint">refusal class: {failure.refusalKind}</p>
      )}
    </div>
  );
}
