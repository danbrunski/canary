import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useFirstFailure } from "./useFirstFailure";
import { usePolling } from "./usePolling";

describe("useFirstFailure (R28.3)", () => {
  afterEach(() => vi.useRealTimers());

  it("reports the first failed read and keeps later failures quiet", async () => {
    vi.useFakeTimers();
    const read = vi.fn(() => Promise.reject(new Error("down")));
    const report = vi.fn();
    renderHook(() => usePolling(useFirstFailure(read, report), { intervalMs: 5000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(report).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(read).toHaveBeenCalledTimes(4);
    expect(report).toHaveBeenCalledTimes(1);
  });

  it("stays quiet when the first read succeeds and a later one fails", async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockResolvedValueOnce(1).mockRejectedValue(new Error("down"));
    const report = vi.fn();
    renderHook(() => usePolling(useFirstFailure(read, report), { intervalMs: 5000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(read).toHaveBeenCalledTimes(3);
    expect(report).not.toHaveBeenCalled();
  });

  it("treats a new read as a new first read", async () => {
    vi.useFakeTimers();
    const report = vi.fn();
    const { rerender } = renderHook(
      ({ read }) => usePolling(useFirstFailure(read, report), { intervalMs: 5000 }),
      { initialProps: { read: () => Promise.resolve(1) } },
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    rerender({ read: () => Promise.reject(new Error("down")) });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(report).toHaveBeenCalledTimes(1);
  });

  it("does not report a read that was aborted", async () => {
    const read = vi.fn((signal: AbortSignal) => {
      return new Promise<number>((_, reject) => {
        signal.addEventListener("abort", () => reject(new Error("aborted")));
      });
    });
    const report = vi.fn();
    const { unmount } = renderHook(() =>
      usePolling(useFirstFailure(read, report), { intervalMs: 5000 }),
    );
    unmount();
    await Promise.resolve();
    expect(report).not.toHaveBeenCalled();
  });
});
