import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { WorkbenchItem } from "../types";
import { Workbench, type DashboardData } from "./Workbench";

const DATA: DashboardData = {
  mode: "parked",
  channels: new Map<string, ChannelSnapshot>(),
  bindings: {},
  events: [],
  liveScores: {},
  status: null,
  statusError: null,
  behaviorModels: null,
  behaviorModelsError: null,
  behaviorLive: null,
  token: "",
  companion: {
    baseUrl: "http://127.0.0.1:8765",
    token: "",
    setBaseUrl: () => {},
    setToken: () => {},
  },
};

function renderAt(active: WorkbenchItem) {
  return render(
    createElement(Workbench, { active, onNavigate: () => {}, data: DATA }),
  );
}

describe("Workbench navigation", () => {
  it("renders all five group labels", () => {
    renderAt("observe-charts");
    for (const label of ["Observe", "Engine Tuning", "Diagnose", "Model", "System"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("places Capture Profiles under System", () => {
    renderAt("observe-charts");
    expect(screen.getByRole("button", { name: "Capture Profiles" }).closest("div"))
      .toHaveTextContent("System");
  });

  it("separates profile editing from the Logging run controls", () => {
    const editor = renderAt("system-capture-profiles");
    expect(screen.getByText(/Define the default collection and temporary logging schemas/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    editor.unmount();

    renderAt("tuning-logging");
    expect(screen.getByText(/records default telemetry continuously/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View logs" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
  });

  it("renders the active item's panel and switches on navigate", () => {
    let active: WorkbenchItem = "observe-charts";
    const { rerender } = render(
      createElement(Workbench, {
        active,
        onNavigate: (id: WorkbenchItem) => {
          active = id;
        },
        data: DATA,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "System Info" }));
    rerender(
      createElement(Workbench, { active, onNavigate: () => {}, data: DATA }),
    );
    expect(screen.getByRole("heading", { name: "System" })).toBeInTheDocument();
  });

  it("system-sessions renders the real Sessions panel (Task 3.1 — no longer a placeholder)", () => {
    renderAt("system-sessions");
    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Replay" })).toBeInTheDocument();
  });

  it("system-export renders the real Export panel (Task 5.6b)", () => {
    renderAt("system-export");
    expect(screen.getByRole("heading", { name: "Export" })).toBeInTheDocument();
  });

  it("no nav item is left marked 'soon' — data-sessions (6.13) was the last placeholder", () => {
    renderAt("observe-charts");
    expect(screen.queryByText("soon")).not.toBeInTheDocument();
  });

  it("distinguishes planned entries from backend placeholders", () => {
    renderAt("tuning-flash");
    expect(screen.getAllByText("planned").length).toBeGreaterThan(0);
    expect(screen.getByText(/waits on the server-side flash interlocks/)).toBeInTheDocument();
  });

  it("tuning-logs loads directly with empty group state: the browser, no viewer (W04)", () => {
    window.localStorage.clear();
    renderAt("tuning-logs");
    expect(screen.getByRole("heading", { name: "Captures" })).toBeInTheDocument();
    expect(screen.getByText("Enter the control token to browse logs.")).toBeInTheDocument();
    expect(screen.queryByText(/W04/)).not.toBeInTheDocument();
  });

  it("tuning-analysis loads directly and asks for sessions (W04)", () => {
    renderAt("tuning-analysis");
    expect(screen.getByText(/Choose sessions or a revision/)).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Delivery error" })).toBeInTheDocument();
  });

  it("renders the detector page as Alert Thresholds, not Tuning", () => {
    renderAt("model-thresholds");
    expect(screen.getByRole("heading", { name: "Alert Thresholds" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tuning" })).not.toBeInTheDocument();
  });
});
