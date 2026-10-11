/** `/control/tires` — tire wear from the trip digests' rolling circumference
 *  (`src/control/tires.py`). `estimate` is null until a gauge reading on the
 *  vehicle ledger has ≥3 measured drives near it; `current` is null with no
 *  measured drive since the series start (the latest rotation/replacement). */
export type TireAction = "tread_measured" | "rotated" | "replaced" | "pressure_changed";

export interface TireDrive {
  session: string;
  started_at: string;
  circumference_m: number;
  n_pairs: number;
  se_pct: number | null;
}

export interface TireEventRef {
  id: string;
  occurred_at: string;
  title: string;
  action: TireAction;
}

export interface TireStatus {
  method_version: number;
  series_start: string | null;
  series_reset_by: TireEventRef | null;
  drives: TireDrive[];
  current: {
    circumference_m: number;
    uncertainty_m: number;
    drives: number;
    drive_scatter_pct: number;
    through: string;
  } | null;
  baseline: {
    tread_depth_mm: number;
    measured_on: string;
    event: TireEventRef | null;
    drives: number;
    status: "pending" | "fixed";
    circumference_m?: number;
  } | null;
  estimate: {
    tread_depth_mm: number;
    worn_since_baseline_mm: number;
    uncertainty_mm: number;
    /** Drives measured after the gauge reading; 0 = the estimate is the reading. */
    drives_since_reading: number;
    as_of: string;
  } | null;
  caveats: string[];
}
