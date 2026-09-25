// Response and request types for the Engine Tuning workbench routes (R08.4).
// They used to be split across lib/workbenchApi.ts, lib/romMaps.ts,
// lib/romTrace.ts and CalibrationPage; the functions that call the routes
// live in api/workbench.ts.

// ---------------------------------------------------------------- logs

/** One row of the capture browser (`GET /control/logs`, W04.1). */
export interface LogSummary {
  session_id: string;
  is_capture: boolean;
  profile: string | null;
  started_at: string | null;
  duration_s: number | null;
  duration_source: "capture_manifest" | "parquet_span" | null;
  row_count: number | null;
  revision: { revision: string | null; source: string; conflict: boolean };
  revision_boundaries: RevisionBoundary[];
  quality: { verdict: string | null; scope: string | null } | null;
  end_reason: string | null;
  tag_count: number;
}

export interface LogsResponse {
  include_sessions: boolean;
  total: number;
  limit: number;
  capped: boolean;
  logs: LogSummary[];
}

export interface RevisionBoundary {
  revision: string;
  flashed_at: string;
}

export type Regime = "all" | "closed_loop" | "enrichment" | "fuel_cut" | "idle";

/** One log as a wide frame (`GET /control/sessions/{id}/frame`, W04.3). */
export interface FrameResponse {
  session_id: string;
  profile: string | null;
  grid: string;
  columns: Array<{ spec: string; unit: string | null }>;
  missing: Array<{ spec: string; reason: string }>;
  timestamps: string[];
  values: Record<string, Array<number | null>>;
  regime_flags: Record<string, Array<boolean | null>>;
  regimes: Record<string, { available: boolean; reason?: string; source?: string }>;
  regime_report: {
    regime: Regime;
    before: number;
    after: number;
    available?: boolean;
    reason?: string;
    removed?: number;
    unknown?: number;
  };
  freshness: { cells_with_any: number; kept: number; dropped: number; rule: string };
  rows_total: number;
  offset: number;
  rows_returned: number;
  row_cap: number;
  capped: boolean;
  tags: Array<{ t: string; label: string }>;
  revision: { revision: string | null; source: string; conflict: boolean };
  revision_boundaries: RevisionBoundary[];
  span: { first: string | null; last: string | null };
}

export interface GateReport {
  started: number;
  filtered: Record<string, number>;
  remaining: number;
  rule?: string;
}

export interface UnavailableSession {
  session_id: string;
  kind: "not_captured" | "refused" | "over_budget";
  reason: string;
  missing: string[];
}

export interface Conditions {
  sessions: string[];
  n_sessions: number;
  revisions: Record<string, string[]>;
  mixed_revisions: boolean;
  revision_boundaries: Record<string, RevisionBoundary[]>;
  baro: {
    spec: string;
    min_kpa: number | null;
    max_kpa: number | null;
    sessions_without: string[];
  };
  gates: GateReport | null;
  [key: string]: unknown;
}

export interface NoiseFloor {
  available: boolean;
  channel: string;
  reason?: string;
  unit?: string;
  sd?: number;
  mean?: number;
  n_cells?: number;
  min?: number | null;
  max?: number | null;
  conditions?: string;
}

export type UnavailableKind =
  "not_captured" | "refused" | "over_budget" | "gates_removed_everything" | "no_sessions" | null;

export interface Selection {
  sessions: string[];
  omitted_by_limit: string[];
  limit: number;
}

interface AnalysisBase {
  available: boolean;
  unavailable_kind: UnavailableKind;
  gates: GateReport | null;
  unavailable: UnavailableSession[];
  conditions: Conditions;
  selection?: Selection;
}

export interface DeliveryBand {
  rpm_band: string;
  n: number;
  commanded: number | null;
  measured: number | null;
  error_pct: number | null;
}

export interface DeliveryErrorResponse extends AnalysisBase {
  measured: string;
  min_load: number | null;
  n: number;
  band_averaged_pct?: number | null;
  pooled_pct?: number | null;
  bands: DeliveryBand[];
  per_session: Array<{ session_id: string; n: number; revision: string | null; gates: GateReport }>;
  noise_floor: NoiseFloor;
}

export interface Cell {
  rpm_index: number;
  airflow_index: number;
  rpm_range: [number, number];
  airflow_range: [number, number];
  n: number;
  sparse: boolean;
  [value: string]: number | boolean | [number, number] | null;
}

export interface Axes {
  rpm: string;
  airflow: string;
  rpm_edges: number[];
  airflow_edges: number[];
}

export interface TrimSurfaceResponse extends AnalysisBase {
  value: string;
  axes: Axes;
  min_n: number;
  n: number;
  banks: Record<string, { n: number; cells: Cell[] }>;
  per_session: Array<{
    session_id: string;
    revision: string | null;
    cells: Record<string, Cell[]>;
  }>;
}

export interface TimingResponse extends AnalysisBase {
  value: string;
  axes: Axes;
  min_n: number;
  n: number;
  cells: Cell[];
}

export interface ComparedCell {
  rpm_index: number;
  airflow_index: number;
  rpm_range: [number, number];
  airflow_range: [number, number];
  n_a: number;
  n_b: number;
  mean_a: number;
  mean_b: number;
  delta: number;
  dropped: boolean;
  delta_sigma: number | null;
}

export interface PullComparisonResponse extends AnalysisBase {
  value: string;
  revision_a: string;
  revision_b: string;
  axes: Axes;
  min_n: number;
  cells: ComparedCell[];
  n_cells: number;
  noise_floor: NoiseFloor | null;
}

export interface SessionChoice {
  sessions: string[];
  revision: string | null;
}

/** Why a whole-session read produced nothing. Each kind has its own words on
 *  the page — "deferred while recording" is not "the gates removed
 *  everything", and neither is "this capture never polled the channel". */
export interface Refusal {
  kind: "deferred" | "busy" | "unavailable" | "refused" | "no_sessions" | "not_found" | "error";
  message: string;
  profile?: string;
  laptopPath?: string;
}

// ---------------------------------------------------------------- calibration

/** Memory-channel evidence state (`GET /control/memory-channels`). */
export type MemoryState = "verified" | "checked earlier" | "carried" | "unverifiable";

export interface MemoryChannel {
  name: string;
  address: string;
  window: string | null;
  unit: string;
  verified_tune_revision: string;
  revision_carried_reason: string | null;
  known_answer: { relation: string; did: number; notes?: string | null } | null;
  state: MemoryState;
}

export interface FlashRecord {
  revision: string;
  flashed_at: string;
  calibration_id: string | null;
  k: number | null;
  rom_file: string | null;
  rom_sha256: string | null;
  notes: string;
}

/** `GET /control/calibration` — the running revision and the flash history.
 *  The one type for the route (AnalysisPage used to re-type it inline). */
export interface CalibrationResponse {
  running_revision: string | null;
  registry_calibration_id: string | null;
  ecu_calibration_id: string | null;
  ecu_calibration_read_at: string | null;
  calibration_matches_registry: boolean | null;
  calibration_changed: boolean;
  history: FlashRecord[];
}

/** `POST /control/calibration` body. */
export interface FlashRecordRequest {
  revision: string;
  flashed_at: string;
  calibration_id: string | null;
  k: number | null;
  rom_file: string | null;
  rom_sha256: string | null;
  notes: string;
}

export interface MemoryChannelsResponse {
  running_revision: string | null;
  count: number;
  channels: MemoryChannel[];
}

/** `POST /control/memory-channels/verify` body. */
export interface VerifyMemoryRequest {
  record_revision: string | null;
  carry_reason: string | null;
}

export interface VerifyMemoryResponse {
  verdicts: Array<{
    channel: string;
    outcome: string;
    reason: string;
    samples: number;
    agreements: number;
  }>;
  counts: Record<string, number>;
  recorded: number;
  notes: string[];
}

// ---------------------------------------------------------------- ROM

export interface RomProvider {
  spec: string;
  transform: "identity" | "scale" | "equiv_ratio_afr";
  factor: number | null;
}

export interface RomTable {
  slot: number;
  kind: "fuel_targets" | "fuel_compensation";
  title: string;
  x_label: string;
  x_unit: string;
  y_label: string;
  y_unit: string;
  unit: string;
  status: "available" | "tbd";
  reason: string | null;
  x_axis: number[];
  y_axis: number[];
  values: number[][];
  raw: number[][];
  /** W05.1: the ECM's trace channels for this table and whether their
   *  mapping onto its axes is verified. Null on TBD tables. */
  trace?: TableTrace | null;
}

export interface RomInspection {
  source_sha256: string;
  revision: string;
  recorded_revision: string | null;
  matches_recorded_revision: boolean;
  map_slots: number[];
  tables: RomTable[];
  live: Record<"rpm" | "base_fuel_schedule" | "commanded_afr", RomProvider | null>;
  warnings: string[];
  afr_stoich: number;
}

export interface AxisTrace {
  channel: string;
  axis_mapping: "verified" | "unverified";
  scale: number | null;
  offset: number | null;
  evidence: string;
  /** Scheduler request carrying the channel (`mem:<window>`), or null. */
  request_channel: string | null;
}

export interface TableTrace {
  x: AxisTrace;
  y: AxisTrace;
  unavailable_note: string | null;
}

/** `GET /control/sessions/{id}/cell-residency` (W05.6). */
export type CellResidency =
  | {
      available: true;
      session_id: string;
      table: string;
      revision: string | null;
      x_axis: number[];
      y_axis: number[];
      counts: number[][];
      dwell_s: number[][];
      samples: number;
      outside_axes: number;
      reason: null;
    }
  | {
      available: false;
      session_id: string;
      table: string;
      reason: string;
      message: string;
      missing?: string[];
    };
