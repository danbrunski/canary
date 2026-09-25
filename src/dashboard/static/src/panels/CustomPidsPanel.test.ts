import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CustomPidsResponse } from "../types";

const mocks = vi.hoisted(() => ({
  getCustomPids: vi.fn(),
  addCustomPid: vi.fn(),
  deleteCustomPid: vi.fn(),
  importCustomPidsCsv: vi.fn(),
}));

// `describeError` narrows on both error classes, so the mocked module has
// to export both — vitest throws on `instanceof` against a missing export.
vi.mock("../api/http", () => ({
  ...mocks,
  HttpError: class extends Error {
    status = 0;
    body = "";
  },
  NetworkError: class extends Error {
    url = "";
  },
}));

const { CustomPidsPanel } = await import("./CustomPidsPanel");

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
});
afterEach(() => vi.clearAllMocks());

const EMPTY: CustomPidsResponse = { custom_pids: [], count: 0 };

const ONE_PID: CustomPidsResponse = {
  count: 1,
  custom_pids: [
    {
      name: "OIL_PRESS",
      display_name: "Oil pressure",
      mode: 0x22,
      pid: 0x1234,
      formula: "A * 0.5",
      unit: "psi",
      category: "custom",
      minimum: null,
      maximum: null,
      poll_tier: "medium",
      dashboard_hint: "graph",
      header: null,
    },
  ],
};

describe("CustomPidsPanel", () => {
  it("prompts for a token without one", () => {
    render(createElement(CustomPidsPanel, { token: "" }));
    expect(screen.getByText(/Enter the control token/)).toBeInTheDocument();
    expect(mocks.getCustomPids).not.toHaveBeenCalled();
  });

  it("lists defined custom PIDs with mode/pid/formula", async () => {
    mocks.getCustomPids.mockResolvedValue(ONE_PID);
    render(createElement(CustomPidsPanel, { token: "secret" }));
    expect(await screen.findByText("Oil pressure")).toBeInTheDocument();
    expect(
      screen.getByText(/OIL_PRESS · mode 0x22 pid 0x1234 · A \* 0.5 \(psi\)/),
    ).toBeInTheDocument();
  });

  it("shows the empty-state placeholder with no custom PIDs", async () => {
    mocks.getCustomPids.mockResolvedValue(EMPTY);
    render(createElement(CustomPidsPanel, { token: "secret" }));
    expect(await screen.findByText(/No custom PIDs defined yet/)).toBeInTheDocument();
  });

  it("adds a custom PID from the form", async () => {
    mocks.getCustomPids.mockResolvedValue(EMPTY);
    mocks.addCustomPid.mockResolvedValue({ custom_pid: ONE_PID.custom_pids[0] });
    render(createElement(CustomPidsPanel, { token: "secret" }));
    await screen.findByText(/No custom PIDs defined yet/);

    fireEvent.change(screen.getByLabelText("Custom PID name"), {
      target: { value: "OIL_PRESS" },
    });
    fireEvent.change(screen.getByLabelText("Custom PID display name"), {
      target: { value: "Oil pressure" },
    });
    fireEvent.change(screen.getByLabelText("Custom PID mode"), { target: { value: "22" } });
    fireEvent.change(screen.getByLabelText("Custom PID number"), { target: { value: "1234" } });
    fireEvent.change(screen.getByLabelText("Custom PID formula"), {
      target: { value: "A * 0.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add custom PID" }));

    await waitFor(() => expect(mocks.addCustomPid).toHaveBeenCalledOnce());
    expect(mocks.addCustomPid).toHaveBeenCalledWith(
      "secret",
      expect.objectContaining({
        name: "OIL_PRESS",
        display_name: "Oil pressure",
        mode: 0x22,
        pid: 0x1234,
        formula: "A * 0.5",
      }),
    );
  });

  it("rejects an add with a missing formula before calling the API", async () => {
    mocks.getCustomPids.mockResolvedValue(EMPTY);
    render(createElement(CustomPidsPanel, { token: "secret" }));
    await screen.findByText(/No custom PIDs defined yet/);
    fireEvent.change(screen.getByLabelText("Custom PID name"), { target: { value: "X" } });
    fireEvent.change(screen.getByLabelText("Custom PID display name"), {
      target: { value: "X" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add custom PID" }));
    expect(await screen.findByText(/required/)).toBeInTheDocument();
    expect(mocks.addCustomPid).not.toHaveBeenCalled();
  });

  it("deletes a custom PID after confirmation", async () => {
    mocks.getCustomPids.mockResolvedValue(ONE_PID);
    mocks.deleteCustomPid.mockResolvedValue({ name: "OIL_PRESS", deleted: true });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(createElement(CustomPidsPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(mocks.deleteCustomPid).toHaveBeenCalledOnce());
    expect(mocks.deleteCustomPid).toHaveBeenCalledWith("secret", "OIL_PRESS");
  });

  it("skips the delete when the confirmation is declined", async () => {
    mocks.getCustomPids.mockResolvedValue(ONE_PID);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(createElement(CustomPidsPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await waitFor(() => expect(window.confirm).toHaveBeenCalledOnce());
    expect(mocks.deleteCustomPid).not.toHaveBeenCalled();
  });

  it("imports a pasted Torque CSV and reports the summary", async () => {
    mocks.getCustomPids.mockResolvedValue(EMPTY);
    mocks.importCustomPidsCsv.mockResolvedValue({
      added: [ONE_PID.custom_pids[0]],
      added_count: 1,
      warnings: [],
    });
    render(createElement(CustomPidsPanel, { token: "secret" }));
    await screen.findByText(/No custom PIDs defined yet/);
    fireEvent.click(screen.getByRole("button", { name: "Import CSV…" }));
    fireEvent.change(screen.getByLabelText("Torque CSV text"), {
      target: { value: "Name,ShortName,ModeAndPID,Equation\nOil Press,OIL_PRESS,220F40,A*0.5\n" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(mocks.importCustomPidsCsv).toHaveBeenCalledOnce());
    expect(await screen.findByText(/Imported 1 PID/)).toBeInTheDocument();
  });
});
