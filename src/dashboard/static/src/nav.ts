// Workbench navigation model — the single source of truth for "where does
// a panel live". Phase-6 panels register here (often as `placeholder: true`
// until their rich UI lands) without ever growing the five top-level groups,
// which is the whole point of the navigation refactor.

import type { NavGroupDef, NavGroupId, NavItemDef, WorkbenchItem } from "./types";

export const NAV: NavGroupDef[] = [
  {
    id: "live",
    label: "Live",
    items: [
      { id: "live-charts", label: "Charts", group: "live" },
      { id: "live-alarms", label: "Alarms", group: "live" },
      { id: "live-builder", label: "Builder", group: "live" },
      { id: "live-trip", label: "Trip", group: "live" },
      { id: "live-rom-maps", label: "ROM Maps", group: "live" },
    ],
  },
  {
    id: "diagnostics",
    label: "Diagnostics",
    items: [
      { id: "diag-data", label: "Data Sources", group: "diagnostics" },
      { id: "diag-anomalies", label: "Anomalies", group: "diagnostics" },
      { id: "diag-dtc", label: "Trouble Codes", group: "diagnostics" },
      { id: "diag-monitors", label: "Monitors", group: "diagnostics" },
      { id: "diag-trends", label: "Drift Trends", group: "diagnostics" },
      { id: "diag-explore", label: "Explore", group: "diagnostics" },
      { id: "diag-system", label: "System", group: "diagnostics" },
      { id: "diag-tpms", label: "TPMS", group: "diagnostics" },
      { id: "diag-llm-assist", label: "LLM Assist", group: "diagnostics" },
    ],
  },
  {
    id: "models",
    label: "Models",
    items: [
      { id: "models-overview", label: "Overview", group: "models" },
      { id: "models-behavior", label: "Behavior", group: "models" },
      { id: "models-training", label: "Training", group: "models" },
      { id: "models-tuning", label: "Tuning", group: "models" },
      { id: "models-health", label: "Drift / Health", group: "models" },
    ],
  },
  {
    id: "data",
    label: "Data",
    items: [
      { id: "data-sessions", label: "Sessions", group: "data" },
      { id: "data-health", label: "Dataset Health", group: "data" },
      { id: "data-custom-pids", label: "Custom PIDs", group: "data" },
      { id: "data-export", label: "Export", group: "data" },
    ],
  },
  {
    id: "control",
    label: "Control",
    items: [
      { id: "control-test", label: "Test", group: "control" },
      { id: "control-main", label: "Control Plane", group: "control" },
      { id: "control-profiles", label: "Profiles", group: "control" },
      { id: "control-capture", label: "Capture Profiles", group: "control" },
      { id: "control-power", label: "Power / UPS", group: "control" },
    ],
  },
];

const ITEMS: NavItemDef[] = NAV.flatMap((g) => g.items);

/** First navigable item — the Workbench default landing view. */
export const DEFAULT_ITEM: WorkbenchItem = ITEMS[0].id;

export function findItem(id: WorkbenchItem): NavItemDef {
  const item = ITEMS.find((i) => i.id === id);
  // The id is a closed union populated from this table, so this is total;
  // the fallback only guards a future hand-typed id.
  return item ?? ITEMS[0];
}

export function groupOf(id: WorkbenchItem): NavGroupId {
  return findItem(id).group;
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
