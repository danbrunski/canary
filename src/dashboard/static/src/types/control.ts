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
