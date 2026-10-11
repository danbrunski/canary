import type { TireAction } from "./tires";

/** One vehicle ledger entry (`src/vehicle/vehicle_ledger.py` `VehicleEvent`). */
export type VehicleEventKind = "maintenance" | "mod" | "repair" | "observation" | "dtc";

/** What kind of intervention a physical event is (E01.4); `null`: unclassified. */
export type PhysicalInterventionKind = "hardware" | "fuel" | "instrumentation";

export interface VehicleLedgerTires {
  action: TireAction;
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
  tires: VehicleLedgerTires | null;
  intervention?: PhysicalInterventionKind | null;
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
  tires: VehicleLedgerTires | null;
  /** Carried through an edit so re-saving never drops the classification. */
  intervention?: PhysicalInterventionKind | null;
}
