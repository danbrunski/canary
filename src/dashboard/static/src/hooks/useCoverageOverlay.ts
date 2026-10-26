// The ROM tables' coverage overlay (W05.6/W05.8), shown by the ROM Editor's live
// overlay (it was ROM Maps' `RomMapsPanel`, R08.8).
// Per table: whether the overlay is on, which source it shows (the selected
// log's stored residency, or this capture's live samples), the residency
// reads, and the client-side live grids. It was four keyed `Record` states,
// an imperative fetch and a refetch effect spread across the panel.

import { useCallback, useEffect, useRef, useState } from "react";

import { getCellResidency } from "../api/workbench";
import {
  addCoverage,
  emptyCoverage,
  mappingRefusal,
  residencyMatchesTable,
  type CellPosition,
  type CoverageGrid,
} from "../lib/romTrace";
import { describeRefusal } from "../lib/workbenchApi";
import type { CellResidency, RomTable } from "../types/workbench";

export type CoverageSource = "log" | "live";

type ResidencyState = { result: CellResidency } | { error: string };

export interface CoverageView {
  grid: CoverageGrid | null;
  dwell: number[][] | null;
  note: string;
  /** The note's remedy is on another page. */
  action?: "logs";
}

export const tableKey = (table: RomTable) => `${table.slot}-${table.kind}`;
const residencyKey = (logId: string, table: RomTable) => `${logId}|${table.kind}`;

function residencyError(err: unknown): string {
  const refusal = describeRefusal(err);
  return refusal.kind === "deferred"
    ? `Coverage for a stored log is deferred while “${refusal.profile ?? "a capture"}” is recording. ${refusal.laptopPath ?? ""}`.trim()
    : refusal.message;
}

export function useCoverageOverlay({
  token,
  selectedLogId,
  captureKey,
  tables,
}: {
  token: string;
  selectedLogId: string | null;
  /** Identity of the recording capture (null when none). Live coverage
   *  belongs to one capture and starts over when it changes. */
  captureKey: string | null;
  tables: RomTable[];
}) {
  const captureActive = captureKey !== null;
  const [on, setOn] = useState<Record<string, boolean>>({});
  const [chosen, setChosen] = useState<Record<string, CoverageSource>>({});
  const [residency, setResidency] = useState<Record<string, ResidencyState>>({});
  const [live, setLive] = useState<{ capture: string | null; grids: Record<string, CoverageGrid> }>(
    { capture: captureKey, grids: {} },
  );
  const liveGrids = live.capture === captureKey ? live.grids : {};
  // Residency reads already started, so a render never requests one twice.
  const requested = useRef(new Set<string>());

  const sourceOf = useCallback(
    (key: string): CoverageSource => chosen[key] ?? (captureActive ? "live" : "log"),
    [chosen, captureActive],
  );

  // Read the selected log's residency for every open log-sourced overlay:
  // on toggle, on a source change and when the selected log changes.
  useEffect(() => {
    if (!token || !selectedLogId) return;
    for (const table of tables) {
      const key = tableKey(table);
      if (!on[key] || sourceOf(key) !== "log") continue;
      const rkey = residencyKey(selectedLogId, table);
      if (requested.current.has(rkey)) continue;
      requested.current.add(rkey);
      getCellResidency(token, selectedLogId, table.kind).then(
        (result) => setResidency((prev) => ({ ...prev, [rkey]: { result } })),
        (err: unknown) =>
          setResidency((prev) => ({ ...prev, [rkey]: { error: residencyError(err) } })),
      );
    }
  }, [token, selectedLogId, tables, on, sourceOf]);

  /** Forget a log read so the effect above asks again (a retry). */
  const forget = (table: RomTable) => {
    if (!selectedLogId) return;
    const rkey = residencyKey(selectedLogId, table);
    requested.current.delete(rkey);
    setResidency((prev) => {
      const next = { ...prev };
      delete next[rkey];
      return next;
    });
  };

  const toggle = (table: RomTable, enable: boolean) => {
    const key = tableKey(table);
    setOn((prev) => ({ ...prev, [key]: enable }));
    setChosen((prev) => ({ ...prev, [key]: sourceOf(key) }));
    if (enable) forget(table);
  };

  const choose = (table: RomTable, source: CoverageSource) => {
    setChosen((prev) => ({ ...prev, [tableKey(table)]: source }));
    if (source === "log") forget(table);
  };

  /** This render's new ECM samples during a capture. Called while rendering
   *  (`useLiveOverlay`), so it writes an absolute value computed from this
   *  render's grids rather than an updater: a StrictMode double render then
   *  records each sample once, not twice. */
  const recordLive = (samples: { table: RomTable; position: CellPosition }[]) => {
    if (captureKey === null || samples.length === 0) return;
    const grids = { ...liveGrids };
    for (const { table, position } of samples) {
      const key = tableKey(table);
      grids[key] = addCoverage(grids[key] ?? emptyCoverage(table), position);
    }
    setLive({ capture: captureKey, grids });
  };

  /** What the overlay shows for `table`, or null while it is off. */
  const view = (table: RomTable): CoverageView | null => {
    const key = tableKey(table);
    if (!on[key] || table.status !== "available") return null;
    if (sourceOf(key) === "live") {
      if (!captureActive)
        return {
          grid: null,
          dwell: null,
          note: "No capture is recording; live coverage accumulates only during a capture and resets when it ends.",
        };
      const refusal = mappingRefusal(table.trace, table);
      if (refusal)
        return { grid: null, dwell: null, note: `Live coverage unavailable. ${refusal}` };
      const grid = liveGrids[key] ?? emptyCoverage(table);
      const total = grid.flat().reduce((a, b) => a + b, 0);
      return {
        grid,
        dwell: null,
        note: `Live coverage for this capture: ${total} ECM samples. Client-side only; resets when the capture ends.`,
      };
    }
    if (!selectedLogId)
      return {
        grid: null,
        dwell: null,
        note: "No log selected. Choose one on Logs to see which cells it visited.",
        action: "logs",
      };
    const state = residency[residencyKey(selectedLogId, table)];
    if (!state)
      return { grid: null, dwell: null, note: `Reading cell residency for ${selectedLogId}…` };
    if ("error" in state) return { grid: null, dwell: null, note: state.error };
    const result = state.result;
    if (!result.available)
      return {
        grid: null,
        dwell: null,
        note: `No coverage for ${selectedLogId}: ${result.message}`,
      };
    if (!residencyMatchesTable(result, table))
      return {
        grid: null,
        dwell: null,
        note: `${selectedLogId} ran revision ${result.revision ?? "unknown"}, whose axes differ from the loaded ROM's; its coverage is not overlaid on this table.`,
      };
    return {
      grid: result.counts,
      dwell: result.dwell_s,
      note: `${selectedLogId}: ${result.samples} ECM samples${result.outside_axes ? `, ${result.outside_axes} outside the axes` : ""}${result.clamped_samples ? `, ${result.clamped_samples} past an axis end, counted in the edge cell the ECM reads` : ""}.`,
    };
  };

  return {
    isOn: (table: RomTable) => !!on[tableKey(table)],
    sourceFor: (table: RomTable) => sourceOf(tableKey(table)),
    toggle,
    choose,
    recordLive,
    view,
  };
}
