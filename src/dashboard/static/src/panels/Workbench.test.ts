import { createElement } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AREA_MEMORY_STORAGE_KEY } from "../hooks/useAreaMemory";

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
  return render(createElement(Workbench, { active, onNavigate: () => {}, data: DATA }));
}

const sidebar = () => within(screen.getByRole("navigation", { name: "Workbench sections" }));
const areas = () => within(screen.getByRole("navigation", { name: "Workbench areas" }));

// Area memory and the collapsed sidebar persist per browser; no test inherits
// another's.
beforeEach(() => window.localStorage.clear());

// These tests are about navigation, and the pages they open poll the daemon.
// Without a stub those requests really went out (jsdom resolves the relative
// URLs against localhost) and settled after the test, updating DtcPanel
// outside act(). A request that never settles keeps every page in its
// loading state; each panel's own test covers what it shows once loaded.
beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>(() => {})),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("Workbench navigation", () => {
  it("renders all five group labels as areas", () => {
    renderAt("observe-charts");
    for (const label of ["Observe", "Engine Tuning", "Diagnose", "Model", "System"]) {
      expect(areas().getByRole("button", { name: label })).toBeInTheDocument();
    }
  });

  it("places Capture Profiles under System", () => {
    renderAt("system-info");
    expect(areas().getByRole("button", { name: "System" })).toHaveAttribute("aria-current", "page");
    expect(sidebar().getByRole("button", { name: "Capture Profiles" })).toBeInTheDocument();
  });

  it("lists only the active area's pages in the sidebar (W10.3)", () => {
    renderAt("tuning-rom-editor");
    expect(areas().getByRole("button", { name: "Engine Tuning" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    for (const label of ["Logging", "Logs", "ROM Library", "ROM Editor"]) {
      expect(sidebar().getByRole("button", { name: label })).toBeInTheDocument();
    }
    for (const label of ["Charts", "Anomalies", "System Info"]) {
      expect(sidebar().queryByRole("button", { name: label })).not.toBeInTheDocument();
    }
  });

  it("lands an area on its first page when nothing is remembered", () => {
    const onNavigate = vi.fn();
    render(createElement(Workbench, { active: "observe-charts", onNavigate, data: DATA }));
    fireEvent.click(areas().getByRole("button", { name: "Diagnose" }));
    expect(onNavigate).toHaveBeenCalledWith("diag-anomalies");
  });

  it("returns an area to the page last open there (W10.1)", () => {
    const onNavigate = vi.fn();
    const view = render(
      createElement(Workbench, { active: "system-power", onNavigate, data: DATA }),
    );
    view.rerender(createElement(Workbench, { active: "observe-trip", onNavigate, data: DATA }));
    fireEvent.click(areas().getByRole("button", { name: "System" }));
    expect(onNavigate).toHaveBeenLastCalledWith("system-power");
    fireEvent.click(areas().getByRole("button", { name: "Observe" }));
    expect(onNavigate).toHaveBeenLastCalledWith("observe-trip");
  });

  it("remembers areas across a reload", () => {
    window.localStorage.setItem(AREA_MEMORY_STORAGE_KEY, JSON.stringify({ diagnose: "diag-dtc" }));
    const onNavigate = vi.fn();
    render(createElement(Workbench, { active: "observe-charts", onNavigate, data: DATA }));
    fireEvent.click(areas().getByRole("button", { name: "Diagnose" }));
    expect(onNavigate).toHaveBeenCalledWith("diag-dtc");
  });

  it("separates profile editing from the Logging run controls", () => {
    const editor = renderAt("system-capture-profiles");
    expect(
      screen.getByText(/Define the default collection and temporary logging schemas/),
    ).toBeInTheDocument();
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
    fireEvent.click(areas().getByRole("button", { name: "System" }));
    rerender(createElement(Workbench, { active, onNavigate: (id) => (active = id), data: DATA }));
    fireEvent.click(sidebar().getByRole("button", { name: "System Info" }));
    rerender(createElement(Workbench, { active, onNavigate: () => {}, data: DATA }));
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
    expect(
      screen.getByText("Locked — needs the control token to browse logs."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/W04/)).not.toBeInTheDocument();
  });

  it("tuning-analysis loads directly and asks for sessions (W04)", () => {
    renderAt("tuning-analysis");
    expect(screen.getByText(/Choose sessions or a revision/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delivery error" })).toBeInTheDocument();
  });

  it("renders the detector page as Alert Thresholds, not Tuning", () => {
    renderAt("model-thresholds");
    expect(screen.getByRole("heading", { name: "Alert Thresholds" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Tuning" })).not.toBeInTheDocument();
  });
});

describe("Workbench sidebar collapse (W10.4)", () => {
  afterEach(() => vi.restoreAllMocks());

  it("collapses to a rail and expands again", () => {
    renderAt("tuning-rom-editor");
    fireEvent.click(sidebar().getByRole("button", { name: "Collapse sidebar" }));
    expect(sidebar().queryByRole("button", { name: "ROM Editor" })).not.toBeInTheDocument();
    const expand = sidebar().getByRole("button", { name: "Expand sidebar" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    // The areas stay reachable while the sidebar is a rail.
    expect(areas().getByRole("button", { name: "Diagnose" })).toBeInTheDocument();
    fireEvent.click(expand);
    expect(sidebar().getByRole("button", { name: "ROM Editor" })).toBeInTheDocument();
    expect(sidebar().getByRole("button", { name: "Collapse sidebar" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("stays collapsed across a remount", () => {
    const first = renderAt("observe-charts");
    fireEvent.click(sidebar().getByRole("button", { name: "Collapse sidebar" }));
    first.unmount();
    renderAt("observe-charts");
    expect(sidebar().getByRole("button", { name: "Expand sidebar" })).toBeInTheDocument();
  });

  it("is expanded when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    renderAt("observe-charts");
    expect(sidebar().getByRole("button", { name: "Charts" })).toBeInTheDocument();
    // Collapsing still works for the session.
    fireEvent.click(sidebar().getByRole("button", { name: "Collapse sidebar" }));
    expect(sidebar().queryByRole("button", { name: "Charts" })).not.toBeInTheDocument();
  });
});

describe("Workbench on a phone (P2.2)", () => {
  it("drops the sidebar; the shell's bottom bar navigates instead", () => {
    render(
      createElement(Workbench, {
        active: "diag-explore",
        onNavigate: () => {},
        data: DATA,
        phone: true,
      }),
    );
    expect(
      screen.queryByRole("navigation", { name: "Workbench sections" }),
    ).not.toBeInTheDocument();
    // A More page has no sibling strip.
    expect(screen.queryByRole("navigation", { name: /pages$/ })).not.toBeInTheDocument();
  });

  it("offers the sibling pages of a bar entry as a strip", () => {
    const onNavigate = vi.fn();
    render(createElement(Workbench, { active: "diag-dtc", onNavigate, data: DATA, phone: true }));
    const strip = within(screen.getByRole("navigation", { name: "Alerts pages" }));
    expect(strip.getByRole("button", { name: "Trouble Codes" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    fireEvent.click(strip.getByRole("button", { name: "Anomalies" }));
    expect(onNavigate).toHaveBeenCalledWith("diag-anomalies");
  });
});
