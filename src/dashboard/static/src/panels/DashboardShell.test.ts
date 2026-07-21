import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { DashboardShell } from "./DashboardShell";

const BASE = {
  theme: "dark" as const,
  onToggleTheme: () => {},
  telemetryState: "open" as const,
  anomalyState: "open" as const,
  controlState: "closed" as const,
  controlReady: false,
  connectivity: "live-pi" as const,
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

  it("shows the 6.35 connectivity badge for each mode", () => {
    const { rerender } = render(
      createElement(DashboardShell, {
        ...BASE,
        appMode: "parked",
        onToggleMode: () => {},
        connectivity: "live-pi",
      }),
    );
    expect(screen.getByText("live pi")).toBeInTheDocument();

    rerender(
      createElement(DashboardShell, {
        ...BASE,
        appMode: "parked",
        onToggleMode: () => {},
        connectivity: "local-library",
      }),
    );
    expect(screen.getByText("local library")).toBeInTheDocument();

    rerender(
      createElement(DashboardShell, {
        ...BASE,
        appMode: "parked",
        onToggleMode: () => {},
        connectivity: "queued-offline",
      }),
    );
    expect(screen.getByText("queued offline")).toBeInTheDocument();
  });
});
