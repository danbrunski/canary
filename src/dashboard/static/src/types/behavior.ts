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

/** Mode-conditioned location/spread with no claimed x relationship — the
 *  expectation is flat across the domain (`RobustBaselineFit`, backend
 *  `src/decode/behavior_models.py`). */
export interface RobustBaselineFit extends BehaviorFitBase {
  kind: "robust_baseline";
  median: number;
  mad: number;
  p01: number;
  p99: number;
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
  // 9.4: backend normalizes the deprecated "relative" alias to "sigma" at
  // config load, so the API never actually serves "relative" — see
  // DeviationConfig in src/decode/behavior_models.py.
  kind: "absolute" | "sigma";
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
  fit: PolynomialFit | LookupTableFit | RobustBaselineFit;
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
