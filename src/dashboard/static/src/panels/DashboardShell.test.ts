import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { ConnectionState } from "../types";
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
  token: "",
  onTokenChange: () => {},
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
    expect(screen.getByRole("button", { name: "switch to the driving screen" })).toHaveTextContent(
      "→ drive",
    );
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

describe("DashboardShell on a phone (P2.2)", () => {
  function renderPhone(overrides: { anomalyState?: ConnectionState } = {}) {
    const onToggleTheme = vi.fn();
    render(
      createElement(DashboardShell, {
        ...BASE,
        ...overrides,
        onToggleTheme,
        appMode: "parked",
        onToggleMode: () => {},
        phone: true,
        bottomNav: createElement("nav", { "aria-label": "Phone sections" }),
      }),
    );
    return { onToggleTheme };
  }

  it("condenses the header to brand, connection status and one menu", () => {
    renderPhone({ anomalyState: "error" });
    expect(screen.getByText("CANary")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "connection: live pi, stream down" })).toBeVisible();
    // The widgets moved into the menu; the bottom bar replaces the mode toggle.
    expect(screen.queryByRole("button", { name: "control token" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /switch to the/ })).not.toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Phone sections" })).toBeInTheDocument();
  });

  it("holds the theme toggle, token pill and stream detail in the menu", () => {
    const { onToggleTheme } = renderPhone();
    fireEvent.click(screen.getByRole("button", { name: "open menu" }));
    const menu = screen.getByRole("dialog", { name: "Menu" });
    expect(menu).toHaveTextContent("tel");
    expect(menu).toHaveTextContent("anom");
    expect(screen.getByRole("button", { name: "control token" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /switch to the night/ }));
    expect(onToggleTheme).toHaveBeenCalledOnce();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Menu" })).not.toBeInTheDocument();
  });

  it("renders no bottom bar on a desktop", () => {
    render(
      createElement(DashboardShell, {
        ...BASE,
        appMode: "parked",
        onToggleMode: () => {},
        bottomNav: createElement("nav", { "aria-label": "Phone sections" }),
      }),
    );
    expect(screen.queryByRole("navigation", { name: "Phone sections" })).not.toBeInTheDocument();
  });
});
