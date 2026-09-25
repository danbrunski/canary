import { useEffect, useState } from "react";

import { openReconnectingSocket } from "../api/websocket";
import type { ConnectionState, ControlEvent } from "../types";

export interface ControlStreamState {
  connection: ConnectionState;
  events: ControlEvent[];
}

const MAX_EVENTS = 80;

export function useControlStream(token: string): ControlStreamState {
  const [state, setState] = useState<ControlStreamState>({
    connection: token ? "connecting" : "closed",
    events: [],
  });

  // Clearing the token closes the stream: reset while rendering, on the
  // change itself, rather than in the effect a commit later.
  const [streamToken, setStreamToken] = useState(token);
  if (token !== streamToken) {
    setStreamToken(token);
    if (!token) setState({ connection: "closed", events: [] });
  }

  useEffect(() => {
    if (!token) return;

    const socket = openReconnectingSocket<ControlEvent>({
      url: `/ws/control?token=${encodeURIComponent(token)}`,
      onMessage: (event) => {
        setState((prev) => ({
          ...prev,
          events: [event, ...prev.events].slice(0, MAX_EVENTS),
        }));
      },
      onState: (connection) => {
        setState((prev) => ({ ...prev, connection }));
      },
    });

    return () => socket.close();
  }, [token]);

  return state;
}
