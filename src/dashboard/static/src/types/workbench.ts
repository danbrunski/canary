// Response and request types for the Engine Tuning workbench routes (R08.4).
// They used to be split across lib/workbenchApi.ts, lib/romMaps.ts,
// lib/romTrace.ts and CalibrationPage; the functions that call the routes
// live in api/workbench.ts.

import type { RomEditorDisagreement } from "./romLibrary";

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

/** Whether a noise floor may grade a comparison (R05.2). A floor is valid
 *  only under the conditions it was measured in; a mismatch is refused, and
 *  the refusal names both regimes. */
export type Significance =
  | {
      available: true;
      channel: string;
      sd: number;
      conditions: string;
      sigma_threshold: number;
    }
  | {
      available: false;
      channel: string;
      kind: "no_floor" | "conditions_mismatch";
      reason: string;
      floor_conditions: string | null;
      requested_conditions: string;
    };

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
  /** Per-sample spread inside the band (R05.16). */
  p05_pct: number | null;
  p95_pct: number | null;
}

/** How target and measurement were paired: by acquisition time, one read each,
 *  within the vehicle's declared separation (R05.16). */
export interface DeliveryPairing {
  method: string;
  max_separation_s: number;
  target_source: string;
  target_spec: string;
  measured_source: string;
  measured_spec: string;
  /** The regime whose rule every surviving sample passed (delivery error is at enrichment). */
  regime: "enrichment";
  steady_target_dwell_s: number;
  target_step_tolerance: number;
  post_fuel_cut_holdoff_s: number;
  /** False on a route whose delay is not estimated (PID 0x44 shares a batch with λ). */
  transport_delay_modelled: boolean;
  /** Frozen exhaust transport delay: the λ read nearest t + delay pairs. */
  transport_delay_s: number | null;
  transport_delay_bracket_s: [number, number] | null;
  transport_delay_note: string;
  /** Drives that chose these constants; a result on one of them is not held-out evidence. */
  constants_informed_by: string[];
  /** Single-session results only. */
  session_informed_constants?: boolean;
}

/** The same result at each end of the delay bracket; unstable says why. */
export interface DelaySensitivity {
  at: Array<{
    delay_s: number;
    n: number;
    band_averaged_pct: number | null;
    pooled_pct: number | null;
  }>;
  stable: boolean;
  reason: string | null;
  spread_pct: number | null;
}

export interface DeliveryErrorResponse extends AnalysisBase {
  /** 2 = acquisition-paired engine; absent on a pre-R05.16 result. */
  engine_version?: number;
  pairing: DeliveryPairing;
  /** Null on a route that models no transport delay. */
  delay_sensitivity: DelaySensitivity | null;
  measured: string;
  min_load: number | null;
  n: number;
  band_averaged_pct?: number | null;
  pooled_pct?: number | null;
  bands: DeliveryBand[];
  per_session: Array<{
    session_id: string;
    n: number;
    revision: string | null;
    gates: GateReport;
    /** This drive chose the enrichment constants: not independent evidence. */
    informed_constants: boolean;
  }>;
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
  /** ``null`` when no floor channel was asked for. */
  significance?: Significance | null;
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
  /** Memory relations that read this channel (D03.38), wherever declared. */
  relations?: MemoryRelation[];
  state: MemoryState;
}

/** A declared memory relation; `owner` is the channel it is declared on. */
export interface MemoryRelation {
  owner: string;
  kind: "memory" | "flag_equivalence" | "invariant" | "mode01_pid";
  notes?: string | null;
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
  /** W05.1: the ROM's axis bytes, which a `raw` trace axis lands on. Empty
   *  on TBD tables; absent where nothing carries them (a raw trace axis is
   *  then refused by name, never compared with the displayed axis). */
  x_axis_raw?: number[];
  y_axis_raw?: number[];
  values: number[][];
  raw: number[][];
  /** W05.1: the ECM's trace channels for this table and whether their
   *  mapping onto its axes is verified. Null on TBD tables. */
  trace?: TableTrace | null;
  /** Where a tuning editor labels this table's axes differently from the
   *  ECM's code; shown on the card even while the table is TBD. */
  editor_disagreement?: RomEditorDisagreement | null;
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
  /** The AFR the ECM states at lambda = 1 (its tables' numerator / 128), not
   *  the fuel's stoich (M01.3). The server also sends it as the deprecated
   *  `afr_stoich`, for bundles built before the rename. */
  afr_scale: number;
}

/** One table kind's trace definition as `GET /control/rom/library/{sha}/trace`
 *  sends it: the viewer's labels and units for the kind, and the ECM's own
 *  coordinates into it (null when the vehicle maps none). The cells and axes
 *  are the image's, from `/tables`. */
export interface RomTraceTableInfo {
  kind: RomTable["kind"];
  title: string;
  x_label: string;
  x_unit: string;
  y_label: string;
  y_unit: string;
  unit: string;
  trace: TableTrace | null;
  editor_disagreement?: RomEditorDisagreement | null;
}

/** `GET /control/rom/library/{sha}/trace`: what the ROM Editor's live overlay
 *  needs beyond the image's tables (the part of `RomInspection` that does not
 *  depend on an uploaded container). */
export interface RomTraceContext {
  sha256: string;
  /** The map slot the trace definitions apply to. */
  map_slot: number;
  /** The ECM's AFR at lambda = 1, as `RomInspection.afr_scale`. */
  afr_scale: number;
  recorded_revision: string | null;
  /** This image is the revision the flash history names as running now. */
  matches_recorded_revision: boolean;
  live: RomInspection["live"];
  warnings: string[];
  tables: RomTraceTableInfo[];
}

export interface AxisTrace {
  channel: string;
  axis_mapping: "verified" | "unverified";
  /** Which breakpoints the mapped value lands on: the axis in its displayed
   *  units, or the ROM's raw axis bytes (from the channel's raw count). Null
   *  when unverified. */
  axis_units: "display" | "raw" | null;
  /** `axis = input × scale + offset`; the input is the stored value
   *  (`display`) or the raw count (`raw`). */
  scale: number | null;
  offset: number | null;
  evidence: string;
  /** Scheduler request carrying the channel (`mem:<window>`), or null. */
  request_channel: string | null;
  /** The catalog decode the channel is stored under, which a `raw` axis
   *  undoes; null when it is not a catalog memory channel. */
  stored_decode: { scale: number; offset: number } | null;
}

export interface TableTrace {
  x: AxisTrace;
  y: AxisTrace;
  unavailable_note: string | null;
  /** What the ECM's lookup reads past an axis end; null when not
   *  established (no cell is then placed past an end). */
  out_of_range: { behaviour: "clamp"; evidence: string } | null;
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
      /** Samples past an axis end, binned in the edge cell the ECM reads
       *  (only when the table declares the clamp). */
      clamped_samples?: number;
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

// ---------------------------------------------------------------- dyno (H07.9)

/** Which kind of empty a dyno answer is — never merged into one "no result". */
export type DynoUnavailableKind =
  | "not_configured"
  | "over_budget"
  | "no_pull_found"
  | "every_pull_refused"
  | "coefficients_pending"
  | "every_run_refused"
  | "prerequisites_missing"
  | null;

/** One fact the power computation rests on, and whether it is measured. */
export interface DynoPrerequisite {
  fact: string;
  status: "recorded" | "stated" | "estimated" | "pending" | "missing";
  detail: string;
  owed_by: string | null;
  blocks_power: boolean;
}

export interface DynoRefusal {
  available: false;
  estimator: string;
  kind: string;
  reason: string;
}

/** One sustained rpm rise found in a session: a pull (gear, heading) or why not. */
export interface DynoWindow {
  start: string;
  end: string;
  rpm_start: number | null;
  rpm_end: number | null;
  course_deg: number | null;
  gear: number | null;
  heading: string | null;
  refusal: DynoRefusal | null;
}

export interface DynoSession {
  session_id: string;
  revision: string | null;
  revision_source: string;
  windows: DynoWindow[];
  n_windows: number;
  n_pulls: number;
}

export type DynoQuantity = "wheel_power_kw" | "wheel_torque_nm";

export interface DynoCurve {
  rpm: Array<number | null>;
  wheel_force_n: Array<number | null>;
  wheel_power_kw: Array<number | null>;
  wheel_torque_nm: Array<number | null>;
  engine_power_kw_estimate: Array<number | null>;
  engine_torque_nm_estimate: Array<number | null>;
}

/** The conditions every dyno result carries (H07 rules); all required. */
export interface DynoRunConditions {
  gear: number;
  direction: "pair" | "single";
  tune_revision: string;
  induction: "na" | "boosted";
  baro_kpa: number;
  intake_temp_c: number;
  ambient_temp_c: number;
  road: string;
  date: string;
  payload_kg: number;
  sessions: string[];
  /** Not stated: the road load's tire pressures are then named as unchecked (M01.8). */
  tire_pressure_front_kpa?: number | null;
  tire_pressure_rear_kpa?: number | null;
}

/** The road load's C moved into the run's air: rho_run / rho_fit (M01.8). */
export interface DynoRoadLoadAir {
  density_ratio: number;
  c_fit_n_per_mps2: number;
  c_applied_n_per_mps2: number;
  fit_baro_kpa: number;
  fit_ambient_temp_c: number;
  run_baro_kpa: number;
  run_ambient_temp_c: number;
  temperature_source: string;
}

/** A run condition that differs from the borrowed road load's, or is unstated (M01.8). */
export interface DynoRoadLoadMismatch {
  field: string;
  status: "differs" | "unstated";
  run: number | null;
  road_load: number;
  detail: string;
}

/** The pulls' GPS speed-scale residual and the power band it implies (M01.10). */
export interface DynoSpeedScale {
  residuals: Array<number | null>;
  speed_scale_residual: number | null;
  power_band: number | null;
  n_unchecked: number;
  meaning: string;
}

export interface DynoFloorBand {
  rpm_min: number;
  rpm_max: number;
  sd: number;
  n_points: number;
}

export type DynoFloor =
  | {
      available: true;
      unit: string;
      n_pull_pairs: number;
      conditions: Record<string, unknown>;
      bands: DynoFloorBand[];
    }
  | DynoRefusal;

export interface DynoRunOk {
  available: true;
  session_id: string;
  gear: number;
  direction: "pair" | "single";
  n_pulls: number;
  curve: DynoCurve;
  per_heading: Record<string, DynoCurve>;
  conditions: DynoRunConditions;
  flags: {
    inertia: "estimated" | "measured_engine";
    squat_corrected: boolean;
    imu_pitch_offset_deg: number;
    imu_pitch_offset_source: string;
    imu_reference_id: string | null;
    correction: "none" | "sae_j1349";
    engine_estimate: string;
    wheel_torque_convention: string;
    /** "apparent squat: ..." when a squat slope was removed (M01.10). */
    squat_slope: string | null;
    road_load_air: DynoRoadLoadAir;
    road_load_mismatches: DynoRoadLoadMismatch[];
    speed_scale: DynoSpeedScale;
  };
  test_mass_kg: number;
  effective_mass_kg: number;
  driveline_efficiency: number;
  uncertainty: {
    noise_floor: Record<DynoQuantity, DynoFloor>;
    heading_spread: {
      meaning: string;
      wheel_power_kw: Array<number | null>;
      wheel_torque_nm: Array<number | null>;
    } | null;
  };
}

export interface DynoRunRefused {
  available: false;
  session_id: string;
  gear: number;
  estimator: string;
  kind: string;
  reason: string;
  n_pulls?: number;
  headings?: string[];
}

export type DynoRun = DynoRunOk | DynoRunRefused;

export interface DynoRequest {
  road: string | null;
  payload_kg: number | null;
  induction: "na" | "boosted" | null;
  ambient_temp_c: number | null;
  allow_single_direction: boolean;
  /** kPa (the form takes psi and converts); `null` is not stated (M01.8). */
  tire_pressure_front_kpa: number | null;
  tire_pressure_rear_kpa: number | null;
}

/** `GET /analysis/dyno` (H07.9). */
export interface DynoResponse {
  available: boolean;
  unavailable_kind: DynoUnavailableKind;
  reason: string | null;
  request: DynoRequest;
  prerequisites: DynoPrerequisite[];
  demand: { spec: string | null; reason: string | null };
  unavailable: UnavailableSession[];
  sessions: DynoSession[];
  runs: DynoRun[];
  conditions: Conditions;
  selection?: Selection;
}
