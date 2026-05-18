import { useEffect, useState } from "react";

import { openReconnectingSocket } from "../api/websocket";
import type { ConnectionState, ScoreMessage } from "../types";

export type ScoreSource = ScoreMessage["source"];
export type ScoresBySource = Partial<Record<ScoreSource, ScoreMessage[]>>;

/** Per-source ring length. The live readout needs the latest value plus a
 *  short history for the sparkline + self-calibrating index reference; more
 *  than this just grows memory on a 1 Hz stream. */
export const MAX_PER_SOURCE = 120;

/** Pure reducer (testable without a socket): append one message to its
 *  source's ring, keeping the last `MAX_PER_SOURCE`. */
export function applyScore(state: ScoresBySource, msg: ScoreMessage): ScoresBySource {
  const prev = state[msg.source] ?? [];
  const next = [...prev, msg].slice(-MAX_PER_SOURCE);
  return { ...state, [msg.source]: next };
}

export interface ScoreStreamState {
  scores: ScoresBySource;
  connection: ConnectionState;
}

/** Subscribe to `/ws/scores` — every inference tick for both models. Unlike
 *  the anomaly stream there's no HTTP seed: scores are ephemeral, the live
 *  value is all the readout needs, so it just starts from the first frame. */
export function useScoreStream(): ScoreStreamState {
  const [scores, setScores] = useState<ScoresBySource>({});
  const [connection, setConnection] = useState<ConnectionState>("connecting");

  useEffect(() => {
    const socket = openReconnectingSocket<ScoreMessage>({
      url: "/ws/scores",
      onMessage: (msg) => {
        if (msg && msg.type === "score") setScores((s) => applyScore(s, msg));
      },
      onState: setConnection,
    });
    return () => socket.close();
  }, []);

  return { scores, connection };
}
