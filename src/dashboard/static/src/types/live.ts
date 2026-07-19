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
  // `fused` (Phase 7.2): agreement-aware calibrated severity over the
  // per-tick sources. `behavior` (Phase 7.4): a behavior/physics-model
  // residual as a first-class detector — `detail.cause` is a human-readable
  // physical explanation and `detail.behavior_residuals` the expected/actual.
  // Both are emitted alongside the per-source events.
  source: "isolation_forest" | "lstm_autoencoder" | "fused" | "behavior";
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

/** One `/ws/scores` frame. Emitted every inference tick for *both* models
 *  (not just anomalies) so the UI can show each model's live current value,
 *  including a normal 0. `score` is null on a missing-feature tick — no
 *  trustworthy value, distinct from a real 0. */
export interface ScoreMessage {
  type: "score";
  source: "isolation_forest" | "lstm_autoencoder" | "fused" | "behavior";
  score: number | null;
  threshold: number | null;
  operating_mode: string | null;
  model_version: string;
  timestamp: string;
  missing_features: boolean;
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
    suppressed_tuning: number;
  } | null;
  health: StatusHealth | null;
  /** Latest GPS fix snapshot; null when no GPS is configured. */
  gps?: GpsStatus | null;
  /** Cumulative GPS trip odometer; null when no GPS is configured. */
  trip?: TripStats | null;
  /** Active sequence-detector backbone + what's available to switch to (9.6). */
  sequence_detector?: SequenceDetectorStatus | null;
  /** Active vehicle profile {slug, name, vin}; null on a non-scoped daemon. */
  profile?: { slug: string; name: string; vin: string | null } | null;
  /** Per-detector health beyond the aggregate `health` block (Task 2.1);
   *  null until a health monitor is wired (tests / bare app). */
  detectors?: DetectorsStatus | null;
}

/** `/status.detectors` (Task 2.1). `blind_modes` are operating modes whose
 *  trained IF feature schema has gone permanently missing from the live
 *  stream — anomaly emits are suppressed for them until they recover;
 *  `missing_feature_rate_by_mode` is the rolling gauge that feeds it. */
export interface DetectorsStatus {
  isolation_forest: {
    blind_modes: string[];
    missing_feature_rate_by_mode: Record<string, number>;
  };
}

/** `/status.sequence_detector` (and `GET/POST /control/sequence-detector`).
 *  Which sequence backbone (LSTM | TCN) is live, plus per-backbone artifact
 *  availability. `selector` is the durable config default; `active` is the live
 *  in-process choice (a switch is not persisted across restart). */
export interface SequenceDetectorStatus {
  selector: string;
  active: string | null;
  active_version: string | null;
  available: Record<
    string,
    { selector: string; artifact_present: boolean; version: string | null; active: boolean }
  >;
}

/** `/status.gps` — latest GPS fix from the serial NMEA receiver. Position
 *  fields are null until a fix is acquired; `has_fix` also goes false once the
 *  last fix is stale. */
export interface GpsStatus {
  has_fix: boolean;
  fix_quality: number;
  num_satellites: number;
  age_seconds: number;
  latitude: number | null;
  longitude: number | null;
  altitude: number | null;
  speed: number | null;
  heading: number | null;
  hdop: number | null;
}

/** `/status.trip` — cumulative trip odometer integrated from GPS speed,
 *  persisted across daemon restarts. */
export interface TripStats {
  distance_km: number;
  distance_miles: number;
  moving_time_seconds: number;
}

/** `/status.health` — mirrors `HealthMonitor.snapshot()` (Task 6.27).
 *  Drift sub-blocks are loosely typed; the Models▸Health panel reads them
 *  best-effort and they're absent until the daemon wires drift inputs. */
export interface StatusHealth {
  observed: number;
  inference_latency_ms: {
    mean: number | null;
    p95: number | null;
    max: number | null;
  };
  missing_feature_rate: number | null;
  data_freshness_seconds: number | null;
  model_swaps: Record<string, number>;
  active_model_version: string | null;
  system: Record<string, number | null>;
  power?: PowerStatus | null;
  feature_drift?: Record<string, unknown>;
  reconstruction_error_drift?: Record<string, unknown>;
  obd_scheduler?: Record<string, unknown>;
}

/** `/status.health.power` — decoded `vcgencmd get_throttled`. `now` is live
 *  conditions; `since_boot` flags latch once they've occurred (the
 *  "past-detected" anomalies). `null` off-Pi / when vcgencmd is unavailable. */
export interface PowerStatus {
  raw: number;
  now: {
    under_voltage: boolean;
    arm_frequency_capped: boolean;
    currently_throttled: boolean;
    soft_temp_limit: boolean;
  };
  since_boot: {
    under_voltage: boolean;
    arm_frequency_capped: boolean;
    throttling: boolean;
    soft_temp_limit: boolean;
  };
}

/** Response from `GET /telemetry/latest?n=...`. */
export interface TelemetryLatestResponse {
  rows: TelemetryRow[];
  count: number;
}

/** Connection state of a WebSocket subscription — the dashboard renders this
 *  as the small dot in the corner. */
export type ConnectionState = "connecting" | "open" | "closed" | "error";
