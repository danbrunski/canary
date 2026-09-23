/** `GET /sessions/{id}/digest` (Task 5.6, eval-longitudinal-and-dtc.md).
 *  Mirrors `SessionDigest.to_dict()` (`src/analysis/digest.py`). Read-only,
 *  unauthenticated like `/sessions/{id}/trip-summary`. */
export interface FingerprintTopMover {
  stat: string;
  value: number;
  reference_mean: number;
  reference_std: number;
  z_score: number | null;
}

export interface BehaviorDeviationEntry {
  channel: string;
  abs_mean_deviation: number;
}

export interface SessionDigestResponse {
  schema_version: number;
  computed_at: string;
  session_id: string;
  duration_seconds: number;
  mode_seconds: Record<string, number>;
  /** Alert-role events only (A06.5, schema 2). */
  anomaly_counts_by_source: Record<string, number>;
  /** A06.5 — events from evidence-role detectors: stored, never alerts.
   *  Absent on a schema-1 digest, which predates roles (all alerts). */
  evidence_counts_by_source?: Record<string, number>;
  top_behavior_deviations: BehaviorDeviationEntry[];
  fingerprint_top_movers: FingerprintTopMover[];
  new_dtcs: Array<Record<string, unknown>>;
  new_ledger_events: Array<Record<string, unknown>>;
}
