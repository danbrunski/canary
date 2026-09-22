// Shared TypeScript types matching the FastAPI shapes.
//
// These mirror the Python schemas in src/dashboard/api.py and src/alerts/events.py.
// When the backend payload shape changes, update this file too — a `tsc -b`
// will catch the consumers that need adjusting. Optional fields use `| null`
// (not `?`) to mirror the JSON Pydantic produces, where absent fields land as
// explicit nulls when they have a None default.

import type { CaptureProfileState, ObdSchedulerStatus, ReplayStatus } from "./control";

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

/** Response from `GET /companion/status` — only served by the companion
 *  app (`src/companion/routes.py`), never the Pi daemon. Its mere presence
 *  is the 6.35 connectivity signal: reachable means this dashboard instance
 *  is backed by the local library, not a live Pi. */
export interface CompanionStatusResponse {
  mode: string;
  library_root: string;
  sessions: number;
  models: number;
  datasets: number;
  queued_actions: number;
  config_path: string;
}

/** Response from `GET /status`. Mirrors `StatusSnapshot.to_dict()`. */
export interface StatusResponse {
  storage_path: string;
  free_mb: number;
  storage_warning_mb?: number | null;
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
  /** Per-loaded-model provenance off each manifest (Task 6.5), keyed like
   *  `model_versions`; null when no `models_dir` is configured (tests /
   *  bare app). A given model's entry is itself null on an unreadable
   *  manifest. */
  model_provenance?: Record<string, ModelProvenance | null> | null;
  /** Accepted-baseline overrides (2.3, online-training-strategy.md
   *  Phase 2) — always present (an empty document reads as zero). */
  accepted_baseline_overrides?: AcceptedBaselineOverridesStatus;
  /** Derived "what's the truck actually doing" classification (Task 1.1,
   *  `classify_vehicle_state`) — read this before concluding anything about
   *  detector behavior from silence. Null only on a bare/test daemon that
   *  never calls `build_status`'s vehicle-state branch. */
  vehicle_state?: VehicleState | null;
  /** Runtime replay state (Task 1.2), mirrored here so the shell can render
   *  the "you're looking at replayed data" banner off the same poll that
   *  already drives everything else — no separate authenticated call. Null
   *  when the daemon has no replay controller wired (tests / bare app). */
  replay?: ReplayStatus | null;
  capture_profile?: CaptureProfileState | null;
  last_capture?: {
    name: string;
    session_dir: string;
    started_at: string;
    ended_at: string | null;
    end_reason: "operator_stop" | "profile_switch" | "daemon_shutdown" | "interrupted" | null;
    quality_verdict: string | null;
  } | null;
  /** ECU-reported calibration id from service 0x21 (W02.2); null until read. */
  ecu_calibration_id?: string | null;
  ecu_calibration_read_at?: string | null;
  /** True/false against the flash history's running revision; null when
   *  either side is unknown. Never derived from the revision name. */
  calibration_matches_registry?: boolean | null;
  /** The flash history's running revision name and its recorded id. */
  registry_revision?: string | null;
  registry_calibration_id?: string | null;
  /** The id changed during this daemon lifetime (W02.3). */
  ecu_calibration_changed?: boolean;
  /** A reflash the registry has not heard about, or one seen mid-lifetime. */
  calibration_alert?: boolean;
  calibration_alert_reason?: string | null;
  /** Engine Tuning readiness chips (W02.4). Absent on an older daemon. */
  tuning_readiness?: TuningReadiness | null;
}

/** One `/status.tuning_readiness` entry. `ok: null` = reported but not
 *  judged (amber); `value: null` = not reported (grey), never green. */
export interface ReadinessEntry {
  ok: boolean | null;
  value: unknown;
  detail: string;
  fix: string | null;
}

export type ReadinessKey =
  | "ups_soc"
  | "storage_free_mb"
  | "memory_channels"
  | "calibration"
  | "vehicle_state";

export type TuningReadiness = Partial<Record<ReadinessKey, ReadinessEntry>>;

/** `/status.vehicle_state` — mirrors
 *  `src.dashboard.status.VehicleState.to_dict()`. */
export interface VehicleState {
  state:
    | "engine_running"
    | "ignition_only"
    | "engine_off"
    | "no_data"
    | "replay"
    | "unknown";
  rpm: number | null;
  coolant_temp: number | null;
  run_time: number | null;
  obd_freshness_seconds: number | null;
  can_freshness_seconds: number | null;
  replay_id: string | null;
  /** Wall clock the freshness numbers were measured against. The derivation
   *  reads Parquet, so the daemon shares one reading across `/status` views
   *  for a couple of seconds — the stamp travels with the state so a reused
   *  reading can't be mistaken for a fresh one. */
  derived_at: string | null;
}

/** `/status.accepted_baseline_overrides` (2.3). */
export interface AcceptedBaselineOverridesStatus {
  /** Non-expired override count — what the StatusStrip's "N active" reads. */
  active: number;
  /** Every override, active or expired, for the expandable detail view. */
  overrides: AcceptedBaselineOverrideSummary[];
}

/** One entry in `/status.accepted_baseline_overrides.overrides`. */
export interface AcceptedBaselineOverrideSummary {
  target: string;
  channel: string | null;
  behavior_model: string | null;
  operating_mode: string | null;
  accepted_center: number | null;
  band: {
    kind: "directional";
    tolerance: number;
    worse_direction: "positive" | "negative";
    worse_warning: number;
    worse_critical: number;
    better_direction: "ignore";
  };
  declared_by: string;
  declared_at: string;
  review_by: string;
  reason: string;
  /** Past `review_by` — inert (no longer recenters/suppresses) and needs
   *  re-confirmation rather than being silently dropped from view. */
  expired: boolean;
}

/** `/status.model_provenance[name]` (Task 6.5). */
export interface ModelProvenance {
  model_version: string | null;
  created_at: string | null;
  training_data_hash: string | null;
  training_sessions: string[];
  artifact_format_version: number | null;
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
  obd_scheduler?: ObdSchedulerStatus;
  /** can0 rx overrun/drop/error sysfs counters (Task 6.8); absent when
   *  there's no can0 interface (dev laptop, or a Pi before the CAN HAT is
   *  up). Nonzero growth in `since_start` during a drive is the (only)
   *  intended trigger to revisit the capture architecture — this is a
   *  passive counter, not an alert. */
  can0?: Can0Counters;
}

/** `/status.health.can0` (Task 6.8). Raw counters read at this poll, plus
 *  the delta since the daemon's `HealthMonitor` first observed them. */
export interface Can0Counters {
  rx_over_errors: number;
  rx_dropped: number;
  rx_errors: number;
  since_start: {
    rx_over_errors: number;
    rx_dropped: number;
    rx_errors: number;
  };
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
