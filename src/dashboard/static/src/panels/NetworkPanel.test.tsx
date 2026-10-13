import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { StatusResponse } from "../types";
import type { NetworkResponse, NetworkSummary } from "../types/network";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getNetwork", "setProvisioningWindow"]);

const { NetworkPanel } = await import("./NetworkPanel");

function summary(overrides: Partial<NetworkSummary> = {}): NetworkSummary {
  return {
    hostname: "canary-1a2b",
    mode: "client",
    ssid: "Starlink",
    wifi_interface: "wlan0",
    addresses: { eth0: ["192.168.137.2"], wlan0: ["192.168.1.23", "2001:db8::23"] },
    dashboard_url: "http://192.168.1.23:8000/",
    read_at: 1_700_000_000,
    age_s: 0,
    error: null,
    ...overrides,
  };
}

function response(windowRemaining = 0, overrides: Partial<NetworkSummary> = {}): NetworkResponse {
  return {
    network: summary(overrides),
    provisioning: { window_remaining_s: windowRemaining, max_minutes: 30 },
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("NetworkPanel", () => {
  it("shows the joined SSID, every interface and the bookmarkable URL", async () => {
    mocks.getNetwork.mockResolvedValue(response());
    render(<NetworkPanel token="secret" status={null} />);
    expect(await screen.findByText("Starlink")).toBeInTheDocument();
    expect(screen.getByText("joined")).toBeInTheDocument();
    expect(screen.getByText("192.168.137.2")).toBeInTheDocument();
    expect(screen.getByText("2001:db8::23")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "http://192.168.1.23:8000/" });
    expect(link).toHaveAttribute("href", "http://192.168.1.23:8000/");
    expect(screen.getByText(/Android cannot open canary\.local/)).toBeInTheDocument();
    expect(screen.getByText("Closed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close now" })).toBeNull();
  });

  it("opens the window for five minutes and counts it down", async () => {
    mocks.getNetwork.mockResolvedValueOnce(response(0)).mockResolvedValue(response(125));
    mocks.setProvisioningWindow.mockResolvedValue({
      provisioning: { window_remaining_s: 300, max_minutes: 30 },
      expiry: 1_700_000_300,
    });
    // Faked from the start, so the countdown's interval is a fake timer.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<NetworkPanel token="secret" status={null} />);
    fireEvent.click(await screen.findByRole("button", { name: "Let a phone change WiFi (5 min)" }));
    await waitFor(() => expect(mocks.setProvisioningWindow).toHaveBeenCalledWith("secret", 5));
    expect(await screen.findByText("Open — 2m 5s left")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(screen.getByText("Open — 2m 2s left")).toBeInTheDocument();
    vi.useRealTimers();

    fireEvent.click(screen.getByRole("button", { name: "Close now" }));
    await waitFor(() => expect(mocks.setProvisioningWindow).toHaveBeenCalledWith("secret", 0));
  });

  it("names a missing NetworkManager instead of claiming no WiFi", async () => {
    mocks.getNetwork.mockResolvedValue(
      response(0, {
        mode: "unknown",
        ssid: null,
        dashboard_url: null,
        error: "nmcli is not installed (NetworkManager and iproute2 are Pi-side tools)",
      }),
    );
    render(<NetworkPanel token="secret" status={null} />);
    expect(await screen.findByText(/nmcli is not installed/)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("shows /status.network without a token, and no window controls", () => {
    const status = { network: summary() } as unknown as StatusResponse;
    render(<NetworkPanel token="" status={status} />);
    expect(screen.getByText("Starlink")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "http://192.168.1.23:8000/" })).toBeInTheDocument();
    expect(screen.getByText(/Enter the control token/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Let a phone change WiFi/ })).toBeNull();
    expect(mocks.getNetwork).not.toHaveBeenCalled();
  });
});
