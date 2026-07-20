import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { StatusResponse, TripSummaryResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getTripSummary"]);

const { TripPanel } = await import("./TripPanel");

function channel(key: string, value: number): [string, ChannelSnapshot] {
  return [
    key,
    {
      latest: { timestamp: "2026-07-19T12:00:00+00:00", source: "derived", channel: key, value, unit: "" },
      history: [value],
    },
  ];
}

const SUMMARY: TripSummaryResponse = {
  schema_version: 1,
  computed_at: "2026-07-19T12:00:00Z",
  session_id: "2026-07-19T110000",
  summary: {
    duration_s: 725,
    distance_km: 12.3,
    fuel_used_l: 1.1,
    avg_economy_l_per_100km: 8.9,
    avg_economy_mpg: 26.4,
    co2_kg: 2.5,
    idle_time_s: 60,
    idle_fraction: 0.08,
    max_speed_kph: 95.0,
    avg_speed_kph: 61.0,
    zero_to_60_mph_s: 9.5,
    quarter_mile_s: 17.1,
    quarter_mile_trap_kph: 130.0,
  },
};

describe("TripPanel", () => {
  it("shows a placeholder for the current drive when no derived channels have streamed yet", () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: null } as unknown as StatusResponse,
      }),
    );
    expect(screen.getByText("no live drive data yet")).toBeInTheDocument();
    expect(screen.getByText("no recorded session yet")).toBeInTheDocument();
  });

  it("renders current-drive stats from the streamed derived:* channels", () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    const channels = new Map([
      channel("derived:TRIP_DISTANCE_KM", 10.0),
      channel("derived:FUEL_USED_L", 0.9),
      channel("derived:CO2_KG", 2.0),
      channel("derived:IDLE_TIME_S", 30.0),
      channel("derived:AVG_ECONOMY_L_PER_100KM", 9.0),
    ]);
    render(
      createElement(TripPanel, {
        channels,
        status: { latest_session: null } as unknown as StatusResponse,
      }),
    );
    expect(screen.getByText(/10\.0 km/)).toBeInTheDocument();
    expect(screen.getByText(/0\.90 L/)).toBeInTheDocument();
    expect(screen.getByText(/2\.00 kg/)).toBeInTheDocument();
    expect(screen.getByText(/9\.0 L\/100km/)).toBeInTheDocument();
  });

  it("fetches and renders the last-drive summary keyed on status.latest_session", async () => {
    mocks.getTripSummary.mockResolvedValue(SUMMARY);
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
      }),
    );
    expect(await screen.findByText("2026-07-19T110000")).toBeInTheDocument();
    expect(mocks.getTripSummary).toHaveBeenCalledWith(
      "2026-07-19T110000",
      expect.anything(),
    );
    expect(screen.getByText(/12\.3 km/)).toBeInTheDocument();
    expect(screen.getByText(/8\.9 L\/100km/)).toBeInTheDocument();
    expect(screen.getByText(/26\.4 mpg/)).toBeInTheDocument();
  });

  it("shows an error state when the last-drive fetch fails", async () => {
    mocks.getTripSummary.mockRejectedValue(new Error("boom"));
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
      }),
    );
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});
