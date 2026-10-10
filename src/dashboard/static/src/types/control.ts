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
 *  label (blank until set). See src/vehicle/profiles.py. */
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
  /** Whether the tracked `profiles.yaml` holds this profile (else it exists
   *  only on this machine). Absent from a profile returned by an edit. */
  tracked?: boolean;
  /** Fields this machine's overlay sets over the tracked registry
   *  (`parameters.<name>` for parameters). */
  overrides?: string[];
}

/** One vehicle parameter's display metadata (`parameter_fields`). */
export interface VehicleParameterField {
  name: string;
  label: string;
  unit: string;
  kind: "number" | "evidence" | "choice" | "ratios";
  choices?: string[];
  /** The evidence field a change to this fact must come with. */
  evidence_field: string | null;
  /** For an evidence field: the facts it supports. */
  evidence_for: string[];
}

/** How this daemon start chose its profile. `vin_read` is the VIN the
 *  startup probe actually read off the vehicle (null when none was read). */
export interface ProfileResolution {
  slug: string;
  how: string;
  vin_read: string | null;
}

/** The `vehicle:` block the daemon is running (copied at startup). */
export interface VehicleInUse {
  make: string;
  model: string;
  year: number;
  vin: string;
  parameters: Record<string, unknown>;
}

export interface VehicleProfilesResponse {
  profiles: VehicleProfile[];
  active_slug: string;
  count: number;
  in_use?: VehicleInUse;
  /** Active-profile fields whose registry value the daemon does not run yet. */
  pending_restart?: string[];
  restart_required?: boolean;
  /** Where an edit to the active profile takes effect. */
  applies?: string;
  resolution?: ProfileResolution | null;
  parameter_fields?: VehicleParameterField[];
}

/** `PATCH /control/profiles/{slug}`: only the fields present are edited. */
export interface VehicleProfileUpdate {
  name?: string;
  make?: string;
  model?: string;
  year?: number | null;
  vin?: string;
  parameters?: Record<string, unknown>;
}

/** An edit or revert's response. */
export interface VehicleProfileEditResponse {
  profile: VehicleProfile;
  changed: string[];
  warnings: string[];
  vin_resolution: string | null;
  active: boolean;
  pending_restart: string[];
  restart_required: boolean;
  applies: string;
}

/** `POST /control/vin/read`. */
export interface VinReadResult {
  ok: boolean;
  vin: string | null;
  refusal: string | null;
  detail: string;
  raw: string | null;
  negative_response_code: string | null;
  check?: {
    vin: string;
    check_digit: "pass" | "fail";
    expected_check_digit: string;
    north_american: boolean;
    warning: string | null;
  } | null;
  check_problem?: string | null;
  held_by?: string | null;
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

/** `GET /control/captures/current` — the read, which answers 200 either way.
 *  Check `active`: a null `capture` is "no window is open", not a failure,
 *  and `detail` names why (a daemon restart drops the in-memory window while
 *  recording continues). The tag/stop siblings refuse with 409 instead. */
export interface CurrentCaptureResponse extends CaptureResponse {
  active: boolean;
  detail: string | null;
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
  notes?: string;
  /** Visible warning: this profile still needs an in-vehicle validation run. */
  experimental: boolean;
  /** OBD PIDs (Mode 01), channel -> relative rate weight. */
  mode01: Record<string, number>;
  /** Manufacturer DIDs (Mode 22 / UDS ReadDataByIdentifier). */
  mode22: string[];
  memory?: string[];
  mode01_batch_groups?: string[][];
  /** Selected DID name -> relative cadence weight; absent members use 1. */
  mode22_rate_weights: Record<string, number>;
  /**
   * Selected memory channel -> relative cadence weight; absent members use 1.
   * One read serves a whole window, so it runs at its members' largest weight.
   */
  memory_rate_weights?: Record<string, number>;
  mode01_enabled: boolean;
  mode22_enabled: boolean;
  did_interval_seconds: number | null;
  round_timestamps: boolean;
  /** Absolute requested cadence per selected channel. */
  target_rate_hz: number | null;
  /** Hold the manufacturer session open instead of alternating services. */
  did_continuous: boolean;
  bus_budget_override: number | null;
  /** null inherits collection priority; [] explicitly disables the lane. */
  priority_channels?: string[] | null;
  expected_total_demand_hz: number | null;
  capacity_evidence: string;
  /**
   * Channels that must be observed together for this profile's headline
   * analysis to yield a sample. Verified as a joint rate, because per-channel
   * rates can all pass while the tuple almost never completes.
   */
  required_tuple: string[];
  /** Per-channel floor the capture contract enforces; null promises nothing. */
  min_achieved_rate_hz: number | null;
  /** Refuse to arm without a named calibration. */
  requires_tune_revision: boolean;
  corpus_eligible: boolean;
  /** Exact behavior-surface recipes this isolated capture may train. */
  behavior_model_recipes: string[];
  storage_prefix: string;
  /**
   * Store every broadcast CAN frame (P02.10). Normal collection stores a frame
   * only when its payload changes; frame counts and sub-second timing need this.
   * Refused on the default profile.
   */
  store_every_frame?: boolean;
  /**
   * Store every USB ADC sample (firmware v2: 503.9 Hz) instead of the mean of
   * each five (~100 Hz). For studying the fuel rail's injector pulsation.
   */
  analog_full_rate?: boolean;
}

export interface CaptureProfileState {
  active: boolean;
  name: string;
  description: string;
  notes?: string;
  experimental: boolean;
  started_at: string | null;
  session_dir: string | null;
  source: string;
  channels: string[];
  mode01: Record<string, number>;
  mode22: string[];
  memory?: string[];
  mode22_rate_weights: Record<string, number>;
  memory_rate_weights?: Record<string, number>;
  mode01_enabled: boolean;
  mode22_enabled: boolean;
  did_interval_seconds: number | null;
  round_timestamps: boolean;
  target_rate_hz: number | null;
  did_continuous: boolean;
  expected_total_demand_hz: number | null;
  capacity_evidence: string;
  required_tuple: string[];
  min_achieved_rate_hz: number | null;
  requires_tune_revision: boolean;
  paused: boolean;
  running_tune_revision?: string | null;
  /** Rows accepted into the current session; absent if the writer is unwired. */
  rows_written?: number;
  corpus_eligible: boolean;
  behavior_model_recipes: string[];
  bus_budget_override: number | null;
}

/**
 * One constraint the capture failed (C03.10). `clause` is one of
 * `unanswered_input` | `rate_shortfall` | `acquisition_gap` |
 * `tuple_incomplete` | `tuple_skew` | `persistence_loss` |
 * `unknown_loss_counters`; `evidence` is the measurement behind it.
 */
export interface CaptureQualityFailure {
  clause: string;
  channels?: string[];
  detail?: string;
  evidence?: Record<string, unknown>;
}

/** Cadence (median interarrival) for one channel — never count / wall time. */
export interface CaptureChannelCadence {
  median_interarrival_s: number | null;
  p95_interarrival_s: number | null;
  hz: number | null;
  samples: number;
}

/** Coverage for one channel: observed versus eligible duration, pauses excluded. */
export interface CaptureChannelCoverage {
  observations: number;
  first_observed_at: number | null;
  last_observed_at: number | null;
  missing_initial_s: number;
  missing_final_s: number;
  max_gap_s: number;
  /** `[start_s, end_s, uncovered_s]` on the capture clock. */
  long_gaps: number[][];
  gap_threshold_s: number;
  uncovered_s: number;
  eligible_s: number;
  fraction: number | null;
}

export interface CaptureChannelQuality {
  channel: string;
  transport_channel: string;
  kind: string | null;
  requested_hz: number | null;
  /** `ok` | `missing` | `shortfall` | `gap`. */
  status: string;
  in_required_tuple: boolean;
  cadence: CaptureChannelCadence;
  coverage: CaptureChannelCoverage;
  /** Trailing 60 s rate from the live scheduler; shown, never scored. */
  trailing_hz: number | null;
}

/** Persisted complete-tuple yield, counted per capture round from the rows. */
export interface CaptureTupleQuality {
  required: string[];
  max_tuple_skew_s: number | null;
  /** Always a bound: the slowest member's cadence. */
  rate_bound_hz: number | null;
  /** `persisted_rows` | `no_round_evidence` | `unavailable` | `no_required_tuple`. */
  source: string;
  scheduled: number | null;
  attempted: number | null;
  complete: number | null;
  persisted: number | null;
  incomplete: number | null;
  skew_rejected: number | null;
  reason?: string;
}

/** One loss counter reconciled across the capture (C03.5). */
export interface CaptureLossCounter {
  scope: string;
  start: number | null;
  stop: number | null;
  delta: number | null;
  /** `verified` | `unknown` | `reset` — unknown is never zero. */
  status: string;
}

export interface CaptureLossScope {
  counters: Record<string, CaptureLossCounter>;
  /** Verified loss in this scope; null when any counter is unverified. */
  lost: number | null;
  unverified: string[];
  status: string;
}

/**
 * The capture contract verdict (sidecar `schema_version` 2). Returned by the
 * stop call, `GET /control/capture/quality`, and persisted beside the data
 * as `capture_quality.json`.
 *
 * `verdict` is `pass` | `degraded` | `fail` | `unverified`. `scope` says what interval
 * it covers: `live` (arm to now), `stop` (the whole capture, after drain and
 * flush), `recompute` (from the rows), or `trailing_snapshot` (a v1 sidecar:
 * 60 s at stop, not the capture). Absent (`null`) means no sidecar was
 * written — that is *unknown*, never a pass, and the UI must not render it
 * as success.
 */
export interface CaptureQuality {
  schema_version: number;
  scope: string;
  verdict: string;
  profile: string;
  interval: {
    source: string;
    elapsed_s: number | null;
    intended_active_s: number | null;
    paused_s: number | null;
    pause_windows: number[][];
  };
  required_tuple: string[];
  /** Slowest member of the required tuple: the ceiling on how often the whole
   *  tuple can complete. A bound — `tuples.complete` is the count. */
  tuple_rate_bound_hz: number | null;
  tuples: CaptureTupleQuality;
  failures: CaptureQualityFailure[];
  channels: CaptureChannelQuality[];
  losses: Record<string, CaptureLossScope>;
  persistence: Record<string, unknown> | null;
  trailing: { window_s: number; observed_seconds: number | null; hz: Record<string, number> };
  /** Batching counters for this capture only (arm-to-now delta). Counters are
   *  null unless `status` is `verified`; `lifetime` is the scheduler's
   *  cumulative block and never this capture's count. Null = batching off. */
  batching?: CaptureBatchingQuality | null;
  notes: string[];
}

export interface CaptureBatchingQuality {
  scope: "capture";
  status: "verified" | "reset" | "unknown";
  reset_counters: string[];
  batched_requests: number | null;
  batched_pids_requested: number | null;
  failures: number | null;
  desyncs: number | null;
  size_narrowings: number | null;
  failures_while_ecu_silent: number | null;
  mean_pids_per_batch: number | null;
  channel_reads_per_second: number | null;
  mode22: Record<string, number | null>;
  config: Record<string, unknown>;
  baseline: Record<string, unknown> | null;
  lifetime: Record<string, unknown> | null;
}

/** Summary of a bounded per-cycle history (roadmap C04.8). */
export interface HybridCycleSummary {
  n: number;
  median: number;
  p95: number;
  max: number;
}

/** Hybrid Mode 01 / Mode 22 cycle state inside `DidWindowStatus` (roadmap C04). */
export interface HybridCycleStatus {
  timing_policy: "cycle" | "interval";
  /** Target start-to-start period; null on the legacy interval policy. */
  cycle_seconds: number | null;
  cycle_feasible: boolean | null;
  plan: Record<string, number | boolean> | null;
  cycles_started: number;
  cycles_missed: number;
  mode01_shortfall: number;
  did_shortfall: number;
  cycle_duration_s: HybridCycleSummary | null;
  occupancy_s: { mode01: number; mode22: number; session: number } | null;
  transition_latency_s: {
    exit_to_mode01: HybridCycleSummary | null;
    entry_to_did: HybridCycleSummary | null;
  };
  cross_mode_separation_s: HybridCycleSummary | null;
  mode01_recovery_pending: boolean;
  mode01_recovered_age_s: number | null;
}

export interface DidWindowStatus {
  phase: "mode01" | "window" | "lapse";
  window_interval_seconds?: number;
  cycle?: HybridCycleStatus;
  fast_exits: number;
  fast_exit_failures: number;
  fast_exit_supported: boolean;
  /** Seconds until the fast exit is re-probed; 0 when it is live. Non-zero
   * means Mode 01 is degraded but recovering on its own. */
  fast_exit_retry_in_seconds?: number;
  /** NRC of the last failed exit, e.g. "subFunctionNotSupported". */
  last_exit_nrc_name?: string | null;
  windows_opened: number;
  windows_completed: number;
}

export interface ObdSchedulerRequestStatus {
  kind: string;
  channel: string;
  profile_channel?: string | null;
  capture_selected?: boolean;
  configured_rate_hz: number;
  requests_sent?: number;
  responses?: number;
  timeouts?: number;
  negative_responses?: number;
  backed_off: boolean;
  last_response_age_seconds: number | null;
}

/** Open-round snapshot inside `CaptureRoundStatus` (roadmap C02). */
export interface CaptureRoundOpenStatus {
  round_id: number;
  members: number;
  attempted: number;
  succeeded: number;
  deadline_in_s: number;
}

/**
 * Explicit capture rounds (roadmap C02): rounds are scheduled on monotonic
 * deadlines and end on membership completion or deadline. `skew_max_s` is
 * the largest measured acquisition spread inside one round — round identity
 * never substitutes for it.
 */
export interface CaptureRoundStatus {
  generation: number;
  period_s: number | null;
  started: number;
  complete: number;
  incomplete: number;
  skipped: number;
  skew_max_s: number;
  last_skew_s: number;
  open: CaptureRoundOpenStatus | null;
}

/** One support walk (Mode 01 PID groups or Mode 06 MID groups), roadmap C06. */
export interface DiscoveryItemStatus {
  service: string;
  complete: boolean;
  pending_group: string | null;
  groups_answered: number;
  attempts: number;
  requests_spent: number;
  seconds_spent: number;
  last_result: "answered" | "nrc" | "timeout" | null;
  last_nrc: string | null;
  next_probe_in_s: number | null;
  supported_count: number;
  responders: string[];
}

/**
 * Support discovery as scheduler work (roadmap C06.6): what is still pending,
 * what it has cost, and why it is waiting if it is.
 */
export interface DiscoveryStatus {
  key: string | null;
  cache_version: number;
  pending: string[];
  deferred_reason: string | null;
  requests_spent: number;
  seconds_spent: number;
  items: Record<string, DiscoveryItemStatus>;
}

/** A request wanted now that the eligibility matrix defers or refuses (C06.7). */
export interface DeferredRequestStatus {
  channel: string;
  kind: string;
  verdict: "deferred" | "ineligible";
  reason: string;
}

export interface ObdSchedulerStatus {
  achieved_hz_60s?: Record<string, number>;
  achieved_hz_capture?: Record<string, number>;
  capture_max_gap_seconds?: Record<string, number>;
  did_profile?: DidWindowStatus | null;
  requests?: ObdSchedulerRequestStatus[];
  rounds?: CaptureRoundStatus | null;
  discovery?: DiscoveryStatus | null;
  deferred?: DeferredRequestStatus[];
  resolved_plan?: ResolvedPollingPlan | null;
}

/** One unique `(service, ECU request id, identifier)` in a resolved plan. Its
 *  `rate_hz` comes from the same rate policy as the scheduler row's
 *  `configured_rate_hz` (R02.3); for a windowed DID that is its window cadence. */
export interface ResolvedPlanMember {
  kind: string;
  service: string;
  request_id: string;
  identifier: string;
  channel: string;
  profile_channel: string | null;
  rate_hz: number;
  configured_rate_hz: number;
  priority: boolean;
  required: boolean;
  response_payload_bytes: number | null;
  decoded_rows_per_read: number;
  /** Memory reads only: the `mem:<NAME>` channels the read decodes for this
   *  profile. The member itself is named after its window. */
  decoded_channels?: string[];
  capture_selected: boolean;
}

export interface ResolvedPollingPlan {
  members?: ResolvedPlanMember[];
  member_count: number;
  aliases_deduplicated: number;
  channel_reads_per_second: number;
  decoded_rows_per_second: number;
  complete_tuples_per_second: number | null;
  diagnostic_requests_per_second: {
    best_case_batched: number;
    single_request_fallback: number;
    session_control: number;
    discovery: number;
  };
  raw_frames_per_second: {
    best_case_batched: number;
    single_request_fallback: number;
    estimate_complete: boolean;
    unknown_response_width_channels: string[];
  };
  priority_requests_per_second_fallback: number;
  bus_budget_requests_per_second: number;
  priority_feasible: boolean;
  best_case_feasible: boolean;
  fallback_feasible: boolean;
  priority_source: "profile" | "collection";
}

export interface CaptureProfilesResponse {
  schema_version: number;
  profiles: CaptureProfile[];
  resolved_plans?: Record<string, ResolvedPollingPlan>;
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
  /** How the replay shown ended; null while running or before any replay.
   *  An idle status (`running: false`) keeps the last replay's id and final
   *  counters, so gate on `running`, never on `replay_id`. Absent from
   *  daemons older than 2026-10-05. */
  ended?: "completed" | "stopped" | "failed" | null;
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
