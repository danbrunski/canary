import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getCompanionStatus: vi.fn(),
  getIdentity: vi.fn(),
}));
vi.mock("../api/http", () => ({
  getCompanionStatus: mocks.getCompanionStatus,
  getIdentity: mocks.getIdentity,
}));

const { useConnectivity, useConnectivityMode } = await import("./useConnectivityMode");

function identity(hostKind: string) {
  return {
    product: "canary",
    api: "identity/1",
    hostname: "box",
    host_kind: hostKind,
    profile: null,
    version: "1",
    git_sha: null,
    dashboard_port: 8000,
  };
}

function companionStatus(pi: unknown = null) {
  return {
    mode: "local_library",
    library_root: "/home/x/.canary/library",
    sessions: 3,
    models: 1,
    datasets: 0,
    queued_actions: 0,
    pi,
    pi_token_present: false,
    discovery: { state: "idle", ran_at: null, found: [], error: null },
  };
}

beforeEach(() => {
  mocks.getCompanionStatus.mockReset();
  mocks.getIdentity.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useConnectivityMode", () => {
  it("is 'live-pi' when /status answers on a Raspberry Pi, without polling companion", async () => {
    mocks.getIdentity.mockResolvedValue(identity("raspberry_pi"));
    const { result } = renderHook(() => useConnectivityMode(true));
    await waitFor(() => expect(mocks.getIdentity).toHaveBeenCalledTimes(1));
    expect(result.current).toBe("live-pi");
    expect(mocks.getCompanionStatus).not.toHaveBeenCalled();
  });

  it("is 'desk' when /status answers and /identity says the host is not a Pi", async () => {
    mocks.getIdentity.mockResolvedValue(identity("host"));
    mocks.getCompanionStatus.mockRejectedValue(new Error("404"));
    const { result } = renderHook(() => useConnectivityMode(true));
    await waitFor(() => expect(result.current).toBe("desk"));
  });

  it("stays 'live-pi' when /identity is unreadable (a daemon older than the endpoint)", async () => {
    mocks.getIdentity.mockRejectedValue(new Error("HTTP 404"));
    const { result } = renderHook(() => useConnectivityMode(true));
    await waitFor(() => expect(mocks.getIdentity).toHaveBeenCalled());
    expect(result.current).toBe("live-pi");
    expect(mocks.getCompanionStatus).not.toHaveBeenCalled();
  });

  it("names the linked Pi on a desk, from /companion/status", async () => {
    mocks.getIdentity.mockResolvedValue(identity("host"));
    mocks.getCompanionStatus.mockResolvedValue(
      companionStatus({
        base_url: "http://canary-8974.local:8000",
        identity: { hostname: "canary-8974" },
        source: "selected",
        selected_at: null,
      }),
    );
    const { result } = renderHook(() => useConnectivity(true));
    await waitFor(() => expect(result.current.linkedPi).toBe("canary-8974"));
    expect(result.current.mode).toBe("desk");
  });

  it("has no linked Pi on a desk with companion mode off", async () => {
    mocks.getIdentity.mockResolvedValue(identity("host"));
    mocks.getCompanionStatus.mockRejectedValue(new Error("HTTP 404"));
    const { result } = renderHook(() => useConnectivity(true));
    await waitFor(() => expect(mocks.getCompanionStatus).toHaveBeenCalled());
    expect(result.current).toEqual({ mode: "desk", linkedPi: null });
  });

  it("falls back to 'local-library' when /status fails but /companion/status answers", async () => {
    mocks.getCompanionStatus.mockResolvedValue(companionStatus());

    const { result } = renderHook(() => useConnectivityMode(false));

    await waitFor(() => expect(result.current).toBe("local-library"));
    expect(mocks.getIdentity).not.toHaveBeenCalled();
  });

  it("is 'queued-offline' when neither backend answers", async () => {
    mocks.getCompanionStatus.mockRejectedValue(new Error("network error"));

    const { result } = renderHook(() => useConnectivityMode(false));

    await waitFor(() => expect(mocks.getCompanionStatus).toHaveBeenCalled());
    expect(result.current).toBe("queued-offline");
  });
});
