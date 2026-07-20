import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { AlarmsPanel } from "./AlarmsPanel";

// Task 5.5: spy on MultiSeriesChart so the Y-axis toggle test can assert the
// `yAxisMode` prop it's actually wired, without depending on canvas drawing.
vi.mock("../components/MultiSeriesChart", () => ({
  MultiSeriesChart: vi.fn(() => null),
}));

const { MultiSeriesChart } = await import("../components/MultiSeriesChart");
const { ChartsPanel } = await import("./ChartsPanel");

function snap(value: number, history: number[]): ChannelSnapshot {
  return {
    latest: {
      timestamp: "2026-05-17T10:00:00+00:00",
      source: "obd",
      channel: "X",
      value,
      unit: "",
    },
    history,
  };
}

const channels = new Map<string, ChannelSnapshot>([
  ["obd:RPM", snap(800, [700, 750, 800, 820, 810])],
  ["obd:COOLANT_TEMP", snap(95, [90, 92, 95])],
]);

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

describe("ChartsPanel", () => {
  it("lists numeric channels and selects them for charting", () => {
    render(createElement(ChartsPanel, { channels }));
    expect(screen.getByText("0/6 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /obd:RPM/ }));
    expect(screen.getByText("1/6 selected")).toBeInTheDocument();
    // Legend shows the selected series with its stats.
    expect(
      screen.getByText((t) => t.includes("min") && t.includes("avg")),
    ).toBeInTheDocument();
  });

  it("pause toggles its label", () => {
    render(createElement(ChartsPanel, { channels }));
    const btn = screen.getByRole("button", { name: /pause/ });
    fireEvent.click(btn);
    expect(screen.getByRole("button", { name: /resume/ })).toBeInTheDocument();
  });

  it("wires the Y-axis mode toggle through to MultiSeriesChart", () => {
    vi.mocked(MultiSeriesChart).mockClear();
    render(createElement(ChartsPanel, { channels }));
    // Need a selected series for MultiSeriesChart to render at all.
    fireEvent.click(screen.getByRole("button", { name: /obd:RPM/ }));
    expect(MultiSeriesChart).toHaveBeenLastCalledWith(
      expect.objectContaining({ yAxisMode: "independent" }),
      expect.anything(),
    );

    fireEvent.click(screen.getByRole("button", { name: "shared" }));

    expect(MultiSeriesChart).toHaveBeenLastCalledWith(
      expect.objectContaining({ yAxisMode: "shared" }),
      expect.anything(),
    );
  });
});

describe("AlarmsPanel", () => {
  it("adds a rule and persists it to localStorage", () => {
    render(createElement(AlarmsPanel, { channels }));
    fireEvent.change(screen.getByPlaceholderText("obd:COOLANT_TEMP"), {
      target: { value: "obd:COOLANT_TEMP" },
    });
    const valueInput = screen.getByLabelText("value");
    fireEvent.change(valueInput, { target: { value: "110" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByText("1 rules")).toBeInTheDocument();
    expect(screen.getByText("obd:COOLANT_TEMP")).toBeInTheDocument();
    const stored = JSON.parse(
      window.localStorage.getItem("canary.alarms") ?? "[]",
    );
    expect(stored).toHaveLength(1);
    expect(stored[0]).toMatchObject({ channel: "obd:COOLANT_TEMP", value: 110 });
  });

  it("loads existing rules from localStorage", () => {
    window.localStorage.setItem(
      "canary.alarms",
      JSON.stringify([
        {
          id: "r1",
          channel: "obd:RPM",
          op: ">",
          value: 6000,
          severity: "critical",
          debounce: 3,
          cooldownS: 30,
          enabled: true,
        },
      ]),
    );
    render(createElement(AlarmsPanel, { channels }));
    expect(screen.getByText("obd:RPM")).toBeInTheDocument();
    // The rule renders its threshold; the severity word also appears in the
    // form's <option>, so assert on the unambiguous value instead.
    expect(screen.getByText(/6000/)).toBeInTheDocument();
  });
});
