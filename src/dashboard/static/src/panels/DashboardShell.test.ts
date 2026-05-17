import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { DashboardShell } from "./DashboardShell";

const BASE = {
  telemetryState: "open" as const,
  anomalyState: "open" as const,
  controlState: "closed" as const,
  controlReady: false,
  events: [],
  status: null,
  channels: new Map<string, ChannelSnapshot>(),
  children: createElement("div", { "data-testid": "body" }, "body"),
};

describe("DashboardShell", () => {
  it("toggles between Drive and Workbench context", () => {
    const onToggleMode = vi.fn();
    const { rerender } = render(
      createElement(DashboardShell, {
        ...BASE,
        appMode: "driving",
        onToggleMode,
      }),
    );
    const toggle = screen.getByRole("button", {
      name: "switch to the parked workbench",
    });
    expect(toggle).toHaveTextContent("→ workbench");
    fireEvent.click(toggle);
    expect(onToggleMode).toHaveBeenCalledOnce();

    rerender(
      createElement(DashboardShell, {
        ...BASE,
        appMode: "parked",
        onToggleMode,
      }),
    );
    expect(
      screen.getByRole("button", { name: "switch to the driving screen" }),
    ).toHaveTextContent("→ drive");
  });

  it("always shows the status strip with a nominal anomaly state", () => {
    render(
      createElement(DashboardShell, {
        ...BASE,
        appMode: "driving",
        onToggleMode: () => {},
      }),
    );
    expect(screen.getByLabelText("System status")).toBeInTheDocument();
    expect(screen.getByText("nominal")).toBeInTheDocument();
    expect(screen.getByTestId("body")).toBeInTheDocument();
  });
});
