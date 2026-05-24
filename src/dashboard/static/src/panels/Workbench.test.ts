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
  onTokenChange: () => {},
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
    renderAt("live-charts");
    for (const label of ["Live", "Diagnostics", "Models", "Data", "Control"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("renders the active item's panel and switches on navigate", () => {
    let active: WorkbenchItem = "live-charts";
    const { rerender } = render(
      createElement(Workbench, {
        active,
        onNavigate: (id: WorkbenchItem) => {
          active = id;
        },
        data: DATA,
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "System" }));
    rerender(
      createElement(Workbench, { active, onNavigate: () => {}, data: DATA }),
    );
    expect(screen.getByRole("heading", { name: "System" })).toBeInTheDocument();
  });

  it("placeholder items explain the backend that's ready", () => {
    // data-export is still a labeled placeholder (6.20 UI pending).
    renderAt("data-export");
    expect(screen.getByText("Dataset export")).toBeInTheDocument();
    expect(screen.getByText("6.20")).toBeInTheDocument();
  });

  it("marks not-yet-built items with a 'soon' hint", () => {
    renderAt("live-charts");
    expect(screen.getAllByText("soon").length).toBeGreaterThan(0);
  });
});
