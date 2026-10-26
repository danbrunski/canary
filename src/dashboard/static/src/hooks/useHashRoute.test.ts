import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useHashRoute } from "./useHashRoute";
import { useTheme } from "./useTheme";

const OLD_ITEM_ROUTES = {
  "live-charts": "observe-charts",
  "live-alarms": "observe-alarms",
  "live-builder": "observe-builder",
  "live-trip": "observe-trip",
  "live-rom-maps": "tuning-rom-editor",
  "tuning-rom-maps": "tuning-rom-editor",
  "diag-anomalies": "diag-anomalies",
  "diag-data": "system-data-sources",
  "diag-dtc": "diag-dtc",
  "diag-monitors": "diag-monitors",
  "diag-trends": "diag-trends",
  "diag-explore": "diag-explore",
  "diag-system": "system-info",
  "diag-tpms": "diag-tpms",
  "diag-llm-assist": "diag-llm-assist",
  "models-overview": "models-overview",
  "models-behavior": "models-behavior",
  "models-training": "models-training",
  "models-tuning": "model-thresholds",
  "models-health": "models-health",
  "data-sessions": "system-sessions",
  "data-health": "model-dataset-health",
  "data-custom-pids": "system-custom-pids",
  "data-export": "system-export",
  "control-main": "system-control",
  "control-test": "system-test",
  "control-profiles": "system-profiles",
  "control-capture": "system-capture-profiles",
  "tuning-capture-profiles": "system-capture-profiles",
  "control-power": "system-power",
} as const;

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
    expect(result.current.item).toBe("observe-charts");
  });

  it.each(Object.entries(OLD_ITEM_ROUTES))(
    "keeps the old %s route as an alias",
    (oldItem, expected) => {
      window.history.replaceState(null, "", `#wb/${oldItem}`);
      const { result } = renderHook(() => useHashRoute());
      expect(result.current.mode).toBe("parked");
      expect(result.current.item).toBe(expected);
    },
  );

  it("sends the retired ROM Maps page to the ROM Editor and rewrites the hash", () => {
    // 2026-10-26: ROM Maps was merged into the ROM Editor. A bookmark to either
    // of its old ids lands on the editor, and the address bar says so.
    for (const old of ["tuning-rom-maps", "live-rom-maps"]) {
      window.history.replaceState(null, "", `#wb/${old}`);
      const { result, unmount } = renderHook(() => useHashRoute());
      expect(result.current.item).toBe("tuning-rom-editor");
      expect(window.location.hash).toBe("#wb/tuning-rom-editor");
      unmount();
    }
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
