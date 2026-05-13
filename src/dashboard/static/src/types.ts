// Shared TypeScript types matching the FastAPI shapes.
//
// These mirror the Python schemas in src/dashboard/api.py and src/alerts/events.py.
// When the backend payload shape changes, update this file too — a `tsc -b`
// will catch the consumers that need adjusting. Optional fields use `| null`
// (not `?`) to mirror the JSON Pydantic produces, where absent fields land as
// explicit nulls when they have a None default.

/** One row from `/telemetry/latest` or one message on `/ws/telemetry`. */
export interface TelemetryRow {
  timestamp: string | null;
  source: string;
  channel: string;
  value: number | string | boolean | null;
  unit: string | null;
}

/** One event from `/anomalies` or `/ws/anomalies` — `AnomalyEvent.to_dict()`. */
export interface AnomalyEvent {
  timestamp: string;
  source: "isolation_forest" | "lstm_autoencoder";
  model_version: string;
  score: number;
  threshold: number | null;
  affected_channels: string[];
  operating_mode: string | null;
  session: string | null;
  window_start_timestamp: string | null;
  window_end_timestamp: string | null;
  latency_ms: number | null;
  detail: Record<string, unknown>;
}

/** Response from `GET /anomalies?since=...`. */
export interface AnomaliesResponse {
  events: AnomalyEvent[];
  count: number;
}

/** Response from `GET /status`. Mirrors `StatusSnapshot.to_dict()`. */
export interface StatusResponse {
  storage_path: string;
  free_mb: number;
  latest_session: string | null;
  latest_session_path: string | null;
  model_versions: Record<string, string | null>;
  vehicle: string;
  emitter_counters: {
    emitted: number;
    suppressed_not_anomaly: number;
    suppressed_dedupe: number;
  } | null;
}

/** Response from `GET /telemetry/latest?n=...`. */
export interface TelemetryLatestResponse {
  rows: TelemetryRow[];
  count: number;
}

/** Connection state of a WebSocket subscription — the dashboard renders this
 *  as the small dot in the corner. */
export type ConnectionState = "connecting" | "open" | "closed" | "error";

/** UI mode. `parked` unlocks the maintenance / diagnostics panels. */
export type AppMode = "driving" | "parked";
