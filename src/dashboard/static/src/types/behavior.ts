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
  tune_revision?: string | null;
}

export interface BehaviorRateCondition {
  kind: "max_abs_rate_of_change";
  channel: string;
  max_abs_per_second: number;
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

// ---------------------------------------------------------------------------
// B03.8 — serving status, artifact identity and validation scope. Additive
// fields on `GET /models/behavior`; mirrors `serving_status`,
// `artifact_identity` and `validation_scope` in src/decode/behavior_models.py.
// ---------------------------------------------------------------------------

/** Why a model may or may not alert. Only `validated` alerts. */
export type BehaviorLifecycle =
  | "validated"
  | "unvalidated"
  | "exploratory"
  | "demoted"
  | "quarantined";

export type BehaviorPermittedUse = "display" | "learned_feature" | "alert";

/** A profile policy entry (`behavior_policy.yaml`) restricting a model. */
export interface BehaviorPolicyEntry {
  model: string;
  lifecycle: "exploratory" | "demoted" | "quarantined";
  permitted_uses: BehaviorPermittedUse[];
  artifact_fingerprint?: string;
  reason: string;
  decided: string;
  evidence: string;
  lift_when?: string;
}

export interface BehaviorServingStatus {
  state: "active" | "diagnostic_only";
  reasons: string[];
  lifecycle?: BehaviorLifecycle;
  permitted_uses?: BehaviorPermittedUse[];
  policy?: BehaviorPolicyEntry | null;
}

export interface BehaviorArtifactIdentity {
  name: string;
  version: number;
  fingerprint: string;
  fitted_at: string;
  training_data_hash: string | null;
  tune_revision: string | null;
  schema_contract: boolean;
  grid_version: number | null;
}

/** Where the artifact's evidence holds; `verdict: "none"` when it has none. */
export interface BehaviorValidationScope {
  verdict: "none" | "passed" | "failed" | "insufficient_evidence";
  evaluated_at?: string;
  failures?: string[];
  tune_revision: string | null;
  operating_mode: string | null;
  mode_taxonomy?: string[];
  test_drives?: string[];
  calibration_drives?: string[];
  train_drives?: number;
  test_evaluated_rows?: number;
  held_out_rows?: number;
  held_out_sessions?: string[];
  criteria_source?: string | null;
  contract_matches?: boolean;
}

/** Fields every model carries on `GET /models/behavior` since B01.6/B03.8.
 *  Optional so an older daemon's payload still type-checks and renders. */
export interface BehaviorModelStatusFields {
  serving_status?: BehaviorServingStatus;
  artifact?: BehaviorArtifactIdentity;
  validation_scope?: BehaviorValidationScope;
}

export interface BehaviorModel1D extends BehaviorModelStatusFields {
  name: string;
  kind: "surface_1d";
  output: string;
  output_unit: string;
  axis: BehaviorAxis1D;
  operating_mode: string | null;
  conditions?: BehaviorRateCondition[];
  version: number;
  fit: PolynomialFit | LookupTableFit | RobustBaselineFit;
  deviation: DeviationConfig | null;
  notes: string | null;
}

export interface BehaviorModel2D extends BehaviorModelStatusFields {
  name: string;
  kind: "surface_2d";
  output: string;
  output_unit: string;
  axes: [BehaviorAxis2D, BehaviorAxis2D];
  operating_mode: string | null;
  conditions?: BehaviorRateCondition[];
  version: number;
  fit: GridBinnedMeanFit;
  deviation: DeviationConfig | null;
  notes: string | null;
}

export type BehaviorModel = BehaviorModel1D | BehaviorModel2D;

export interface BehaviorModelsResponse {
  schema_version: number | null;
  models: BehaviorModel[];
  /** B03.8 — the profile policy file governing the artifact. */
  policy?: {
    path: string;
    present: boolean;
    unmatched_entries: string[];
  };
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
  /** B01.4 — why the model produced no deviation this tick. */
  reason?: string | null;
  /** B01.6 — "active" | "diagnostic_only" and the named reasons. */
  status?: string;
  status_reasons?: string[];
  /** B03.8 — lifecycle, typed permitted uses, the serving artifact version and
   *  why this tick raised no trusted verdict (null when it could have). */
  lifecycle?: BehaviorLifecycle;
  permitted_uses?: BehaviorPermittedUse[];
  artifact_version?: number | null;
  abstain_reason?: string | null;
}

/** `GET /models/behavior/live`. Empty `models` when no daemon is wired. */
export interface BehaviorLiveResponse {
  observed: number;
  timestamp: string | null;
  operating_mode: string | null;
  models: Record<string, BehaviorLiveModel>;
}
