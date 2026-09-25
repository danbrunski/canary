import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CAPTURE_API,
  installCaptureMocks,
  status as captureStatus,
} from "../../test/captureFixtures";
import { mockHttp } from "../../test/harness";
import type { StatusResponse } from "../../types";

const mocks = mockHttp([
  ...CAPTURE_API,
  "getCurrentCapture",
  "tagCapture",
  "getPowerStatus",
  "setKeepAlive",
]);

const { LoggingPage } = await import("./LoggingPage");

const CAPTURE = { capture_id: "cap-7", purpose: "pulls", started_at: "2026-09-24T10:00:00Z" };

function renderPage(status: StatusResponse, onNavigate = vi.fn()) {
  installCaptureMocks(mocks);
  // The power card is not under test here; leave it loading.
  mocks.getPowerStatus.mockReturnValue(new Promise(() => {}));
  return {
    onNavigate,
    ...render(
      <LoggingPage token="secret" status={status} channels={new Map()} onNavigate={onNavigate} />,
    ),
  };
}

describe("LoggingPage (R08.6)", () => {
  beforeEach(() => window.localStorage.clear());

  it("runs logs through the capture runner, never the profile editor", async () => {
    mocks.getCurrentCapture.mockResolvedValue({ capture: null });
    renderPage(captureStatus(null));
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    expect(screen.getByTestId("capture-controls")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New profile" })).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(mocks.startProfileCapture).toHaveBeenCalledWith("secret", "fuel-map", undefined),
    );
  });

  it("disables the physical-action tags until a capture is open", async () => {
    mocks.getCurrentCapture.mockResolvedValue({ capture: null });
    renderPage(captureStatus(null));
    expect(await screen.findByText("start a capture to tag")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pull start" })).toBeDisabled();
  });

  it("tags a physical action against the open capture at tap time", async () => {
    mocks.getCurrentCapture.mockResolvedValue({ capture: CAPTURE });
    mocks.tagCapture.mockResolvedValue({});
    renderPage(captureStatus("fuel-map"));
    const pull = await screen.findByRole("button", { name: "Pull start" });
    await waitFor(() => expect(pull).toBeEnabled());
    fireEvent.click(pull);
    await waitFor(() => expect(mocks.tagCapture).toHaveBeenCalled());
    const [token, tag] = mocks.tagCapture.mock.calls[0] as [string, Record<string, unknown>];
    expect(token).toBe("secret");
    expect(tag).toMatchObject({ label: "tuning_pull_start", kind: "physical_action" });
    expect(typeof tag.timestamp).toBe("number");
    expect(await screen.findByRole("button", { name: "Pull start (1)" })).toBeInTheDocument();
  });

  it("says when the tag status cannot be read", async () => {
    const { HttpErrorStub } = await import("../../test/harness");
    const refused = Object.assign(new HttpErrorStub("HTTP 401"), { status: 401 });
    mocks.getCurrentCapture.mockRejectedValue(refused);
    renderPage(captureStatus(null));
    expect(await screen.findByText(/Tag status:/)).toBeInTheDocument();
  });

  it("warns about key-on-engine-off and low storage", async () => {
    mocks.getCurrentCapture.mockResolvedValue({ capture: null });
    const status = {
      ...captureStatus(null),
      vehicle_state: { state: "ignition_only" },
      storage_warning_mb: 500,
      free_mb: 120,
    } as unknown as StatusResponse;
    renderPage(status);
    expect(await screen.findByText(/Key on, engine off/)).toBeInTheDocument();
    expect(screen.getByText(/below the 500 MB warning floor/)).toBeInTheDocument();
  });

  it("links to Logs and to profile editing in System", async () => {
    mocks.getCurrentCapture.mockResolvedValue({ capture: null });
    const { onNavigate } = renderPage(captureStatus(null));
    fireEvent.click(screen.getByRole("button", { name: "View logs" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit profiles in System" }));
    expect(onNavigate.mock.calls).toEqual([["tuning-logs"], ["system-capture-profiles"]]);
  });
});
