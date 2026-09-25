// Behaviour of ControlPanel's Capture section, pinned before R08.12 moved it
// onto <Panel>: the header states idle/active, Start sends the typed fields,
// Tag and Stop act on the open window, and every action waits for a token.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { mockHttp } from "../test/harness";
import type { CaptureState } from "../types";

const mocks = mockHttp([
  "getPreflight",
  "getControlVersion",
  "getCurrentCapture",
  "listJobs",
  "getSequenceDetector",
  "startCapture",
  "stopCapture",
  "tagCapture",
  "cancelJob",
  "getJobLogs",
  "submitJob",
  "uploadModelArtifact",
  "switchSequenceDetector",
]);
vi.doMock("../api/websocket", () => ({
  openReconnectingSocket: () => ({ close: () => undefined }),
}));

const { ControlPanel } = await import("./ControlPanel");

const COMPANION = { baseUrl: "", token: "", setBaseUrl: () => {}, setToken: () => {} };

const ACTIVE: CaptureState = {
  capture_id: "c1",
  session_id: "_capture-drive-2026-09-25T120000",
  session_path: "/data/canary/_capture-drive-2026-09-25T120000",
  started_at: "2026-09-25T12:00:00Z",
  purpose: "fuel pulls",
  protocol: "drive",
  vehicle_state: "driving",
};

function serve(capture: CaptureState | null): void {
  mocks.getPreflight.mockResolvedValue({
    ok: true,
    checked_at: "2026-09-25T12:00:00Z",
    checks: [],
  });
  mocks.getControlVersion.mockResolvedValue({
    api_version: 1,
    package_version: "0",
    git_sha: null,
    config_token_env: "CANARY_CONTROL_TOKEN",
  });
  mocks.getCurrentCapture.mockResolvedValue({ capture, active: capture !== null, detail: null });
  mocks.listJobs.mockResolvedValue({ jobs: [], count: 0 });
  mocks.getSequenceDetector.mockResolvedValue({
    sequence_detector: { selector: "tcn", active: "tcn", active_version: null, available: {} },
  });
}

/** The Capture panel: the <section> whose <header> holds the "Capture" <h2>. */
function capturePanel(): HTMLElement {
  const heading = screen.getByRole("heading", { level: 2, name: "Capture" });
  const header = heading.closest("header");
  expect(header).not.toBeNull();
  const section = header!.parentElement!;
  expect(section.tagName).toBe("SECTION");
  return section;
}

function button(panel: HTMLElement, name: string): HTMLButtonElement {
  return within(panel).getByRole("button", { name });
}

describe("ControlPanel capture section", () => {
  beforeEach(() => {
    mocks.startCapture.mockResolvedValue({ capture: ACTIVE });
    mocks.tagCapture.mockResolvedValue({ tag: {}, capture: ACTIVE });
    mocks.stopCapture.mockResolvedValue({ capture: null });
  });

  it("reads idle and starts a capture with the typed fields", async () => {
    serve(null);
    render(<ControlPanel token="t" companion={COMPANION} />);
    const panel = capturePanel();
    await waitFor(() => expect(mocks.getCurrentCapture).toHaveBeenCalled());
    expect(within(panel.querySelector("header")!).getByText("idle")).toHaveClass("text-fg-dim");

    fireEvent.change(within(panel).getByPlaceholderText("purpose"), {
      target: { value: "fuel pulls" },
    });
    await waitFor(() => expect(button(panel, "Start")).toBeEnabled());
    fireEvent.click(button(panel, "Start"));
    await waitFor(() =>
      expect(mocks.startCapture).toHaveBeenCalledWith("t", {
        purpose: "fuel pulls",
        protocol: "drive",
        vehicle_state: "driving",
        fields: {},
      }),
    );
    // Tag and Stop need an open window.
    expect(button(panel, "Tag")).toBeDisabled();
    expect(button(panel, "Stop")).toBeDisabled();
  });

  it("shows the active window, tags it and stops it", async () => {
    serve(ACTIVE);
    render(<ControlPanel token="t" companion={COMPANION} />);
    const panel = capturePanel();
    await waitFor(() =>
      expect(within(panel.querySelector("header")!).getByText("active")).toHaveClass("text-ok"),
    );
    expect(within(panel).getByText(ACTIVE.session_id)).toBeInTheDocument();
    expect(button(panel, "Start")).toBeDisabled();

    fireEvent.change(within(panel).getByPlaceholderText("tag label"), {
      target: { value: " pull 1 " },
    });
    fireEvent.click(button(panel, "Tag"));
    await waitFor(() =>
      expect(mocks.tagCapture).toHaveBeenCalledWith("t", {
        label: "pull 1",
        kind: "event",
        fields: {},
      }),
    );

    fireEvent.change(within(panel).getByPlaceholderText("stop reason"), {
      target: { value: "parked" },
    });
    await waitFor(() => expect(button(panel, "Stop")).toBeEnabled());
    fireEvent.click(button(panel, "Stop"));
    await waitFor(() =>
      expect(mocks.stopCapture).toHaveBeenCalledWith("t", { reason: "parked", fields: {} }),
    );
  });

  it("disables every capture action without a token", () => {
    render(<ControlPanel token="" companion={COMPANION} />);
    const panel = capturePanel();
    expect(button(panel, "Start")).toBeDisabled();
    expect(button(panel, "Tag")).toBeDisabled();
    expect(button(panel, "Stop")).toBeDisabled();
    expect(mocks.getCurrentCapture).not.toHaveBeenCalled();
  });
});
