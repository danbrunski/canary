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
  status: null,
  statusError: null,
  behaviorModels: null,
  behaviorModelsError: null,
  token: "",
  onTokenChange: () => {},
};

function renderAt(active: WorkbenchItem) {
  return render(
    createElement(Workbench, { active, onNavigate: () => {}, data: DATA }),
  );
}

describe("Workbench navigation", () => {
  it("renders all five group labels", () => {
    renderAt("live-gauges");
    for (const label of ["Live", "Diagnostics", "Models", "Data", "Control"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("renders the active item's panel and switches on navigate", () => {
    let active: WorkbenchItem = "live-gauges";
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
    renderAt("diag-coverage");
    expect(screen.getByText("OBD coverage")).toBeInTheDocument();
    expect(screen.getByText(/GET \/obd\/coverage/)).toBeInTheDocument();
    expect(screen.getByText("6.3")).toBeInTheDocument();
  });

  it("marks not-yet-built items with a 'soon' hint", () => {
    renderAt("live-gauges");
    expect(screen.getAllByText("soon").length).toBeGreaterThan(0);
  });
});
