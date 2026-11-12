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

/** One trip's answer in the session (D03.63): a digest, or the refusal that
 *  stands in for one. Added by the route; never stored in the sidecar. */
export interface SessionDigestTrip {
  trip_key: string;
  status: "digest" | "skipped";
  started_at: string | null;
  stopped_at: string | null;
  end_session: string | null;
  /** Refusals only: the `REASON_*` kind and its one-line detail. */
  reason?: string | null;
  detail?: string | null;
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
  /** The digest served is the session's newest trip's; this lists every
   *  trip's answer (D03.63). Absent on a response from an older daemon. */
  trips?: SessionDigestTrip[];
}
