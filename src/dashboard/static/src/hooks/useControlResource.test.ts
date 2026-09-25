import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, NetworkError } from "../api/http";
import { describeError, useControlResource } from "./useControlResource";

describe("describeError", () => {
  it("names the unreachable daemon instead of echoing the browser's transport error", () => {
    const text = describeError(
      new NetworkError(
        "http://canary.local:8000/control/capture",
        new TypeError("NetworkError when attempting to fetch resource"),
      ),
    );

    expect(text).toContain("Can't reach the daemon");
    expect(text).toContain("http://canary.local:8000/control/capture");
    // The service worker serves the shell from cache, so "the page loaded"
    // is not evidence the daemon is up — the notice has to say so.
    expect(text).toContain("cached offline");
  });

  it("surfaces the daemon's own reason for a 409 rather than the bare status", () => {
    const err = new HttpError(409, '{"detail":"a capture profile is already active"}');

    expect(describeError(err)).toBe("Refused by the server: a capture profile is already active");
  });

  it("surfaces a 404 detail the same way", () => {
    const err = new HttpError(404, '{"detail":"capture profile not found: fuel-map"}');

    expect(describeError(err)).toBe("Refused by the server: capture profile not found: fuel-map");
  });

  it("falls back to the raw body when the error page isn't FastAPI JSON", () => {
    const err = new HttpError(409, "<html>gateway said no</html>");

    expect(describeError(err)).toBe("Refused by the server: <html>gateway said no</html>");
  });

  it("keeps the existing auth and unwired-feature wording", () => {
    expect(describeError(new HttpError(401, ""))).toBe(
      "Not authorized — enter or check the control token.",
    );
    expect(describeError(new HttpError(503, ""))).toBe(
      "This API isn't available on the daemon (feature not wired).",
    );
    expect(describeError(new HttpError(500, "boom"))).toBe("Server error 500.");
  });
});

describe("useControlResource polling (R08.9)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (err: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  it("never starts a poll while the previous one is still in flight", async () => {
    vi.useFakeTimers();
    const pending: Array<ReturnType<typeof deferred<number>>> = [];
    const fetcher = vi.fn(() => {
      const d = deferred<number>();
      pending.push(d);
      return d.promise;
    });
    const { result } = renderHook(() => useControlResource(fetcher, { intervalMs: 2500 }));
    expect(fetcher).toHaveBeenCalledTimes(1);
    // The Pi is slow: 10 s pass with the first request unanswered.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      pending[0].resolve(1);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.data).toBe(1);
    // The next poll is scheduled from completion, not from the first start.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2499);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("keeps the last good data and reports the described error on failure", async () => {
    vi.useFakeTimers();
    const fetcher = vi
      .fn<(signal: AbortSignal) => Promise<string>>()
      .mockResolvedValueOnce("first")
      .mockRejectedValueOnce(new HttpError(401, ""));
    const { result } = renderHook(() => useControlResource(fetcher, { intervalMs: 1000 }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.data).toBe("first");
    expect(result.current.loading).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(result.current.data).toBe("first");
    expect(result.current.status).toBe(401);
    expect(result.current.error).toBe("Not authorized — enter or check the control token.");
  });

  it("fetches exactly once with intervalMs: null (R08.2)", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(() => Promise.resolve(7));
    const { result } = renderHook(() => useControlResource(fetcher, { intervalMs: null }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe(7);
    act(() => result.current.refresh());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("maps errors through a caller's describe (R08.2)", async () => {
    const fetcher = vi.fn(() => Promise.reject(new HttpError(409, '{"detail":"busy"}')));
    const describe = (err: unknown) => ({ kind: "refusal", status: (err as HttpError).status });
    const { result } = renderHook(() =>
      useControlResource(fetcher, { intervalMs: null, describe }),
    );
    await waitFor(() => expect(result.current.error).toEqual({ kind: "refusal", status: 409 }));
  });

  it("does nothing while disabled and clears on disable", async () => {
    const fetcher = vi.fn(() => Promise.resolve("x"));
    const { result, rerender } = renderHook(
      ({ enabled }) => useControlResource(fetcher, { enabled, intervalMs: null }),
      { initialProps: { enabled: false } },
    );
    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.data).toBe("x"));
    rerender({ enabled: false });
    expect(result.current.data).toBeNull();
  });

  it("aborts the in-flight request on unmount", () => {
    let seen: AbortSignal | null = null;
    const fetcher = vi.fn((signal: AbortSignal) => {
      seen = signal;
      return new Promise<never>(() => {});
    });
    const { unmount } = renderHook(() => useControlResource(fetcher, { intervalMs: 1000 }));
    unmount();
    expect(seen!.aborted).toBe(true);
  });
});
