import { useEffect, useState } from "react";

import { approvePairingRequest, listPairingRequests } from "../api/pairedDevices";
import type { PendingPairingRequest } from "../api/pairedDevices";
import { describeError } from "../hooks/useControlResource";
import { useControlStream } from "../hooks/useControlStream";
import { isPairingEvent } from "../lib/pairedDevices";
import { buttonClass, cn, noticeClass, type NoticeState } from "../styles";

/** How long an approval's outcome stays on screen. */
const OUTCOME_MS = 6000;

const cardClass =
  "pointer-events-auto w-full max-w-md rounded-card border border-accent/55 bg-elev p-3 shadow-panel";

/** "Android phone wants to pair — Approve / Ignore", on every page of a
 *  device that already holds a token (docs/roadmap/phone-ui.md P2.4).
 *
 *  The daemon's waiting list is the truth: each `pairing.*` control event only
 *  says "re-read it", so a request approved by a key cycle, by another phone,
 *  or expired disappears here too. Ignore hides it on this device only. */
export function PairingApprovalPrompt({ token }: { token: string }) {
  const { events } = useControlStream(token);
  // Newest first: a new pairing event is a new object, which re-runs the read.
  const latestPairingEvent = events.find(isPairingEvent);
  const [waiting, setWaiting] = useState<PendingPairingRequest[]>([]);
  const [reread, setReread] = useState(0);
  const [ignored, setIgnored] = useState<ReadonlySet<string>>(() => new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<NoticeState | null>(null);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    let expiry: number | undefined;
    listPairingRequests(token, controller.signal)
      .then(({ requests }) => {
        setWaiting(requests);
        if (requests.length > 0) {
          // Re-read when the soonest expires, so a stale prompt cannot linger.
          const soonest = Math.min(...requests.map((r) => r.expires_in_s));
          expiry = window.setTimeout(() => setReread((n) => n + 1), (soonest + 1) * 1000);
        }
      })
      .catch(() => {
        // Not authorized or the Pi is away: show nothing rather than an error
        // on every page. The Paired devices panel reports it properly.
        if (!controller.signal.aborted) setWaiting([]);
      });
    return () => {
      controller.abort();
      window.clearTimeout(expiry);
    };
  }, [token, latestPairingEvent, reread]);

  useEffect(() => {
    if (outcome === null) return;
    const timer = window.setTimeout(() => setOutcome(null), OUTCOME_MS);
    return () => window.clearTimeout(timer);
  }, [outcome]);

  const approve = async (request: PendingPairingRequest) => {
    setBusy(request.id);
    try {
      await approvePairingRequest(token, request.id);
      setOutcome({ kind: "ok", text: `Approved ${request.name}. It finishes pairing on its own.` });
    } catch (error: unknown) {
      setOutcome({ kind: "error", text: describeError(error) });
    } finally {
      setBusy(null);
      setReread((n) => n + 1);
    }
  };

  const visible = token ? waiting.filter((r) => !ignored.has(r.id)) : [];
  if (visible.length === 0 && outcome === null) return null;

  return (
    <div
      className="pointer-events-none fixed inset-x-0 top-2 z-50 flex flex-col items-center gap-2 px-2"
      aria-live="polite"
    >
      {visible.map((request) => (
        <div
          key={request.id}
          role="alertdialog"
          aria-label={`${request.name} wants to pair`}
          className={cardClass}
        >
          <p className="m-0 break-words font-semibold text-fg">{request.name} wants to pair</p>
          <p className="m-0 mt-1 text-[0.8rem] text-fg-dim">
            Approve only if it is yours: it gets its own control token for this Pi.
          </p>
          <div className="mt-2 flex flex-wrap justify-end gap-2">
            <button
              type="button"
              className={cn(buttonClass, "text-xs")}
              disabled={busy !== null}
              onClick={() => setIgnored((prev) => new Set(prev).add(request.id))}
            >
              Ignore
            </button>
            <button
              type="button"
              className={cn(buttonClass, "border-accent text-xs text-accent")}
              disabled={busy !== null}
              onClick={() => approve(request)}
            >
              {busy === request.id ? "Approving…" : "Approve"}
            </button>
          </div>
        </div>
      ))}
      {outcome !== null && (
        <div className={cn(noticeClass(outcome.kind), "pointer-events-auto w-full max-w-md")}>
          {outcome.text}
        </div>
      )}
    </div>
  );
}
