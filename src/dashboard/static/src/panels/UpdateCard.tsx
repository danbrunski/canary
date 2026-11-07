import { useCallback, useEffect, useState } from "react";

import { NetworkError } from "../api/http";
import { getUpdate, requestUpdate } from "../api/update";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { describeError } from "../hooks/useControlResource";
import { usePolling } from "../hooks/usePolling";
import {
  AWAITED_UPDATE_KEY,
  type AwaitedRequest,
  IDLE_POLL_MS,
  RUNNING_POLL_MS,
  checkSummary,
  hasUpdate,
  isRunning,
  isStarting,
  outcomeNotice,
  parseAwaited,
  progressText,
  updateBlockedReason,
} from "../lib/update";
import {
  buttonClass,
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  noticeClass,
  placeholderClass,
} from "../styles";
import type { UpdateAction, UpdateCheck, UpdateSnapshot } from "../types/update";
import { TokenRequired } from "../components/TokenRequired";

interface UpdateCardProps {
  token: string;
}

function readAwaited(): AwaitedRequest | null {
  try {
    return parseAwaited(window.sessionStorage.getItem(AWAITED_UPDATE_KEY));
  } catch {
    return null;
  }
}

function writeAwaited(awaited: AwaitedRequest | null): void {
  try {
    if (awaited === null) window.sessionStorage.removeItem(AWAITED_UPDATE_KEY);
    else window.sessionStorage.setItem(AWAITED_UPDATE_KEY, JSON.stringify(awaited));
  } catch {
    /* storage unavailable: the server status still says what happened */
  }
}

function Phases({ update }: { update: UpdateSnapshot }) {
  const phases = update.status.phases ?? [];
  if (phases.length === 0) return null;
  return (
    <ol className="m-0 mt-2 flex list-none flex-col gap-1 p-0 text-[0.86rem]">
      {phases.map((phase, index) => {
        const current = index === phases.length - 1 && isRunning(update);
        return (
          <li
            key={`${phase.name}-${phase.at}`}
            className={cn("flex gap-2", current ? "font-semibold text-fg" : "text-fg-dim")}
          >
            <span aria-hidden className="w-4 shrink-0 text-center">
              {current ? "→" : "✓"}
            </span>
            <span className="break-words">{phase.name}</span>
          </li>
        );
      })}
    </ol>
  );
}

function CheckResult({ check }: { check: UpdateCheck }) {
  const commits = check.commits ?? [];
  return (
    <div className="mt-3">
      <p className={cn("m-0 text-[0.9rem]", !check.ok && "text-bad")}>{checkSummary(check)}</p>
      {commits.length > 0 && (
        <ul className="m-0 mt-2 flex max-h-60 list-none flex-col gap-1 overflow-y-auto p-0 text-[0.84rem]">
          {commits.map((commit) => (
            <li key={commit.sha} className="flex gap-2">
              <code className="shrink-0 text-fg-faint">{commit.sha}</code>
              <span className="min-w-0 break-words">{commit.subject}</span>
            </li>
          ))}
          {check.commits_truncated && (
            <li className="text-fg-faint">…and {(check.behind ?? 0) - commits.length} more</li>
          )}
        </ul>
      )}
      {check.dirty && (
        <p className={cn(noticeClass("error"), "mt-2")}>
          The checkout has local changes ({(check.dirty_files ?? []).join(", ")}); canary-update
          will refuse until they are committed or reverted.
        </p>
      )}
      {(check.ahead ?? 0) > 0 && (
        <p className={cn(noticeClass("error"), "mt-2")}>
          The checkout has {check.ahead} commit(s) the remote does not; the fast-forward will fail.
        </p>
      )}
    </div>
  );
}

/** Software update (phone-ui P3.3): what is installed, what is available,
 *  and an Update that runs `canary-update` on the Pi.
 *
 *  The daemon serving this page restarts in the middle of an update, so a
 *  failed poll while one runs is "reconnecting", not an error. When the
 *  daemon answers again the outcome comes from the root runner's status
 *  file, which survives the restart: "Updated to <sha>." or "Rolled back:
 *  <reason>." */
export function UpdateCard({ token }: UpdateCardProps) {
  const [awaited, setAwaited] = useState<AwaitedRequest | null>(readAwaited);
  const [confirming, setConfirming] = useState(false);
  const { notice, busy, run } = useAsyncAction();
  useEffect(() => writeAwaited(awaited), [awaited]);

  const fetcher = useCallback((signal: AbortSignal) => getUpdate(token, signal), [token]);
  const [fastPoll, setFastPoll] = useState(awaited !== null);
  const { data, error, loading, refresh } = usePolling(fetcher, {
    enabled: Boolean(token),
    intervalMs: fastPoll ? RUNNING_POLL_MS : IDLE_POLL_MS,
  });

  const update = data?.update ?? null;
  const running = update !== null && isRunning(update);
  // This tab filed a request the runner has not reported on yet: the status
  // still describes an earlier run (or the request was just taken).
  const starting = update !== null && isStarting(update, awaited);
  const inProgress = running || starting;
  // Adjust state from what the last poll said (render-phase, as usePolling
  // does): poll fast while anything is in flight, stop awaiting once done.
  const wantFast = inProgress || awaited !== null;
  if (wantFast !== fastPoll) setFastPoll(wantFast);
  if (awaited !== null && update !== null && !inProgress) setAwaited(null);

  const file = (action: UpdateAction) =>
    run(
      action === "check" ? "check" : "update",
      async () => {
        const response = await requestUpdate(token, action);
        setAwaited({
          id: response.request.request_id,
          requestedAt: response.request.requested_at,
        });
        setConfirming(false);
        refresh();
      },
      { okText: null },
    );

  if (!token) {
    return (
      <Panel title="Software update">
        <TokenRequired purpose="update" />
      </Panel>
    );
  }
  if (loading && update === null) {
    return (
      <Panel title="Software update">
        <p className={placeholderClass}>loading…</p>
      </Panel>
    );
  }

  const inFlight = inProgress || awaited !== null;
  // The daemon is down mid-update (it restarts itself): not an error.
  const reconnecting = error instanceof NetworkError && inFlight;
  const blocked = update ? updateBlockedReason(update) : null;
  const outcome = update && !inFlight ? outcomeNotice(update.status) : null;
  const check = update?.last_check ?? null;

  return (
    <Panel title="Software update">
      {reconnecting ? (
        <p className={cn(noticeClass("busy"), "mb-2")} role="status">
          CANary is restarting to finish the update — reconnecting…
        </p>
      ) : (
        error !== null && <p className={cn(noticeClass("error"), "mb-2")}>{describeError(error)}</p>
      )}
      <Notice notice={notice} className="mb-2" />
      {update && (
        <>
          <dl className={definitionListClass}>
            <div className={definitionRowClass}>
              <dt className={definitionTermClass}>installed</dt>
              <dd className={cn(definitionValueClass, "break-all text-right")}>
                v{update.installed.version}
                {update.installed.sha && (
                  <span className="text-fg-dim"> · {update.installed.sha}</span>
                )}
              </dd>
            </div>
          </dl>
          {!update.trigger_installed && (
            <p className={cn(placeholderClass, "mt-2")}>
              The <code>canary-update.path</code> unit isn&apos;t installed, so the Pi cannot be
              updated from here yet. Run <code>sudo canary-update</code> once from a shell.
            </p>
          )}
          {update.pending_request?.stuck && (
            <p className={cn(noticeClass("error"), "mt-2")}>
              Nothing picked up the last request: canary-update.path is not running on the Pi.
            </p>
          )}
          {inProgress && (
            <div className="mt-3" aria-live="polite">
              <p className={cn(noticeClass("busy"), "m-0")}>
                {running ? progressText(update) : "Waiting for canary-update to start…"}
              </p>
              {!running && (
                <button
                  type="button"
                  className="mt-1 text-[0.8rem] text-fg-dim underline"
                  onClick={() => setAwaited(null)}
                >
                  Stop waiting
                </button>
              )}
              {running && <Phases update={update} />}
            </div>
          )}
          {outcome && (
            <div className="mt-3" aria-live="polite">
              <p className={cn(noticeClass(outcome.kind), "m-0")}>{outcome.text}</p>
              {update.status.action === "update" &&
                update.status.outcome === "updated" &&
                update.installed.sha === update.status.after && (
                  <button
                    type="button"
                    className={cn(buttonClass, "mt-2")}
                    onClick={() => window.location.reload()}
                  >
                    Reload to load the new dashboard
                  </button>
                )}
              {outcome.kind === "error" && (update.status.log_tail ?? []).length > 0 && (
                <details className="mt-2 text-[0.8rem]">
                  <summary className="cursor-pointer text-fg-dim">last output</summary>
                  <pre className="m-0 mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words text-fg-dim">
                    {(update.status.log_tail ?? []).join("\n")}
                  </pre>
                </details>
              )}
            </div>
          )}
          {check && !inProgress && <CheckResult check={check} />}
          {update.blocked_by.length > 0 && !inProgress && (
            <ul className="m-0 mt-2 list-none p-0 text-[0.84rem] text-warn">
              {update.blocked_by.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
          {confirming && update.last_check ? (
            <div className={cn(noticeClass("busy"), "mt-3")}>
              <p className="m-0">
                Update to {update.last_check.upstream}? CANary restarts during the update; this page
                reconnects by itself and says how it went.
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button
                  type="button"
                  className={buttonClass}
                  disabled={busy !== null || blocked !== null}
                  onClick={() => void file("update")}
                >
                  Update now
                </button>
                <button type="button" className={buttonClass} onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                className={buttonClass}
                disabled={busy !== null || inFlight || !update.trigger_installed}
                onClick={() => void file("check")}
              >
                Check for updates
              </button>
              {hasUpdate(update) && (
                <button
                  type="button"
                  className={buttonClass}
                  disabled={busy !== null || blocked !== null || inFlight}
                  onClick={() => setConfirming(true)}
                >
                  Update…
                </button>
              )}
            </div>
          )}
          {blocked && hasUpdate(update) && !inFlight && (
            <p className="m-0 mt-2 text-[0.82rem] text-fg-dim">{blocked}</p>
          )}
        </>
      )}
    </Panel>
  );
}
