// Workbench navigation model — the single source of truth for "where does
// a panel live". Phase-6 panels register here (often as `placeholder: true`
// until their rich UI lands) without ever growing the five top-level groups,
// which is the whole point of the navigation refactor.

import type { NavGroupDef, NavGroupId, NavItemDef, WorkbenchItem } from "./types";

export const NAV: NavGroupDef[] = [
  {
    id: "observe",
    label: "Observe",
    items: [
      { id: "observe-charts", label: "Charts", group: "observe" },
      { id: "observe-trip", label: "Trip", group: "observe" },
      { id: "observe-alarms", label: "Alarms", group: "observe" },
      { id: "observe-builder", label: "Builder", group: "observe" },
    ],
  },
  {
    id: "tuning",
    label: "Engine Tuning",
    items: [
      { id: "tuning-logging", label: "Logging", group: "tuning" },
      { id: "tuning-logs", label: "Logs", group: "tuning" },
      { id: "tuning-analysis", label: "Analysis", group: "tuning" },
      { id: "tuning-rom-maps", label: "ROM Maps", group: "tuning" },
      { id: "tuning-rom-library", label: "ROM Library", group: "tuning" },
      { id: "tuning-calibration", label: "Calibration", group: "tuning" },
      { id: "tuning-rom-editor", label: "ROM Editor", group: "tuning", planned: true },
      { id: "tuning-flash", label: "Flash", group: "tuning", planned: true },
    ],
  },
  {
    id: "diagnose",
    label: "Diagnose",
    items: [
      { id: "diag-anomalies", label: "Anomalies", group: "diagnose" },
      { id: "diag-dtc", label: "Trouble Codes", group: "diagnose" },
      { id: "diag-functions", label: "Diagnostic Functions", group: "diagnose" },
      { id: "diag-monitors", label: "Monitors", group: "diagnose" },
      { id: "diag-trends", label: "Drift Trends", group: "diagnose" },
      { id: "diag-tpms", label: "Tyres", group: "diagnose" },
      { id: "diag-ledger", label: "Vehicle Ledger", group: "diagnose" },
      { id: "diag-brain", label: "Investigations", group: "diagnose" },
      { id: "diag-llm-assist", label: "LLM Assist", group: "diagnose" },
      { id: "diag-explore", label: "Explore", group: "diagnose" },
    ],
  },
  {
    id: "model",
    label: "Model",
    items: [
      { id: "models-overview", label: "Overview", group: "model" },
      { id: "models-behavior", label: "Behavior", group: "model" },
      { id: "models-training", label: "Training", group: "model" },
      { id: "model-thresholds", label: "Alert Thresholds", group: "model" },
      { id: "models-health", label: "Drift / Health", group: "model" },
      { id: "model-dataset-health", label: "Dataset Health", group: "model" },
    ],
  },
  {
    id: "system",
    label: "System",
    items: [
      { id: "system-data-sources", label: "Data Sources", group: "system" },
      { id: "system-sessions", label: "Sessions", group: "system" },
      { id: "system-export", label: "Export", group: "system" },
      { id: "system-custom-pids", label: "Custom PIDs", group: "system" },
      { id: "system-profiles", label: "Profiles", group: "system" },
      { id: "system-capture-profiles", label: "Capture Profiles", group: "system" },
      { id: "system-power", label: "Power / UPS", group: "system" },
      { id: "system-control", label: "Control Plane", group: "system" },
      { id: "system-test", label: "Test", group: "system" },
      { id: "system-info", label: "System Info", group: "system" },
    ],
  },
];

/** Stable redirects for bookmarks and documentation predating W01. */
export const ITEM_ALIASES: Record<string, WorkbenchItem> = {
  "live-charts": "observe-charts",
  "live-trip": "observe-trip",
  "live-alarms": "observe-alarms",
  "live-builder": "observe-builder",
  "live-rom-maps": "tuning-rom-maps",
  // W08.6: the checklist names the page `#wb/rom-library`.
  "rom-library": "tuning-rom-library",
  "models-tuning": "model-thresholds",
  "data-health": "model-dataset-health",
  "diag-data": "system-data-sources",
  "data-sessions": "system-sessions",
  "data-export": "system-export",
  "data-custom-pids": "system-custom-pids",
  "control-profiles": "system-profiles",
  "control-capture": "system-capture-profiles",
  "tuning-capture-profiles": "system-capture-profiles",
  "control-power": "system-power",
  "control-main": "system-control",
  "control-test": "system-test",
  "diag-system": "system-info",
};

export function resolveItem(value: string): WorkbenchItem | undefined {
  if (isWorkbenchItem(value)) return value;
  return ITEM_ALIASES[value];
}

const ITEMS: NavItemDef[] = NAV.flatMap((g) => g.items);

/** First navigable item — the Workbench default landing view. */
export const DEFAULT_ITEM: WorkbenchItem = ITEMS[0].id;

export function findItem(id: WorkbenchItem): NavItemDef {
  const item = ITEMS.find((i) => i.id === id);
  // The id is a closed union populated from this table, so this is total;
  // the fallback only guards a future hand-typed id.
  return item ?? ITEMS[0];
}

/** Runtime guard for an untrusted id (e.g. parsed from the URL hash). */
export function isWorkbenchItem(value: string): value is WorkbenchItem {
  return ITEMS.some((i) => i.id === value);
}

/** The active item when the operator clicks into a group (its first item). */
export function defaultItemForGroup(group: NavGroupId): WorkbenchItem {
  const g = NAV.find((x) => x.id === group);
  return (g?.items[0] ?? ITEMS[0]).id;
}
