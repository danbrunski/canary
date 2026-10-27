import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { itemForGroup } from "../nav";
import type { WorkbenchItem } from "../types";
import {
  AREA_MEMORY_STORAGE_KEY,
  SIDEBAR_COLLAPSED_STORAGE_KEY,
  readAreaMemory,
  useAreaMemory,
  useSidebarCollapsed,
} from "./useAreaMemory";

function store(value: string) {
  window.localStorage.setItem(AREA_MEMORY_STORAGE_KEY, value);
}

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.restoreAllMocks());

describe("itemForGroup (W10.1)", () => {
  it("prefers the remembered page", () => {
    expect(itemForGroup("tuning", { tuning: "tuning-rom-editor" })).toBe("tuning-rom-editor");
  });

  it("falls back to the area's first page", () => {
    expect(itemForGroup("tuning", {})).toBe("tuning-logging");
  });

  it("ignores a remembered page of another area", () => {
    expect(itemForGroup("tuning", { tuning: "diag-dtc" })).toBe("tuning-logging");
  });
});

describe("readAreaMemory", () => {
  it("reads what was stored", () => {
    store(JSON.stringify({ tuning: "tuning-logs", system: "system-power" }));
    expect(readAreaMemory()).toEqual({ tuning: "tuning-logs", system: "system-power" });
  });

  it("drops an id that is no longer a page", () => {
    store(JSON.stringify({ tuning: "tuning-rom-maps-old", system: "system-power" }));
    expect(readAreaMemory()).toEqual({ system: "system-power" });
  });

  it("drops a page filed under an area it does not belong to", () => {
    store(JSON.stringify({ tuning: "system-power" }));
    expect(readAreaMemory()).toEqual({});
  });

  it.each(["not json", "null", "[]", '"tuning-logs"', "42", '{"tuning": 7}'])(
    "reads junk %s as nothing remembered",
    (raw) => {
      store(raw);
      expect(readAreaMemory()).toEqual({});
    },
  );

  it("reads a throwing localStorage as nothing remembered", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(readAreaMemory()).toEqual({});
  });
});

describe("useAreaMemory", () => {
  it("files each page under its area and stores it", () => {
    const { result, rerender } = renderHook((active: WorkbenchItem) => useAreaMemory(active), {
      initialProps: "tuning-logs",
    });
    expect(result.current).toEqual({ tuning: "tuning-logs" });
    rerender("diag-dtc");
    rerender("tuning-rom-editor");
    expect(result.current).toEqual({ tuning: "tuning-rom-editor", diagnose: "diag-dtc" });
    expect(readAreaMemory()).toEqual({ tuning: "tuning-rom-editor", diagnose: "diag-dtc" });
  });

  it("keeps the memory for the session when storage throws", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    const { result, rerender } = renderHook((active: WorkbenchItem) => useAreaMemory(active), {
      initialProps: "system-power",
    });
    rerender("observe-trip");
    expect(result.current).toEqual({ system: "system-power", observe: "observe-trip" });
  });
});

describe("useSidebarCollapsed (W10.4)", () => {
  it("defaults to expanded", () => {
    const { result } = renderHook(() => useSidebarCollapsed());
    expect(result.current[0]).toBe(false);
  });

  it("reads only a stored true as collapsed", () => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, '"yes"');
    expect(renderHook(() => useSidebarCollapsed()).result.current[0]).toBe(false);
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, "true");
    expect(renderHook(() => useSidebarCollapsed()).result.current[0]).toBe(true);
  });
});
