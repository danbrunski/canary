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

/** `GET /can/coverage` — static catalog of decodable broadcast-CAN signals
 *  (bindings + DBC) with their source CAN IDs. The CAN tab overlays live values
 *  + discovered/undecoded IDs from the telemetry stream client-side. */
export interface CanSignal {
  can_id: number;
  can_id_hex: string;
  name: string;
  unit: string;
  source: "decoded" | "dbc";
  /** Telemetry-stream key for the live value, e.g. `decoded:ECM_RPM`. */
  channel: string;
}

export interface CanCoverageResponse {
  signals: CanSignal[];
  signal_count: number;
  decoded_id_count: number;
  decoded_ids_hex: string[];
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
