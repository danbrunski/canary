// ---------------------------------------------------------------------------
// Control plane — mirrors the authenticated /control/* FastAPI routes.
// ---------------------------------------------------------------------------

/** `GET /test-profiles` — mirrors src/control/test_profiles.py. A profile is
 *  a capture preset + an unordered palette of tappable event buttons. */
export interface TestEvent {
  label: string;
  tag: string;
  kind: "event" | "state";
  hint: string | null;
  advisory_duration_s: number | null;
}

export interface TestProfile {
  id: string;
  label: string;
  description: string;
  protocol: string;
  vehicle_state: string;
  purpose: string;
  order: number | null;
  events: TestEvent[];
}

export interface TestProfileError {
  file: string;
  error: string;
}

/** `GET /control/profiles` — a registered vehicle (multi-vehicle support).
 *  `slug` is the stable directory key; `name` is the operator-editable display
 *  label (blank until set). See src/profiles.py. */
export interface VehicleProfile {
  slug: string;
  vin: string;
  name: string;
  make: string;
  model: string;
  year: number | null;
  parameters: Record<string, unknown>;
  /** Mode 09 InfoType 0x04/0x06 (Task 3.4) — last-seen ECU calibration ID /
   *  verification number. Blank until the first successful probe. */
  calibration_id: string;
  cvn: string;
}

export interface VehicleProfilesResponse {
  profiles: VehicleProfile[];
  active_slug: string;
  count: number;
}

export interface TestProfilesResponse {
  profiles: TestProfile[];
  errors: TestProfileError[];
}

export interface CaptureState {
  capture_id: string;
  session_id: string;
  session_path: string;
  started_at: string;
  purpose: string | null;
  protocol: string | null;
  vehicle_state: string | null;
}

export interface CaptureResponse {
  capture: CaptureState | null;
}

export interface CaptureTagResponse {
  tag: Record<string, unknown>;
  capture: CaptureState | null;
}

export interface SessionSummary {
  session_id: string;
  session_path: string;
  started_at: string | null;
  telemetry_chunks: number;
  tag_count: number;
  has_manifest: boolean;
  pinned: boolean;
  metadata: Record<string, unknown>;
  size_bytes: number;
}

export interface SessionsResponse {
  sessions: SessionSummary[];
  count: number;
}

export interface CaptureProfile {
  name: string;
  description: string;
  /** Visible warning: this profile still needs an in-vehicle validation run. */
  experimental: boolean;
  /** OBD PIDs (Mode 01), channel -> relative rate weight. */
  mode01: Record<string, number>;
  /** Manufacturer DIDs (Mode 22 / UDS ReadDataByIdentifier). */
  mode22: string[];
  mode01_enabled: boolean;
  mode22_enabled: boolean;
  did_interval_seconds: number | null;
  round_timestamps: boolean;
  /** Absolute requested cadence per selected channel. */
  target_rate_hz: number | null;
  /** Hold the manufacturer session open instead of alternating services. */
  did_continuous: boolean;
  bus_budget_override: number | null;
  expected_total_demand_hz: number | null;
  capacity_evidence: string;
  broadcast_decode: boolean;
  corpus_eligible: boolean;
  /** Exact behavior-surface recipes this isolated capture may train. */
  behavior_model_recipes: string[];
  storage_prefix: string;
}

export interface CaptureProfileState {
  active: boolean;
  name: string;
  description: string;
  experimental: boolean;
  started_at: string | null;
  session_dir: string | null;
  source: string;
  channels: string[];
  mode01: Record<string, number>;
  mode22: string[];
  mode01_enabled: boolean;
  mode22_enabled: boolean;
  did_interval_seconds: number | null;
  round_timestamps: boolean;
  target_rate_hz: number | null;
  did_continuous: boolean;
  expected_total_demand_hz: number | null;
  capacity_evidence: string;
  paused: boolean;
  /** Rows accepted into the current session; absent if the writer is unwired. */
  rows_written?: number;
  broadcast_decode: boolean;
  corpus_eligible: boolean;
  behavior_model_recipes: string[];
  bus_budget_override: number | null;
}

export interface DidWindowStatus {
  phase: "mode01" | "window" | "lapse";
  window_interval_seconds?: number;
  fast_exits: number;
  fast_exit_failures: number;
  fast_exit_supported: boolean;
  windows_opened: number;
  windows_completed: number;
}

export interface ObdSchedulerRequestStatus {
  kind: string;
  channel: string;
  configured_rate_hz: number;
  backed_off: boolean;
  last_response_age_seconds: number | null;
}

export interface ObdSchedulerStatus {
  achieved_hz_60s?: Record<string, number>;
  achieved_hz_capture?: Record<string, number>;
  capture_max_gap_seconds?: Record<string, number>;
  did_profile?: DidWindowStatus | null;
  requests?: ObdSchedulerRequestStatus[];
}

export interface CaptureProfilesResponse {
  schema_version: number;
  profiles: CaptureProfile[];
}

export interface CaptureProfileWarning {
  model: string;
  missing_channels: string[];
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface JobRecord {
  job_id: string;
  kind: string;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  returncode: number | null;
  log_path: string;
  command: string[];
  params: Record<string, unknown>;
}

export interface JobsResponse {
  jobs: JobRecord[];
  count: number;
}

export interface JobResponse {
  job: JobRecord;
}

export interface JobLogsResponse {
  job_id: string;
  log: string;
}

export interface PreflightCheck {
  name: string;
  ok: boolean;
  severity: "error" | "warning" | "info";
  detail: string;
  [key: string]: unknown;
}

export interface PreflightResponse {
  ok: boolean;
  checked_at: string;
  checks: PreflightCheck[];
}

export interface VersionResponse {
  api_version: number;
  package_version: string;
  git_sha: string | null;
  config_token_env: string;
}

export interface ControlEvent {
  timestamp?: string;
  action?: string;
  result?: string;
  payload?: Record<string, unknown>;
  type?: string;
  job?: JobRecord;
}

/** `GET /control/power` — UPS (X1202) watchdog state, read from the
 *  watchdog's own heartbeat file (see scripts/ups_shutdown_watch.py). All
 *  fields but `keepalive_remaining_s` are `null` when the watchdog isn't
 *  running (`watchdog_running: false`) or hasn't written its first
 *  heartbeat yet. */
export interface PowerSnapshot {
  watchdog_running: boolean;
  watchdog_stale: boolean | null;
  armed: boolean | null;
  input_lost: boolean | null;
  on_battery: boolean | null;
  soc: number | null;
  vcell: number | null;
  seconds_lost: number | null;
  grace_s: number | null;
  keepalive_remaining_s: number;
  status_age_s: number | null;
}

export interface PowerStatusResponse {
  power: PowerSnapshot;
}

export interface PowerKeepaliveResponse {
  power: PowerSnapshot;
  expiry: number;
}

/** `GET /control/trends` (Task 4.5) — mirrors
 *  `src.analysis.trends.MannKendallResult`/`CusumResult`. */
export interface MannKendallResult {
  n: number;
  s: number;
  var_s: number;
  z: number;
  p_value: number;
  tau: number;
  trend: "increasing" | "decreasing" | "no trend";
}

export interface CusumResult {
  n: number;
  k: number;
  h: number;
  sh: number;
  sl: number;
  alarm: boolean;
  direction: "upper" | "lower" | null;
  first_alarm_index: number | null;
}

/** One session-fingerprint stat's drift picture — mirrors
 *  `src.analysis.trends.StatTrend.to_dict()`. `session_ids`/`values` are the
 *  full session-ordered series (small: one float per fingerprinted
 *  session), used for the dashboard sparkline. */
export interface StatTrend {
  stat: string;
  n: number;
  session_ids: string[];
  values: number[];
  latest_session: string;
  latest_value: number;
  reference_mean: number;
  reference_std: number;
  reference_band: [number, number];
  mann_kendall: MannKendallResult | null;
  cusum: CusumResult | null;
}

export interface TrendsResponse {
  available: boolean;
  reason?: string;
  frozen_at?: string;
  reference_sessions?: number;
  stats: Record<string, StatTrend>;
}

/** `GET/POST /control/replay` (+ `/control/replay/stop`) — mirrors
 *  `src.control.replay_control.ReplayStatus.to_dict()`. Also embedded at
 *  `/status.replay` (Task 1.2) so the shell can show the banner without a
 *  separate authenticated poll. */
export interface ReplayStatus {
  running: boolean;
  replay_id: string | null;
  source_session_id: string | null;
  started_at: string | null;
  speed: number | null;
  loop: boolean | null;
  output_session_dir: string | null;
  output_anomalies_path: string | null;
}

export interface ReplayStatusResponse {
  replay: ReplayStatus;
}

/** One IF-scored row from a `replay_window` report (Task 4.1) — mirrors
 *  `src.analysis.replay_window._build_if_block`'s per-row records. */
export interface ReplayIfScoreRow {
  timestamp: string | null;
  operating_mode: string | null;
  score: number | null;
  label: number | null;
}

export interface ReplayIfBlock {
  loaded: boolean;
  model_version: string | null;
  rows_scored: number;
  scores: ReplayIfScoreRow[];
  anomalies: ReplayIfScoreRow[];
}

/** One scored LSTM window from a `replay_window` report — mirrors
 *  `src.analysis.replay_window._build_lstm_block`'s per-window records. */
export interface ReplayLstmWindow {
  timestamp: string;
  window_start_timestamp: string | null;
  window_end_timestamp: string | null;
  global_score: number;
  global_threshold: number | null;
  anomalous_channels: string[];
  channel_scores: Record<string, number>;
  channel_thresholds: Record<string, number>;
  is_anomaly: boolean;
  latency_ms: number | null;
}

export interface ReplayLstmBlock {
  loaded: boolean;
  model_version: string | null;
  windows_scored: number;
  windows: ReplayLstmWindow[];
  anomalies: ReplayLstmWindow[];
}

/** `replay_window` job report (Task 4.1) — mirrors
 *  `src.analysis.replay_window.ReplayWindowReport.to_dict()`. Fetched via
 *  `GET /control/jobs/{id}/result` once the submitted job succeeds. */
export interface ReplayWindowReport {
  session_id: string;
  window: {
    start: string | null;
    end: string | null;
    actual_start: string | null;
    actual_end: string | null;
  };
  summary: {
    rows_scored: number;
    if_anomalies_replayed: number;
    lstm_windows_scored: number;
    lstm_anomalies_replayed: number;
    historical_events: number;
    matched: number;
    replayed_only: number;
    historical_only: number;
  };
  isolation_forest: ReplayIfBlock;
  lstm_autoencoder: ReplayLstmBlock;
  historical_events: Record<string, unknown>[];
  diff: Record<string, unknown>;
  warnings: string[];
}

/** `GET /control/jobs/{id}/result` — `result` is `null` until the job's
 *  report file has landed (e.g. still running) or the job kind has no
 *  associated result (404 instead, this shape only covers the 200 case). */
export interface JobResultResponse {
  job: JobRecord;
  result: ReplayWindowReport | null;
}
