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

const { useTelemetryStream, FLUSH_FALLBACK_MS } = await import("./useTelemetryStream");

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
    mocks.openReconnectingSocket.mockImplementation(
      (opts: { onMessage: (r: TelemetryRow) => void }) => {
        onMessage = opts.onMessage;
        return { close: vi.fn() };
      },
    );
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      onMessage!(makeRow({ value: 700 }));
      onMessage!(makeRow({ value: 720 }));
      onMessage!(makeRow({ value: 750 }));
    });
    await waitFor(() => expect(result.current.channels.get("obd:RPM")).toBeDefined());
    const snap = result.current.channels.get("obd:RPM");
    expect(snap).toBeDefined();
    expect(snap!.history).toEqual([700, 720, 750]);
    expect(snap!.latest.value).toBe(750);
  });

  it("caps history at 120 samples (sliding ring buffer)", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    let onMessage: ((r: TelemetryRow) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation(
      (opts: { onMessage: (r: TelemetryRow) => void }) => {
        onMessage = opts.onMessage;
        return { close: vi.fn() };
      },
    );
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      for (let i = 0; i < 200; i++) onMessage!(makeRow({ value: i }));
    });
    await waitFor(() => expect(result.current.channels.get("obd:RPM")).toBeDefined());
    const snap = result.current.channels.get("obd:RPM");
    expect(snap!.history.length).toBe(120);
    // The latest values stay, the oldest fall off — last buffered value is 199
    expect(snap!.history[snap!.history.length - 1]).toBe(199);
    expect(snap!.history[0]).toBeGreaterThanOrEqual(80);
  });

  it("skips non-numeric values from the history but keeps the latest record", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    let onMessage: ((r: TelemetryRow) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation(
      (opts: { onMessage: (r: TelemetryRow) => void }) => {
        onMessage = opts.onMessage;
        return { close: vi.fn() };
      },
    );
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      onMessage!(makeRow({ channel: "VIN", source: "obd", value: "JN8AR..." }));
    });
    await waitFor(() => expect(result.current.channels.get("obd:VIN")).toBeDefined());
    const snap = result.current.channels.get("obd:VIN");
    expect(snap!.latest.value).toBe("JN8AR...");
    expect(snap!.history).toEqual([]); // non-numeric → no history entry
  });

  it("ignores malformed rows (missing source/channel)", async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    let onMessage: ((r: TelemetryRow) => void) | null = null;
    mocks.openReconnectingSocket.mockImplementation(
      (opts: { onMessage: (r: TelemetryRow) => void }) => {
        onMessage = opts.onMessage;
        return { close: vi.fn() };
      },
    );
    const { result } = renderHook(() => useTelemetryStream());
    await waitFor(() => expect(onMessage).not.toBeNull());

    act(() => {
      // @ts-expect-error — deliberately malformed
      onMessage!({ value: 1 });
    });
    expect(result.current.channels.size).toBe(0);
  });
});

describe("useTelemetryStream frame coalescing", () => {
  interface Wired {
    onMessage: (r: TelemetryRow) => void;
    onState: (s: string) => void;
    close: ReturnType<typeof vi.fn>;
  }

  /** Mount the hook under fake timers with the socket captured. `renders`
   *  counts committed renders, so "one state update" is observable. */
  const mount = async () => {
    mocks.getLatestPerChannel.mockResolvedValue({ rows: [], count: 0 });
    const wired = {} as Wired;
    wired.close = vi.fn();
    mocks.openReconnectingSocket.mockImplementation(
      (opts: { onMessage: Wired["onMessage"]; onState: Wired["onState"] }) => {
        wired.onMessage = opts.onMessage;
        wired.onState = opts.onState;
        return { close: wired.close };
      },
    );
    let renders = 0;
    const hook = renderHook(() => {
      renders += 1;
      return useTelemetryStream();
    });
    // Let the seed promise settle; its own flush is not what is under test.
    await act(async () => {
      await Promise.resolve();
    });
    return { wired, hook, renders: () => renders };
  };

  beforeEach(() => {
    vi.useFakeTimers({
      toFake: ["requestAnimationFrame", "cancelAnimationFrame", "setTimeout", "clearTimeout"],
    });
  });
  afterEach(() => vi.useRealTimers());

  it("turns N messages inside one frame into one state update, last value per channel wins", async () => {
    const { wired, hook, renders } = await mount();
    const before = renders();

    act(() => {
      for (let i = 0; i < 500; i++) {
        wired.onMessage(makeRow({ channel: "RPM", value: 700 + i }));
        wired.onMessage(makeRow({ channel: "SPEED", value: i, unit: "km/h" }));
      }
    });
    // Nothing is committed until the frame fires.
    expect(renders()).toBe(before);
    expect(hook.result.current.channels.size).toBe(0);

    act(() => {
      vi.advanceTimersByTime(17);
    });
    expect(renders()).toBe(before + 1);
    expect(hook.result.current.channels.get("obd:RPM")!.latest.value).toBe(1199);
    expect(hook.result.current.channels.get("obd:SPEED")!.latest.value).toBe(499);
    expect(hook.result.current.connection).toBe("open");

    // The frame is spent: no further update without a new message.
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(renders()).toBe(before + 1);
  });

  it("starts a new window after a flush", async () => {
    const { wired, renders, hook } = await mount();
    const before = renders();
    act(() => wired.onMessage(makeRow({ value: 1 })));
    act(() => void vi.advanceTimersByTime(17));
    act(() => wired.onMessage(makeRow({ value: 2 })));
    act(() => void vi.advanceTimersByTime(17));
    expect(renders()).toBe(before + 2);
    expect(hook.result.current.channels.get("obd:RPM")!.history).toEqual([1, 2]);
  });

  it("falls back to a timer when animation frames do not fire (hidden tab)", async () => {
    const raf = vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);
    const { wired, hook } = await mount();

    act(() => wired.onMessage(makeRow({ value: 900 })));
    act(() => void vi.advanceTimersByTime(FLUSH_FALLBACK_MS - 1));
    expect(hook.result.current.channels.size).toBe(0);
    act(() => void vi.advanceTimersByTime(1));
    expect(hook.result.current.channels.get("obd:RPM")!.latest.value).toBe(900);
    raf.mockRestore();
  });

  it("cancels the pending frame on unmount", async () => {
    const { wired, hook, renders } = await mount();
    const cancel = vi.spyOn(window, "cancelAnimationFrame");
    act(() => wired.onMessage(makeRow({ value: 1 })));
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    const before = renders();

    hook.unmount();
    expect(cancel).toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    act(() => void vi.advanceTimersByTime(1000));
    expect(renders()).toBe(before);
    cancel.mockRestore();
  });

  it("a connection change flushes what is pending at once and leaves no frame behind", async () => {
    const { wired, hook, renders } = await mount();
    act(() => wired.onMessage(makeRow({ value: 1234 })));
    const before = renders();

    act(() => wired.onState("connecting"));
    expect(renders()).toBe(before + 1);
    expect(hook.result.current.connection).toBe("connecting");
    expect(hook.result.current.channels.get("obd:RPM")!.latest.value).toBe(1234);
    expect(vi.getTimerCount()).toBe(0);

    act(() => void vi.advanceTimersByTime(1000));
    expect(renders()).toBe(before + 1);
  });

  it("keeps the Map identity across a connection-only change", async () => {
    const { wired, hook } = await mount();
    act(() => wired.onMessage(makeRow({ value: 1 })));
    act(() => void vi.advanceTimersByTime(17));
    const channels = hook.result.current.channels;
    act(() => wired.onState("closed"));
    expect(hook.result.current.connection).toBe("closed");
    expect(hook.result.current.channels).toBe(channels);
  });
});
