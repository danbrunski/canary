// Request state for the Engine Tuning pages (R08.2). Those pages hand-rolled
// fetch/loading/error state eight times; two copies raced (SessionChooser
// kept a stale refusal and had no AbortController) and one shared a refusal
// between two views (TimingView). These hooks put them on the shared
// vocabulary: `useControlResource` for reads, `describeRefusal` for errors.

import { useCallback, useEffect, useRef, useState } from "react";

import { getLogs } from "../api/workbench";
import { describeRefusal } from "../lib/workbenchApi";
import type { LogsResponse, Refusal } from "../types/workbench";
import { useControlResource, type ResourceState } from "./useControlResource";

/** `GET /control/logs`, fetched once per (token, includeSessions). Toggling
 *  aborts the previous listing, so a late reply can never overwrite the new
 *  one, and a success clears the last refusal. */
export function useLogs(
  token: string,
  includeSessions: boolean,
): ResourceState<LogsResponse, Refusal> {
  const fetcher = useCallback(
    (signal: AbortSignal) => getLogs(token, includeSessions, signal),
    [token, includeSessions],
  );
  return useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: null,
    describe: describeRefusal,
  });
}

export interface AnalysisRun<T> {
  result: T | null;
  refusal: Refusal | null;
  busy: boolean;
  /** Start `load`; a run already in flight is aborted and its reply dropped. */
  run: (load: (signal: AbortSignal) => Promise<T>) => Promise<void>;
}

interface RunState<T> {
  result: T | null;
  refusal: Refusal | null;
  busy: boolean;
}

/** One on-demand analysis: result, structured refusal, busy flag. Each view
 *  (and each Timing mode) owns its own, so a refusal is shown only where it
 *  happened. The previous result stays up while a rerun is busy; a refusal
 *  clears it. */
export function useAnalysisRun<T>(): AnalysisRun<T> {
  const [state, setState] = useState<RunState<T>>({ result: null, refusal: null, busy: false });
  const inflight = useRef<AbortController | null>(null);

  useEffect(() => () => inflight.current?.abort(), []);

  const run = useCallback(async (load: (signal: AbortSignal) => Promise<T>) => {
    inflight.current?.abort();
    const controller = new AbortController();
    inflight.current = controller;
    setState((prev) => ({ ...prev, refusal: null, busy: true }));
    try {
      const result = await load(controller.signal);
      if (controller.signal.aborted) return;
      setState({ result, refusal: null, busy: false });
    } catch (error) {
      if (controller.signal.aborted) return;
      setState({ result: null, refusal: describeRefusal(error), busy: false });
    }
  }, []);

  return { ...state, run };
}
