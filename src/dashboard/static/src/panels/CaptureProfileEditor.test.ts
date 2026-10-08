import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CAPTURE_API, installCaptureMocks, status } from "../test/captureFixtures";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(CAPTURE_API);

const { CaptureProfileEditor } = await import("./CaptureProfileEditor");

function renderPanel(activeName: string | null = null, extra: Record<string, unknown> = {}) {
  installCaptureMocks(mocks);
  return render(
    createElement(CaptureProfileEditor, {
      token: "secret",
      status: status(activeName),
      ...extra,
    }),
  );
}

describe("CaptureProfileEditor — System → Capture profiles (capture-profiles.md 3.2)", () => {
  it("edits profiles and never starts one: an unsaved draft cannot be launched", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    expect(screen.getByRole("heading", { name: "Capture profiles" })).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    await screen.findByText("High-rate fueling and timing capture");
    fireEvent.change(screen.getByLabelText("Target rate"), { target: { value: "8" } });

    expect(screen.queryByRole("button", { name: "Start" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("capture-controls")).not.toBeInTheDocument();
    expect(screen.getByText(/Start a log from Engine Tuning → Logging/)).toBeInTheDocument();
    expect(mocks.startProfileCapture).not.toHaveBeenCalled();
  });

  it("reloads a saved profile after New profile when it is picked again", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    expect(await screen.findByText(/editing .fuel-map./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    expect(await screen.findByText(/editing .fuel-map./)).toBeInTheDocument();
    expect(screen.getByDisplayValue("_capture-fuel-map")).toBeInTheDocument();
  });

  it("lists saved profiles and offers only decoded channels", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    expect(await screen.findByText("RPM")).toBeInTheDocument();
    // The picker is sourced from the live inventory, so a channel the ECU
    // does not answer must never be offered as pollable.
    expect(screen.queryByText("NOT_SUPPORTED")).not.toBeInTheDocument();
  });

  it("shows RAM reads in the editor and saves them with the profile", async () => {
    renderPanel();
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "fuel-map" } });
    const target = screen.getByRole("checkbox", { name: "ECM_TARGET_EQUIV_RATIO" });
    fireEvent.click(target);
    expect(target).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mocks.saveCaptureProfile).toHaveBeenCalled());
    expect(mocks.saveCaptureProfile.mock.calls.at(-1)?.[1].memory).toEqual([
      "ECM_TARGET_EQUIV_RATIO",
    ]);
  });

  it("shows resolved batched and fallback demand as distinct units", async () => {
    renderPanel();
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "fuel-map" } });

    expect(await screen.findByText("44 channel reads/s")).toBeInTheDocument();
    expect(screen.getByText("9 requests/s batched")).toBeInTheDocument();
    expect(screen.getByText("44 requests/s fallback")).toBeInTheDocument();
    expect(screen.getByText(/Single-request fallback exceeds/)).toBeInTheDocument();
  });

  it("blocks deleting the profile that is currently capturing", async () => {
    renderPanel("fuel-map");
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });

  it("names a new profile after the current date and time", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "New profile" }));

    // A slug the backend name validator accepts: `2026-07-31-1830`.
    const nameField = screen.getByDisplayValue(/^\d{4}-\d{2}-\d{2}-\d{4}$/);
    expect(nameField).toBeInTheDocument();
    const name = (nameField as HTMLInputElement).value;
    expect(screen.getByDisplayValue(`_capture-${name}`)).toBeInTheDocument();
  });

  it("carries an auto-derived storage prefix through a rename", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
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

  it("edits persisted DID weights under the selected profile name", async () => {
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "tuning-hybrid-research" },
    });
    fireEvent.click(screen.getByLabelText(/per-channel rate weights/));

    const weight = screen.getByLabelText("ECM_BASE_FUEL_SCHEDULE_CANDIDATE rate weight");
    expect(weight).toHaveValue(0.25);
    fireEvent.change(weight, { target: { value: "0.5" } });
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));

    await waitFor(() => expect(mocks.saveCaptureProfile).toHaveBeenCalled());
    expect(mocks.saveCaptureProfile.mock.calls.at(-1)?.[1].mode22_rate_weights).toEqual({
      ECM_BASE_FUEL_SCHEDULE_CANDIDATE: 0.5,
    });
  });

  it("drops a memory channel's weight when the channel is deselected", async () => {
    // The daemon refuses a weight on a channel `memory:` does not select
    // (D03.38), so a stale weight would turn the next Save into an error.
    renderPanel();
    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "fuel-map" } });
    fireEvent.click(screen.getByLabelText(/per-channel rate weights/));
    fireEvent.click(screen.getByRole("checkbox", { name: "ECM_TARGET_EQUIV_RATIO" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "ECM_ENGINE_PERIOD" }));
    fireEvent.change(screen.getByLabelText("ECM_TARGET_EQUIV_RATIO rate weight"), {
      target: { value: "0.25" },
    });
    fireEvent.change(screen.getByLabelText("ECM_ENGINE_PERIOD rate weight"), {
      target: { value: "0.5" },
    });
    // A selected row's name now includes its weight input's value.
    fireEvent.click(screen.getByRole("checkbox", { name: /^ECM_TARGET_EQUIV_RATIO\b/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Save$/ }));

    await waitFor(() => expect(mocks.saveCaptureProfile).toHaveBeenCalled());
    const saved = mocks.saveCaptureProfile.mock.calls.at(-1)?.[1];
    expect(saved.memory).toEqual(["ECM_ENGINE_PERIOD"]);
    expect(saved.memory_rate_weights).toEqual({ ECM_ENGINE_PERIOD: 0.5 });
  });

  it("says whether the editor is on a saved profile or an unsaved new one", async () => {
    // "New" gave no clue whether it was required before every capture. The
    // editor now states which profile it is editing, and the button says what
    // it makes.
    renderPanel();
    await waitFor(() => expect(mocks.getCaptureProfiles).toHaveBeenCalled());
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "fuel-map" } });
    expect(await screen.findByText(/editing .fuel-map./)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New profile" }));
    expect(screen.getByText(/new, unsaved profile/)).toBeInTheDocument();
  });
});
