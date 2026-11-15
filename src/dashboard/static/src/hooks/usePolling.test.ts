import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePolling, type RefreshOutcome } from "./usePolling";

describe("usePolling delayFirst (R28.3)", () => {
  afterEach(() => vi.useRealTimers());

  it("fetches on mount by default", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => Promise.resolve(1));
    renderHook(() => usePolling(fetcher, { intervalMs: 2000 }));
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  });

  it("waits one interval before the first fetch, then chains from completion", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => Promise.resolve(1));
    const { result } = renderHook(() =>
      usePolling(fetcher, { intervalMs: 2000, delayFirst: true }),
    );
    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999);
    });
    expect(fetcher).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("fetches at once, not never, when there is no interval to wait", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => Promise.resolve(1));
    const { result } = renderHook(() =>
      usePolling(fetcher, { intervalMs: null, delayFirst: true }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe(1);
    let settled = false;
    act(() => {
      void result.current.refresh().then(() => {
        settled = true;
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(settled).toBe(true);
  });

  it("fetches nothing when unmounted before the first interval", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => Promise.resolve(1));
    const { unmount } = renderHook(() =>
      usePolling(fetcher, { intervalMs: 2000, delayFirst: true }),
    );
    unmount();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("usePolling refresh promise (R28.3)", () => {
  afterEach(() => vi.useRealTimers());

  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (err: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  it("settles when the read it started settles, not before", async () => {
    vi.useFakeTimers();
    const reads = [deferred<number>(), deferred<number>()];
    let n = 0;
    const fetcher = vi.fn(() => reads[n++].promise);
    const { result } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    await act(async () => {
      reads[0].resolve(1);
      await vi.advanceTimersByTimeAsync(0);
    });
    let settled = false;
    act(() => {
      void result.current.refresh().then(() => {
        settled = true;
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(settled).toBe(false);
    await act(async () => {
      reads[1].resolve(2);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(settled).toBe(true);
    expect(result.current.data).toBe(2);
  });

  it("resolves, without rejecting, when the read fails", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn().mockResolvedValueOnce(1).mockRejectedValueOnce(new Error("down"));
    const { result } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    let outcome = "pending";
    act(() => {
      result.current.refresh().then(
        () => {
          outcome = "resolved";
        },
        () => {
          outcome = "rejected";
        },
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(outcome).toBe("resolved");
    expect(result.current.error).toBeInstanceOf(Error);
  });

  it("resolves when a newer refresh supersedes it, and when the hook unmounts", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => new Promise<number>(() => {}));
    const { result, unmount } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    let first = false;
    let second = false;
    act(() => {
      void result.current.refresh().then(() => {
        first = true;
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    act(() => {
      void result.current.refresh().then(() => {
        second = true;
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(first).toBe(true);
    expect(second).toBe(false);
    unmount();
    await vi.advanceTimersByTimeAsync(0);
    expect(second).toBe(true);
  });

  it("resolves a refresh that is followed by an unmount in the same batch", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => new Promise<number>(() => {}));
    const { result, unmount } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    let settled = false;
    act(() => {
      void result.current.refresh().then(() => {
        settled = true;
      });
      unmount();
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(settled).toBe(true);
  });

  it("resolves at once while the hook is disabled", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => Promise.resolve(1));
    const { result } = renderHook(() => usePolling(fetcher, { enabled: false, intervalMs: null }));
    let done = false;
    act(() => {
      void result.current.refresh().then(() => {
        done = true;
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(done).toBe(true);
    expect(fetcher).not.toHaveBeenCalled();
  });
});

describe("usePolling refresh outcome (R28.3, operator 2026-11-15: a failed follow-up is loud)", () => {
  afterEach(() => vi.useRealTimers());

  /** Call `refresh()` and keep what it resolved to. */
  function track(refresh: () => Promise<RefreshOutcome>): { outcome: RefreshOutcome | null } {
    const seen: { outcome: RefreshOutcome | null } = { outcome: null };
    void refresh().then((o) => {
      seen.outcome = o;
    });
    return seen;
  }

  it("is ok when the read succeeds", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => Promise.resolve(1));
    const { result } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    let seen!: { outcome: RefreshOutcome | null };
    act(() => {
      seen = track(result.current.refresh);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(seen.outcome).toEqual({ status: "ok" });
  });

  it("is failed, carrying the raw error, when the read fails", async () => {
    vi.useFakeTimers();
    const down = new Error("down");
    const fetcher = vi.fn().mockResolvedValueOnce(1).mockRejectedValueOnce(down);
    const { result } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    let seen!: { outcome: RefreshOutcome | null };
    act(() => {
      seen = track(result.current.refresh);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(seen.outcome).toEqual({ status: "failed", error: down });
  });

  it("is abandoned, not failed, when a newer read supersedes it", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => new Promise<number>(() => {}));
    const { result } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    let first!: { outcome: RefreshOutcome | null };
    act(() => {
      first = track(result.current.refresh);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    act(() => {
      void result.current.refresh();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(first.outcome).toEqual({ status: "abandoned" });
  });

  it("is abandoned, not failed, on unmount and while disabled", async () => {
    vi.useFakeTimers();
    const hanging = vi.fn(() => new Promise<number>(() => {}));
    const mounted = renderHook(() => usePolling(hanging, { intervalMs: null }));
    let unmounted!: { outcome: RefreshOutcome | null };
    act(() => {
      unmounted = track(mounted.result.current.refresh);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    mounted.unmount();
    await vi.advanceTimersByTimeAsync(0);
    expect(unmounted.outcome).toEqual({ status: "abandoned" });

    const off = renderHook(() =>
      usePolling(() => Promise.resolve(1), { enabled: false, intervalMs: null }),
    );
    let disabled!: { outcome: RefreshOutcome | null };
    act(() => {
      disabled = track(off.result.current.refresh);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(disabled.outcome).toEqual({ status: "abandoned" });
  });

  it("an aborted read (the fetcher rejects with AbortError) is abandoned, not failed", async () => {
    vi.useFakeTimers();
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    const fetcher = vi.fn().mockResolvedValueOnce(1).mockRejectedValueOnce(abort);
    const { result } = renderHook(() => usePolling(fetcher, { intervalMs: null }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    let seen!: { outcome: RefreshOutcome | null };
    act(() => {
      seen = track(result.current.refresh);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(seen.outcome).toEqual({ status: "abandoned" });
  });
});
