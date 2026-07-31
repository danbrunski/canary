import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mockHttp } from "../test/harness";
import type { CaptureProfile, StatusResponse } from "../types";

const mocks = mockHttp([
  "getCaptureProfiles",
  "getObdCoverage",
  "getDids",
  "startProfileCapture",
  "stopProfileCapture",
  "saveCaptureProfile",
  "deleteCaptureProfile",
]);

const { CaptureProfilesPanel } = await import("./CaptureProfilesPanel");

const DEFAULT_PROFILE: CaptureProfile = {
  name: "default",
  description: "Normal telemetry and anomaly-detection collection",
  mode01: { AUTO: 1 },
  mode22: [],
  bus_budget_override: null,
  broadcast_decode: true,
  corpus_eligible: true,
  storage_prefix: "",
};

const FUEL_MAP: CaptureProfile = {
  name: "fuel-map",
  description: "High-rate fueling and timing capture",
  mode01: { RPM: 1, ENGINE_LOAD: 1 },
  mode22: [],
  bus_budget_override: null,
  broadcast_decode: true,
  corpus_eligible: false,
  storage_prefix: "_capture-fuel-map",
};

function status(activeName: string | null): StatusResponse {
  return {
    capture_profile: activeName
      ? {
          active: true,
          name: activeName,
          started_at: "2026-07-31T10:00:00+00:00",
          session_dir: `_capture-${activeName}-2026-07-31T100000`,
          source: "control",
          channels: [],
          mode01: {},
          mode22: [],
          broadcast_decode: true,
          corpus_eligible: false,
          bus_budget_override: null,
        }
      : null,
  } as unknown as StatusResponse;
}

function renderPanel(activeName: string | null = null) {
  mocks.getCaptureProfiles.mockResolvedValue({
    schema_version: 1,
    profiles: [DEFAULT_PROFILE, FUEL_MAP],
  });
  mocks.getObdCoverage.mockResolvedValue({
    rows: [
      { channel: "RPM", status: "decoded" },
      { channel: "ENGINE_LOAD", status: "decoded" },
      { channel: "NOT_SUPPORTED", status: "unsupported" },
    ],
  });
  mocks.getDids.mockResolvedValue({ dids: [{ name: "ECM_AF_SENSOR_V_B1" }] });
  return render(
    createElement(CaptureProfilesPanel, { token: "secret", status: status(activeName) }),
  );
}

describe("CaptureProfilesPanel (capture-profiles.md 3.2)", () => {
  it("lists saved profiles and offers only decoded channels", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    expect(await screen.findByText("RPM")).toBeInTheDocument();
    // The picker is sourced from the live inventory, so a channel the ECU
    // does not answer must never be offered as pollable.
    expect(screen.queryByText("NOT_SUPPORTED")).not.toBeInTheDocument();
  });

  it("starts a capture with the selected profile", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(mocks.startProfileCapture).toHaveBeenCalled());
    expect(mocks.startProfileCapture.mock.calls[0][1]).toBe("fuel-map");
  });

  it("refuses to start the reserved default profile", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    // `default` is normal collection, not a bounded capture.
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("blocks deleting the profile that is currently capturing", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });

  it("stops an active capture", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(mocks.stopProfileCapture).toHaveBeenCalled());
  });
});
