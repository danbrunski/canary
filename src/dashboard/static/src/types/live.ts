// Shared TypeScript types matching the FastAPI shapes.
//
// These mirror the Python schemas in src/dashboard/api.py and src/alerts/events.py.
// When the backend payload shape changes, update this file too — a `tsc -b`
// will catch the consumers that need adjusting. Optional fields use `| null`
// (not `?`) to mirror the JSON Pydantic produces, where absent fields land as
// explicit nulls when they have a None default.

import type { CaptureProfileState, ObdSchedulerStatus, ReplayStatus } from "./control";
import type { FeedbackRecord } from "./coverage";
import type { NetworkSummary } from "./network";

/** One row from `/telemetry/latest` or one message on `/ws/telemetry`. */
export interface TelemetryRow {
  timestamp: string | null;
  source: string;
  channel: string;
  value: number | string | boolean | null;
  unit: string | null;
}

/** Every `AnomalyEvent.source`, in the order of `src/alerts/events.py`
 *  `AnomalySource` (`tests/test_dashboard_ts_contracts.py` fails when the two
 *  differ). The first four are the per-tick detectors; the rest are
 *  low-frequency events from the diagnostic, trend, plausibility and committee
 *  checks. */
export const ANOMALY_SOURCES = [
  "isolation_forest",
  "lstm_autoencoder",
  "fused",
  "behavior",
  "dtc",
  "trend",
  "misfire",
  "plausibility",
  "committee",
] as const;

export type AnomalySource = (typeof ANOMALY_SOURCES)[number];

/** The sources `/ws/scores` carries: one frame per inference tick for each of
 *  the four per-tick detectors (`_publish_score` in
 *  `src/daemon/detectors/`). A subset of `ANOMALY_SOURCES`, so a source that
 *  is renamed there cannot survive here. */
export const SCORE_SOURCES = [
  "isolation_forest",
  "lstm_autoencoder",
  "fused",
  "behavior",
] as const satisfies readonly AnomalySource[];

export type ScoreSource = (typeof SCORE_SOURCES)[number];

/** One event from `/anomalies` or `/ws/anomalies` — `AnomalyEvent.to_dict()`. */
export interface AnomalyEvent {
  timestamp: string;
  // `fused` (Phase 7.2): agreement-aware calibrated severity over the
  // per-tick sources. `behavior` (Phase 7.4): a behavior/physics-model
  // residual as a first-class detector — `detail.cause` is a human-readable
  // physical explanation and `detail.behavior_residuals` the expected/actual.
  // Both are emitted alongside the per-source events. The rest are the
  // non-per-tick sources (`ANOMALY_SOURCES`).
  source: AnomalySource;
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
  /** A06.5 — `evidence` marks an event from a detector configured as
   *  non-alerting (`models.roles`): stored and listed, never an active
   *  alert. Absent on records written before A06.5, which are alerts. */
  role?: "alert" | "evidence";
  /** 6.4 — the emitter's dedupe identity when it differs from
   *  `(source, model_version)`: `"fused"`, `"dtc:<code>:<status>"`,
   *  `"trend:<stat>"`, ... `null` (every per-detector event) falls back to
   *  `model_version`. Absent on records written before it existed. */
  dedupe_key?: string | null;
  /** The operator's verdict, joined on by `GET /anomalies` when a feedback
   *  store is wired (Task 6.24): `null` is "unlabelled". Absent on a `/ws`
   *  frame and on a daemon without a store. */
  feedback?: FeedbackRecord | null;
}

/** One `/ws/scores` frame. Emitted every inference tick for *both* models
 *  (not just anomalies) so the UI can show each model's live current value,
 *  including a normal 0. `score` is null on a missing-feature tick — no
 *  trustworthy value, distinct from a real 0. */
export interface ScoreMessage {
  type: "score";
  source: ScoreSource;
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

/** Response from `GET /identity` (`src/dashboard/identity.py`): who answers
 *  on this origin. No token. `host_kind` is the connectivity signal: a
 *  `raspberry_pi` is the live Pi dashboard, anything else is a desk daemon. */
export interface IdentityResponse {
  product: "canary";
  api: string;
  hostname: string;
  host_kind: "raspberry_pi" | "host";
  profile: string | null;
  version: string;
  git_sha: string | null;
  dashboard_port: number;
}

/** A Pi's identity as the companion probed it (`src/companion/discovery.py`). */
export interface DaemonIdentity {
  product: "canary";
  hostname: string;
  host_kind: string;
  version: string | null;
  profile: string | null;
  dashboard_port: number;
  source_path: string;
}

/** The Pi a companion-mode daemon is linked to. `source` says how: found on
 *  the network, chosen in the dashboard, or named in the config. */
export interface PiLink {
  base_url: string;
  identity: DaemonIdentity | null;
  source: "selected" | "discovered" | "configured";
  selected_at: string | null;
}

export interface DiscoveredPi {
  base_url: string;
  host: string;
  source: "env" | "name" | "wired" | "sweep";
  identity: DaemonIdentity;
}

export interface CompanionDiscovery {
  state: "idle" | "running" | "done";
  ran_at: string | null;
  found: DiscoveredPi[];
  error: string | null;
}

/** Response from `GET /companion/status` — served by a daemon running with
 *  `companion.enabled` (`src/companion/routes.py`), same origin as the
 *  dashboard; 404 when companion mode is off. */
export interface CompanionStatusResponse {
  mode: string;
  library_root: string;
  sessions: number;
  models: number;
  datasets: number;
  queued_actions: number;
  pi: PiLink | null;
  pi_token_present: boolean;
  discovery: CompanionDiscovery;
}

/** Response from `GET /companion/discover`. */
export interface CompanionDiscoverResponse {
  found: DiscoveredPi[];
  pi: PiLink | null;
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
    /** A06.5 — events stored from evidence-role detectors (not alerts). */
    emitted_evidence?: number;
    suppressed_not_anomaly: number;
    suppressed_dedupe: number;
    suppressed_tuning: number;
  } | null;
  health: StatusHealth | null;
  /** Latest GPS fix snapshot; null when no GPS is configured. */
  gps?: GpsStatus | null;
  /** Cumulative GPS trip odometer; null when no GPS is configured. */
  trip?: TripStats | null;
  /** HAT IMU collector health (H02.5); null when no IMU is configured. */
  imu?: ImuStatus | null;
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
  /** The CAN transmit gate (`tx_gate.snapshot()`): passive switch, ECM
   *  session owner and the last foreign tester heard. Null when unwired. */
  passive?: PassiveGateStatus | null;
  capture_profile?: CaptureProfileState | null;
  /** The active capture profile again (`tuning_profile.snapshot()`), kept for
   *  older clients; `capture_profile` is the one to read. */
  tuning_mode?: CaptureProfileState | null;
  /** The OBD request-rate override (`bus_budget.snapshot()`): an armed one
   *  silently changes every channel's achieved rate. */
  bus_budget?: BusBudgetStatus | null;
  /** Seconds from process start to each startup stage (S01.1); `null` when no
   *  health monitor is wired. */
  startup?: StatusStartup | null;
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
  /** The address a phone should bookmark (P2.3); a cached read, null until
   *  the first one. Absent on an older daemon. */
  network?: NetworkSummary | null;
  /** The CANary-HAT on the header and how it was identified. Absent on an
   *  older daemon. */
  hat?: HatStatus | null;
}

/** `/status.hat` — `src.collection.hat_revision.hat_status`: the CANary-HAT
 *  on the header, how it was identified, the power board the config names, and
 *  what is wrong with the pair. A blank ID EEPROM reads `source: "probed"`
 *  (the board's own ADC answered); `none` drives no HAT pin at all. */
export interface HatStatus {
  /** `null`: no CANary-HAT identified. */
  revision: string | null;
  source: "config" | "eeprom" | "probed" | "none";
  eeprom_product: string | null;
  /** What the ADC probe saw; `null` when the config or the EEPROM decided. */
  probe: string | null;
  /** The revision's device-tree overlay, and whether the kernel shows it. */
  overlay: string | null;
  overlay_loaded: boolean | null;
  /** The CANary-PWR revision `hardware.power_board` names, or `null`. */
  power_board: string | null;
  problems: string[];
}

/** `/status.bus_budget` — `bus_budget.snapshot()`. */
export interface BusBudgetStatus {
  override_requests_per_second: number | null;
  source: string;
  ttl_remaining_seconds: number | null;
  max_requests_per_second: number;
}

/** `/status.startup` — `StartupTimeline.snapshot()`. Stage values are seconds
 *  from the process start, `null` for a stage not yet reached. */
export interface StatusStartup {
  clock: string;
  process_start: { at: string; source: string };
  stages: Record<string, number | null>;
  first_record_stored_by_source: Record<string, number>;
  first_record_timestamp_by_source: Record<string, string>;
  model_loaded: Record<string, number>;
  skipped: Record<string, string>;
  pending: string[];
  complete: boolean;
}

/** Response from `GET /status?view=compact` (`compact_status`): the pollable
 *  projection of `StatusResponse`. Each field sits at the path it has in the
 *  full payload but may carry fewer keys (`capture_profile` is `{name, active}`,
 *  `health.obd_scheduler` has its counters and no request table,
 *  `detectors` only `inference`). `null` is "not instrumented", never zero.
 *  `lib/statusView.ts` overlays it onto the last full payload. */
export interface CompactStatusResponse {
  view: "compact";
  /** When the daemon read the counters, so a poll can bound its own window. */
  served_at: string;
  vehicle_state: VehicleState;
  free_mb: number;
  emitter_counters: StatusResponse["emitter_counters"];
  passive: PassiveGateStatus | null;
  capture_profile: { name: string; active: boolean };
  health: Partial<StatusHealth> | null;
  detectors: { inference: Record<string, unknown> } | null;
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
  "ups_soc" | "storage_free_mb" | "memory_channels" | "calibration" | "vehicle_state";

export type TuningReadiness = Partial<Record<ReadinessKey, ReadinessEntry>>;

/** `/status.passive` — mirrors `src.collection.tx_gate.snapshot()`. */
export interface PassiveGateStatus {
  /** The operator's passive switch is armed. */
  enabled: boolean;
  source: string | null;
  suppressed_frames: number;
  ttl_remaining_seconds: number | null;
  /** Who holds the ECM's diagnostic session right now. */
  ecm_session_owner: "can_diagnostics" | "kline" | "foreign" | "rom_backup" | "memory_verify";
  /** Holder → seconds left on its claim (null: no expiry). */
  ecm_session_claims: Record<string, number | null>;
  tx_suppressed: boolean;
  /** The last frame another tester sent (H06.4); kept after its claim
   *  expires. Null: no other tester heard since start-up. */
  foreign_tester: {
    tester_address: string | null;
    module_address: string | null;
    last_heard_at: string;
    seconds_since_heard: number;
  } | null;
  survives_restart: boolean;
}

/** `/status.vehicle_state` — mirrors
 *  `src.dashboard.status.VehicleState.to_dict()`. */
export interface VehicleState {
  state:
    | "engine_running"
    | "ignition_only"
    | "engine_off"
    | "parked"
    | "bench"
    | "no_data"
    | "replay"
    | "passive"
    | "unknown";
  rpm: number | null;
  /** The decoded broadcast engine speed while CAN is fresh, else null. Kept
   *  beside `rpm` because the verdict prefers a fresh OBD rpm, whose last `0`
   *  outlives an engine start by seconds (R29.14). Absent from daemons older
   *  than that change. */
  broadcast_rpm?: number | null;
  coolant_temp: number | null;
  run_time: number | null;
  obd_freshness_seconds: number | null;
  can_freshness_seconds: number | null;
  /** The HAT's 12 V sense while fresh, else null. With both buses silent,
   *  a battery here is `parked`, its absence `no_data`. */
  supply_voltage?: number | null;
  replay_id: string | null;
  /** CANary is not transmitting (passive armed or another tester holds the
   *  ECM), so a stale OBD means "not asking" (D03.30). Absent from daemons
   *  older than that change. */
  passive?: boolean;
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

/** `/status.imu` — the IMU collector's counters (H02.5). `achieved_hz` is
 *  measured over the samples' own timestamps in the trailing
 *  `rate_window_seconds`, and is null with fewer than two samples there.
 *  `age_seconds` is null until the first sample; `fresh` uses the same
 *  threshold as the bus freshness behind `vehicle_state`. */
export interface ImuStatus {
  fresh: boolean;
  age_seconds: number | null;
  configured_hz: number | null;
  achieved_hz: number | null;
  rate_window_seconds: number;
  samples: number;
  overruns: number;
  undated: number;
  queue_dropped: number;
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
  /** Host load, memory and temperature (`HostMetrics.system()`), then the
   *  cgroup ceiling (`memory_limit_status`). Not a number map: the ceiling
   *  carries a flag and the path it was read from. `null` is "the platform
   *  does not say", never zero. */
  system: {
    load_1m: number | null;
    cpu_count: number | null;
    load_per_cpu: number | null;
    mem_used_fraction: number | null;
    mem_total_mb: number | null;
    cpu_temp_c: number | null;
    /** `true`: a byte ceiling is set. `false`: the controller is there and the
     *  ceiling is `max`. `null`: unknown (no cgroup file), neither reassurance
     *  nor alarm. */
    memory_limits_enforced: boolean | null;
    memory_max_bytes: number | null;
    memory_max_source: string | null;
    /** Busy fraction (0..1) over the sampler's trailing window; `null` until
     *  it has two samples. */
    cpu_percent: number | null;
  };
  power?: PowerStatus | null;
  /** Per model manager: what it serves against what its manifest sidecar
   *  claims, present only while they disagree (a partial deploy: the manifest
   *  landed, the binary did not). Absent when every manager agrees. */
  model_version_mismatches?: Record<string, VersionMismatch>;
  /** The serving sequence engine's suppress list against its artifact's (A01.5);
   *  absent while no engine is built. */
  sequence_suppression?: SequenceSuppression;
  /** Why the sequence detector is not served: its alert level was chosen at
   *  another forward cadence (A06.6). Absent while it serves. */
  sequence_cadence_refusal?: string;
  /** Each learned detector's configured and effective role, by source (A06.5). */
  detector_roles?: Record<string, DetectorRoleStatus>;
  /** Arm-to-now statistics for the active capture (C03.2), beside the
   *  scheduler's trailing 60 s numbers. Absent with no capture active. The
   *  per-channel and per-request tables are typed loosely. */
  capture_lifetime?: CaptureLifetimeStatus;
  /** Per telemetry sink (`ingest`, `storage`, `can_receive`, `obd_collect`):
   *  queue depth, high-water mark, drops and errors. Absent before any sink
   *  has reported. Each sink's state is typed loosely: the receive-path and
   *  hand-off counters differ by sink. */
  telemetry_sinks?: Record<string, Record<string, unknown>>;
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

/** One `/status.health.model_version_mismatches` entry. */
export interface VersionMismatch {
  serving: string;
  manifest: string;
}

/** `/status.health.sequence_suppression` — `SuppressionComparison.to_dict()`.
 *  `training` and the two `*_at_serve` lists are `null` when the manifest
 *  predates the record. */
export interface SequenceSuppression {
  model_version: string;
  recorded_at_training: boolean;
  training: string[] | null;
  serving: string[];
  differs: boolean;
  added_at_serve: string[] | null;
  dropped_at_serve: string[] | null;
  trained_columns_suppressed_at_serve: string[];
}

/** One `/status.health.detector_roles` entry — `RoleResolution.to_dict()`, or
 *  just `configured`, `effective: null` and `reason` before it is resolved. */
export interface DetectorRoleStatus {
  configured: "alert" | "evidence";
  effective: "alert" | "evidence" | null;
  reason: string;
  artifact_path?: string | null;
  artifact_sha256?: string | null;
  model_version?: string | null;
  verdict_path?: string | null;
  verdict?: Record<string, unknown> | null;
}

/** `/status.health.capture_lifetime` — `CaptureLifetime.to_dict(events=False)`. */
export interface CaptureLifetimeStatus {
  source: string;
  profile: string;
  generation_at_start: number;
  started_monotonic: number;
  started_wall: number;
  elapsed_s: number;
  paused_s: number;
  intended_active_s: number;
  pause_windows: unknown[];
  pause_reasons: string[];
  channels: Record<string, Record<string, unknown>>;
  requests: Record<string, Record<string, unknown>>;
  request_event_capacity: number;
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
