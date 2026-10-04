import type { TyreAction } from "./tyres";

/** One vehicle ledger entry (`src/vehicle/vehicle_ledger.py` `VehicleEvent`). */
export type VehicleEventKind = "maintenance" | "mod" | "repair" | "observation" | "dtc";

export interface VehicleLedgerTyres {
  action: TyreAction;
  tread_depth_mm: number | null;
  pressure_psi: number | null;
}

export interface VehicleLedgerEvent {
  id: string;
  recorded_at: string;
  occurred_at: string;
  odometer_km: number | null;
  kind: VehicleEventKind;
  title: string;
  details: string;
  affected_channels: string[];
  corpus_boundary: boolean;
  tyres: VehicleLedgerTyres | null;
}

/** What the edit form submits: an existing entry's id replaces it on disk. */
export interface VehicleLedgerDraft {
  event_id?: string;
  occurred_at: string;
  kind: VehicleEventKind;
  title: string;
  details: string;
  odometer_km: number | null;
  affected_channels: string[];
  corpus_boundary: boolean;
  tyres: VehicleLedgerTyres | null;
}
