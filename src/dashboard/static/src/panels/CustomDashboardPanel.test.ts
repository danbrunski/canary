import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { CustomDashboardPanel } from "./CustomDashboardPanel";

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

beforeEach(() => window.localStorage.clear());
afterEach(() => window.localStorage.clear());

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

  it("adds only the live channels from the AFR / fueling preset (Task 2.8b)", () => {
    const afrChannels = new Map<string, ChannelSnapshot>([
      ...channels.entries(),
      [
        "obd:O2_WR_LAMBDA_S1",
        {
          latest: {
            timestamp: "2026-05-17T10:00:00+00:00",
            source: "obd",
            channel: "O2_WR_LAMBDA_S1",
            value: 1.0,
            unit: "lambda",
          },
          history: [1.0],
        },
      ],
      [
        "derived:FUEL_RATE_LPH",
        {
          latest: {
            timestamp: "2026-05-17T10:00:00+00:00",
            source: "derived",
            channel: "FUEL_RATE_LPH",
            value: 2.5,
            unit: "L/h",
          },
          history: [2.5],
        },
      ],
    ]);
    render(createElement(CustomDashboardPanel, { channels: afrChannels }));
    fireEvent.click(screen.getByRole("button", { name: "+ AFR / fueling" }));
    // Only the two present AFR channels land as tiles, not the whole preset
    // list (e.g. obd:SHORT_FUEL_TRIM_1, never reported here).
    expect(screen.getByText("2 tiles")).toBeInTheDocument();
    const stored = JSON.parse(
      window.localStorage.getItem("canary.dashboard") ?? "{}",
    );
    expect(stored.tiles).toEqual(
      expect.arrayContaining(["obd:O2_WR_LAMBDA_S1", "derived:FUEL_RATE_LPH"]),
    );
    expect(stored.tiles).not.toContain("obd:SHORT_FUEL_TRIM_1");
  });
});
