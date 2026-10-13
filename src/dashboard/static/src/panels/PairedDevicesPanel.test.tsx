import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PairedDevice, PendingPairingRequest } from "../api/pairedDevices";
import { PairedDevicesPanel } from "./PairedDevicesPanel";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const PHONE: PairedDevice = {
  id: "d1",
  name: "Android phone",
  paired_at: 1_800_000_000,
  method: "key-cycle",
  last_seen: 1_800_000_600,
};
const LAPTOP: PairedDevice = {
  id: "d2",
  name: "Linux computer",
  paired_at: 1_800_001_000,
  method: "approved-by:d1",
  last_seen: null,
};
const WAITING: PendingPairingRequest = { id: "r1", name: "Tablet", age_s: 7.2, expires_in_s: 112 };

/** A Pi with two paired devices, one waiting request; this browser is the phone. */
function fakePi({ me = PHONE }: { me?: PairedDevice | null } = {}) {
  let devices = [PHONE, LAPTOP];
  let requests = [WAITING];
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url === "/control/devices") return Promise.resolve(json({ devices }));
    if (url === "/control/devices/me") {
      return Promise.resolve(
        json(
          me
            ? { kind: "device", shared_token: false, device: me }
            : { kind: "shared-token", shared_token: true, device: null },
        ),
      );
    }
    if (url === "/control/pairing/requests") return Promise.resolve(json({ requests }));
    if (url === "/control/pairing/requests/r1/approve" && method === "POST") {
      requests = [];
      return Promise.resolve(json({ approved: "r1", by: "d1" }));
    }
    if (url.startsWith("/control/devices/") && method === "PATCH") {
      const { name } = JSON.parse(init?.body as string) as { name: string };
      devices = devices.map((d) => (url.endsWith(d.id) ? { ...d, name } : d));
      return Promise.resolve(json({ device: devices[0] }));
    }
    if (url.startsWith("/control/devices/") && method === "DELETE") {
      devices = devices.filter((d) => !url.endsWith(d.id));
      return Promise.resolve(json({ removed: PHONE }));
    }
    return Promise.resolve(json({ detail: "not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function calls(fetchMock: ReturnType<typeof fakePi>, method: string) {
  return fetchMock.mock.calls.filter(([, init]) => (init?.method ?? "GET") === method);
}

describe("PairedDevicesPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks for a token without one", () => {
    const fetchMock = fakePi();
    render(<PairedDevicesPanel token="" />);
    expect(screen.getByText(/Pair this device, or enter the control token/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("lists devices, marks this one, and names the approver", async () => {
    fakePi();
    render(<PairedDevicesPanel token="cd_phone" />);
    const phoneRow = (await screen.findByText("Android phone")).closest("li");
    expect(phoneRow).not.toBeNull();
    expect(within(phoneRow!).getByText("this device")).toBeInTheDocument();
    const laptopRow = screen.getByText("Linux computer").closest("li");
    expect(within(laptopRow!).queryByText("this device")).toBeNull();
    expect(within(laptopRow!).getByText(/approved by Android phone/)).toBeInTheDocument();
    expect(within(laptopRow!).getByText(/last seen never/)).toBeInTheDocument();
  });

  it("approves a waiting request", async () => {
    const fetchMock = fakePi();
    render(<PairedDevicesPanel token="cd_phone" />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve Tablet" }));
    expect(await screen.findByText(/Approved Tablet/)).toBeInTheDocument();
    const [url, init] = calls(fetchMock, "POST")[0] as [string, RequestInit];
    expect(url).toBe("/control/pairing/requests/r1/approve");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer cd_phone");
    await waitFor(() => expect(screen.getByText(/No device is waiting/)).toBeInTheDocument());
  });

  it("renames inline", async () => {
    const fetchMock = fakePi();
    render(<PairedDevicesPanel token="cd_phone" />);
    const row = (await screen.findByText("Linux computer")).closest("li")!;
    fireEvent.click(within(row).getByRole("button", { name: "Rename" }));
    const input = within(row).getByRole("textbox", { name: "New name for Linux computer" });
    fireEvent.change(input, { target: { value: "  Work laptop " } });
    fireEvent.click(within(row).getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Work laptop")).toBeInTheDocument();
    const [url, init] = calls(fetchMock, "PATCH")[0] as [string, RequestInit];
    expect(url).toBe("/control/devices/d2");
    expect(JSON.parse(init.body as string)).toEqual({ name: "Work laptop" });
  });

  it("removes only after a confirmation, and warns when it is this device", async () => {
    const fetchMock = fakePi();
    render(<PairedDevicesPanel token="cd_phone" />);
    const row = (await screen.findByText("Android phone")).closest("li")!;
    fireEvent.click(within(row).getByRole("button", { name: "Remove" }));
    expect(within(row).getByRole("alert")).toHaveTextContent(/the device you are using/);
    fireEvent.click(within(row).getByRole("button", { name: "Keep" }));
    expect(calls(fetchMock, "DELETE")).toHaveLength(0);

    const laptop = screen.getByText("Linux computer").closest("li")!;
    fireEvent.click(within(laptop).getByRole("button", { name: "Remove" }));
    fireEvent.click(within(laptop).getByRole("button", { name: "Yes, remove" }));
    await waitFor(() => expect(screen.queryByText("Linux computer")).toBeNull());
    expect((calls(fetchMock, "DELETE")[0] as [string])[0]).toBe("/control/devices/d2");
  });

  it("says when this browser holds the shared token", async () => {
    fakePi({ me: null });
    render(<PairedDevicesPanel token="shared" />);
    expect(await screen.findByText(/uses the shared control token/)).toBeInTheDocument();
    expect(screen.queryByText("this device")).toBeNull();
  });
});
