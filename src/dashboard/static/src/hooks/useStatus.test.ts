import { act, render, renderHook } from "@testing-library/react";
import { createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockHttp } from "../test/harness";
import type { CompactStatusResponse, StatusResponse } from "../types";

const mocks = mockHttp(["getStatus", "getStatusCompact", "getBehaviorModels", "getBehaviorLive"]);

const { useStatus } = await import("./useStatus");
const { useLiveFullStatus } = await import("./statusDemand");
const { TelemetryPanel } = await import("../panels/TelemetryPanel");
const { overlayCompact } = await import("../lib/statusView");
const { useBehaviorModels } = await import("./useBehaviorModels");
const { useBehaviorLive } = await import("./useBehaviorLive");

const FULL = {
  storage_path: "/data",
  free_mb: 500,
  latest_session: "2026-11-12T120000",
  vehicle: "2008 Nissan Xterra",
  model_versions: {},
  health: null,
  capture_profile: { active: false, name: "default", started_at: null },
  vehicle_state: { state: "engine_off", replay_id: null },
} as unknown as StatusResponse;

function compact({ active = false, free = 500 } = {}): CompactStatusResponse {
  return {
    view: "compact",
    served_at: "2026-11-12T12:00:00Z",
    free_mb: free,
    vehicle_state: { state: "engine_off", replay_id: null },
    capture_profile: { name: active ? "tuning" : "default", active },
    passive: null,
    emitter_counters: null,
    health: null,
    detectors: null,
  } as unknown as CompactStatusResponse;
}

async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("status / behavior poll hooks", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("useStatus loads, then re-polls the compact view after each completion", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact.mockResolvedValue(compact());
    const { result } = renderHook(() => useStatus(5000));
    expect(result.current.loading).toBe(true);
    await flush();
    expect(result.current.status).toEqual(overlayCompact(FULL, compact()));
    expect(result.current.loading).toBe(false);
    expect(mocks.getStatusCompact).toHaveBeenCalledTimes(1);
    await flush(5000);
    expect(mocks.getStatusCompact).toHaveBeenCalledTimes(2);
    // The full payload is not asked for again at the compact cadence.
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
  });

  it("useStatus is null until the first full payload lands", async () => {
    mocks.getStatusCompact.mockResolvedValue(compact());
    mocks.getStatus.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useStatus(5000));
    await flush();
    expect(mocks.getStatusCompact).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBeNull();
    expect(result.current.loading).toBe(true);
  });

  it("useStatus takes the full payload every 30 s, 5 s while a panel shows it", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact.mockResolvedValue(compact());
    renderHook(() => useStatus(5000));
    await flush();
    await flush(25_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);

    // A panel that shows full-only fields mounts just after a fetch: nothing is
    // fetched for it now (the payload is a moment old), the next one is 5 s
    // after the last, and then every 5 s.
    const panel = renderHook(() => useLiveFullStatus());
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
    await flush(4000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
    await flush(1000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(3);
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(4);

    // It unmounts: nothing is fetched for that, and the slow cadence is back,
    // counted from the last fetch.
    panel.unmount();
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(4);
    await flush(29_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(4);
    await flush(1000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(5);
  });

  it("a panel mounting beside a payload older than its cadence fetches at once", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact.mockResolvedValue(compact());
    renderHook(() => useStatus(5000));
    await flush();
    await flush(20_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
    // 20 s old against a 5 s cadence: due now.
    renderHook(() => useLiveFullStatus());
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
    await flush(4000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
    await flush(1000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(3);
  });

  it("TelemetryPanel alone holds the full payload at 15 s, and a 5 s panel beside it at 5 s", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact.mockResolvedValue(compact());
    renderHook(() => useStatus(5000));
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);

    // The Drive screen's panel mounts: the next full fetch is 15 s after the
    // last, and then every 15 s, not 5.
    const telemetry = render(createElement(TelemetryPanel, { channels: new Map(), bindings: {} }));
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
    await flush(14_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
    await flush(1000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
    await flush(15_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(3);

    // A panel that wants 5 s mounts beside it: the shortest cadence wins.
    const fast = renderHook(() => useLiveFullStatus());
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(3);
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(4);
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(5);

    // It leaves; the telemetry panel's 15 s is back.
    fast.unmount();
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(5);
    await flush(14_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(5);
    await flush(1000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(6);

    // And with the telemetry panel gone too, the idle 30 s.
    telemetry.unmount();
    await flush();
    expect(mocks.getStatus).toHaveBeenCalledTimes(6);
    await flush(29_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(6);
    await flush(1000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(7);
  });

  it("a panel asking for a cadence slower than the idle one never slows the poll past it", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact.mockResolvedValue(compact());
    renderHook(() => useStatus(5000));
    renderHook(() => useLiveFullStatus(120_000));
    await flush();
    const first = mocks.getStatus.mock.calls.length;
    await flush(29_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(first);
    await flush(1000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(first + 1);
  });

  it("a panel asking for no delay (or none that is a number) polls at most once a second", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact.mockResolvedValue(compact());
    renderHook(() => useStatus(5000));
    renderHook(() => useLiveFullStatus(0));
    await flush();
    await flush(10_000);
    // One at mount, then one a second, never back to back.
    expect(mocks.getStatus.mock.calls.length).toBeGreaterThanOrEqual(10);
    expect(mocks.getStatus.mock.calls.length).toBeLessThanOrEqual(12);
    const callsBefore = mocks.getStatus.mock.calls.length;
    renderHook(() => useLiveFullStatus(Number.NaN));
    await flush(10_000);
    // NaN is the default 5 s, which the 1 s panel beside it outbids.
    expect(mocks.getStatus.mock.calls.length - callsBefore).toBeLessThanOrEqual(11);
  });

  /** A `/status` that takes `ms` to answer and counts how it was asked. */
  function slowFull(ms: number) {
    const stats = { calls: 0, inFlight: 0, maxInFlight: 0, aborts: 0 };
    mocks.getStatus.mockImplementation(
      (signal: AbortSignal) =>
        new Promise((resolve, reject) => {
          stats.calls += 1;
          stats.inFlight += 1;
          stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight);
          const answer = setTimeout(() => {
            stats.inFlight -= 1;
            resolve(FULL);
          }, ms);
          signal.addEventListener("abort", () => {
            clearTimeout(answer);
            stats.inFlight -= 1;
            stats.aborts += 1;
            reject(new DOMException("aborted", "AbortError"));
          });
        }),
    );
    return stats;
  }

  it("a first load on a live page makes one full request, not an aborted one and a second", async () => {
    mocks.getStatusCompact.mockResolvedValue(compact());
    const stats = slowFull(2000);
    renderHook(() => {
      useLiveFullStatus();
      return useStatus(5000);
    });
    await flush(100);
    await flush(3000);
    expect(stats).toMatchObject({ calls: 1, aborts: 0, maxInFlight: 1 });
  });

  it("a panel flapping while a full request is in flight neither aborts it nor starts another", async () => {
    mocks.getStatusCompact.mockResolvedValue(compact());
    const stats = slowFull(2000);
    renderHook(() => useStatus(5000));
    await flush(100);
    // Mounted and unmounted twenty times, 300 ms apart, over the first request.
    for (let i = 0; i < 20; i += 1) {
      const panel = renderHook(() => useLiveFullStatus(i % 2 === 0 ? 5000 : 15_000));
      await flush(50);
      panel.unmount();
      await flush(50);
    }
    expect(stats).toMatchObject({ calls: 1, aborts: 0, maxInFlight: 1 });
    await flush(1000);
    expect(stats.calls).toBe(1);
  });

  it("a flap storm after the payload landed never has two requests in flight, or aborts one", async () => {
    mocks.getStatusCompact.mockResolvedValue(compact());
    const stats = slowFull(2000);
    renderHook(() => useStatus(5000));
    await flush(2500);
    for (let i = 0; i < 20; i += 1) {
      const panel = renderHook(() => useLiveFullStatus());
      await flush(300);
      panel.unmount();
      await flush(300);
    }
    await flush(5000);
    expect(stats.aborts).toBe(0);
    expect(stats.maxInFlight).toBe(1);
    // The old effect restart: 1 + 20 mounts + 20 unmounts, each aborting the last.
    expect(stats.calls).toBeLessThanOrEqual(4);
  });

  it("useStatus fetches the full block at once when the daemon's state changes", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact
      .mockResolvedValueOnce(compact({ active: false }))
      .mockResolvedValueOnce(compact({ active: false }))
      .mockResolvedValue(compact({ active: true }));
    renderHook(() => useStatus(5000));
    await flush();
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1); // nothing changed
    await flush(5000); // the capture started: this poll sees it
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
  });

  it("useStatus shows the compact poll's fields over the last full payload", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact
      .mockResolvedValueOnce(compact({ free: 100 }))
      .mockResolvedValue(compact({ free: 90 }));
    const { result } = renderHook(() => useStatus(5000));
    await flush();
    expect(result.current.status?.free_mb).toBe(100);
    await flush(5000);
    expect(result.current.status?.free_mb).toBe(90);
    expect(result.current.status?.vehicle).toBe("2008 Nissan Xterra"); // full-only, kept
  });

  it("useStatus reports an Error and keeps the last snapshot", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact
      .mockResolvedValueOnce(compact({ free: 100 }))
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(compact({ free: 80 }));
    const { result } = renderHook(() => useStatus(1000));
    await flush();
    await flush(1000);
    expect(result.current.error?.message).toBe("boom");
    expect(result.current.status?.free_mb).toBe(100);
    await flush(1000);
    expect(result.current.error).toBeNull();
    expect(result.current.status?.free_mb).toBe(80);
  });

  it("useStatus reports a failed full payload too", async () => {
    mocks.getStatusCompact.mockResolvedValue(compact());
    mocks.getStatus.mockRejectedValue(new Error("full down"));
    const { result } = renderHook(() => useStatus(1000));
    await flush();
    expect(result.current.error?.message).toBe("full down");
    expect(result.current.status).toBeNull();
    expect(result.current.loading).toBe(false);
  });

  it("useStatus retries a failed first full fetch within one compact interval", async () => {
    mocks.getStatusCompact.mockResolvedValue(compact());
    mocks.getStatus.mockRejectedValueOnce(new Error("503")).mockResolvedValue(FULL);
    const { result } = renderHook(() => useStatus(5000));
    await flush();
    expect(result.current.status).toBeNull();
    expect(result.current.error?.message).toBe("503");
    // Not 30 s later: the next compact interval.
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
    expect(result.current.status).not.toBeNull();
    expect(result.current.error).toBeNull();
  });

  it("useStatus keeps retrying a full payload that keeps failing, every compact interval", async () => {
    mocks.getStatusCompact.mockResolvedValue(compact());
    mocks.getStatus.mockRejectedValue(new Error("503"));
    renderHook(() => useStatus(5000));
    await flush();
    await flush(5000);
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(3);
  });

  it("useStatus never overlaps a slow /status", async () => {
    mocks.getStatusCompact.mockReturnValue(new Promise(() => {}));
    mocks.getStatus.mockReturnValue(new Promise(() => {}));
    renderHook(() => useStatus(1000));
    await flush(30_000);
    expect(mocks.getStatusCompact).toHaveBeenCalledTimes(1);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
  });

  it("useBehaviorModels exposes data and error", async () => {
    mocks.getBehaviorModels.mockRejectedValueOnce(new Error("down"));
    mocks.getBehaviorModels.mockResolvedValue({ models: [] });
    const { result } = renderHook(() => useBehaviorModels(1000));
    await flush();
    expect(result.current.error?.message).toBe("down");
    expect(result.current.data).toBeNull();
    await flush(1000);
    expect(result.current.data).toEqual({ models: [] });
    expect(result.current.error).toBeNull();
  });

  it("useBehaviorLive swallows errors and keeps the last good data", async () => {
    mocks.getBehaviorLive
      .mockResolvedValueOnce({ tick: 1 })
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValue({ tick: 2 });
    const { result } = renderHook(() => useBehaviorLive(500));
    await flush();
    expect(result.current).toEqual({ tick: 1 });
    await flush(500);
    expect(result.current).toEqual({ tick: 1 });
    await flush(500);
    expect(result.current).toEqual({ tick: 2 });
  });

  it("stops polling on unmount", async () => {
    mocks.getStatus.mockResolvedValue(FULL);
    mocks.getStatusCompact.mockResolvedValue(compact());
    const { unmount } = renderHook(() => useStatus(1000));
    await flush();
    unmount();
    await flush(60_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
    expect(mocks.getStatusCompact).toHaveBeenCalledTimes(1);
  });
});
