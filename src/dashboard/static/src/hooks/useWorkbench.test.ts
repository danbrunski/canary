import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlJson } from "../api/core";
import { LOGS, deferred, logsResponse } from "../test/workbenchFixtures";
import type { LogsResponse } from "../types/workbench";
import { useAnalysisRun, useLogs } from "./useWorkbench";

vi.mock("../api/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/core")>();
  return { ...actual, controlJson: vi.fn() };
});
const control = vi.mocked(controlJson);

afterEach(() => vi.clearAllMocks());

describe("useAnalysisRun", () => {
  it("starts idle, is busy while a run waits, and keeps the last result while it reruns", async () => {
    const { result } = renderHook(() => useAnalysisRun<string>());
    expect(result.current).toMatchObject({ result: null, refusal: null, busy: false });

    await act(() => result.current.run(() => Promise.resolve("first")));
    expect(result.current).toMatchObject({ result: "first", refusal: null, busy: false });

    const second = deferred<string>();
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.run(() => second.promise);
    });
    expect(result.current).toMatchObject({ result: "first", busy: true });
    second.resolve("second");
    await act(() => pending);
    expect(result.current).toMatchObject({ result: "second", busy: false });
  });

  it("clears the result on a refusal, in the refusal's own kind, and a success clears the refusal", async () => {
    const { result } = renderHook(() => useAnalysisRun<string>());
    await act(() => result.current.run(() => Promise.resolve("ok")));
    await act(() =>
      result.current.run(() =>
        Promise.reject(
          new HttpError(
            409,
            JSON.stringify({
              detail: { error: "analysis_deferred", profile: "tuning", message: "m" },
            }),
          ),
        ),
      ),
    );
    expect(result.current.result).toBeNull();
    expect(result.current.refusal).toMatchObject({ kind: "deferred", profile: "tuning" });
    expect(result.current.busy).toBe(false);

    let pending!: Promise<void>;
    const next = deferred<string>();
    act(() => {
      pending = result.current.run(() => next.promise);
    });
    // The refusal is gone as soon as the next run starts.
    expect(result.current.refusal).toBeNull();
    next.resolve("again");
    await act(() => pending);
    expect(result.current).toMatchObject({ result: "again", refusal: null });
  });

  it("aborts a run in flight when another starts, and drops its late reply", async () => {
    const { result } = renderHook(() => useAnalysisRun<string>());
    const first = deferred<string>();
    const signals: AbortSignal[] = [];
    let firstRun!: Promise<void>;
    let secondRun!: Promise<void>;
    act(() => {
      firstRun = result.current.run((signal) => {
        signals.push(signal);
        return first.promise;
      });
    });
    act(() => {
      secondRun = result.current.run((signal) => {
        signals.push(signal);
        return Promise.resolve("newer");
      });
    });
    await act(() => secondRun);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    first.resolve("older");
    await act(() => firstRun);
    expect(result.current).toMatchObject({ result: "newer", busy: false });
  });

  it("does not report a refusal from a run that was superseded", async () => {
    const { result } = renderHook(() => useAnalysisRun<string>());
    const first = deferred<string>();
    let firstRun!: Promise<void>;
    act(() => {
      firstRun = result.current.run(() => first.promise);
    });
    await act(() => result.current.run(() => Promise.resolve("newer")));
    first.reject(new HttpError(500, "boom"));
    await act(() => firstRun);
    expect(result.current).toMatchObject({ result: "newer", refusal: null });
  });

  it("aborts the run in flight when the view unmounts", () => {
    const { result, unmount } = renderHook(() => useAnalysisRun<string>());
    let signal!: AbortSignal;
    act(() => {
      void result.current.run((s) => {
        signal = s;
        return new Promise<string>(() => {});
      });
    });
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });
});

describe("useLogs", () => {
  it("asks for nothing without a token", async () => {
    const { result } = renderHook(() => useLogs("", false));
    await new Promise((r) => setTimeout(r, 20));
    expect(control).not.toHaveBeenCalled();
    expect(result.current.data).toBeNull();
  });

  it("lists once per (token, include sessions), and a refusal reads as a Refusal", async () => {
    control.mockRejectedValueOnce(new HttpError(500, "boom"));
    control.mockResolvedValueOnce(logsResponse({ include_sessions: true }));
    const { result, rerender } = renderHook(({ include }) => useLogs("t", include), {
      initialProps: { include: false },
    });
    await waitFor(() => expect(result.current.error).toMatchObject({ kind: "error" }));
    expect(control).toHaveBeenCalledWith("/control/logs?include_sessions=false", "t", {
      signal: expect.any(AbortSignal),
    });
    rerender({ include: true });
    await waitFor(() => expect(result.current.data?.include_sessions).toBe(true));
    expect(result.current.error).toBeNull();
    expect(control).toHaveBeenCalledTimes(2);
  });

  it("drops the previous listing's late reply after the toggle", async () => {
    const stale = deferred<LogsResponse>();
    control.mockImplementation((path: string) =>
      path.endsWith("=false")
        ? stale.promise
        : Promise.resolve(logsResponse({ include_sessions: true })),
    );
    const { result, rerender } = renderHook(({ include }) => useLogs("t", include), {
      initialProps: { include: false },
    });
    rerender({ include: true });
    await waitFor(() => expect(result.current.data?.include_sessions).toBe(true));
    stale.resolve(LOGS);
    await new Promise((r) => setTimeout(r, 20));
    expect(result.current.data?.include_sessions).toBe(true);
  });
});
