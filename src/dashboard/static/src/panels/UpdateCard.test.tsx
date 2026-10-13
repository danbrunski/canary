import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AWAITED_UPDATE_KEY, RUNNING_POLL_MS } from "../lib/update";
import type { UpdateSnapshot, UpdateStatus } from "../types/update";
import { UpdateCard } from "./UpdateCard";

const REQUESTED_AT = "2026-10-12T10:00:00+00:00";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function snapshot(status: Partial<UpdateStatus>, extra: Partial<UpdateSnapshot> = {}) {
  return {
    update: {
      installed: { version: "0.1.0", sha: "aaa1111" },
      status: { state: "idle", ...status },
      last_check: {
        ok: true,
        reason: null,
        message: null,
        checked_at: "2026-10-12T09:59:00+00:00",
        current: "aaa1111",
        upstream: "ccc3333",
        behind: 2,
        ahead: 0,
        commits: [
          { sha: "ccc3333", subject: "feat: update from the phone" },
          { sha: "bbb2222", subject: "fix: a bug" },
        ],
        dirty: false,
      },
      pending_request: null,
      trigger_installed: true,
      internet: { default_route: true },
      blocked_by: [],
      ...extra,
    },
  };
}

const running = (phase: string) =>
  snapshot({ state: "updating", action: "update", request_id: "r1", phase, phases: [] });

/** A Pi that answers each GET with the next scripted reply (the last one
 *  repeats); `"down"` is the daemon restarting: the request never connects. */
function fakePi(gets: Array<unknown>) {
  const replies = [...gets];
  return vi.fn((_url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      return Promise.resolve(
        json(
          {
            request: {
              action: "update",
              request_id: "r1",
              requested_at: REQUESTED_AT,
              options: { full: false },
            },
            ...snapshot(
              {},
              { pending_request: { action: "update", request_id: "r1", age_s: 0, stuck: false } },
            ),
          },
          202,
        ),
      );
    }
    const next = replies.length > 1 ? replies.shift() : replies[0];
    if (next === "down") return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.resolve(json(next));
  });
}

describe("UpdateCard", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    window.sessionStorage.clear();
  });

  async function poll() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(RUNNING_POLL_MS);
    });
  }

  it("lists the commits a check found, with their subjects", async () => {
    vi.stubGlobal("fetch", fakePi([snapshot({ state: "done", action: "check" })]));
    render(<UpdateCard token="t" />);
    await poll();

    expect(screen.getByText("v0.1.0")).toBeTruthy();
    expect(screen.getByText("2 commits available: aaa1111 → ccc3333.")).toBeTruthy();
    expect(screen.getByText("feat: update from the phone")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Update…" })).toBeTruthy();
  });

  it("reconnects after the daemon restarts and reports the new commit", async () => {
    const fetchMock = fakePi([
      snapshot({ state: "done", action: "check" }),
      running("Fetching CANary update"),
      running("Restarting services"),
      "down",
      "down",
      snapshot(
        {
          state: "done",
          action: "update",
          request_id: "r1",
          outcome: "updated",
          after: "ccc3333",
          finished_at: "2026-10-12T10:02:00+00:00",
          message: "Updated to ccc3333.",
        },
        { installed: { version: "0.1.0", sha: "ccc3333" }, last_check: null },
      ),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdateCard token="t" />);
    await poll();

    fireEvent.click(screen.getByRole("button", { name: "Update…" }));
    expect(screen.getByText(/Update to ccc3333\?/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Update now" }));
    await poll();
    const [, postInit] = fetchMock.mock.calls.find(([, init]) => init?.method === "POST") as [
      string,
      RequestInit,
    ];
    expect(JSON.parse(postInit.body as string)).toEqual({ action: "update" });
    expect(JSON.parse(window.sessionStorage.getItem(AWAITED_UPDATE_KEY) ?? "null")).toEqual({
      id: "r1",
      requestedAt: REQUESTED_AT,
    });
    expect(screen.getByText(/Updating: Fetching CANary update/)).toBeTruthy();

    await poll();
    expect(screen.getByText(/Updating: Restarting services/)).toBeTruthy();

    await poll();
    expect(screen.getByText(/reconnecting/)).toBeTruthy();
    expect(screen.queryByText(/Can't reach the daemon/)).toBeNull();
    await poll();
    expect(screen.getByText(/reconnecting/)).toBeTruthy();

    await poll();
    expect(screen.getByText("Updated to ccc3333.")).toBeTruthy();
    expect(screen.queryByText(/reconnecting/)).toBeNull();
    expect(screen.getByRole("button", { name: "Reload to load the new dashboard" })).toBeTruthy();
    expect(window.sessionStorage.getItem(AWAITED_UPDATE_KEY)).toBeNull();
  });

  it("says why it rolled back, after a reload while the daemon was down", async () => {
    // The tab reloaded mid-update: only the stored request id is left.
    window.sessionStorage.setItem(
      AWAITED_UPDATE_KEY,
      JSON.stringify({ id: "r1", requestedAt: REQUESTED_AT }),
    );
    vi.stubGlobal(
      "fetch",
      fakePi([
        "down",
        snapshot({
          state: "failed",
          action: "update",
          request_id: "r1",
          outcome: "rolled_back",
          finished_at: "2026-10-12T10:04:00+00:00",
          message:
            "Rolled back: health check FAILED (no answer within 90 s). Back on aaa1111, healthy.",
          log_tail: ["CANary did not answer at http://localhost:8000/status within 90 s."],
        }),
      ]),
    );
    render(<UpdateCard token="t" />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    // The first poll after the reload finds the daemon still down.
    expect(screen.getByText(/reconnecting/)).toBeTruthy();
    expect(screen.queryByText(/Rolled back/)).toBeNull();

    await poll();
    expect(screen.getByText(/^Rolled back: health check FAILED/)).toBeTruthy();
    expect(screen.getByText("last output")).toBeTruthy();
    expect(window.sessionStorage.getItem(AWAITED_UPDATE_KEY)).toBeNull();
  });

  it("shows the server's refusal and files nothing more", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(json({ detail: "no internet: the Pi has no default route." }, 409));
      }
      return Promise.resolve(json(snapshot({ state: "idle" }, { last_check: null })));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<UpdateCard token="t" />);
    await poll();

    fireEvent.click(screen.getByRole("button", { name: "Check for updates" }));
    await poll();

    expect(screen.getByText(/no internet: the Pi has no default route/)).toBeTruthy();
    expect(window.sessionStorage.getItem(AWAITED_UPDATE_KEY)).toBeNull();
  });

  it("holds the Update button while a capture records, and says why", async () => {
    const reason = "capture 'fuel-map' is recording (_capture-fuel-map-1); stop it first.";
    vi.stubGlobal("fetch", fakePi([snapshot({ state: "idle" }, { blocked_by: [reason] })]));
    render(<UpdateCard token="t" />);
    await poll();

    const button = screen.getByRole("button", { name: "Update…" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByText(reason).length).toBeGreaterThan(0);
  });

  it("says no internet when the last check could not reach the remote", async () => {
    vi.stubGlobal(
      "fetch",
      fakePi([
        snapshot(
          {
            state: "failed",
            action: "check",
            message: "No internet: git fetch could not reach the git remote.",
          },
          {
            last_check: {
              ok: false,
              reason: "no_internet",
              message: "No internet: git fetch could not reach the git remote.",
              checked_at: REQUESTED_AT,
            },
          },
        ),
      ]),
    );
    render(<UpdateCard token="t" />);
    await poll();

    expect(screen.getAllByText(/^No internet: git fetch/).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Update…" })).toBeNull();
  });
});
