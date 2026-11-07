import { createElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PowerSnapshot, PowerStatusResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getPowerStatus", "setKeepAlive"]);

const { PowerPanel } = await import("./PowerPanel");

function snapshot(overrides: Partial<PowerSnapshot> = {}): PowerStatusResponse {
  return {
    power: {
      watchdog_running: true,
      watchdog_stale: false,
      armed: true,
      input_lost: false,
      on_battery: false,
      soc: 91.5,
      vcell: 4.12,
      seconds_lost: 0,
      grace_s: 300,
      keepalive_remaining_s: 0,
      status_age_s: 1.2,
      ...overrides,
    },
  };
}

describe("PowerPanel", () => {
  it("prompts for a token without one", () => {
    render(createElement(PowerPanel, { token: "" }));
    expect(screen.getByText(/needs the control token/)).toBeInTheDocument();
    expect(mocks.getPowerStatus).not.toHaveBeenCalled();
  });

  it("shows watchdog state, SoC, and cell voltage when armed on external power", async () => {
    mocks.getPowerStatus.mockResolvedValue(snapshot());
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("external power")).toBeInTheDocument();
    expect(screen.getByText("91.5%")).toBeInTheDocument();
    expect(screen.getByText("4.12 V")).toBeInTheDocument();
  });

  it("shows 'armed' when on_battery is not yet determined (no gauge data)", async () => {
    mocks.getPowerStatus.mockResolvedValue(snapshot({ on_battery: null }));
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("armed")).toBeInTheDocument();
  });

  it("shows a not-running notice when the watchdog isn't up", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({
        watchdog_running: false,
        watchdog_stale: null,
        armed: null,
        on_battery: null,
        soc: null,
        vcell: null,
        seconds_lost: null,
        grace_s: null,
        status_age_s: null,
      }),
    );
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("not running")).toBeInTheDocument();
    expect(screen.getByText(/isn't running/)).toBeInTheDocument();
  });

  it("shows a shutdown countdown while on battery", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({ on_battery: true, seconds_lost: 45, grace_s: 300 }),
    );
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("on battery")).toBeInTheDocument();
    expect(screen.getByText("4m 15s")).toBeInTheDocument();
  });

  it("sets a keep-alive hold and refreshes", async () => {
    mocks.getPowerStatus.mockResolvedValue(snapshot());
    mocks.setKeepAlive.mockResolvedValue(snapshot({ keepalive_remaining_s: 600 }));
    render(createElement(PowerPanel, { token: "secret" }));
    await screen.findByText("external power");

    const input = screen.getByLabelText("Keep-alive minutes");
    fireEvent.change(input, { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "Keep alive" }));

    await waitFor(() => expect(mocks.setKeepAlive).toHaveBeenCalledWith("secret", 10));
  });

  it("cancels an active keep-alive hold", async () => {
    mocks.getPowerStatus.mockResolvedValue(snapshot({ keepalive_remaining_s: 300 }));
    mocks.setKeepAlive.mockResolvedValue(snapshot({ keepalive_remaining_s: 0 }));
    render(createElement(PowerPanel, { token: "secret" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel hold" }));
    await waitFor(() => expect(mocks.setKeepAlive).toHaveBeenCalledWith("secret", 0));
  });
});
