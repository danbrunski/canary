import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getStatus", "getBehaviorModels", "getBehaviorLive"]);

const { useStatus } = await import("./useStatus");
const { useBehaviorModels } = await import("./useBehaviorModels");
const { useBehaviorLive } = await import("./useBehaviorLive");

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

  it("useStatus loads, then re-polls after each completion", async () => {
    mocks.getStatus.mockResolvedValue({ state: "ok" });
    const { result } = renderHook(() => useStatus(5000));
    expect(result.current.loading).toBe(true);
    await flush();
    expect(result.current.status).toEqual({ state: "ok" });
    expect(result.current.loading).toBe(false);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
    await flush(5000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(2);
  });

  it("useStatus reports an Error and keeps the last snapshot", async () => {
    mocks.getStatus
      .mockResolvedValueOnce({ state: "ok" })
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue({ state: "again" });
    const { result } = renderHook(() => useStatus(1000));
    await flush();
    await flush(1000);
    expect(result.current.error?.message).toBe("boom");
    expect(result.current.status).toEqual({ state: "ok" });
    await flush(1000);
    expect(result.current.error).toBeNull();
    expect(result.current.status).toEqual({ state: "again" });
  });

  it("useStatus never overlaps a slow /status", async () => {
    mocks.getStatus.mockReturnValue(new Promise(() => {}));
    renderHook(() => useStatus(1000));
    await flush(30_000);
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
    mocks.getStatus.mockResolvedValue({ state: "ok" });
    const { unmount } = renderHook(() => useStatus(1000));
    await flush();
    unmount();
    await flush(10_000);
    expect(mocks.getStatus).toHaveBeenCalledTimes(1);
  });
});
