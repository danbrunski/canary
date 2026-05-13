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
  source: "isolation_forest" | "lstm_autoencoder";
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
  } | null;
}

/** Response from `GET /telemetry/latest?n=...`. */
export interface TelemetryLatestResponse {
  rows: TelemetryRow[];
  count: number;
}

/** Connection state of a WebSocket subscription — the dashboard renders this
 *  as the small dot in the corner. */
export type ConnectionState = "connecting" | "open" | "closed" | "error";

/** UI mode. `parked` unlocks the maintenance / diagnostics panels. */
export type AppMode = "driving" | "parked";

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
