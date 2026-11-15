import { useCallback, useState } from "react";

import type { NoticeState } from "../styles";
import { requireFresh } from "./followUpRead";
import { describeError } from "./useControlResource";
import type { RefreshOutcome } from "./usePolling";

/** The narrowed `run(label, action)` shape a panel builds around its own
 *  `useAsyncAction` (fixed precheck + refresh) and hands down to its
 *  extracted cards — see ControlPanel's card split (Task 3.2). */
export type RunAction = (label: string, action: () => Promise<void>) => Promise<void>;

interface RunOptions {
  /** Abort before running if this returns a message (shown as an error
   *  notice) — e.g. a "enter the control token first" gate. */
  precheck?: () => string | null;
  /** Runs inside the try after `action` succeeds (e.g. a refresh). A throw
   *  here surfaces as the error notice, matching the hand-rolled panels. A
   *  refresh passed here as is (`then: refresh`) returns its outcome, and a
   *  failed one surfaces as the notice too: the action went through, the
   *  view could not be reloaded. */
  then?: () => Promise<RefreshOutcome | void> | void;
  /** Success-notice text. Defaults to `"<label> completed."`; pass `null`
   *  to show no success notice (the caller sets its own). */
  okText?: string | null;
}

/** The notice + busy + `run(label, action)` trio every mutating panel
 *  re-implemented (ControlPanel's was the reference copy). `busy` is the
 *  active action's label (or null); `notice` is status-aware via
 *  `describeError`. */
export function useAsyncAction() {
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const run = useCallback(
    async (label: string, action: () => Promise<void>, opts: RunOptions = {}): Promise<void> => {
      const gate = opts.precheck?.();
      if (gate) {
        setNotice({ kind: "error", text: gate });
        return;
      }
      setBusy(label);
      setNotice(null);
      try {
        await action();
        requireFresh(await opts.then?.());
        if (opts.okText !== null) {
          setNotice({ kind: "ok", text: opts.okText ?? `${label} completed.` });
        }
      } catch (err) {
        setNotice({ kind: "error", text: describeError(err) });
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  return { notice, setNotice, busy, run };
}
