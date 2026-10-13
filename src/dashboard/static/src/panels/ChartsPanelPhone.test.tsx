// P4.2: Charts at phone width — the selection survives a reload, each
// series gets its own row below 600px, and a touch reads values.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { ChartsPanel } from "./ChartsPanel";

const KEY = "canary.charts.selected";

function snap(value: number, history: number[]): ChannelSnapshot {
  return {
    latest: {
      timestamp: "2026-10-12T10:00:00+00:00",
      source: "obd",
      channel: "X",
      value,
      unit: "",
    },
    history,
  };
}

const channels = new Map<string, ChannelSnapshot>([
  ["obd:RPM", snap(2400, [800, 1600, 2400])],
  ["obd:COOLANT_TEMP", snap(92, [88, 90, 92])],
]);

/** Below 600px, as far as `useIsPhone` can tell. */
function asPhone(matches: boolean) {
  vi.stubGlobal("matchMedia", (media: string): Partial<MediaQueryList> => ({
    matches,
    media,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ChartsPanel selection persistence", () => {
  it("keeps the chosen channels across a reload, as the Builder does", () => {
    const first = render(<ChartsPanel channels={channels} />);
    fireEvent.click(screen.getByRole("button", { name: /obd:RPM/ }));
    fireEvent.click(screen.getByRole("button", { name: /obd:COOLANT_TEMP/ }));
    expect(JSON.parse(window.localStorage.getItem(KEY) ?? "null")).toEqual([
      "obd:RPM",
      "obd:COOLANT_TEMP",
    ]);
    first.unmount();

    render(<ChartsPanel channels={channels} />);
    expect(screen.getByText("2/6 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /obd:RPM/ }));
    expect(JSON.parse(window.localStorage.getItem(KEY) ?? "null")).toEqual(["obd:COOLANT_TEMP"]);
  });

  it("reads a malformed stored selection as nothing selected", () => {
    window.localStorage.setItem(KEY, JSON.stringify({ not: "a list" }));
    render(<ChartsPanel channels={channels} />);
    expect(screen.getByText("0/6 selected")).toBeInTheDocument();
  });
});

describe("ChartsPanel at phone width", () => {
  it("draws one series per row and reads every row at the touched instant", () => {
    asPhone(true);
    window.localStorage.setItem(KEY, JSON.stringify(["obd:RPM", "obd:COOLANT_TEMP"]));
    const { container } = render(<ChartsPanel channels={channels} />);
    const rows = screen.getByTestId("chart-rows");
    const canvases = rows.querySelectorAll("canvas");
    expect(canvases).toHaveLength(2);
    for (const canvas of canvases) {
      vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
        left: 0,
        width: 300,
      } as DOMRect);
    }
    fireEvent.pointerDown(canvases[0], { clientX: 150, pointerType: "touch" });
    const readouts = within(rows).getAllByTestId("chart-readout");
    expect(readouts).toHaveLength(2);
    expect(readouts[0]).toHaveTextContent("1600");
    expect(readouts[1]).toHaveTextContent("90");
    // No export toolbar per row; the desktop chart keeps it.
    expect(container.querySelectorAll("button[aria-label='clear readout']")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "export CSV" })).not.toBeInTheDocument();
  });

  it("keeps the single overlaid chart on a desktop", () => {
    asPhone(false);
    window.localStorage.setItem(KEY, JSON.stringify(["obd:RPM", "obd:COOLANT_TEMP"]));
    render(<ChartsPanel channels={channels} />);
    expect(screen.queryByTestId("chart-rows")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "export CSV" })).toBeInTheDocument();
  });
});
