/** `/control/tyres` — tyre wear from the trip digests' rolling circumference
 *  (`src/control/tyres.py`). `estimate` is null until a gauge reading on the
 *  vehicle ledger has ≥3 measured drives near it; `current` is null with no
 *  measured drive since the series start (the latest rotation/replacement). */
export type TyreAction = "tread_measured" | "rotated" | "replaced" | "pressure_changed";

export interface TyreDrive {
  session: string;
  started_at: string;
  circumference_m: number;
  n_pairs: number;
  se_pct: number | null;
}

export interface TyreEventRef {
  id: string;
  occurred_at: string;
  title: string;
  action: TyreAction;
}

export interface TyreStatus {
  method_version: number;
  series_start: string | null;
  series_reset_by: TyreEventRef | null;
  drives: TyreDrive[];
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
    event: TyreEventRef | null;
    drives: number;
    status: "pending" | "fixed";
    circumference_m?: number;
  } | null;
  estimate: {
    tread_depth_mm: number;
    worn_since_baseline_mm: number;
    uncertainty_mm: number;
    as_of: string;
  } | null;
  caveats: string[];
}
