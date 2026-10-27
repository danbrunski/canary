import { useEffect, useState } from "react";

import { groupOf, isWorkbenchItem } from "../nav";
import type { NavGroupId, WorkbenchItem } from "../types";
import { readJson, writeJson } from "../util/storage";
import { useLocalStorageJson } from "./useLocalStorage";

export const AREA_MEMORY_STORAGE_KEY = "canary.workbench.lastPage.v1";
export const SIDEBAR_COLLAPSED_STORAGE_KEY = "canary.workbench.sidebarCollapsed.v1";

/** The page last open in each Workbench area. */
export type AreaMemory = Partial<Record<NavGroupId, WorkbenchItem>>;

/** The stored memory, keeping only entries that still name a page of the area
 *  they are filed under. A renamed page, or one moved to another area, is
 *  dropped rather than stranding its old area on a page it no longer has. */
export function readAreaMemory(): AreaMemory {
  const stored = readJson<unknown>(AREA_MEMORY_STORAGE_KEY, null);
  if (stored === null || typeof stored !== "object" || Array.isArray(stored)) return {};
  const memory: AreaMemory = {};
  for (const [group, item] of Object.entries(stored)) {
    if (typeof item === "string" && isWorkbenchItem(item) && groupOf(item) === group) {
      memory[groupOf(item)] = item;
    }
  }
  return memory;
}

/** Remembers the page open in each area (W10.1), so clicking an area returns
 *  to where the operator left it. Kept in memory for the session and mirrored
 *  to localStorage across reloads. */
export function useAreaMemory(active: WorkbenchItem): AreaMemory {
  const [memory, setMemory] = useState(readAreaMemory);
  const group = groupOf(active);
  // Adjusting state while rendering, React's pattern for state derived from a
  // prop: the new page is in the memory on this very render.
  if (memory[group] !== active) setMemory({ ...memory, [group]: active });
  useEffect(() => {
    writeJson(AREA_MEMORY_STORAGE_KEY, memory);
  }, [memory]);
  return memory;
}

/** Whether the operator collapsed the Workbench sidebar to a rail (W10.4),
 *  per browser. Anything stored but `true` reads as expanded. */
export function useSidebarCollapsed() {
  const [stored, setStored] = useLocalStorageJson<unknown>(SIDEBAR_COLLAPSED_STORAGE_KEY, false);
  return [stored === true, (collapsed: boolean) => setStored(collapsed)] as const;
}
