import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AnomalyEvent } from "../types";

// We need to mock the http + websocket modules *before* the hook imports them.
// vi.hoisted gives the mock factories access to the shared registry.
const mocks = vi.hoisted(() => ({
  getAnomalies: vi.fn(),
  openReconnectingSocket: vi.fn(),
}));
vi.mock("../api/http", () => ({
  getAnomalies: mocks.getAnomalies,
}));
vi.mock("../api/websocket", () => ({
  openReconnectingSocket: mocks.openReconnectingSocket,
}));

// Imported AFTER the mocks so the hook picks them up.
const { useAnomalyStream } = await import("./useAnomalyStream");

const makeEvent = (overrides: Partial<AnomalyEvent> = {}): AnomalyEvent => ({
  timestamp: "2026-05-13T12:00:00+00:00",
  source: "isolation_forest",
  model_version: "if_v1",
  score: 0.5,
  threshold: null,
  affected_channels: [],
  operating_mode: "idle",
  session: "2026-05-13T120000",
  window_start_timestamp: null,
  window_end_timestamp: null,
  latency_ms: null,
  detail: {},
  ...overrides,
});

beforeEach(() => {
  mocks.getAnomalies.mockReset();
  mocks.openReconnectingSocket.mockReset();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("useAnomalyStream", () => {
  it("seeds from /anomalies then dedupes the first WS frame if it overlaps", async () => {
    const seed: AnomalyEvent[] = [
      makeEvent({ timestamp: "2026-05-13T12:00:00+00:00", score: 1.0 }),
      makeEvent({ timestamp: "2026-05-13T12:01:00+00:00", score: 2.0 }),
    ];
    mocks.getAnomalies.mockResolvedValue({ events: seed, count: seed.length });

    // Capture the WS onMessage so the test can pump events into it.
    let onMessage: ((e: AnomalyEvent) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation((opts: { onMessage: (e: AnomalyEvent) => void }) => {
      onMessage = opts.onMessage;
      return { close: vi.fn() };
    });

    const { result } = renderHook(() => useAnomalyStream());

    await waitFor(() => {
      // seed lands → 2 events
      expect(result.current.events.length).toBe(2);
    });
    // newest-first ordering after the seed (the seed is reversed before prepending)
    expect(result.current.events[0].score).toBe(2.0);

    // WS re-emits an event we already saw via seed — must be deduped
    expect(onMessage).not.toBeNull();
    act(() => {
      onMessage!(makeEvent({ timestamp: "2026-05-13T12:01:00+00:00", score: 2.0 }));
    });
    expect(result.current.events).toHaveLength(2);

    // New event with a different timestamp lands as #3
    act(() => {
      onMessage!(makeEvent({ timestamp: "2026-05-13T12:02:00+00:00", score: 3.0 }));
    });
    expect(result.current.events).toHaveLength(3);
    expect(result.current.events[0].score).toBe(3.0);
  });

  it("dedupe key separates same-timestamp events from different sources", async () => {
    mocks.getAnomalies.mockResolvedValue({ events: [], count: 0 });
    let onMessage: ((e: AnomalyEvent) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation((opts: { onMessage: (e: AnomalyEvent) => void }) => {
      onMessage = opts.onMessage;
      return { close: vi.fn() };
    });

    const { result } = renderHook(() => useAnomalyStream());

    const ts = "2026-05-13T12:00:00+00:00";
    act(() => {
      onMessage!(makeEvent({ timestamp: ts, source: "isolation_forest" }));
      onMessage!(makeEvent({ timestamp: ts, source: "lstm_autoencoder", model_version: "lstm_v1" }));
    });
    expect(result.current.events).toHaveLength(2);
  });

  it("propagates the connection state through the hook", async () => {
    mocks.getAnomalies.mockResolvedValue({ events: [], count: 0 });
    let onState: ((s: "open" | "closed" | "connecting" | "error") => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation(
      (opts: { onState: (s: "open" | "closed" | "connecting" | "error") => void }) => {
        onState = opts.onState;
        return { close: vi.fn() };
      },
    );

    const { result } = renderHook(() => useAnomalyStream());
    expect(result.current.connection).toBe("connecting");

    act(() => onState!("open"));
    expect(result.current.connection).toBe("open");

    act(() => onState!("closed"));
    expect(result.current.connection).toBe("closed");
  });

  it("closes the WS subscription on unmount (no leak)", async () => {
    mocks.getAnomalies.mockResolvedValue({ events: [], count: 0 });
    const close = vi.fn();
    mocks.openReconnectingSocket.mockReturnValue({ close });
    const { unmount } = renderHook(() => useAnomalyStream());
    unmount();
    expect(close).toHaveBeenCalledTimes(1);
  });
});
