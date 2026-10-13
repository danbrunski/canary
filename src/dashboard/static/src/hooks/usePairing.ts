import { useCallback, useEffect, useRef, useState } from "react";

import {
  cancelPairingRequest,
  createPairingRequest,
  getPairingRequest,
  type PairingRequestCreated,
  type PairingRequestStatus,
} from "../api/http";
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

  useEffect(() => {
    if (request === null) return;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const status = await getPairingRequest(request, controller.signal);
        if (status.status === "approved" && status.token) {
          finish({ phase: "idle" });
          onPairedRef.current(status.token);
        } else if (status.status === "waiting") {
          setView({ phase: "waiting", status });
        } else {
          finish({ phase: "failed", message: status.detail || "Pairing did not finish." });
        }
      } catch (error: unknown) {
        if (controller.signal.aborted) return;
        finish({ phase: "failed", message: failureMessage(error) });
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [request, finish]);

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
