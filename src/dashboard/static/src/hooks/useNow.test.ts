import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useNow } from "./useNow";

describe("useNow (R28.3)", () => {
  afterEach(() => vi.useRealTimers());

  it("ticks every interval, default 1 s", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const { result } = renderHook(() => useNow());
    expect(result.current).toBe(1_000_000);
    act(() => {
      vi.advanceTimersByTime(999);
    });
    expect(result.current).toBe(1_000_000);
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe(1_001_000);
    act(() => {
      vi.advanceTimersByTime(2000);
    });
    expect(result.current).toBe(1_003_000);
  });

  it("takes the interval as given", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const { result } = renderHook(() => useNow(250));
    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(result.current).toBe(250);
  });

  it("runs no timer while disabled, and starts one when enabled", () => {
    vi.useFakeTimers();
    vi.setSystemTime(0);
    const { result, rerender } = renderHook(({ on }) => useNow(1000, on), {
      initialProps: { on: false },
    });
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    rerender({ on: true });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current).toBe(6000);
  });

  it("stops its timer on unmount", () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() => useNow());
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
