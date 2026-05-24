import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type {
  CanCoverageResponse,
  DidMapResponse,
  ObdCoverageResponse,
  StatusResponse,
} from "../types";

const mocks = vi.hoisted(() => ({
  getObdCoverage: vi.fn(),
  getDids: vi.fn(),
  getCanCoverage: vi.fn(),
}));
vi.mock("../api/http", () => ({
  getObdCoverage: mocks.getObdCoverage,
  getDids: mocks.getDids,
  getCanCoverage: mocks.getCanCoverage,
  HttpError: class extends Error {
    status = 0;
    body = "";
  },
}));

const { DataSourcesPanel } = await import("./DataSourcesPanel");

const EMPTY_OBD: ObdCoverageResponse = {
  supported_count: 0,
  decoded_count: 0,
  supported_not_decoded_count: 0,
  responded_count: 0,
  backed_off_count: 0,
  undecoded_supported: [],
  rows: [],
};
const EMPTY_DIDS: DidMapResponse = {
  schema_version: 1,
  vehicle: {},
  access: {},
  dids: [],
};
const EMPTY_CAN: CanCoverageResponse = {
  signals: [],
  signal_count: 0,
  decoded_id_count: 0,
  decoded_ids_hex: [],
};

function channel(key: string, value: number, unit: string): [string, ChannelSnapshot] {
  return [
    key,
    {
      latest: { timestamp: "2026-05-24T10:00:00+00:00", source: "", channel: "", value, unit },
      history: [value],
    },
  ];
}

beforeEach(() => {
  mocks.getObdCoverage.mockResolvedValue(EMPTY_OBD);
  mocks.getDids.mockResolvedValue(EMPTY_DIDS);
  mocks.getCanCoverage.mockResolvedValue(EMPTY_CAN);
});
afterEach(() => vi.clearAllMocks());

describe("DataSourcesPanel", () => {
  it("OBD tab shows a supported PID with its live value", async () => {
    mocks.getObdCoverage.mockResolvedValue({
      ...EMPTY_OBD,
      supported_count: 1,
      decoded_count: 1,
      rows: [
        {
          mode: 1,
          pid: 12,
          pid_hex: "0x0C",
          status: "decoded",
          channel: "RPM",
          display_name: "Engine RPM",
          category: "engine",
          unit: "rpm",
          poll_tier: "fast",
          responded_recently: true,
          backed_off: false,
        },
      ],
    });
    const channels = new Map([channel("obd:RPM", 850, "rpm")]);
    render(createElement(DataSourcesPanel, { channels, status: null }));
    expect(await screen.findByText("Engine RPM")).toBeInTheDocument();
    expect(screen.getByText("850 rpm")).toBeInTheDocument();
  });

  it("switches to the DID tab and overlays the live value", async () => {
    mocks.getDids.mockResolvedValue({
      ...EMPTY_DIDS,
      dids: [
        {
          did: 0x1208,
          name: "INJECTOR_PW",
          unit: "ms",
          confidence: "high",
          scale: null,
          offset: null,
          plausible_min: null,
          plausible_max: null,
          notes: null,
        },
      ],
    });
    const channels = new Map([channel("did:INJECTOR_PW", 2.5, "ms")]);
    render(createElement(DataSourcesPanel, { channels, status: null }));
    fireEvent.click(screen.getByRole("button", { name: "Nissan DIDs" }));
    expect(await screen.findByText("INJECTOR_PW")).toBeInTheDocument();
    expect(screen.getByText("0x1208")).toBeInTheDocument();
    expect(screen.getByText("2.5 ms")).toBeInTheDocument();
  });

  it("GPS tab renders fix state from status", () => {
    const status = {
      gps: {
        has_fix: true,
        fix_quality: 1,
        num_satellites: 7,
        age_seconds: 0.2,
        latitude: 39.9,
        longitude: -104.8,
        altitude: 1900,
        speed: 0,
        heading: 90,
        hdop: 1.2,
      },
    } as unknown as StatusResponse;
    render(createElement(DataSourcesPanel, { channels: new Map(), status }));
    fireEvent.click(screen.getByRole("button", { name: "GPS" }));
    expect(screen.getByText("fix · 7 sats")).toBeInTheDocument();
  });
});
