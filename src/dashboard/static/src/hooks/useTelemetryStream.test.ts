import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TelemetryRow } from "../types";

const mocks = vi.hoisted(() => ({
  getLatestPerChannel: vi.fn(),
  openReconnectingSocket: vi.fn(),
}));
vi.mock("../api/http", () => ({ getLatestPerChannel: mocks.getLatestPerChannel }));
vi.mock("../api/websocket", () => ({
  openReconnectingSocket: mocks.openReconnectingSocket,
}));

const { useTelemetryStream } = await import("./useTelemetryStream");

const makeRow = (overrides: Partial<TelemetryRow> = {}): TelemetryRow => ({
  timestamp: "2026-05-13T12:00:00+00:00",
  source: "obd",
  channel: "RPM",
  value: 700,
  unit: "rpm",
  ...overrides,
});

beforeEach(() => {
  mocks.getLatestPerChannel.mockReset();
  mocks.openReconnectingSocket.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("useTelemetryStream", () => {
  it("seeds channels from /telemetry/latest", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({
      rows: [makeRow({ channel: "RPM", value: 700 })],
      count: 1,
    });
    mocks.openReconnectingSocket.mockReturnValue({ close: vi.fn() });

    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => {
      expect(result.current.channels.get("obd:RPM")).toBeDefined();
    });
    expect(result.current.channels.get("obd:RPM")!.latest.value).toBe(700);
  });

  it("ingests live messages and accumulates per-channel history", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    let onMessage: ((r: TelemetryRow) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation((opts: { onMessage: (r: TelemetryRow) => void }) => {
      onMessage = opts.onMessage;
      return { close: vi.fn() };
    });
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      onMessage!(makeRow({ value: 700 }));
      onMessage!(makeRow({ value: 720 }));
      onMessage!(makeRow({ value: 750 }));
    });
    const snap = result.current.channels.get("obd:RPM");
    expect(snap).toBeDefined();
    expect(snap!.history).toEqual([700, 720, 750]);
    expect(snap!.latest.value).toBe(750);
  });

  it("caps history at 120 samples (sliding ring buffer)", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    let onMessage: ((r: TelemetryRow) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation((opts: { onMessage: (r: TelemetryRow) => void }) => {
      onMessage = opts.onMessage;
      return { close: vi.fn() };
    });
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      for (let i = 0; i < 200; i++) onMessage!(makeRow({ value: i }));
    });
    const snap = result.current.channels.get("obd:RPM");
    expect(snap!.history.length).toBe(120);
    // The latest values stay, the oldest fall off — last buffered value is 199
    expect(snap!.history[snap!.history.length - 1]).toBe(199);
    expect(snap!.history[0]).toBeGreaterThanOrEqual(80);
  });

  it("skips non-numeric values from the history but keeps the latest record", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    let onMessage: ((r: TelemetryRow) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation((opts: { onMessage: (r: TelemetryRow) => void }) => {
      onMessage = opts.onMessage;
      return { close: vi.fn() };
    });
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      onMessage!(makeRow({ channel: "VIN", source: "obd", value: "JN8AR..." }));
    });
    const snap = result.current.channels.get("obd:VIN");
    expect(snap!.latest.value).toBe("JN8AR...");
    expect(snap!.history).toEqual([]); // non-numeric → no history entry
  });

  it("ignores malformed rows (missing source/channel)", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    let onMessage: ((r: TelemetryRow) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation((opts: { onMessage: (r: TelemetryRow) => void }) => {
      onMessage = opts.onMessage;
      return { close: vi.fn() };
    });
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      // @ts-expect-error — deliberately malformed
      onMessage!({ value: 1 });
    });
    expect(result.current.channels.size).toBe(0);
  });
});
