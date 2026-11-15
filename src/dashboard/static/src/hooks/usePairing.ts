import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelPairingRequest,
  createPairingRequest,
  getPairingRequest,
  type PairingRequestCreated,
  type PairingRequestStatus,
} from "../api/http";
import { usePolling } from "./usePolling";
import { deviceName, failureMessage, loadActiveRequest, saveActiveRequest } from "../pairing";

const POLL_MS = 1000;

export type PairingView =
  | { phase: "idle" }
  | { phase: "waiting"; status: PairingRequestStatus | null }
  | { phase: "failed"; message: string };

/** Drive one key-cycle pairing request; `onPaired` receives the token. */
export function usePairing(onPaired: (token: string) => void) {
  const [request, setRequest] = useState<PairingRequestCreated | null>(loadActiveRequest);
  const [view, setView] = useState<PairingView>(() =>
    request === null ? { phase: "idle" } : { phase: "waiting", status: null },
  );
  const onPairedRef = useRef(onPaired);
  useEffect(() => {
    onPairedRef.current = onPaired;
  }, [onPaired]);

  const finish = useCallback((next: PairingView) => {
    saveActiveRequest(null);
    setRequest(null);
    setView(next);
  }, []);

  // The poll loop is `usePolling`'s (a slow answer never overlaps the next
  // request, and unmount or a finished request aborts the one in flight).
  // Every outcome is handled here, in the fetcher, because each one either
  // ends the request (which switches the poll off) or is the next waiting view.
  const poll = useCallback(
    async (signal: AbortSignal): Promise<null> => {
      if (request === null) return null;
      try {
        const status = await getPairingRequest(request, signal);
        if (signal.aborted) return null;
        if (status.status === "approved" && status.token) {
          finish({ phase: "idle" });
          onPairedRef.current(status.token);
        } else if (status.status === "waiting") {
          setView({ phase: "waiting", status });
        } else {
          finish({ phase: "failed", message: status.detail || "Pairing did not finish." });
        }
      } catch (error: unknown) {
        if (signal.aborted) return null;
        finish({ phase: "failed", message: failureMessage(error) });
      }
      return null;
    },
    [request, finish],
  );
  usePolling(poll, { enabled: request !== null, intervalMs: POLL_MS });

  const start = useCallback(async () => {
    setView({ phase: "waiting", status: null });
    try {
      const created = await createPairingRequest(deviceName(navigator.userAgent));
      saveActiveRequest(created);
      setRequest(created);
    } catch (error: unknown) {
      setView({ phase: "failed", message: failureMessage(error) });
    }
  }, []);

  const cancel = useCallback(() => {
    if (request !== null) void cancelPairingRequest(request).catch(() => undefined);
    finish({ phase: "idle" });
  }, [request, finish]);

  return { view, start, cancel };
}
