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
  // useRef-backed Map lets us mutate without forcing a full re-render of
  // every column on every message. We bump a counter for React's sake.
  const channelsRef = useRef<Map<string, ChannelSnapshot>>(new Map());

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    const ingest = (row: TelemetryRow) => {
      if (!row || !row.source || !row.channel) return;
      const key = `${row.source}:${row.channel}`;
      const prior = channelsRef.current.get(key);
      const history = prior ? prior.history.slice(-HISTORY_LENGTH + 1) : [];
      if (typeof row.value === "number" && Number.isFinite(row.value)) {
        history.push(row.value);
      }
      channelsRef.current.set(key, { latest: row, history });
    };

    // Seed via the REST snapshot so panels render with real values before
    // the first WebSocket frame lands. Per-channel (latest sample for every
    // channel) — a flat last-N seed is ~60:1 CAN-dominated and would leave
    // the ~1 Hz OBD gauges blank until the socket slowly backfilled them.
    getLatestPerChannel(controller.signal)
      .then((seed) => {
        if (cancelled) return;
        for (const row of seed.rows) ingest(row);
        setState({
          channels: new Map(channelsRef.current),
          connection: "connecting",
        });
      })
      .catch(() => {
        /* the WebSocket will populate; silent fallback */
      });

    const socket = openReconnectingSocket<TelemetryRow>({
      url: "/ws/telemetry",
      onMessage: (msg) => {
        ingest(msg);
        // Cheap: replace the Map reference, share the inner snapshot objects.
        setState({
          channels: new Map(channelsRef.current),
          connection: "open",
        });
      },
      onState: (connection) => {
        setState((prev) => ({ ...prev, connection }));
      },
    });

    return () => {
      cancelled = true;
      controller.abort();
      socket.close();
    };
  }, []);

  return state;
}
