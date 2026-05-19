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
  // `fused` (Phase 7.2): the agreement-aware calibrated severity over the
  // per-tick IF + LSTM + behavior-residual sources. Emitted alongside the
  // per-source events; its per-source breakdown is in `detail.sources`.
  source: "isolation_forest" | "lstm_autoencoder" | "fused";
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
  source: "isolation_forest" | "lstm_autoencoder" | "fused";
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
  feature_drift?: Record<string, unknown>;
  reconstruction_error_drift?: Record<string, unknown>;
  obd_scheduler?: Record<string, unknown>;
}

/** Response from `GET /telemetry/latest?n=...`. */
export interface TelemetryLatestResponse {
  rows: TelemetryRow[];
  count: number;
}

/** Connection state of a WebSocket subscription — the dashboard renders this
 *  as the small dot in the corner. */
export type ConnectionState = "connecting" | "open" | "closed" | "error";

/** Top-level context. `driving` = the locked, glanceable single screen;
 *  `parked` = the full Workbench with grouped navigation. The mode drives
 *  the *entire* layout, not just which panels are gated. */
export type AppMode = "driving" | "parked";

/** Workbench navigation model (replaces the old flat `DashboardView`).
 *  One canonical home per panel; new Phase-6 panels register in `nav.tsx`
 *  without ever growing the five top-level groups. */
export type NavGroupId = "live" | "diagnostics" | "models" | "data" | "control";

export type WorkbenchItem =
  | "live-gauges"
  | "live-charts"
  | "live-alarms"
  | "live-builder"
  | "diag-anomalies"
  | "diag-coverage"
  | "diag-dtc"
  | "diag-did"
  | "diag-system"
  | "diag-tpms"
  | "models-overview"
  | "models-behavior"
  | "models-training"
  | "models-tuning"
  | "models-health"
  | "data-sessions"
  | "data-health"
  | "data-export"
  | "control-main"
  | "control-test";

export interface NavItemDef {
  id: WorkbenchItem;
  label: string;
  group: NavGroupId;
  /** Backend exists; the rich UI is a later Phase-6 task (rendered as a
   *  labeled placeholder for now so the shell can already hold it). */
  placeholder?: boolean;
}

export interface NavGroupDef {
  id: NavGroupId;
  label: string;
  items: NavItemDef[];
}

// ---------------------------------------------------------------------------
// Behavior models — Task 4.E.9 dashboard performance feed.
// Mirrors src/decode/behavior_models.py.
// ---------------------------------------------------------------------------

export interface BehaviorAxis1D {
  name: string;
  channel: string;
  unit: string;
  domain: [number, number];
}

export interface BehaviorAxis2D {
  name: string;
  channel: string;
  unit: string;
  bin_edges: number[];
}

export interface BehaviorFitBase {
  sample_count: number;
  r_squared: number | null;
  sessions: string[];
  fitted_at: string;
  training_data_hash: string;
}

export interface PolynomialFit extends BehaviorFitBase {
  kind: "poly";
  coefficients: number[];
}

export interface LookupTableFit extends BehaviorFitBase {
  kind: "lookup_table";
  x_knots: number[];
  y_knots: number[];
}

export interface GridCell {
  mean: number;
  std: number;
  count: number;
}

export interface GridBinnedMeanFit extends BehaviorFitBase {
  kind: "grid_binned_mean";
  cells: GridCell[];
}

export interface DeviationConfig {
  kind: "absolute" | "relative";
  warning_band: number | null;
  critical_band: number | null;
  reference_std: number | null;
}

export interface BehaviorModel1D {
  name: string;
  kind: "surface_1d";
  output: string;
  output_unit: string;
  axis: BehaviorAxis1D;
  operating_mode: string | null;
  version: number;
  fit: PolynomialFit | LookupTableFit;
  deviation: DeviationConfig | null;
  notes: string | null;
}

export interface BehaviorModel2D {
  name: string;
  kind: "surface_2d";
  output: string;
  output_unit: string;
  axes: [BehaviorAxis2D, BehaviorAxis2D];
  operating_mode: string | null;
  version: number;
  fit: GridBinnedMeanFit;
  deviation: DeviationConfig | null;
  notes: string | null;
}

export type BehaviorModel = BehaviorModel1D | BehaviorModel2D;

export interface BehaviorModelsResponse {
  schema_version: number | null;
  models: BehaviorModel[];
}

// ---------------------------------------------------------------------------
// Behavior live overlay — mirrors src/dashboard/behavior_live.py. The daemon
// evaluates every surface each inference tick; this is the "you are here"
// feed the Models▸Behavior overlay draws on top of the static surfaces.
// ---------------------------------------------------------------------------

export type BehaviorSeverity = "ok" | "warning" | "critical";

/** One rolling-trail point. Any field is null when the model didn't speak
 *  for the operating point (outside domain, empty cell, mode mismatch). */
export interface BehaviorLivePoint {
  t: string | null;
  x: number | null;
  y: number | null;
  actual: number | null;
  expected: number | null;
  deviation: number | null;
  severity: BehaviorSeverity;
}

export interface BehaviorLiveModel {
  kind: "surface_1d" | "surface_2d";
  x: number | null;
  y: number | null;
  actual: number | null;
  expected: number | null;
  deviation: number | null;
  severity: BehaviorSeverity;
  /** model.operating_mode is unset OR equals the current operating mode. */
  mode_match: boolean;
  series: BehaviorLivePoint[];
}

/** `GET /models/behavior/live`. Empty `models` when no daemon is wired. */
export interface BehaviorLiveResponse {
  observed: number;
  timestamp: string | null;
  operating_mode: string | null;
  models: Record<string, BehaviorLiveModel>;
}

// ---------------------------------------------------------------------------
// Bindings — display-projection from data/bindings.yaml.
// ---------------------------------------------------------------------------

/** One binding entry as the dashboard sees it. Mirrors
 *  `read_bindings_for_display`'s payload in src/dashboard/api.py. */
export interface BindingDisplay {
  name: string;
  scale: number | null;
  offset: number | null;
  unit: string;
  confidence: "confirmed" | "high" | "medium" | "low" | "hypothesized";
  plausible_min: number | null;
  plausible_max: number | null;
}

/** Whole `/bindings` payload — keyed by `decoded:<NAME>`. */
export type BindingsByChannel = Record<string, BindingDisplay>;

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

// ---------------------------------------------------------------------------
// Phase-6 panel payloads.
// ---------------------------------------------------------------------------

/** `GET /obd/coverage` — mirrors `CoverageReport.to_dict()` (Task 6.3). */
export type PidCoverageStatus =
  | "decoded"
  | "supported_not_decoded"
  | "unsupported"
  | "unknown_pid";

export interface ObdCoverageRow {
  mode: number;
  pid: number;
  pid_hex: string;
  status: PidCoverageStatus;
  channel: string | null;
  display_name: string | null;
  category: string | null;
  unit: string | null;
  poll_tier: string | null;
  responded_recently: boolean;
  backed_off: boolean;
}

export interface ObdCoverageResponse {
  supported_count: number;
  decoded_count: number;
  supported_not_decoded_count: number;
  responded_count: number;
  backed_off_count: number;
  undecoded_supported: string[];
  rows: ObdCoverageRow[];
}

/** `GET /control/datasets/health` — mirrors `CorpusHealth.to_dict()`
 *  (Task 6.23). Per-session shape is loose where the panel only displays. */
export interface DatasetSessionHealth {
  session_id: string;
  row_count: number;
  duration_seconds: number | null;
  mode_coverage: Record<string, number>;
  tags: string[];
  gap_fraction: number | null;
  pinned: boolean;
  training_candidate: boolean;
  parse_error_ratio: number | null;
  warnings: string[];
}

export interface DatasetHealthResponse {
  sessions: DatasetSessionHealth[];
  total_rows: number;
  total_duration_seconds: number;
  mode_coverage: Record<string, number>;
  channel_availability: Record<string, number>;
  readiness: { ready: boolean; reasons: string[] };
  warnings: string[];
}

/** `GET /control/training-runs` — TrainingRunRecord list (Task 6.22). */
export interface TrainingRun {
  run_id: string;
  model_type: string;
  model_version: string | null;
  created_at: string;
  status: "succeeded" | "failed";
  input_sessions: string[];
  feature_schema: string[];
  hyperparams: Record<string, unknown>;
  metrics: Record<string, unknown>;
  artifacts: Record<string, string>;
  publish_status: "unpublished" | "published" | "active" | "rolled_back";
  notes: string | null;
}

export interface TrainingRunsResponse {
  runs: TrainingRun[];
  count: number;
}

/** `GET/POST /control/tuning` — versioned `TuningConfig` (Task 6.25). The
 *  rules are edited as JSON in the panel, so the inner shapes stay loose. */
export interface TuningConfig {
  version: number;
  threshold_overrides: Record<string, unknown>[];
  suppression_rules: Record<string, unknown>[];
  debounce_policies: Record<string, unknown>[];
}

export interface TuningResponse {
  tuning: TuningConfig;
}

export interface TuningEstimate {
  before: { evaluated: number; would_emit: number; would_suppress: number };
  after: { evaluated: number; would_emit: number; would_suppress: number };
  newly_suppressed: number;
}

/** `GET /control/feedback` (Task 6.24). */
export interface FeedbackRecord {
  anomaly_key: string;
  label: "true_positive" | "false_positive" | "ignored" | "needs_review";
  reason: string | null;
  labeled_at: string;
  event: Record<string, unknown>;
}

export interface FeedbackResponse {
  feedback: FeedbackRecord[];
  count: number;
  summary: Record<string, number>;
}

/** `GET /dids` — Nissan Mode-22 DID catalog (Task 6.16). Mirrors
 *  `DidMap.model_dump()`; only the fields the workbench renders are typed. */
export interface DidEntry {
  did: number;
  name: string;
  unit: string;
  confidence: "confirmed" | "high" | "medium" | "low" | "hypothesized";
  scale: number | null;
  offset: number | null;
  plausible_min: number | null;
  plausible_max: number | null;
  notes: string | null;
}

export interface DidMapResponse {
  schema_version: number | null;
  vehicle: Record<string, unknown>;
  access: Record<string, unknown> | null;
  dids: DidEntry[];
}
