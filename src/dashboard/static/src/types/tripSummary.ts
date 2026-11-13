/** `GET /sessions/{id}/trip-summary` (Task 1.5, engine-analysis-and-mapping.md).
 *  Mirrors `TripSummaryResponse` / `TripSummaryFields` in
 *  `src/dashboard/schemas.py`, which mirror `TripSummary.to_dict()`
 *  (`src/signals/derived.py`). Read-only, unauthenticated like `/obd/dtc`. */
export interface TripSummaryFields {
  duration_s: number;
  distance_km: number | null;
  fuel_used_l: number | null;
  avg_economy_l_per_100km: number | null;
  avg_economy_mpg: number | null;
  co2_kg: number | null;
  idle_time_s: number | null;
  idle_fraction: number | null;
  max_speed_kph: number | null;
  avg_speed_kph: number | null;
  zero_to_60_mph_s: number | null;
  quarter_mile_s: number | null;
  quarter_mile_trap_kph: number | null;
  /** The speed the distance and speeds were computed on (M01.2). */
  speed_basis: string;
  /** The fuel constants and fuel-cut handling (M01.3(b), M01.4). */
  fuel_basis: string;
}

export interface TripSummaryResponse {
  schema_version: number;
  computed_at: string;
  session_id: string;
  summary: TripSummaryFields;
}
