import { useMemo, useState } from "react";

import { useLocalStorageJson } from "./useLocalStorage";

/** The ROM the editor has in view: which library image, and what to call it.
 *  Held in browser memory; the image itself is the library's. */
export interface LoadedRom {
  sha256: string;
  name: string;
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
 *  the subject after a reload. The loaded ROM does not persist here: the
 *  editor keeps its choice in the URL (`?rom=<sha>`), and this only lets the
 *  context bar name it while another Engine Tuning page is open. */
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
