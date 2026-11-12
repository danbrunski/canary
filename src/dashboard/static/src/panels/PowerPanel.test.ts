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
      discharging: false,
      discharge_rate_pct_per_hour: 0,
      min_discharge_rate_pct_per_hour: 6,
      soc_critical: false,
      critical_soc_pct: 15,
      shutdown_state: "input_present",
      countdown_reason: null,
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

  it("shows 'armed' when nothing is determined yet (no gauge data)", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({ on_battery: null, discharging: null, shutdown_state: null }),
    );
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
        discharging: null,
        discharge_rate_pct_per_hour: null,
        min_discharge_rate_pct_per_hour: null,
        soc_critical: null,
        critical_soc_pct: null,
        shutdown_state: null,
        countdown_reason: null,
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

  it("shows a shutdown countdown with its reason after real input loss", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({
        input_lost: true,
        on_battery: true,
        discharging: true,
        discharge_rate_pct_per_hour: -30,
        shutdown_state: "counting_down",
        countdown_reason: "input_lost_discharging",
        seconds_lost: 45,
        grace_s: 300,
      }),
    );
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("shutdown pending")).toBeInTheDocument();
    expect(screen.getByText("lost")).toBeInTheDocument();
    expect(screen.getByText("discharging (-30.0 %/h)")).toBeInTheDocument();
    expect(screen.getByText(/4m 15s — input lost, battery discharging/)).toBeInTheDocument();
  });

  it("falls back to on_battery for a heartbeat from a watchdog older than D03.67", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({
        input_lost: true,
        on_battery: true,
        discharging: null,
        discharge_rate_pct_per_hour: null,
        soc_critical: null,
        shutdown_state: null,
        seconds_lost: 45,
        grace_s: 300,
      }),
    );
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("on battery")).toBeInTheDocument();
    expect(screen.getByText("4m 15s")).toBeInTheDocument();
  });

  it("2026-11-10: critical SoC while charging with input present reads as such, no countdown", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({
        soc: 12.9,
        soc_critical: true,
        discharging: false,
        discharge_rate_pct_per_hour: 40,
      }),
    );
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("external power")).toBeInTheDocument();
    expect(screen.getByText("present")).toBeInTheDocument();
    expect(screen.getByText("charging (+40.0 %/h)")).toBeInTheDocument();
    expect(screen.getByText(/12\.9% — critical \(at or below 15%\)/)).toBeInTheDocument();
    expect(screen.queryByText("Shutdown in")).not.toBeInTheDocument();
  });

  it("2026-11-12: a measured discharge with input present is shown as discharging, not as input loss", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({ soc: 94.5, discharging: true, discharge_rate_pct_per_hour: -18.5 }),
    );
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("external power")).toBeInTheDocument();
    expect(screen.getByText("present")).toBeInTheDocument();
    expect(screen.getByText("discharging (-18.5 %/h)")).toBeInTheDocument();
    expect(screen.queryByText("Shutdown in")).not.toBeInTheDocument();
  });

  it("says a keep-alive hold is overriding a critical-SoC countdown", async () => {
    mocks.getPowerStatus.mockResolvedValue(
      snapshot({
        input_lost: true,
        on_battery: true,
        soc: 5,
        soc_critical: true,
        shutdown_state: "held_keepalive",
        countdown_reason: "input_lost_soc_critical",
        keepalive_remaining_s: 600,
      }),
    );
    render(createElement(PowerPanel, { token: "secret" }));
    expect(await screen.findByText("held by keep-alive")).toBeInTheDocument();
    expect(
      screen.getByText(
        /held by the keep-alive hold \(input lost, SoC at or below the critical floor\)/,
      ),
    ).toBeInTheDocument();
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
