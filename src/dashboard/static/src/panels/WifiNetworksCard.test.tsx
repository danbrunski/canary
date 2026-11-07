import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { NetworkSummary, WifiNetwork, WifiState } from "../types/network";
import { HttpErrorStub, NetworkErrorStub, mockHttp } from "../test/harness";

const mocks = mockHttp([
  "getWifi",
  "scanWifi",
  "addWifi",
  "connectWifi",
  "orderWifi",
  "forgetWifi",
]);

const { WifiNetworksCard } = await import("./WifiNetworksCard");

function net(overrides: Partial<WifiNetwork> = {}): WifiNetwork {
  return {
    id: "id-home",
    ssid: "Home",
    priority: 100,
    autoconnect: true,
    hidden: false,
    security: "wpa-psk",
    active: true,
    in_range: true,
    signal: 72,
    managed_by: "canary",
    ...overrides,
  };
}

function state(overrides: Partial<WifiState> = {}): WifiState {
  return {
    networks: [
      net(),
      net({
        id: "id-phone",
        ssid: "PhoneSpot",
        priority: 90,
        active: false,
        in_range: false,
        signal: null,
      }),
      net({
        id: "id-pre",
        ssid: "preconfigured",
        priority: 80,
        active: false,
        security: null,
        managed_by: "other",
        signal: 40,
      }),
    ],
    scan: [
      { ssid: "Home", signal: 70, security: "WPA2", saved: true },
      { ssid: "CafeOpen", signal: 55, security: "", saved: false },
      { ssid: "Neighbour", signal: 30, security: "WPA2 WPA3", saved: false },
    ],
    scanned_at: 1_700_000_000,
    interface: "wlan0",
    ...overrides,
  };
}

const NETWORK: NetworkSummary = {
  hostname: "canary-1a2b",
  mode: "client",
  ssid: "Home",
  wifi_interface: "wlan0",
  addresses: { eth0: ["192.168.137.2"], wlan0: ["192.168.1.23", "2001:db8::23"] },
  dashboard_url: "http://192.168.1.23:8000/",
  read_at: 1_700_000_000,
  age_s: 0,
  error: null,
};

function httpError(status: number, detail: string): Error {
  const err = new HttpErrorStub();
  err.status = status;
  err.body = JSON.stringify({ detail });
  return err;
}

function mount(host = "192.168.137.2", token = "secret") {
  return render(<WifiNetworksCard token={token} network={NETWORK} host={host} />);
}

afterEach(() => {
  vi.restoreAllMocks();
});

const passphrase = () => screen.getByLabelText<HTMLInputElement>("Passphrase");

describe("WifiNetworksCard", () => {
  it("lists saved networks in priority order with their markers", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mount();
    const list = await screen.findByRole("list", { name: "Saved networks" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector("span")?.textContent)).toEqual([
      "Home",
      "PhoneSpot",
      "preconfigured",
    ]);
    expect(within(rows[0]).getByText("connected")).toBeInTheDocument();
    expect(within(rows[0]).getByText("in range · signal 72%")).toBeInTheDocument();
    expect(within(rows[0]).queryByRole("button", { name: /^connect to/ })).toBeNull();
    expect(within(rows[1]).getByText("not in range")).toBeInTheDocument();
    expect(within(rows[1]).getByRole("button", { name: "connect to PhoneSpot" })).toBeEnabled();
    expect(within(rows[2]).getByText("set up outside CANary")).toBeInTheDocument();
    expect(within(rows[2]).getByText("open")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("button", { name: "move Home up" })).toBeDisabled();
    expect(within(rows[2]).getByRole("button", { name: "move preconfigured down" })).toBeDisabled();
  });

  it("marks scan entries that are already saved", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mount();
    const home = await screen.findByRole("button", { name: "choose Home" });
    expect(within(home).getByText("saved")).toBeInTheDocument();
    const cafe = screen.getByRole("button", { name: "choose CafeOpen" });
    expect(within(cafe).queryByText("saved")).toBeNull();
    expect(within(cafe).getByText("open")).toBeInTheDocument();
  });

  it("sends the full new order when a network moves up", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mocks.orderWifi.mockResolvedValue({
      wifi: state({ networks: [net({ id: "id-phone", ssid: "PhoneSpot" })] }),
    });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "move PhoneSpot up" }));
    await waitFor(() =>
      expect(mocks.orderWifi).toHaveBeenCalledWith("secret", ["id-phone", "id-home", "id-pre"]),
    );
    expect(await screen.findByText("Order saved.")).toBeInTheDocument();
  });

  it("asks before forgetting, and forgets only on yes", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mocks.forgetWifi.mockResolvedValue({ wifi: state({ networks: [net()] }) });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "forget PhoneSpot" }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("Forget PhoneSpot?"));
    expect(mocks.forgetWifi).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "forget PhoneSpot" }));
    await waitFor(() => expect(mocks.forgetWifi).toHaveBeenCalledWith("secret", "id-phone"));
    expect(await screen.findByText("Forgot PhoneSpot.")).toBeInTheDocument();
    expect(screen.queryByText("PhoneSpot")).toBeNull();
  });

  it("joins a scanned secured network now and clears the passphrase", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mocks.addWifi.mockResolvedValue({
      network: net({ id: "id-new", ssid: "Neighbour" }),
      wifi: state(),
    });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "choose Neighbour" }));
    fireEvent.change(passphrase(), { target: { value: "hunter2hunter2" } });
    expect(passphrase().type).toBe("password");
    fireEvent.click(screen.getByRole("button", { name: "Show" }));
    expect(passphrase().type).toBe("text");
    fireEvent.click(screen.getByRole("button", { name: "Join now" }));
    await waitFor(() =>
      expect(mocks.addWifi).toHaveBeenCalledWith("secret", {
        ssid: "Neighbour",
        password: "hunter2hunter2",
        hidden: false,
        join: true,
      }),
    );
    expect(await screen.findByText("Joined Neighbour.")).toBeInTheDocument();
    expect(passphrase().value).toBe("");
    expect(passphrase().type).toBe("password");
    expect(screen.queryByText("hunter2hunter2")).toBeNull();
  });

  it("shows the Pi's reason when a join fails and clears the passphrase", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mocks.addWifi.mockRejectedValue(httpError(409, "Secrets were required, but not provided"));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "choose Neighbour" }));
    fireEvent.change(passphrase(), { target: { value: "wrongpass99" } });
    fireEvent.click(screen.getByRole("button", { name: "Join now" }));
    expect(
      await screen.findByText(
        "The Pi could not join Neighbour: Secrets were required, but not provided. Nothing was saved.",
      ),
    ).toBeInTheDocument();
    expect(passphrase().value).toBe("");
    expect(screen.queryByText(/wrongpass99/)).toBeNull();
  });

  it("shows the server's 422 sentence", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mocks.addWifi.mockRejectedValue(httpError(422, "passphrase must be 8-63 characters"));
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "choose Neighbour" }));
    fireEvent.change(passphrase(), { target: { value: "short" } });
    fireEvent.click(screen.getByRole("button", { name: "Save for later" }));
    expect(await screen.findByText("passphrase must be 8-63 characters")).toBeInTheDocument();
  });

  it("saves for later with join false, and sends no passphrase for an open network", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mocks.addWifi.mockResolvedValue({ network: net({ id: "n", ssid: "CafeOpen" }), wifi: state() });
    mount();
    fireEvent.click(await screen.findByRole("button", { name: "choose CafeOpen" }));
    expect(screen.queryByLabelText("Passphrase")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save for later" }));
    await waitFor(() =>
      expect(mocks.addWifi).toHaveBeenCalledWith("secret", {
        ssid: "CafeOpen",
        password: null,
        hidden: false,
        join: false,
      }),
    );
    expect(await screen.findByText("Saved CafeOpen for later.")).toBeInTheDocument();
  });

  it("takes a typed hidden network", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state() });
    mocks.addWifi.mockResolvedValue({ network: net({ id: "n", ssid: "Secret" }), wifi: state() });
    mount();
    await screen.findByRole("button", { name: "choose Neighbour" });
    fireEvent.change(screen.getByLabelText("Network name"), { target: { value: " Secret " } });
    fireEvent.click(screen.getByLabelText("Hidden network"));
    fireEvent.change(passphrase(), { target: { value: "longenoughpass" } });
    fireEvent.click(screen.getByRole("button", { name: "Join now" }));
    await waitFor(() =>
      expect(mocks.addWifi).toHaveBeenCalledWith("secret", {
        ssid: "Secret",
        password: "longenoughpass",
        hidden: true,
        join: true,
      }),
    );
  });

  it("rescans and shows the new list", async () => {
    mocks.getWifi.mockResolvedValue({ wifi: state({ scan: [], scanned_at: null }) });
    mocks.scanWifi.mockResolvedValue({ wifi: state() });
    mount();
    expect(await screen.findByText("Not scanned yet.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Rescan" }));
    expect(await screen.findByRole("button", { name: "choose Neighbour" })).toBeInTheDocument();
  });

  describe("dropped connection", () => {
    it("warns before joining when this page is on the WiFi address", async () => {
      mocks.getWifi.mockResolvedValue({ wifi: state() });
      mocks.addWifi.mockResolvedValue({ network: net(), wifi: state() });
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
      mount("192.168.1.23");
      fireEvent.click(await screen.findByRole("button", { name: "choose Neighbour" }));
      fireEvent.change(passphrase(), { target: { value: "longenoughpass" } });
      fireEvent.click(screen.getByRole("button", { name: "Join now" }));
      expect(confirm).toHaveBeenCalledWith(
        "This page will lose its connection; the Pi will move to Neighbour. " +
          "Reach it again at its new address or over the cable at 192.168.137.2.",
      );
      expect(mocks.addWifi).not.toHaveBeenCalled();
    });

    it("warns before connecting to a different network, and for a bracketed IPv6 host", async () => {
      mocks.getWifi.mockResolvedValue({ wifi: state() });
      mocks.connectWifi.mockRejectedValue(new NetworkErrorStub());
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
      mount("[2001:db8::23]");
      fireEvent.click(await screen.findByRole("button", { name: "connect to PhoneSpot" }));
      expect(confirm).toHaveBeenCalledWith(
        expect.stringContaining("the Pi will move to PhoneSpot"),
      );
      await waitFor(() => expect(mocks.connectWifi).toHaveBeenCalledWith("secret", "id-phone"));
      expect(await screen.findByText(/connection dropped, as expected/)).toBeInTheDocument();
    });

    it("warns before forgetting the active network", async () => {
      mocks.getWifi.mockResolvedValue({ wifi: state() });
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
      mount("192.168.1.23");
      fireEvent.click(await screen.findByRole("button", { name: "forget Home" }));
      expect(confirm).toHaveBeenCalledWith(
        expect.stringContaining("This page will lose its connection"),
      );
    });

    it("does not warn over the cable", async () => {
      mocks.getWifi.mockResolvedValue({ wifi: state() });
      mocks.connectWifi.mockResolvedValue({ wifi: state() });
      const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
      mount("192.168.137.2");
      fireEvent.click(await screen.findByRole("button", { name: "connect to PhoneSpot" }));
      await waitFor(() => expect(mocks.connectWifi).toHaveBeenCalled());
      expect(confirm).not.toHaveBeenCalled();
    });
  });

  it("shows the 503 detail as the section's state, not an empty list", async () => {
    mocks.getWifi.mockRejectedValue(
      httpError(503, "canary-provision is not running: /run/canary/network.sock is missing"),
    );
    mount();
    expect(
      await screen.findByText(
        "canary-provision is not running: /run/canary/network.sock is missing",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("No saved WiFi networks.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Join now" })).toBeNull();
  });

  it("is locked without a token", () => {
    mount("192.168.137.2", "");
    expect(screen.getByText(/needs the control token to manage WiFi networks/)).toBeInTheDocument();
    expect(mocks.getWifi).not.toHaveBeenCalled();
  });
});
