import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mockHttp } from "../test/harness";
import type { CaptureProfile, StatusResponse } from "../types";

const mocks = mockHttp([
  "pauseProfileCapture",
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
  mode01_enabled: true,
  mode22_enabled: true,
  did_interval_seconds: null,
  round_timestamps: true,
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
  mode01_enabled: true,
  mode22_enabled: true,
  did_interval_seconds: null,
  round_timestamps: true,
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
          mode01: { RPM: 1, ENGINE_LOAD: 1 },
          mode22: [],
          rows_written: 12345,
          mode01_enabled: true,
          mode22_enabled: true,
          did_interval_seconds: null,
          round_timestamps: true,
          paused: false,
          broadcast_decode: true,
          corpus_eligible: false,
          bus_budget_override: null,
        }
      : null,
  } as unknown as StatusResponse;
}

function liveChannels() {
  return new Map([
    ["obd:RPM", { latest: { value: 812.5, unit: "rpm" }, history: [] }],
    ["obd:ENGINE_LOAD", { latest: { value: 19.6, unit: "%" }, history: [] }],
  ]) as never;
}

function renderPanel(activeName: string | null = null, extra: Record<string, unknown> = {}) {
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
    createElement(CaptureProfilesPanel, {
      token: "secret",
      status: status(activeName),
      ...extra,
    }),
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

    fireEvent.click(screen.getByRole("button", { name: "End capture" }));
    await waitFor(() => expect(mocks.stopProfileCapture).toHaveBeenCalled());
  });

  it("names a new profile after the current date and time", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "New" }));

    // A slug the backend name validator accepts: `2026-07-31-1830`.
    const nameField = screen.getByDisplayValue(/^\d{4}-\d{2}-\d{2}-\d{4}$/);
    expect(nameField).toBeInTheDocument();
    const name = (nameField as HTMLInputElement).value;
    expect(screen.getByDisplayValue(`_capture-${name}`)).toBeInTheDocument();
  });

  it("carries an auto-derived storage prefix through a rename", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "New" }));
    const nameField = screen.getByDisplayValue(/^\d{4}-\d{2}-\d{2}-\d{4}$/);
    fireEvent.change(nameField, { target: { value: "dyno-pulls" } });

    // Otherwise captures land under the name the profile used to have.
    expect(screen.getByDisplayValue("_capture-dyno-pulls")).toBeInTheDocument();
  });

  it("disables a whole service without discarding its selection", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });

    const didToggle = screen.getByLabelText("enable Manufacturer DIDs (Mode 22)");
    const did = screen.getByRole("checkbox", { name: "ECM_AF_SENSOR_V_B1" });
    fireEvent.click(did);
    expect(did).toBeChecked();

    // Switching the service off greys the list but keeps the ticks, so
    // re-enabling doesn't mean rebuilding the selection.
    fireEvent.click(didToggle);
    expect(did).toBeChecked();
    expect(did).toBeDisabled();

    fireEvent.click(didToggle);
    expect(did).toBeChecked();
    expect(did).toBeEnabled();
  });

  it("labels each service by both its common and protocol name", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    expect(screen.getByText("OBD PIDs (Mode 01)")).toBeInTheDocument();
    expect(screen.getByText("Manufacturer DIDs (Mode 22)")).toBeInTheDocument();
  });

  it("hides per-channel rate weights unless asked for", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });

    // A capture normally wants every selected channel sampled together, so
    // uneven rates are an advanced control rather than the default view.
    expect(screen.queryByLabelText("RPM rate weight")).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(/per-channel rate weights/));
    expect(screen.getByLabelText("RPM rate weight")).toBeInTheDocument();
  });

  it("pauses and resumes a running capture", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: /Pause/ }));
    await waitFor(() => expect(mocks.pauseProfileCapture).toHaveBeenCalled());
    expect(mocks.pauseProfileCapture.mock.calls[0][1]).toBe(true);
  });

  it("uses the spacebar as a pause hotkey", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.keyDown(document.body, { code: "Space" });
    await waitFor(() => expect(mocks.pauseProfileCapture).toHaveBeenCalled());
  });

  it("leaves the spacebar alone while the operator is typing", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    // Typing a space in any field must not toggle recording.
    fireEvent.keyDown(screen.getByPlaceholderText("ROM / tune revision note"), { code: "Space" });
    expect(mocks.pauseProfileCapture).not.toHaveBeenCalled();
  });

  it("shows rows recorded and the live value of each captured channel", async () => {
    renderPanel("fuel-map", { channels: liveChannels() });
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    expect(screen.getByText("RECORDING")).toBeInTheDocument();
    expect(screen.getByText("12,345 rows")).toBeInTheDocument();
    expect(screen.getByText("812.50")).toBeInTheDocument();
    expect(screen.getByText("19.60")).toBeInTheDocument();
  });
});
