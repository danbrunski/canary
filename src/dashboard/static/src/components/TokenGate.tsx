import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import { getPreflight } from "../api/http";
import { useControlResource } from "../hooks/useControlResource";
import { cn } from "../styles";
import { useTokenEntry } from "./TokenEntry";
import { TokenField } from "./TokenField";
import { TokenHint } from "./TokenHint";

interface TokenGateProps {
  token: string;
  onTokenChange: (token: string) => void;
}

type TokenGateState = "not needed" | "required" | "rejected" | "unlocked";

const DOT_CLASS: Record<TokenGateState, string> = {
  "not needed": "bg-fg-faint",
  required: "bg-warn shadow-[0_0_0_3px_rgba(255,209,102,0.18)]",
  rejected: "bg-bad shadow-[0_0_0_3px_rgba(255,111,111,0.2)]",
  unlocked: "bg-ok shadow-[0_0_0_3px_rgba(95,227,179,0.18)]",
};

/** What the closed pill says. A locked pill names itself ("control token")
 *  rather than only a state word, so a first-time user can find where the
 *  token goes. */
const LABEL: Record<TokenGateState, string> = {
  "not needed": "not needed",
  required: "🔒 control token",
  rejected: "🔒 token rejected",
  unlocked: "unlocked",
};

const HELP: Record<TokenGateState, ReactNode> = {
  "not needed":
    "This server allows control from loopback (dev/sim) — no token needed here. You'd only paste one pointed at the Pi.",
  required: (
    <>
      Paste the control token here. <TokenHint />
    </>
  ),
  rejected:
    "The server rejected this token (401/403) — wrong or stale value. Re-enter it or Clear and try again.",
  unlocked:
    "Stored on this device; unlocks every gated panel (Sessions, Control Plane, Training, ...).",
};

/** Single shell-level control-token entry (Task 5.1): lock icon → popover
 *  entry → unlocked, replacing the `TokenField` ControlPanel and
 *  TestSessionPanel each used to render on their own. Adopts
 *  TestSessionPanel's four-state vocabulary ("not needed" / "required" /
 *  "rejected" / "unlocked") as the one place that logic lives now. The
 *  `/control/preflight` probe only runs once the popover is open — a
 *  closed pill costs nothing and reads as a plain locked/unlocked toggle
 *  until the operator asks for more. */
export function TokenGate({ token, onTokenChange }: TokenGateProps) {
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const { pending, consume } = useTokenEntry();
  // A panel's Unlock button (anything calling `requestTokenEntry`) opens the
  // popover. Adjusted during render rather than in an effect so the field
  // is mounted by the time the effect below focuses it.
  const [seenPending, setSeenPending] = useState(false);
  if (pending !== seenPending) {
    setSeenPending(pending);
    if (pending) setOpen(true);
  }
  useEffect(() => {
    if (!pending) return;
    // Deferred a microtask: inside the phone menu, the Sheet's own
    // focus-the-dialog effect runs after this one and would take it back.
    queueMicrotask(() => inputRef.current?.focus());
    consume();
  }, [pending, consume]);
  const fetcher = useCallback((signal: AbortSignal) => getPreflight(token, signal), [token]);
  const probe = useControlResource(fetcher, { enabled: open, intervalMs: 10000 });
  const authRejected = probe.status === 401 || probe.status === 403;
  const openNoToken = !token && !authRejected && probe.data !== null;

  const state: TokenGateState = token
    ? authRejected
      ? "rejected"
      : "unlocked"
    : authRejected
      ? "required"
      : openNoToken
        ? "not needed"
        : "required";

  return (
    <div className="relative">
      <button
        type="button"
        className="inline-flex min-h-6 items-center gap-1.5 rounded-full border border-border-soft bg-black/25 px-2.5 py-1 text-[0.66rem] font-semibold uppercase tracking-[0.04em] text-fg-dim"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="control token"
      >
        <span
          className={cn(
            "h-2 w-2 rounded-full transition-[background,box-shadow]",
            DOT_CLASS[state],
          )}
        />
        {LABEL[state]}
      </button>
      {open && (
        <div className="absolute right-0 top-full z-10 mt-1 w-72 rounded-[8px] border border-border-soft bg-elev p-3 text-[0.78rem] shadow-lg">
          <p className="m-0 mb-2 text-fg-dim">{HELP[state]}</p>
          <TokenField
            value={token}
            onChange={onTokenChange}
            className="w-full"
            inputRef={inputRef}
          />
        </div>
      )}
    </div>
  );
}
