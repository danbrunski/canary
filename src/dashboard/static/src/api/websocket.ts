// Reconnecting WebSocket helper.
//
// The dashboard's two live streams (/ws/telemetry, /ws/anomalies) share
// identical semantics: open a JSON-message socket, deliver each parsed
// message via callback, reconnect on close/error with exponential backoff
// capped at a few seconds. Pages don't usually stay live long enough to
// need anything fancier; the daemon's emitter side already drops oldest
// messages on backpressure (see src/daemon/broadcast.py), so a brief
// disconnect → reconnect just lands the dashboard back where the live data
// is now.

import type { ConnectionState } from "../types";

export interface ReconnectingSocketOptions<T> {
  url: string;
  /** Called on every parsed JSON message. Caller decides what to do with it. */
  onMessage: (data: T) => void;
  /** Called on every state change (connecting → open / error / closed). */
  onState: (state: ConnectionState) => void;
  /** Initial / minimum reconnect delay. Default 250 ms. */
  initialDelayMs?: number;
  /** Cap on backoff. Default 5 s. */
  maxDelayMs?: number;
}

export interface ReconnectingSocket {
  /** Close the socket and stop reconnecting. Idempotent. */
  close: () => void;
}

/** Open a JSON WebSocket that auto-reconnects until ``close()`` is called.
 *
 *  Each successful open resets the backoff. Parse failures (non-JSON
 *  messages) are silently dropped — the daemon only ever sends JSON, but a
 *  proxy / dev tool might inject something else, and we'd rather skip the
 *  bad message than tear down the connection.
 */
export function openReconnectingSocket<T>(
  options: ReconnectingSocketOptions<T>,
): ReconnectingSocket {
  const { url, onMessage, onState, initialDelayMs = 250, maxDelayMs = 5000 } = options;
  let closed = false;
  let delay = initialDelayMs;
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  const absoluteUrl = url.startsWith("ws")
    ? url
    : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${url}`;

  const scheduleReconnect = () => {
    if (closed) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
    delay = Math.min(delay * 2, maxDelayMs);
  };

  const connect = () => {
    if (closed) return;
    onState("connecting");
    try {
      socket = new WebSocket(absoluteUrl);
    } catch {
      onState("error");
      scheduleReconnect();
      return;
    }
    socket.onopen = () => {
      delay = initialDelayMs;
      onState("open");
    };
    socket.onmessage = (event: MessageEvent<unknown>) => {
      // Text frames only: a binary frame never parsed (JSON.parse of a Blob
      // throws), so it is skipped before the attempt rather than inside it.
      if (typeof event.data !== "string") return;
      try {
        onMessage(JSON.parse(event.data) as T);
      } catch {
        // Skip; see module-level comment.
      }
    };
    socket.onerror = () => {
      onState("error");
    };
    socket.onclose = () => {
      onState("closed");
      socket = null;
      scheduleReconnect();
    };
  };

  connect();

  return {
    close() {
      closed = true;
      if (reconnectTimer !== null) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      if (socket !== null) {
        socket.close();
        socket = null;
      }
    },
  };
}
