import { useMemo, useState } from "react";

import type { RomInspection } from "../lib/romMaps";
import { useLocalStorageJson } from "./useLocalStorage";

/** A ROM parsed by `/control/rom/inspect` and held in browser memory. */
export interface LoadedRom {
  filename: string;
  inspection: RomInspection;
}

/** What the Engine Tuning pages share without sharing a sequence (W02.5). */
export interface TuningContext {
  selectedLogId: string | null;
  setSelectedLogId: (id: string | null) => void;
  loadedRom: LoadedRom | null;
  setLoadedRom: (rom: LoadedRom | null) => void;
}

export const SELECTED_LOG_STORAGE_KEY = "canary.tuning.selectedLogId.v1";

/** Group-scoped Engine Tuning state, owned by the Workbench so it survives
 *  navigation between pages.
 *
 *  `selectedLogId` persists to localStorage: a log picked on Logs is still
 *  the subject after a reload. The ROM deliberately does **not** persist —
 *  `docs/offline-rom-targets.md` keeps the parsed view out of storage and the
 *  file never leaves memory on the daemon. Holding it here only keeps it
 *  across navigation within the running page, which is the behaviour wanted,
 *  without weakening that guarantee. */
export function useTuningContext(): TuningContext {
  const [selectedLogId, setSelectedLogId] = useLocalStorageJson<string | null>(
    SELECTED_LOG_STORAGE_KEY,
    null,
  );
  const [loadedRom, setLoadedRom] = useState<LoadedRom | null>(null);
  return useMemo(
    () => ({ selectedLogId, setSelectedLogId, loadedRom, setLoadedRom }),
    [selectedLogId, setSelectedLogId, loadedRom],
  );
}
