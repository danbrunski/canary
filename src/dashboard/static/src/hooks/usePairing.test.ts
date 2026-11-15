import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ACTIVE_REQUEST_KEY } from "../pairing";
import { usePairing } from "./usePairing";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const waiting = { id: "r1", status: "waiting", step: "turn_on", detail: "", expires_in_s: 100 };

/** Poll timing of the pairing request (R28.3: it rides `usePolling`). */
describe("usePairing poll timing", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  function resume() {
    window.sessionStorage.setItem(ACTIVE_REQUEST_KEY, JSON.stringify({ id: "r1", secret: "s1" }));
  }

  it("polls at once for a resumed request, then every second", async () => {
    vi.useFakeTimers();
    resume();
    const fetchMock = vi.fn(() => Promise.resolve(json(waiting)));
    vi.stubGlobal("fetch", fetchMock);
    renderHook(() => usePairing(vi.fn()));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(999);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("never overlaps a slow poll", async () => {
    vi.useFakeTimers();
    resume();
    const fetchMock = vi.fn(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    renderHook(() => usePairing(vi.fn()));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops polling once a poll fails", async () => {
    vi.useFakeTimers();
    resume();
    const fetchMock = vi.fn(() => Promise.resolve(json({ detail: "gone" }, 404)));
    vi.stubGlobal("fetch", fetchMock);
    const { result } = renderHook(() => usePairing(vi.fn()));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.view.phase).toBe("failed");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops polling on unmount", async () => {
    vi.useFakeTimers();
    resume();
    const fetchMock = vi.fn(() => Promise.resolve(json(waiting)));
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => usePairing(vi.fn()));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    unmount();
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
