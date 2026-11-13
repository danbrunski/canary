import { useEffect, useRef, useState } from "react";

import { openReconnectingSocket } from "../api/websocket";
import { getLatestPerChannel } from "../api/http";
import type { ConnectionState, TelemetryRow } from "../types";

/** Per-channel state the dashboard cares about. */
export interface ChannelSnapshot {
  latest: TelemetryRow;
  history: number[]; // numeric values; non-numeric channels carry an empty array
}

export interface TelemetryStreamState {
  channels: Map<string, ChannelSnapshot>;
  connection: ConnectionState;
}

const HISTORY_LENGTH = 120; // 2 minutes at 1 Hz; plenty for sparklines

/** Longest a pending flush may wait when `requestAnimationFrame` does not
 *  fire. rAF is paused in a background or hidden tab, and the audible alarms
 *  (`AlarmsPanel` evaluates `channels`) must keep working there, so a timer
 *  races the frame and whichever fires first flushes. Browsers clamp a hidden
 *  tab's timers to ~1 Hz, which is enough for an alarm and costs a hidden tab
 *  one render a second instead of none. */
export const FLUSH_FALLBACK_MS = 250;

/** Subscribe to `/ws/telemetry` plus seed from `/telemetry/latest?n=…` so the
 *  panel has values immediately instead of waiting for the first message.
 *
 *  Returns a Map keyed by ``"<source>:<channel>"`` — what the daemon sends and
 *  what every downstream display formatter expects.
 */
export function useTelemetryStream() {
  const [state, setState] = useState<TelemetryStreamState>({
    channels: new Map(),
    connection: "connecting",
  });
  // useRef-backed Map takes every message. The daemon publishes per record,
  // per decoded CAN row and per DID (~2,000 messages a second in a tuning
  // capture), so React state is replaced at most once per animation frame, not
  // once per message; the last value per channel wins because the ref is
  // overwritten on each ingest.
  const channelsRef = useRef<Map<string, ChannelSnapshot>>(new Map());

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    let connection: ConnectionState = "connecting";
    let dirty = false;
    let frame: number | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const cancelPending = () => {
      if (frame !== null) cancelAnimationFrame(frame);
      if (timer !== null) clearTimeout(timer);
      frame = null;
      timer = null;
    };

    // One state update for everything ingested since the last one, carrying
    // the newest connection state. The Map is only copied when a channel
    // changed, so a connection-only update keeps its identity.
    const flush = () => {
      cancelPending();
      if (cancelled) return;
      const channels = dirty ? new Map(channelsRef.current) : null;
      dirty = false;
      const next = connection;
      setState((prev) => ({ channels: channels ?? prev.channels, connection: next }));
    };

    const schedule = () => {
      if (frame !== null || timer !== null) return;
      frame = requestAnimationFrame(flush);
      timer = setTimeout(flush, FLUSH_FALLBACK_MS);
    };

    const ingest = (row: TelemetryRow) => {
      if (!row || !row.source || !row.channel) return;
      const key = `${row.source}:${row.channel}`;
      const prior = channelsRef.current.get(key);
      const history = prior ? prior.history.slice(-HISTORY_LENGTH + 1) : [];
      if (typeof row.value === "number" && Number.isFinite(row.value)) {
        history.push(row.value);
      }
      channelsRef.current.set(key, { latest: row, history });
      dirty = true;
    };

    // Seed via the REST snapshot so panels render with real values before
    // the first WebSocket frame lands. Per-channel (latest sample for every
    // channel) — a flat last-N seed is ~60:1 CAN-dominated and would leave
    // the ~1 Hz OBD gauges blank until the socket slowly backfilled them.
    getLatestPerChannel(controller.signal)
      .then((seed) => {
        if (cancelled) return;
        for (const row of seed.rows) ingest(row);
        flush();
      })
      .catch(() => {
        /* the WebSocket will populate; silent fallback */
      });

    const socket = openReconnectingSocket<TelemetryRow>({
      url: "/ws/telemetry",
      onMessage: (msg) => {
        ingest(msg);
        connection = "open";
        schedule();
      },
      // A connection change is rare and is shown at once; it also carries any
      // channels still waiting for their frame, so a reconnect leaves no
      // pending flush behind.
      onState: (next) => {
        connection = next;
        flush();
      },
    });

    return () => {
      cancelled = true;
      cancelPending();
      controller.abort();
      socket.close();
    };
  }, []);

  return state;
}
