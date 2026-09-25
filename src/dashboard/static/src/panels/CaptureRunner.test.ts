import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  CAPTURE_API,
  HYBRID,
  installCaptureMocks,
  liveChannels,
  status,
} from "../test/captureFixtures";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(CAPTURE_API);

// Dynamic, after mockHttp: a static import would load the real api/http.
const { CaptureRunner, capturePhase } = await import("./CaptureRunner");

function renderPanel(activeName: string | null = null, extra: Record<string, unknown> = {}) {
  installCaptureMocks(mocks);
  return render(
    createElement(CaptureRunner, {
      token: "secret",
      status: status(activeName),
      ...extra,
    }),
  );
}

describe("capturePhase (R08.6)", () => {
  it("names an in-flight transition before the settled state", () => {
    const recording = { active: true, paused: false };
    expect(capturePhase("capture start", null)).toBe("starting");
    expect(capturePhase("capture stop", recording)).toBe("stopping");
    expect(capturePhase("capture pause", recording)).toBe("pausing");
    expect(capturePhase("capture resume", { active: true, paused: true })).toBe("resuming");
    expect(capturePhase(null, recording)).toBe("recording");
    expect(capturePhase(null, { active: true, paused: true })).toBe("paused");
    expect(capturePhase(null, { active: false, paused: false })).toBe("idle");
    expect(capturePhase("some other action", null)).toBe("idle");
  });
});

describe("CaptureRunner — Engine Tuning → Logging (capture-profiles.md 3.2)", () => {
  it("is the Logging panel, with no profile editor", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    expect(screen.getByRole("heading", { name: "Logging" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New profile" })).not.toBeInTheDocument();
    expect(screen.queryByText("OBD PIDs (Mode 01)")).not.toBeInTheDocument();
  });

  it("starts a capture with the selected profile", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    await waitFor(() => expect(mocks.startProfileCapture).toHaveBeenCalled());
    expect(mocks.startProfileCapture.mock.calls[0][1]).toBe("fuel-map");
  });

  it("shows resolved batched and fallback demand as distinct units", async () => {
    renderPanel();
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "fuel-map" } });

    expect(await screen.findByText("44 channel reads/s")).toBeInTheDocument();
    expect(screen.getByText("9 requests/s batched")).toBeInTheDocument();
    expect(screen.getByText("44 requests/s fallback")).toBeInTheDocument();
    expect(screen.getByText(/Single-request fallback exceeds/)).toBeInTheDocument();
  });

  it("shows and starts the experimental hybrid plan without hiding its policy", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "tuning-hybrid-research" },
    });

    const plan = await screen.findByTestId("capture-plan");
    expect(plan).toHaveTextContent("Experimental");
    expect(plan).toHaveTextContent("4 Hz/channel");
    expect(plan).toHaveTextContent("Mode 01 / Mode 22 alternating");
    expect(plan).toHaveTextContent("DID window every 0.25s");
    expect(plan).toHaveTextContent("3 behavior-model recipes allowed");
    expect(plan).toHaveTextContent("10 81 restored Mode 01 on the next request");
    // The runner lists what the saved profile polls; it has no channel picker.
    expect(plan).toHaveTextContent("Mode 22: ECM_AF_SENSOR_V_B1, ECM_BASE_FUEL_SCHEDULE_CANDIDATE");

    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(mocks.startProfileCapture).toHaveBeenCalledWith(
        "secret",
        "tuning-hybrid-research",
        undefined,
      ),
    );
  });

  it("shows achieved rates, gaps, and fast-exit failures for an active capture", async () => {
    const hybridStatus = status("tuning-hybrid-research");
    hybridStatus.capture_profile = {
      ...hybridStatus.capture_profile!,
      ...HYBRID,
      active: true,
      source: "control",
      channels: Object.keys(HYBRID.mode01),
      started_at: "2026-08-07T12:00:00+00:00",
      session_dir: "_capture-tuning-hybrid-research-2026-08-07T120000",
      paused: false,
    };
    hybridStatus.health = {
      observed: 1,
      inference_latency_ms: { mean: null, p95: null, max: null },
      missing_feature_rate: null,
      data_freshness_seconds: 0.1,
      model_swaps: {},
      active_model_version: null,
      system: {},
      obd_scheduler: {
        achieved_hz_capture: { RPM: 3.95 },
        capture_max_gap_seconds: { RPM: 0.31 },
        did_profile: {
          phase: "mode01",
          fast_exits: 8,
          fast_exit_failures: 1,
          fast_exit_supported: false,
          windows_opened: 9,
          windows_completed: 8,
        },
      },
    };
    renderPanel("tuning-hybrid-research", { status: hybridStatus });

    expect(await screen.findByText(/fast exits 8/)).toHaveTextContent("failures 1");
    expect(screen.getByText(/fast exit unavailable/)).toBeInTheDocument();
    expect(screen.getByText("3.95")).toBeInTheDocument();
    expect(screen.getByText("0.31 s")).toBeInTheDocument();
  });

  it("says a disabled fast exit is recovering, not dead", async () => {
    // "unavailable" read as permanent, so a self-healing degradation looked
    // like a capture that needed a restart. Name the NRC and the countdown.
    const hybridStatus = status("tuning-hybrid-research");
    hybridStatus.health = {
      observed: 1,
      inference_latency_ms: { mean: null, p95: null, max: null },
      missing_feature_rate: null,
      data_freshness_seconds: 0.1,
      model_swaps: {},
      active_model_version: null,
      system: {},
      obd_scheduler: {
        did_profile: {
          phase: "lapse",
          fast_exits: 139,
          fast_exit_failures: 2,
          fast_exit_supported: false,
          fast_exit_retry_in_seconds: 27.4,
          last_exit_nrc_name: "subFunctionNotSupported",
          windows_opened: 145,
          windows_completed: 145,
        },
      },
    };
    renderPanel("tuning-hybrid-research", { status: hybridStatus });

    const banner = await screen.findByText(/fast exits 139/);
    expect(banner).toHaveTextContent("Mode 01 degraded (subFunctionNotSupported)");
    expect(banner).toHaveTextContent("retrying in 28s");
    expect(screen.queryByText(/fast exit unavailable/)).not.toBeInTheDocument();
  });

  it("refuses to start the reserved default profile", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    // `default` is normal collection, not a bounded capture.
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("stops an active capture", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(mocks.stopProfileCapture).toHaveBeenCalled());
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

  it("lists selected diagnostic channels while waiting for this log's first sample", async () => {
    const captureStatus = status("fuel-map");
    captureStatus.capture_profile = {
      ...captureStatus.capture_profile!,
      mode22: ["ECM_AF_SENSOR_V_B1"],
      memory: ["ECM_TARGET_EQUIV_RATIO"],
    };
    const channels = new Map([
      [
        "did:ECM_AF_SENSOR_V_B1",
        {
          latest: { value: 2.5, unit: "V", timestamp: "2026-07-30T10:00:00+00:00" },
          history: [],
        },
      ],
    ]);
    renderPanel("fuel-map", { status: captureStatus, channels });
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    expect(screen.getByText("did:ECM_AF_SENSOR_V_B1").closest("tr")).toHaveTextContent(
      "Waiting for sample",
    );
    expect(screen.getByText("mem:ECM_TARGET_EQUIV_RATIO").closest("tr")).toHaveTextContent(
      "Waiting for sample",
    );
  });

  it("shows only channels from enabled polling services", async () => {
    const captureStatus = status("fuel-map");
    captureStatus.capture_profile = {
      ...captureStatus.capture_profile!,
      mode22: ["ECM_AF_SENSOR_V_B1"],
      memory: ["ECM_TARGET_EQUIV_RATIO"],
      mode22_enabled: false,
    };
    renderPanel("fuel-map", { status: captureStatus, channels: liveChannels() });
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    expect(screen.getAllByText("RPM").length).toBeGreaterThan(0);
    expect(screen.queryByText("did:ECM_AF_SENSOR_V_B1")).not.toBeInTheDocument();
    expect(screen.queryByText("mem:ECM_TARGET_EQUIV_RATIO")).not.toBeInTheDocument();
  });

  it("shows the capture state without waiting for the next status poll", async () => {
    // /status polls every 5 s. Before this, a capture that had already started
    // still read "Not capturing" for up to five seconds, which is the whole
    // "it takes a while to respond" complaint — the request was fast, the UI
    // was not. The mutation response carries the new snapshot; use it.
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    expect(screen.getByTestId("capture-phase")).toHaveTextContent("Collection status unavailable");

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));

    // The `status` prop is deliberately never updated in this test.
    await waitFor(() => expect(screen.getByTestId("capture-phase")).toHaveTextContent("Recording"));
    expect(screen.getByRole("button", { name: /Pause/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop" })).toBeInTheDocument();
  });

  it("reports a pause immediately rather than on the next poll", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    expect(screen.getByTestId("capture-phase")).toHaveTextContent("Recording");

    fireEvent.click(screen.getByRole("button", { name: /Pause/ }));

    await waitFor(() => expect(screen.getByTestId("capture-phase")).toHaveTextContent("Paused"));
    expect(screen.getByRole("button", { name: /Resume/ })).toBeInTheDocument();
  });

  it("surfaces the contract verdict when a capture ends", async () => {
    // Stopping used to report nothing at all. The verdict is the answer to
    // "was that capture any good", and it is only available at this moment.
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    // After renderPanel: it installs the default mocks and would overwrite this.
    mocks.stopProfileCapture.mockResolvedValue({
      capture_profile: { ...status(null), active: false, name: "default", paused: false },
      capture_quality: {
        schema_version: 2,
        scope: "stop",
        verdict: "fail",
        profile: "fuel-map",
        interval: {
          source: "live",
          elapsed_s: 1434,
          intended_active_s: 1434,
          paused_s: 0,
          pause_windows: [],
        },
        required_tuple: ["RPM", "COMMANDED_EQUIV_RATIO"],
        tuple_rate_bound_hz: 0.28,
        tuples: {
          required: ["RPM", "COMMANDED_EQUIV_RATIO"],
          max_tuple_skew_s: 0.125,
          rate_bound_hz: 0.28,
          source: "persisted_rows",
          scheduled: null,
          attempted: 1120,
          complete: 2,
          persisted: 2,
          incomplete: 1118,
          skew_rejected: 0,
        },
        failures: [{ clause: "rate_shortfall", channels: ["COMMANDED_EQUIV_RATIO"] }],
        channels: [],
        losses: {},
        persistence: null,
        trailing: { window_s: 60, observed_seconds: 60, hz: {} },
        notes: [],
      },
    });

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));

    const verdict = await screen.findByTestId("capture-verdict");
    expect(verdict).toHaveTextContent("contract fail");
    expect(verdict).toHaveTextContent("scope: stop");
    expect(verdict).toHaveTextContent("rate_shortfall");
    expect(verdict).toHaveTextContent("2 complete tuples of 1120 rounds");
    expect(verdict).toHaveTextContent("0.28 Hz (bound)");
  });
});
