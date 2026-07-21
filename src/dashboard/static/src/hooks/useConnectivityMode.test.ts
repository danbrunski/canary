import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getCompanionStatus: vi.fn(),
}));
vi.mock("../api/http", () => ({
  getCompanionStatus: mocks.getCompanionStatus,
}));

const { useConnectivityMode } = await import("./useConnectivityMode");

beforeEach(() => {
  mocks.getCompanionStatus.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useConnectivityMode", () => {
  it("is 'live-pi' whenever /status is reachable, without polling companion", async () => {
    const { result } = renderHook(() => useConnectivityMode(true));
    expect(result.current).toBe("live-pi");
    expect(mocks.getCompanionStatus).not.toHaveBeenCalled();
  });

  it("falls back to 'local-library' when /status fails but /companion/status answers", async () => {
    mocks.getCompanionStatus.mockResolvedValue({
      mode: "local_library",
      library_root: "/home/x/.canary/library",
      sessions: 3,
      models: 1,
      datasets: 0,
      queued_actions: 0,
      config_path: "/home/x/.canary/library/canary.local.yaml",
    });

    const { result } = renderHook(() => useConnectivityMode(false));

    await waitFor(() => expect(result.current).toBe("local-library"));
  });

  it("is 'queued-offline' when neither backend answers", async () => {
    mocks.getCompanionStatus.mockRejectedValue(new Error("network error"));

    const { result } = renderHook(() => useConnectivityMode(false));

    await waitFor(() => expect(mocks.getCompanionStatus).toHaveBeenCalled());
    expect(result.current).toBe("queued-offline");
  });
});
