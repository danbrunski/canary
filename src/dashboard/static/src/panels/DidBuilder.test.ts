import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { DidMapResponse } from "../types";

const mocks = vi.hoisted(() => ({ getDids: vi.fn() }));
vi.mock("../api/http", () => ({
  getDids: mocks.getDids,
  HttpError: class extends Error {
    status = 0;
    body = "";
  },
}));

const { DidWorkbenchPanel } = await import("./DidWorkbenchPanel");
const { CustomDashboardPanel } = await import("./CustomDashboardPanel");

beforeEach(() => {
  mocks.getDids.mockReset();
  window.localStorage.clear();
});
afterEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

describe("DidWorkbenchPanel", () => {
  it("renders the DID catalog", async () => {
    const resp: DidMapResponse = {
      schema_version: 1,
      vehicle: {},
      access: {},
      dids: [
        {
          did: 0x1208,
          name: "INJECTOR_PULSE_WIDTH",
          unit: "ms",
          confidence: "high",
          scale: null,
          offset: null,
          plausible_min: null,
          plausible_max: null,
          notes: "scale TBD",
        },
      ],
    };
    mocks.getDids.mockResolvedValue(resp);
    render(createElement(DidWorkbenchPanel));
    expect(await screen.findByText("INJECTOR_PULSE_WIDTH")).toBeInTheDocument();
    expect(screen.getByText("0x1208")).toBeInTheDocument();
    expect(screen.getByText("high")).toBeInTheDocument();
    expect(screen.getByText("1 known")).toBeInTheDocument();
  });
});

const channels = new Map<string, ChannelSnapshot>([
  [
    "obd:RPM",
    {
      latest: {
        timestamp: "2026-05-17T10:00:00+00:00",
        source: "obd",
        channel: "RPM",
        value: 850,
        unit: "rpm",
      },
      history: [800, 820, 850],
    },
  ],
]);

describe("CustomDashboardPanel", () => {
  it("adds a tile and persists the profile", () => {
    render(createElement(CustomDashboardPanel, { channels }));
    expect(screen.getByText(/empty dashboard/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /obd:RPM/ }));
    expect(screen.getByText("1 tiles")).toBeInTheDocument();
    const stored = JSON.parse(
      window.localStorage.getItem("canary.dashboard") ?? "{}",
    );
    expect(stored.tiles).toContain("obd:RPM");
  });

  it("hydrates from a stored profile", () => {
    window.localStorage.setItem(
      "canary.dashboard",
      JSON.stringify({ title: "Race", tiles: ["obd:RPM"] }),
    );
    render(createElement(CustomDashboardPanel, { channels }));
    expect(screen.getByDisplayValue("Race")).toBeInTheDocument();
    // The tile shows the live value.
    expect(screen.getByText("850.00")).toBeInTheDocument();
  });
});
