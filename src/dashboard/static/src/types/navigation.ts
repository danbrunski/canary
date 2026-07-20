/** Top-level context. `driving` = the locked, glanceable single screen;
 *  `parked` = the full Workbench with grouped navigation. The mode drives
 *  the *entire* layout, not just which panels are gated. */
export type AppMode = "driving" | "parked";

/** Workbench navigation model (replaces the old flat `DashboardView`).
 *  One canonical home per panel; new Phase-6 panels register in `nav.tsx`
 *  without ever growing the five top-level groups. */
export type NavGroupId = "live" | "diagnostics" | "models" | "data" | "control";

export type WorkbenchItem =
  | "live-charts"
  | "live-alarms"
  | "live-builder"
  | "live-trip"
  | "diag-anomalies"
  | "diag-data"
  | "diag-dtc"
  | "diag-monitors"
  | "diag-trends"
  | "diag-explore"
  | "diag-system"
  | "diag-tpms"
  | "models-overview"
  | "models-behavior"
  | "models-training"
  | "models-tuning"
  | "models-health"
  | "data-sessions"
  | "data-health"
  | "data-custom-pids"
  | "data-export"
  | "control-main"
  | "control-test"
  | "control-profiles"
  | "control-power";

export interface NavItemDef {
  id: WorkbenchItem;
  label: string;
  group: NavGroupId;
  /** Backend exists; the rich UI is a later Phase-6 task (rendered as a
   *  labeled placeholder for now so the shell can already hold it). */
  placeholder?: boolean;
}

export interface NavGroupDef {
  id: NavGroupId;
  label: string;
  items: NavItemDef[];
}
