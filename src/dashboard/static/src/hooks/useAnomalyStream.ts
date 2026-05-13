import { useEffect, useState } from "react";

import { getAnomalies } from "../api/http";
import { openReconnectingSocket } from "../api/websocket";
import type { AnomalyEvent, ConnectionState } from "../types";

export interface AnomalyStreamState {
  /** Events newest-first, capped at ``MAX_KEPT`` so the timeline doesn't grow unbounded. */
  events: AnomalyEvent[];
  connection: ConnectionState;
}

/** How many events to keep in memory. The dashboard timeline scrolls; further
 *  back, the user clicks through to the `/anomalies?since=…` paginated view. */
const MAX_KEPT = 500;

/** Subscribe to live anomaly events and seed from the JSONL via HTTP.
 *
 *  Initial fetch loads the last day's anomalies (no `since=` filter) so the
 *  timeline isn't empty after a daemon restart. WebSocket messages get
 *  prepended; duplicates by (timestamp, source, model_version) are dropped —
 *  the seed + first WS frame can otherwise overlap.
 */
export function useAnomalyStream() {
  const [state, setState] = useState<AnomalyStreamState>({
    events: [],
    connection: "connecting",
  });

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    const seen = new Set<string>();
    const keyOf = (e: AnomalyEvent) => `${e.timestamp}|${e.source}|${e.model_version}`;

    const prepend = (event: AnomalyEvent) => {
      const key = keyOf(event);
      if (seen.has(key)) return;
      seen.add(key);
      setState((prev) => {
        const events = [event, ...prev.events].slice(0, MAX_KEPT);
        return { ...prev, events };
      });
    };

    getAnomalies(undefined, controller.signal)
      .then((seed) => {
        if (cancelled) return;
        // Reverse so the newest end up first after iteration.
        for (const event of [...seed.events].reverse()) prepend(event);
      })
      .catch(() => {
        /* WS will populate; silent fallback */
      });

    const socket = openReconnectingSocket<AnomalyEvent>({
      url: "/ws/anomalies",
      onMessage: prepend,
      onState: (connection) => setState((prev) => ({ ...prev, connection })),
    });

    return () => {
      cancelled = true;
      controller.abort();
      socket.close();
    };
  }, []);

  return state;
}
