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

  it("data-sessions renders the real Sessions panel (Task 3.1 — no longer a placeholder)", () => {
    renderAt("data-sessions");
    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Replay" })).toBeInTheDocument();
  });

  it("data-export renders the real Export panel (Task 5.6b)", () => {
    renderAt("data-export");
    expect(screen.getByRole("heading", { name: "Export" })).toBeInTheDocument();
  });

  it("no nav item is left marked 'soon' — data-sessions (6.13) was the last placeholder", () => {
    renderAt("live-charts");
    expect(screen.queryByText("soon")).not.toBeInTheDocument();
  });
});
