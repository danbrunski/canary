import { useCallback } from "react";

import { getCaptureProfiles, getDids, getObdCoverage } from "../api/http";
import type { CaptureProfile, ResolvedPollingPlan } from "../types";
import { useControlResource } from "./useControlResource";

/** Everything a capture profile can be built from (R08.6, out of
 *  CaptureProfilesPanel): the saved profiles with their resolved polling
 *  plans, and the channels this vehicle offers for each service. */
export interface CaptureCatalog {
  profiles: CaptureProfile[];
  resolvedPlans: Record<string, ResolvedPollingPlan>;
  /** Mode 01 PIDs the ECU answers and CANary decodes, sorted. */
  pids: string[];
  /** Mode 22 DIDs from this vehicle's DID map, sorted. */
  dids: string[];
  /** RAM (service 0x23) channels from the vehicle's memory map, sorted. */
  memory: string[];
}

async function loadCatalog(token: string, signal: AbortSignal): Promise<CaptureCatalog> {
  const [catalog, obd, didMap] = await Promise.all([
    getCaptureProfiles(token, signal),
    getObdCoverage(signal),
    getDids(signal),
  ]);
  return {
    profiles: catalog.profiles,
    resolvedPlans: catalog.resolved_plans ?? {},
    pids: obd.rows
      .filter((row) => row.channel && row.status === "decoded")
      .map((row) => row.channel as string)
      .sort(),
    dids: didMap.dids.map((entry) => entry.name).sort(),
    memory: (didMap.memory ?? []).map((entry) => entry.name).sort(),
  };
}

/** Read the catalog once per token; `refresh` after a save or delete. */
export function useCaptureCatalog(token: string) {
  const fetcher = useCallback((signal: AbortSignal) => loadCatalog(token, signal), [token]);
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: null,
  });
  return { catalog: data, error, loading, refresh };
}
