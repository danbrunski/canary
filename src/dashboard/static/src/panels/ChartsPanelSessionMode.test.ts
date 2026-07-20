import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelSnapshot } from "../hooks/useTelemetryStream";
import { mockHttp } from "../test/harness";
import type { SessionChartResponse } from "../types";

const mocks = mockHttp(["getSessionChart", "downloadSessionGpx", "downloadSessionKml"]);

const { ChartsPanel } = await import("./ChartsPanel");

const channels = new Map<string, ChannelSnapshot>();

function chart(): SessionChartResponse {
  return {
    session_id: "2026-07-19T000000",
    resampled: true,
    hz: 1.0,
    channels: {
      "obd:RPM": {
        timestamps: ["2026-07-19T00:00:00Z", "2026-07-19T00:00:01Z"],
        values: [800, 810],
      },
    },
  };
}

beforeEach(() => {
  // jsdom doesn't implement the Blob URL API; the GPX/KML export path only
  // needs it to exist, not to resolve to a real object URL.
  URL.createObjectURL = vi.fn(() => "blob:mock");
  URL.revokeObjectURL = vi.fn();
});

describe("ChartsPanel session mode (Task 5.3)", () => {
  it("loads a stored session and charts it", async () => {
    mocks.getSessionChart.mockResolvedValue(chart());
    render(createElement(ChartsPanel, { channels, token: "secret" }));

    fireEvent.click(screen.getByRole("button", { name: "session" }));
    fireEvent.change(screen.getByPlaceholderText("2026-07-19T000000"), {
      target: { value: "2026-07-19T000000" },
    });
    fireEvent.change(screen.getByPlaceholderText("obd:RPM,obd:ENGINE_LOAD"), {
      target: { value: "obd:RPM" },
    });
    fireEvent.click(screen.getByRole("button", { name: "load" }));

    await screen.findByText((t) => t.includes("obd:RPM"));
    expect(mocks.getSessionChart).toHaveBeenCalledWith(
      "secret",
      "2026-07-19T000000",
      ["obd:RPM"],
      1,
    );
  });

  it("requires a token before loading a session", async () => {
    render(createElement(ChartsPanel, { channels, token: "" }));
    fireEvent.click(screen.getByRole("button", { name: "session" }));
    fireEvent.change(screen.getByPlaceholderText("2026-07-19T000000"), {
      target: { value: "2026-07-19T000000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "load" }));
    expect(await screen.findByText(/control token/)).toBeInTheDocument();
    expect(mocks.getSessionChart).not.toHaveBeenCalled();
  });

  it("exports the session's GPS track as GPX", async () => {
    mocks.downloadSessionGpx.mockResolvedValue(new Blob(["<gpx/>"]));
    render(createElement(ChartsPanel, { channels, token: "secret" }));
    fireEvent.click(screen.getByRole("button", { name: "session" }));
    fireEvent.change(screen.getByPlaceholderText("2026-07-19T000000"), {
      target: { value: "2026-07-19T000000" },
    });

    fireEvent.click(screen.getByRole("button", { name: "GPX" }));

    await screen.findByText(/export gpx completed/i);
    expect(mocks.downloadSessionGpx).toHaveBeenCalledWith("secret", "2026-07-19T000000");
  });
});
