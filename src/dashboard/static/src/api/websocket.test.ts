import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { openReconnectingSocket } from "./websocket";

/** Minimal in-memory WebSocket mock that lets tests trigger open/close/error
 *  deterministically. The real `WebSocket` would race the event loop. */
class MockSocket {
  static instances: MockSocket[] = [];
  url: string;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  closeCalled = false;

  constructor(url: string) {
    this.url = url;
    MockSocket.instances.push(this);
  }
  close() {
    this.closeCalled = true;
    this.onclose?.(new CloseEvent("close"));
  }
  emitMessage(data: string) {
    this.onmessage?.(new MessageEvent("message", { data }));
  }
  emitError() {
    this.onerror?.(new Event("error"));
  }
  emitOpen() {
    this.onopen?.(new Event("open"));
  }
}

beforeEach(() => {
  MockSocket.instances = [];
  vi.stubGlobal("WebSocket", MockSocket);
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("openReconnectingSocket", () => {
  it("delivers parsed JSON messages via onMessage", () => {
    const messages: unknown[] = [];
    const states: string[] = [];
    openReconnectingSocket<{ a: number }>({
      url: "ws://test/ws/telemetry",
      onMessage: (m) => messages.push(m),
      onState: (s) => states.push(s),
    });
    expect(states).toContain("connecting");
    expect(MockSocket.instances).toHaveLength(1);
    MockSocket.instances[0].emitOpen();
    expect(states).toContain("open");

    MockSocket.instances[0].emitMessage(JSON.stringify({ a: 7 }));
    expect(messages).toEqual([{ a: 7 }]);
  });

  it("silently drops non-JSON messages without tearing down the connection", () => {
    const onMessage = vi.fn();
    openReconnectingSocket<unknown>({
      url: "ws://test/ws/x",
      onMessage,
      onState: () => {},
    });
    MockSocket.instances[0].emitOpen();
    MockSocket.instances[0].emitMessage("not-json");
    MockSocket.instances[0].emitMessage(JSON.stringify({ ok: true }));
    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(onMessage).toHaveBeenCalledWith({ ok: true });
  });

  it("reconnects with exponential backoff after close, resets on open", () => {
    const states: string[] = [];
    openReconnectingSocket({
      url: "ws://test/ws/x",
      onMessage: () => {},
      onState: (s) => states.push(s),
      initialDelayMs: 100,
      maxDelayMs: 5000,
    });
    expect(MockSocket.instances).toHaveLength(1);

    MockSocket.instances[0].emitOpen();
    // server drops connection
    MockSocket.instances[0].close();

    // Reconnect after the initial 100ms
    vi.advanceTimersByTime(100);
    expect(MockSocket.instances).toHaveLength(2);

    // Second close → next delay doubled (200ms)
    MockSocket.instances[1].close();
    vi.advanceTimersByTime(99);
    expect(MockSocket.instances).toHaveLength(2); // not yet
    vi.advanceTimersByTime(101); // total 200ms
    expect(MockSocket.instances).toHaveLength(3);

    // Successful open should reset the backoff for next reconnect
    MockSocket.instances[2].emitOpen();
    MockSocket.instances[2].close();
    vi.advanceTimersByTime(100); // back to initialDelayMs
    expect(MockSocket.instances).toHaveLength(4);
  });

  it("close() stops reconnect attempts", () => {
    const handle = openReconnectingSocket({
      url: "ws://test/ws/x",
      onMessage: () => {},
      onState: () => {},
      initialDelayMs: 50,
    });
    MockSocket.instances[0].emitOpen();
    handle.close();
    // The socket's onclose fires from `socket.close()`, but reconnect is
    // gated by the `closed` flag — no new sockets should be created.
    vi.advanceTimersByTime(10_000);
    expect(MockSocket.instances).toHaveLength(1);
  });

  it("transitions through error → reconnect when constructor throws", () => {
    const states: string[] = [];
    const FaultyWS = vi.fn(() => {
      throw new Error("blocked");
    });
    vi.stubGlobal("WebSocket", FaultyWS);

    openReconnectingSocket({
      url: "ws://test/x",
      onMessage: () => {},
      onState: (s) => states.push(s),
      initialDelayMs: 30,
    });
    expect(states.at(-1)).toBe("error");
    vi.advanceTimersByTime(35);
    expect(FaultyWS).toHaveBeenCalledTimes(2);
  });
});
