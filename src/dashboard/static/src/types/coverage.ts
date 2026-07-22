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

/** A custom PID (Task 2.6/2.8), listed separately from the Mode-01 registry
 *  `rows` above since it has no PID number in that registry. Live value
 *  streams at `obd:${channel}` (`channel` is already `custom:<name>`). */
export interface CustomPidCoverageEntry {
  name: string;
  display_name: string;
  channel: string;
  unit: string;
  category: string;
}

export interface ObdCoverageResponse {
  supported_count: number;
  decoded_count: number;
  supported_not_decoded_count: number;
  responded_count: number;
  backed_off_count: number;
  undecoded_supported: string[];
  rows: ObdCoverageRow[];
  custom_pids: CustomPidCoverageEntry[];
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
  /** Task 6.36 — which machine ran the job ("laptop" | "pi"); `null` for
   *  runs recorded before this field existed. */
  compute_target: string | null;
  /** Task 6.36 — the operator's explicit approve/unapprove decision,
   *  distinct from `publish_status` (the Pi-side upload/activation state). */
  approved: boolean;
  approved_at: string | null;
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
  /** Catalog names the daemon actually polls (`collection.obd_dids`). The
   *  catalog is the whole RE record, so most entries are characterized but
   *  never requested — without this an unpolled DID is indistinguishable
   *  from one that stopped answering. */
  polled: string[];
}

/** `GET /obd/dtc` (Task 1.5) — the cached `DtcReport` plus staleness.
 *  Mirrors `DtcReportResponse` in `src/dashboard/schemas.py`. Read-only:
 *  there is no clear-codes endpoint or UI control. */
export interface DtcCodeEntry {
  code: string;
  description: string;
  system: string;
  generic: boolean;
  curated: boolean;
}

export interface DtcReportResponse {
  captured_at: string | null;
  stored: DtcCodeEntry[];
  pending: DtcCodeEntry[];
  permanent: DtcCodeEntry[];
  mil_on: boolean | null;
  dtc_count: number;
  /** Mode 02 freeze frame (Task 3.3) — channel name -> decoded value, read
   *  once when a stored DTC most recently first appeared. `null` until the
   *  first stored code triggers a read. */
  freeze_frame: Record<string, number | string> | null;
  stale: boolean;
}

/** `GET /obd/monitors` (Task 3.1) — cached Mode 06 on-board monitor sweep:
 *  per-cylinder misfire counts + raw monitor test results, with staleness.
 *  Mirrors the payload shape `src.control.monitor_state.MonitorState.snapshot`
 *  serves. */
export interface MonitorTestResultEntry {
  monitor_id: number;
  test_id: number;
  unit_and_scaling_id: number;
  test_value: number;
  min_limit: number;
  max_limit: number;
  passed: boolean;
}

export interface MonitorsResponse {
  captured_at: string | null;
  /** Cylinder number (as a string key, JSON object) -> raw misfire count. */
  misfire_counts: Record<string, number>;
  results: MonitorTestResultEntry[];
  stale: boolean;
}

/** `GET/POST/DELETE /control/custom-pids` (Task 2.8c) — mirrors
 *  `CustomPid`/`CustomPidSet` in `src/collection/custom_pids.py`. Streams
 *  live as `obd:custom:<name>` (the collector tags every reading `source:
 *  "obd"`, so the request-scoped `custom:` prefix rides in the channel
 *  field itself, not the source). */
export interface CustomPid {
  name: string;
  display_name: string;
  mode: number;
  pid: number;
  formula: string;
  unit: string;
  category: string;
  minimum: number | null;
  maximum: number | null;
  poll_tier: "fast" | "medium" | "slow" | "on_demand";
  dashboard_hint: "gauge" | "graph" | "text" | "indicator" | "list";
  header: number | null;
}

export interface CustomPidsResponse {
  custom_pids: CustomPid[];
  count: number;
}

export interface CustomPidImportResponse {
  added: CustomPid[];
  added_count: number;
  warnings: string[];
}
