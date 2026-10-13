import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ACTIVE_REQUEST_KEY } from "../pairing";
import { PairingCard } from "./PairingCard";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A Pi whose key the test turns; each poll reports the current step. */
function fakePi(steps: unknown[]) {
  const polls = [...steps];
  return vi.fn((url: string, init?: RequestInit) => {
    if (url === "/pair/request" && init?.method === "POST") {
      return Promise.resolve(json({ id: "r1", secret: "s1" }, 201));
    }
    if (init?.method === "DELETE") return Promise.resolve(json({ cancelled: "r1" }));
    const next = polls.length > 1 ? polls.shift() : polls[0];
    return Promise.resolve(json(next));
  });
}

const waiting = (step: string) => ({
  id: "r1",
  status: "waiting",
  step,
  detail: "",
  vehicle_state: "engine_off",
  expires_in_s: 100,
});

describe("PairingCard", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  async function flush() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
  }

  it("walks ACC, ON, ACC and hands over the token", async () => {
    const fetchMock = fakePi([
      waiting("turn_on"),
      waiting("back_to_acc"),
      { ...waiting("back_to_acc"), status: "approved", token: "cd_tok" },
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const onPaired = vi.fn();
    render(<PairingCard onPaired={onPaired} />);

    fireEvent.click(screen.getByRole("button", { name: "Pair this device" }));
    await flush();
    expect(screen.getByText(/Turn the key to ON/)).toBeTruthy();
    expect(JSON.parse(window.sessionStorage.getItem(ACTIVE_REQUEST_KEY) ?? "null")).toEqual({
      id: "r1",
      secret: "s1",
    });
    await flush();
    expect(screen.getByText(/back to ACC/)).toBeTruthy();
    await flush();
    expect(onPaired).toHaveBeenCalledWith("cd_tok");
    expect(window.sessionStorage.getItem(ACTIVE_REQUEST_KEY)).toBeNull();
    const [, pollInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(new Headers(pollInit.headers).get("X-Pairing-Secret")).toBe("s1");
  });

  it("shows a conflict and offers a retry", async () => {
    vi.stubGlobal(
      "fetch",
      fakePi([
        {
          ...waiting("turn_on"),
          status: "conflict",
          detail: "2 devices asked to pair at once (Android phone, Galaxy S23).",
        },
      ]),
    );
    render(<PairingCard onPaired={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Pair this device" }));
    await flush();
    expect(screen.getByText(/Galaxy S23/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("resumes a request after a reload instead of opening a second", async () => {
    window.sessionStorage.setItem(ACTIVE_REQUEST_KEY, JSON.stringify({ id: "r1", secret: "s1" }));
    const fetchMock = fakePi([waiting("back_to_acc")]);
    vi.stubGlobal("fetch", fetchMock);
    render(<PairingCard onPaired={vi.fn()} />);
    await flush();
    expect(screen.getByText(/back to ACC/)).toBeTruthy();
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(0);
  });
});
