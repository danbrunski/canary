import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type { SessionDigestResponse, StatusResponse, TripSummaryResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getTripSummary", "getSessionDigest"]);

const { TripPanel } = await import("./TripPanel");

function channel(key: string, value: number): [string, ChannelSnapshot] {
  return [
    key,
    {
      latest: {
        timestamp: "2026-07-19T12:00:00+00:00",
        source: "derived",
        channel: key,
        value,
        unit: "",
      },
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

const DIGEST: SessionDigestResponse = {
  schema_version: 1,
  computed_at: "2026-07-19T12:00:00Z",
  session_id: "2026-07-19T110000",
  duration_seconds: 725,
  mode_seconds: { highway: 700, idle: 25 },
  anomaly_counts_by_source: { fused: 2, behavior: 1 },
  top_behavior_deviations: [],
  fingerprint_top_movers: [
    { stat: "idle_rpm_mean", value: 900, reference_mean: 800, reference_std: 20, z_score: 5.0 },
  ],
  new_dtcs: [{ code: "P0171" }],
  new_ledger_events: [{ id: "abc" }],
};

describe("TripPanel", () => {
  it("shows a placeholder for the current drive when no derived channels have streamed yet", () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    mocks.getSessionDigest.mockReturnValue(new Promise(() => {}));
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: null } as unknown as StatusResponse,
        token: "secret",
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
        token: "secret",
      }),
    );
    expect(screen.getByText(/10\.0 km/)).toBeInTheDocument();
    expect(screen.getByText(/0\.90 L/)).toBeInTheDocument();
    expect(screen.getByText(/2\.00 kg/)).toBeInTheDocument();
    expect(screen.getByText(/9\.0 L\/100km/)).toBeInTheDocument();
  });

  it("fetches and renders the last-drive summary keyed on status.latest_session", async () => {
    mocks.getTripSummary.mockResolvedValue(SUMMARY);
    mocks.getSessionDigest.mockResolvedValue(DIGEST);
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
        token: "secret",
      }),
    );
    expect(await screen.findByText("2026-07-19T110000")).toBeInTheDocument();
    expect(mocks.getTripSummary).toHaveBeenCalledWith("2026-07-19T110000", expect.anything());
    expect(screen.getByText(/12\.3 km/)).toBeInTheDocument();
    expect(screen.getByText(/8\.9 L\/100km/)).toBeInTheDocument();
    expect(screen.getByText(/26\.4 mpg/)).toBeInTheDocument();
  });

  it("shows an error state when the last-drive fetch fails", async () => {
    mocks.getTripSummary.mockRejectedValue(new Error("boom"));
    mocks.getSessionDigest.mockResolvedValue(DIGEST);
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
        token: "secret",
      }),
    );
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });

  it("renders the last-drive digest keyed on status.latest_session", async () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    mocks.getSessionDigest.mockResolvedValue(DIGEST);
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
        token: "secret",
      }),
    );
    expect(mocks.getSessionDigest).toHaveBeenCalledWith(
      "secret",
      "2026-07-19T110000",
      expect.anything(),
    );
    expect(await screen.findByText(/fused=2/)).toBeInTheDocument();
    expect(screen.getByText("P0171")).toBeInTheDocument();
    expect(screen.getByText("idle_rpm_mean")).toBeInTheDocument();
  });

  it("keeps evidence-role events out of the gated anomaly count (A06.5)", async () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    mocks.getSessionDigest.mockResolvedValue({
      ...DIGEST,
      schema_version: 2,
      anomaly_counts_by_source: { fused: 1 },
      evidence_counts_by_source: { lstm_autoencoder: 4 },
    });
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
        token: "secret",
      }),
    );
    expect(await screen.findByText(/fused=1/)).toBeInTheDocument();
    expect(screen.getByText("gated anomalies").nextElementSibling).toHaveTextContent(/^1/);
    expect(screen.getByText("evidence (not alerts)").nextElementSibling).toHaveTextContent(
      "4 (lstm_autoencoder=4)",
    );
  });

  it("omits the evidence row for a schema-1 digest", async () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    mocks.getSessionDigest.mockResolvedValue(DIGEST);
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
        token: "secret",
      }),
    );
    expect(await screen.findByText(/fused=2/)).toBeInTheDocument();
    expect(screen.queryByText("evidence (not alerts)")).toBeNull();
  });

  it("shows an error state when the digest fetch fails", async () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    mocks.getSessionDigest.mockRejectedValue(new Error("digest boom"));
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
        token: "secret",
      }),
    );
    expect(await screen.findByText("digest boom")).toBeInTheDocument();
  });

  it("does not fetch the digest and shows a token prompt when no control token is set", () => {
    mocks.getTripSummary.mockReturnValue(new Promise(() => {}));
    render(
      createElement(TripPanel, {
        channels: new Map(),
        status: { latest_session: "2026-07-19T110000" } as unknown as StatusResponse,
        token: "",
      }),
    );
    expect(mocks.getSessionDigest).not.toHaveBeenCalled();
    expect(
      screen.getByText("Locked — needs the control token to view the last-drive digest."),
    ).toBeInTheDocument();
  });
});
