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
      { id: "live-gauges", label: "Gauges", group: "live" },
      { id: "live-charts", label: "Charts", group: "live", placeholder: true },
      { id: "live-alarms", label: "Alarms", group: "live", placeholder: true },
    ],
  },
  {
    id: "diagnostics",
    label: "Diagnostics",
    items: [
      { id: "diag-anomalies", label: "Anomalies", group: "diagnostics" },
      { id: "diag-coverage", label: "OBD Coverage", group: "diagnostics", placeholder: true },
      { id: "diag-dtc", label: "Trouble Codes", group: "diagnostics", placeholder: true },
      { id: "diag-did", label: "Nissan DIDs", group: "diagnostics", placeholder: true },
      { id: "diag-system", label: "System", group: "diagnostics" },
      { id: "diag-tpms", label: "TPMS", group: "diagnostics" },
    ],
  },
  {
    id: "models",
    label: "Models",
    items: [
      { id: "models-overview", label: "Overview", group: "models" },
      { id: "models-behavior", label: "Behavior", group: "models" },
      { id: "models-training", label: "Training", group: "models", placeholder: true },
      { id: "models-tuning", label: "Tuning", group: "models", placeholder: true },
      { id: "models-health", label: "Drift / Health", group: "models", placeholder: true },
    ],
  },
  {
    id: "data",
    label: "Data",
    items: [
      { id: "data-sessions", label: "Sessions", group: "data", placeholder: true },
      { id: "data-health", label: "Dataset Health", group: "data", placeholder: true },
      { id: "data-export", label: "Export", group: "data", placeholder: true },
    ],
  },
  {
    id: "control",
    label: "Control",
    items: [{ id: "control-main", label: "Control Plane", group: "control" }],
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
