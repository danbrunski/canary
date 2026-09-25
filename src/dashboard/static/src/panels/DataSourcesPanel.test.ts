import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import type {
  CanCoverageResponse,
  DidMapResponse,
  ObdCoverageResponse,
  StatusResponse,
} from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getObdCoverage", "getDids", "getCanCoverage"]);

const { DataSourcesPanel } = await import("./DataSourcesPanel");

const EMPTY_OBD: ObdCoverageResponse = {
  supported_count: 0,
  decoded_count: 0,
  supported_not_decoded_count: 0,
  responded_count: 0,
  backed_off_count: 0,
  undecoded_supported: [],
  rows: [],
  custom_pids: [],
};
const EMPTY_DIDS: DidMapResponse = {
  schema_version: 1,
  vehicle: {},
  access: {},
  dids: [],
  polled: [],
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

  it("OBD tab lists a custom PID with its live value (Task 2.8a)", async () => {
    mocks.getObdCoverage.mockResolvedValue({
      ...EMPTY_OBD,
      custom_pids: [
        {
          name: "OIL_PRESS",
          display_name: "Oil pressure",
          channel: "custom:OIL_PRESS",
          unit: "psi",
          category: "custom",
        },
      ],
    });
    const channels = new Map([channel("obd:custom:OIL_PRESS", 42, "psi")]);
    render(createElement(DataSourcesPanel, { channels, status: null }));
    expect(await screen.findByText("Custom PIDs (1)")).toBeInTheDocument();
    expect(screen.getByText("Oil pressure")).toBeInTheDocument();
    expect(screen.getByText("42 psi")).toBeInTheDocument();
  });

  it("switches to the DID tab and overlays the live value", async () => {
    mocks.getDids.mockResolvedValue({
      ...EMPTY_DIDS,
      polled: ["INJECTOR_PULSE"],
      dids: [
        {
          did: 0x1206,
          name: "INJECTOR_PULSE",
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
    const channels = new Map([channel("did:INJECTOR_PULSE", 2.5, "ms")]);
    render(createElement(DataSourcesPanel, { channels, status: null }));
    fireEvent.click(screen.getByRole("button", { name: "Manufacturer DIDs (Mode 22)" }));
    expect(await screen.findByText("INJECTOR_PULSE")).toBeInTheDocument();
    expect(screen.getByText("0x1206")).toBeInTheDocument();
    expect(screen.getByText("2.5 ms")).toBeInTheDocument();
  });

  it("labels an unscaled DID `raw` instead of its target unit", async () => {
    // The injector-pulse DID's ms scale is still unpinned, so the
    // daemon ships the raw count. Printing the catalog unit beside it would
    // read as a 28-second injector pulse.
    mocks.getDids.mockResolvedValue({
      ...EMPTY_DIDS,
      polled: ["INJECTOR_PULSE"],
      dids: [
        {
          did: 0x1206,
          name: "INJECTOR_PULSE",
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
    const channels = new Map([channel("did:INJECTOR_PULSE", 28364, "raw")]);
    render(createElement(DataSourcesPanel, { channels, status: null }));
    fireEvent.click(screen.getByRole("button", { name: "Manufacturer DIDs (Mode 22)" }));
    expect(await screen.findByText("28364 raw")).toBeInTheDocument();
    expect(screen.queryByText("ms")).not.toBeInTheDocument();
  });

  it("separates catalogued-but-unpolled DIDs from silent ones", async () => {
    // Only `collection.obd_dids` is ever requested. Counting the rest of the
    // catalog as "not responding" reported a fault where there is none.
    mocks.getDids.mockResolvedValue({
      ...EMPTY_DIDS,
      polled: ["INJECTOR_PULSE"],
      dids: [
        {
          did: 0x1206,
          name: "INJECTOR_PULSE",
          unit: "ms",
          confidence: "high",
          scale: null,
          offset: null,
          plausible_min: null,
          plausible_max: null,
          notes: null,
        },
        {
          did: 0x1101,
          name: "COOLANT",
          unit: "degC",
          confidence: "confirmed",
          scale: 1,
          offset: -40,
          plausible_min: null,
          plausible_max: null,
          notes: null,
        },
      ],
    });
    const channels = new Map([channel("did:INJECTOR_PULSE", 2.5, "ms")]);
    render(createElement(DataSourcesPanel, { channels, status: null }));
    fireEvent.click(screen.getByRole("button", { name: "Manufacturer DIDs (Mode 22)" }));

    expect(
      await screen.findByText("1/1 default DIDs responding (2 catalogued)"),
    ).toBeInTheDocument();
    // An unpolled DID can never have a value, so it counts as unavailable
    // and the default filter hides it.
    expect(screen.queryByText("COOLANT")).not.toBeInTheDocument();

    // Unfiltered, it says why it is blank instead of reading as silent.
    fireEvent.click(screen.getByRole("button", { name: /hide unavailable/ }));
    expect(screen.getByText("COOLANT")).toBeInTheDocument();
    expect(screen.getByText("not selected")).toBeInTheDocument();
  });

  it("uses the current capture profile for DID selection", async () => {
    mocks.getDids.mockResolvedValue({
      ...EMPTY_DIDS,
      polled: [],
      dids: [
        {
          did: 0x1206,
          name: "INJECTOR_PULSE",
          unit: "ms",
          confidence: "high",
          scale: 0.01,
          offset: 0,
          plausible_min: null,
          plausible_max: null,
          notes: null,
        },
      ],
    });
    const status = {
      capture_profile: {
        active: true,
        mode22_enabled: true,
        mode22: ["INJECTOR_PULSE"],
        memory: [],
      },
    } as unknown as StatusResponse;
    const channels = new Map([channel("did:INJECTOR_PULSE", 2.5, "ms")]);
    render(createElement(DataSourcesPanel, { channels, status }));
    fireEvent.click(screen.getByRole("button", { name: "Manufacturer DIDs (Mode 22)" }));
    expect(
      await screen.findByText("1/1 capture DIDs responding (1 catalogued)"),
    ).toBeInTheDocument();
    expect(screen.getByText("2.5 ms")).toBeInTheDocument();
  });

  it("lists RAM channels and overlays the current service-0x23 value", async () => {
    mocks.getDids.mockResolvedValue({
      ...EMPTY_DIDS,
      memory: [
        {
          name: "ECM_TARGET_EQUIV_RATIO",
          address: 0xffff26be,
          unit: "phi",
          confidence: "high",
          window: "fuel_target",
        },
      ],
    });
    const status = {
      capture_profile: {
        active: true,
        mode22_enabled: true,
        mode22: [],
        memory: ["ECM_TARGET_EQUIV_RATIO"],
      },
    } as unknown as StatusResponse;
    const channels = new Map([channel("obd:mem:ECM_TARGET_EQUIV_RATIO", 1.2, "phi")]);
    render(createElement(DataSourcesPanel, { channels, status }));
    fireEvent.click(screen.getByRole("button", { name: "ECU RAM (service 0x23)" }));
    expect(await screen.findByText("ECM_TARGET_EQUIV_RATIO")).toBeInTheDocument();
    expect(screen.getByText("0xFFFF26BE")).toBeInTheDocument();
    expect(screen.getByText("1.2 phi")).toBeInTheDocument();
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
