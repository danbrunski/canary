import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PendingPairingRequest } from "../api/pairedDevices";
import { PairingApprovalPrompt } from "./PairingApprovalPrompt";

/** Just enough WebSocket for useControlStream: the test pushes control events. */
class MockSocket {
  static instances: MockSocket[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  constructor(public url: string) {
    MockSocket.instances.push(this);
  }
  close() {}
  emit(event: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const PHONE: PendingPairingRequest = {
  id: "r1",
  name: "Android phone",
  age_s: 1,
  expires_in_s: 119,
};

/** The daemon's waiting list, which the test edits between events. */
function fakePi(initial: PendingPairingRequest[]) {
  const pi = { waiting: initial };
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (url === "/control/pairing/requests") return Promise.resolve(json({ requests: pi.waiting }));
    if (url.endsWith("/approve") && init?.method === "POST") {
      pi.waiting = [];
      return Promise.resolve(json({ approved: "r1", by: "d1" }));
    }
    return Promise.resolve(json({ detail: "not found" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { pi, fetchMock };
}

function controlEvent(action: string, payload: Record<string, unknown>) {
  act(() => MockSocket.instances[0].emit({ action, result: "ok", payload }));
}

describe("PairingApprovalPrompt", () => {
  beforeEach(() => {
    MockSocket.instances = [];
    vi.stubGlobal("WebSocket", MockSocket);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("shows nothing while no one is waiting", async () => {
    const { fetchMock } = fakePi([]);
    const { container } = render(<PairingApprovalPrompt token="cd_phone" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
    expect(MockSocket.instances[0].url).toContain("/ws/control?token=cd_phone");
  });

  it("pops up on pairing.requested and approves", async () => {
    const { pi, fetchMock } = fakePi([]);
    render(<PairingApprovalPrompt token="cd_phone" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    pi.waiting = [PHONE];
    controlEvent("pairing.requested", { id: "r1", name: "Android phone" });
    const dialog = await screen.findByRole("alertdialog", { name: "Android phone wants to pair" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/Approved Android phone/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === "POST") as [string];
    expect(post[0]).toBe("/control/pairing/requests/r1/approve");
  });

  it("ignore hides it on this device without approving", async () => {
    const { fetchMock } = fakePi([PHONE]);
    render(<PairingApprovalPrompt token="cd_phone" />);
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Ignore" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("closes when another device or the key approves it", async () => {
    const { pi } = fakePi([PHONE]);
    render(<PairingApprovalPrompt token="cd_phone" />);
    await screen.findByRole("alertdialog");
    pi.waiting = [];
    controlEvent("pairing.approved", { id: "r1", name: "Android phone", by: "key-cycle" });
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });
});
