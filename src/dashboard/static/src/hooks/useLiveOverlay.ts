// The ROM Editor's live overlay (what was ROM Maps, W05; merged 2026-10-26):
// the live RPM / BFS / commanded-AFR reads, the trace gate, the ECM cell and
// its agreement tally per table, and the coverage overlay. It was the top half
// of RomMapsPanel; the page that renders it is the editor's `LiveOverlay`.
//
// Nothing is called an active cell unless the operator asserts this ROM and
// slot are running and nothing the daemon knows contradicts it (`traceGate`).

import { useCallback, useEffect, useState } from "react";

import { getCaptureProfiles } from "../api/http";
import type { GridOverlay } from "../panels/cellOverlay";
import { liveValue, streamRow, tracePosition } from "../lib/romMaps";
import type { RomTable } from "../lib/romMaps";
import {
  cellDisagreement,
  describeClamp,
  ecmDisplay,
  ecmPoint,
  ecmPosition,
  ecmValueRefusal,
  mappingRefusal,
  traceGate,
  traceLiveness,
} from "../lib/romTrace";
import type {
  CellPosition,
  Disagreement,
  EcmDisplay,
  EcmPosition,
  TraceLiveness,
} from "../lib/romTrace";
import type { StatusResponse } from "../types";
import type { RomTraceContext } from "../types/workbench";
import { useControlResource } from "./useControlResource";
import { tableKey, useCoverageOverlay } from "./useCoverageOverlay";
import type { ChannelSnapshot } from "./useTelemetryStream";

/** ECM-vs-inferred cell agreement over the samples stepped so far (W05.3). */
interface EcmTally {
  checks: number;
  disagreements: number;
  streakSince: number | null;
}

interface EcmState {
  position: EcmPosition | null;
  at: EcmDisplay | null;
  liveness: TraceLiveness | null;
  reason: string;
  xTimestamp: number | null;
}

/** What the overlay says about one table. */
export interface TableOverlay {
  /** The marks `CellGrid` draws. */
  grid: GridOverlay;
  inferred: CellPosition | null;
  ecm: EcmState | undefined;
  disagreement: Disagreement | null;
  clampNote: string;
  tally: EcmTally | undefined;
  /** The operating point is outside the table's verified axes. */
  outOfRange: boolean;
}

export function useLiveOverlay({
  token,
  channels,
  status,
  selectedLogId,
  context,
  tables,
}: {
  token: string;
  channels: Map<string, ChannelSnapshot>;
  status: StatusResponse | null;
  /** The Engine Tuning log picked on Logs, the coverage overlay's subject. */
  selectedLogId: string | null;
  context: RomTraceContext;
  /** The tables the overlay applies to (see `overlayTableOf`). */
  tables: RomTable[];
}) {
  // The operator's assertion that this ROM is running. It is about the image,
  // not about the slot being looked at: the overlay's subject is fixed at
  // `context.map_slot` whichever slot the page shows, so changing the slot
  // neither makes the assertion false nor needs asking again. (ROM Maps cleared
  // it on a slot change because its dropdown chose which slot to trace.)
  const [confirmed, setConfirmed] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  // Per table: the last ECM sample stepped (its X timestamp) and the
  // disagreement tally (W05.3). Render state, not a ref, because the step
  // happens while rendering (below).
  const [ecmSteps, setEcmSteps] = useState<{
    seen: Record<string, number>;
    tally: Record<string, EcmTally>;
  }>({ seen: {}, tally: {} });
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, []);

  // The vehicle's capture-profile catalog, to name a profile that carries the
  // trace channels when the active one does not (W05.4).
  const catalog = useControlResource(
    useCallback((signal: AbortSignal) => getCaptureProfiles(token, signal), [token]),
    { enabled: Boolean(token) && tables.length > 0, intervalMs: null },
  );
  const profiles = catalog.data
    ? Array.isArray(catalog.data.profiles)
      ? catalog.data.profiles
      : []
    : null;

  const rpm = liveValue(channels, context.live.rpm, now, context.afr_scale);
  const bfs = liveValue(channels, context.live.base_fuel_schedule, now, context.afr_scale);
  const commanded = liveValue(channels, context.live.commanded_afr, now, context.afr_scale);
  const fuelCutRow = context.live.commanded_afr
    ? streamRow(channels, `${context.live.commanded_afr.spec}_SENTINEL`)
    : null;
  const fuelCut =
    !!fuelCutRow?.timestamp && now - Date.parse(fuelCutRow.timestamp) < 2500 && !commanded;
  const { gate: gateReason, trace: traceReason } = traceGate({
    confirmed,
    rom: context,
    status,
    now,
    fuelCut,
    rpm,
    bfs,
  });

  const captureActive = !!status?.capture_profile?.active;
  const captureKey = captureActive
    ? `${status?.capture_profile?.name}|${status?.capture_profile?.started_at}`
    : null;
  const coverage = useCoverageOverlay({ token, selectedLogId, captureKey, tables });

  // Per table: the ECM cell (W05.2), and whether the trace is live (W05.4).
  const ecm = new Map<string, EcmState>();
  for (const table of tables) {
    if (table.status !== "available" || !table.trace) continue;
    const liveness = traceLiveness(table.trace, channels, now, status, profiles);
    const refusal = mappingRefusal(table.trace, table);
    const point = ecmPoint(table.trace, channels, now);
    let reason =
      gateReason ||
      refusal ||
      (liveness.state !== "live" ? "ECM trace greyed out (see above)." : "");
    const position = !reason && point ? ecmPosition(table, point) : null;
    if (!reason && !point)
      reason =
        ecmValueRefusal(table.trace, channels, now) ??
        "Waiting for paired X/Y trace samples within 750 ms.";
    else if (!reason && !position)
      reason = "ECM coordinates are outside this table's axes; no ECM cell outlined.";
    ecm.set(tableKey(table), {
      position,
      at: point ? ecmDisplay(table, point) : null,
      liveness,
      reason,
      xTimestamp: point?.xTimestamp ?? null,
    });
  }

  // One step per new ECM sample: the disagreement tally (W05.3) and, during
  // a capture, the client-side live coverage (W05.8). Stepped while
  // rendering, guarded by `seen`, so the next render finds nothing new and
  // stops; both writes are absolute values from this render (StrictMode-safe).
  const fresh = tables.flatMap((table) => {
    const key = tableKey(table);
    const state = ecm.get(key);
    if (!state?.position || state.xTimestamp === null || ecmSteps.seen[key] === state.xTimestamp)
      return [];
    return [{ table, key, position: state.position, xTimestamp: state.xTimestamp }];
  });
  if (fresh.length > 0) {
    const seen = { ...ecmSteps.seen };
    const nextTally = { ...ecmSteps.tally };
    for (const { table, key, position, xTimestamp } of fresh) {
      seen[key] = xTimestamp;
      const inferred =
        !traceReason && rpm && bfs ? tracePosition(table, rpm.value, bfs.value) : null;
      if (!inferred) continue;
      const differs = inferred.row !== position.row || inferred.column !== position.column;
      const old = nextTally[key] ?? { checks: 0, disagreements: 0, streakSince: null };
      nextTally[key] = {
        checks: old.checks + 1,
        disagreements: old.disagreements + (differs ? 1 : 0),
        streakSince: differs ? (old.streakSince ?? xTimestamp) : null,
      };
    }
    setEcmSteps({ seen, tally: nextTally });
    coverage.recordLive(fresh);
  }

  /** Everything the overlay says about `table`. */
  const forTable = (table: RomTable): TableOverlay => {
    const key = tableKey(table);
    const inferred = !traceReason && rpm && bfs ? tracePosition(table, rpm.value, bfs.value) : null;
    const state = ecm.get(key);
    const disagreement =
      state?.position && inferred && rpm && bfs && state.at
        ? cellDisagreement(state.position, inferred, state.at, { x: bfs.value, y: rpm.value })
        : null;
    const view = coverage.view(table);
    return {
      grid: {
        position: inferred,
        ecm: state?.position ?? null,
        coverage: view?.grid ?? null,
        dwell: view?.dwell ?? null,
      },
      inferred,
      ecm: state,
      disagreement,
      clampNote: state?.position ? describeClamp(state.position, table) : "",
      tally: ecmSteps.tally[key],
      outOfRange: !traceReason && !!rpm && !!bfs && table.status === "available" && !inferred,
    };
  };

  return {
    now,
    rpm,
    bfs,
    commanded,
    confirmed,
    setConfirmed,
    traceReason,
    coverage,
    forTable,
  };
}
