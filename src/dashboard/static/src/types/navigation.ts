/** Top-level context. `driving` = the locked, glanceable single screen;
 *  `parked` = the full Workbench with grouped navigation. The mode drives
 *  the *entire* layout, not just which panels are gated. */
export type AppMode = "driving" | "parked";

/** Workbench navigation model (replaces the old flat `DashboardView`).
 *  One canonical home per panel; new Phase-6 panels register in `nav.tsx`
 *  without ever growing the five top-level groups. */
export type NavGroupId = "observe" | "tuning" | "diagnose" | "model" | "system";

export type WorkbenchItem =
  | "observe-charts"
  | "observe-alarms"
  | "observe-builder"
  | "observe-trip"
  | "tuning-logging"
  | "tuning-logs"
  | "tuning-analysis"
  | "tuning-rom-maps"
  | "tuning-rom-library"
  | "tuning-calibration"
  | "tuning-rom-editor"
  | "tuning-flash"
  | "diag-anomalies"
  | "diag-dtc"
  | "diag-functions"
  | "diag-monitors"
  | "diag-trends"
  | "diag-explore"
  | "diag-tpms"
  | "diag-ledger"
  | "diag-brain"
  | "diag-llm-assist"
  | "models-overview"
  | "models-behavior"
  | "models-training"
  | "model-thresholds"
  | "models-health"
  | "model-dataset-health"
  | "system-data-sources"
  | "system-sessions"
  | "system-export"
  | "system-custom-pids"
  | "system-profiles"
  | "system-capture-profiles"
  | "system-power"
  | "system-network"
  | "system-paired-devices"
  | "system-control"
  | "system-test"
  | "system-info";

export interface NavItemDef {
  id: WorkbenchItem;
  label: string;
  group: NavGroupId;
  /** Backend exists; the rich UI is a later Phase-6 task (rendered as a
   *  labeled placeholder for now so the shell can already hold it). */
  placeholder?: boolean;
  /** Reserved workflow whose implementation intentionally has not landed. */
  planned?: boolean;
}

export interface NavGroupDef {
  id: NavGroupId;
  label: string;
  items: NavItemDef[];
}
