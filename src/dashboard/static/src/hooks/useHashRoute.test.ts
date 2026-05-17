import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useHashRoute } from "./useHashRoute";
import { useTheme } from "./useTheme";

afterEach(() => {
  window.history.replaceState(null, "", "/");
  delete document.documentElement.dataset.theme;
  window.localStorage.clear();
});

describe("useHashRoute", () => {
  it("defaults to the Drive context", () => {
    const { result } = renderHook(() => useHashRoute());
    expect(result.current.mode).toBe("driving");
  });

  it("mirrors mode + item to the URL hash", () => {
    const { result } = renderHook(() => useHashRoute());
    act(() => result.current.setMode("parked"));
    act(() => result.current.setItem("diag-anomalies"));
    expect(window.location.hash).toBe("#wb/diag-anomalies");
  });

  it("hydrates from an existing hash and reacts to hashchange", () => {
    window.history.replaceState(null, "", "#wb/models-overview");
    const { result } = renderHook(() => useHashRoute());
    expect(result.current.mode).toBe("parked");
    expect(result.current.item).toBe("models-overview");

    act(() => {
      window.history.replaceState(null, "", "#drive");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(result.current.mode).toBe("driving");
  });

  it("ignores an unknown workbench item, falling back to the default", () => {
    window.history.replaceState(null, "", "#wb/not-a-real-view");
    const { result } = renderHook(() => useHashRoute());
    expect(result.current.mode).toBe("parked");
    expect(result.current.item).toBe("live-gauges");
  });
});

describe("useTheme", () => {
  it("defaults to dark with no html attribute", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe("dark");
    expect(document.documentElement.dataset.theme).toBeUndefined();
  });

  it("toggles to night and sets the html data attribute + storage", () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.toggle());
    expect(result.current.theme).toBe("night");
    expect(document.documentElement.dataset.theme).toBe("night");
    expect(window.localStorage.getItem("canary.theme")).toBe("night");
  });
});
