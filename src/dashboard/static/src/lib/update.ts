// What the Update card says, from `GET /control/update` (phone-ui P3.3).
// Pure, so the card's states are tested without rendering.

import type { NoticeState } from "../styles";
import type { UpdateCheck, UpdateSnapshot, UpdateStatus } from "../types/update";

/** Poll fast while something is happening, so each step shows as it starts. */
export const RUNNING_POLL_MS = 2000;
export const IDLE_POLL_MS = 30000;

/** The request id this tab is waiting on, kept across a reload: the daemon
 *  restarts mid-update, and a phone may reload the page while it is down. */
export const AWAITED_UPDATE_KEY = "canary.update.awaiting";

/** The request this tab filed and is waiting to hear about. */
export interface AwaitedRequest {
  id: string;
  /** The Pi's own timestamp for it, compared with the Pi's status times. */
  requestedAt: string;
}

export function parseAwaited(raw: string | null): AwaitedRequest | null {
  if (raw === null) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      parsed !== null &&
      typeof parsed === "object" &&
      typeof (parsed as AwaitedRequest).id === "string" &&
      typeof (parsed as AwaitedRequest).requestedAt === "string"
    ) {
      return parsed as AwaitedRequest;
    }
  } catch {
    /* not ours: ignore it */
  }
  return null;
}

/** The awaited request has not been reported on yet: it is still waiting
 *  for pickup, or was just taken and the status still describes an earlier
 *  run. A status that finished after the request was filed, for another
 *  request, means the runner has moved past it: stop waiting. */
export function isStarting(update: UpdateSnapshot, awaited: AwaitedRequest | null): boolean {
  if (awaited === null) return false;
  const status = update.status;
  if (status.request_id === awaited.id || status.state === "lost") return false;
  if (update.pending_request?.stuck) return false;
  if (update.pending_request !== null) return true;
  const finished = status.finished_at ? Date.parse(status.finished_at) : NaN;
  return !(finished >= Date.parse(awaited.requestedAt));
}

/** A runner is working, or a request is waiting to be picked up. */
export function isRunning(update: UpdateSnapshot): boolean {
  const state = update.status.state;
  if (state === "checking" || state === "updating") return true;
  return update.pending_request !== null && !update.pending_request.stuck;
}

/** One line for the step in progress. */
export function progressText(update: UpdateSnapshot): string {
  const status = update.status;
  if (status.state === "checking" || status.state === "updating") {
    const verb = status.state === "checking" ? "Checking for updates" : "Updating";
    return status.phase ? `${verb}: ${status.phase}` : `${verb}…`;
  }
  return "Waiting for canary-update to start…";
}

const FAILED_STATES = new Set(["failed", "refused", "lost", "unknown"]);

/** The finished run's outcome, worded by the runner ("Updated to <sha>.",
 *  "Rolled back: <reason>. …"). A finished *check* is shown by the check
 *  block instead, unless it failed. */
export function outcomeNotice(status: UpdateStatus): NoticeState | null {
  if (status.state === "lost" || status.state === "unknown") {
    return { kind: "error", text: status.message ?? status.error ?? "Update status unknown." };
  }
  if (status.state === "done") {
    if (status.action !== "update") return null;
    return { kind: "ok", text: status.message ?? "Update finished." };
  }
  if (FAILED_STATES.has(status.state)) {
    return { kind: "error", text: status.message ?? `Update ${status.state}.` };
  }
  return null;
}

/** "Up to date" / "N commits available" for a successful check. */
export function checkSummary(check: UpdateCheck): string {
  if (!check.ok) return check.message ?? "The check failed.";
  const behind = check.behind ?? 0;
  if (behind === 0) return `Up to date (${check.current ?? "?"}).`;
  const plural = behind === 1 ? "" : "s";
  return `${behind} commit${plural} available: ${check.current ?? "?"} → ${check.upstream ?? "?"}.`;
}

/** Why the Update button cannot be pressed now, or null when it can. */
export function updateBlockedReason(update: UpdateSnapshot): string | null {
  if (!update.trigger_installed) {
    return "canary-update.path is not installed on this Pi. Run 'sudo canary-update' once from a shell.";
  }
  if (isRunning(update)) return "An update or check is already running.";
  if (update.internet.default_route === false) {
    return "No internet: the Pi has no default route. Join a WiFi network with internet access.";
  }
  const check = update.last_check;
  if (check && !check.ok && check.reason === "no_internet") {
    return "No internet at the last check. Check again once the Pi is online.";
  }
  if (update.blocked_by.length > 0) return update.blocked_by.join(" ");
  return null;
}

/** Whether there is something to update to: a successful check found
 *  commits. A check comes first, so the button says what it will install. */
export function hasUpdate(update: UpdateSnapshot): boolean {
  const check = update.last_check;
  return Boolean(check?.ok && (check.behind ?? 0) > 0);
}
