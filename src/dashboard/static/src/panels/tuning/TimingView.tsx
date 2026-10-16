import { useCallback, useState } from "react";

import { getCalibration, getPullComparison, getTiming } from "../../api/workbench";
import { Panel } from "../../components/Panel";
import { SegmentedControl, type SegmentOption } from "../../components/SegmentedControl";
import { useControlResource } from "../../hooks/useControlResource";
import { useAnalysisRun } from "../../hooks/useWorkbench";
import { comparedAsCells } from "../../lib/analysisPlot";
import { buttonClass, cn, inputClass, selectClass } from "../../styles";
import type { PullComparisonResponse, SessionChoice, TimingResponse } from "../../types/workbench";
import { SignificanceNote } from "./NoiseFloorNotes";
import {
  CellGrid,
  ConditionsBlock,
  EmptyResult,
  RefusalNotice,
  UnavailableList,
} from "./WorkbenchNotices";

type TimingMode = "cells" | "compare";
const TIMING_MODES: ReadonlyArray<SegmentOption<TimingMode>> = [
  { id: "cells", label: "Cells" },
  { id: "compare", label: "Compare revisions" },
];

/** Final timing per cell of the chosen sessions, or the change between two
 *  calibration revisions. The two modes are independent requests: each owns
 *  its run (so a refusal shows only under the mode that got it, and one can be
 *  in flight while the other is read), and both survive a switch of mode. */
export function TimingView({ token, choice }: { token: string; choice: SessionChoice }) {
  // One run per mode: a Compare refusal belongs under Compare, not Cells.
  const cells = useAnalysisRun<TimingResponse>();
  const comparison = useAnalysisRun<PullComparisonResponse>();
  const result = cells.result;
  const compare = comparison.result;
  const [mode, setMode] = useState<TimingMode>("cells");
  const [revA, setRevA] = useState("");
  const [revB, setRevB] = useState("");
  const [floorChannel, setFloorChannel] = useState("");
  const calibration = useControlResource(
    useCallback((signal: AbortSignal) => getCalibration(token, signal), [token]),
    { enabled: Boolean(token) && mode === "compare", intervalMs: null },
  );
  const revisions = calibration.data?.history.map((h) => h.revision) ?? [];
  const ready = Boolean(token && (choice.sessions.length || choice.revision));
  return (
    <Panel
      title="Final timing per cell"
      actions={
        <SegmentedControl
          label="Timing mode"
          options={TIMING_MODES}
          value={mode}
          onChange={setMode}
        />
      }
    >
      {mode === "cells" && (
        <>
          <button
            type="button"
            className={cn(buttonClass, "mb-3")}
            disabled={!ready || cells.busy}
            onClick={() => cells.run((signal) => getTiming(token, choice, signal))}
          >
            {cells.busy ? "Running…" : "Run"}
          </button>
          <RefusalNotice refusal={cells.refusal} />
          {result &&
            (result.available ? (
              <CellGrid
                cells={result.cells}
                rpmEdges={result.axes.rpm_edges}
                airflowEdges={result.axes.airflow_edges}
                valueKey="timing"
                unit={`${result.value}; rows ${result.axes.rpm}, columns ${result.axes.airflow}`}
                label="Timing cells"
              />
            ) : (
              <EmptyResult
                kind={result.unavailable_kind}
                gates={result.gates}
                unavailable={result.unavailable}
              />
            ))}
          {result && <UnavailableList items={result.unavailable} />}
          {result && <ConditionsBlock conditions={result.conditions} />}
        </>
      )}
      {mode === "compare" && (
        <>
          <div className="mb-3 flex flex-wrap items-end gap-2 text-xs">
            {(
              [
                ["Revision A", revA, setRevA],
                ["Revision B", revB, setRevB],
              ] as const
            ).map(([label, value, set]) => (
              <label key={label} className="flex flex-col gap-1">
                {label}
                <select
                  className={selectClass}
                  aria-label={label}
                  value={value}
                  onChange={(e) => set(e.target.value)}
                >
                  <option value="">choose</option>
                  {revisions.map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </select>
              </label>
            ))}
            <label className="flex flex-col gap-1">
              Noise-floor channel (optional)
              <input
                className={inputClass}
                value={floorChannel}
                onChange={(e) => setFloorChannel(e.target.value)}
              />
            </label>
            <button
              type="button"
              className={buttonClass}
              disabled={!token || !revA || !revB || revA === revB || comparison.busy}
              onClick={() =>
                comparison.run((signal) =>
                  getPullComparison(token, revA, revB, floorChannel.trim() || null, signal),
                )
              }
            >
              {comparison.busy ? "Running…" : "Compare"}
            </button>
          </div>
          <RefusalNotice refusal={comparison.refusal} />
          {compare &&
            (compare.available ? (
              <>
                <CellGrid
                  cells={comparedAsCells(compare)}
                  rpmEdges={compare.axes.rpm_edges}
                  airflowEdges={compare.axes.airflow_edges}
                  valueKey="delta"
                  unit={`${compare.value} change ${compare.revision_a} → ${compare.revision_b}`}
                  highlight={(cell) => cell.dropped === true}
                  label="Timing comparison"
                  scale="diverging"
                />
                <p className="m-0 mt-1 text-xs text-warn">
                  {compare.cells.filter((c) => c.dropped).length} cell(s) outlined: timing dropped
                  where {compare.revision_a} had advanced — the ECM's emergency retard, the only
                  knock proxy this ECM offers.
                </p>
              </>
            ) : (
              <EmptyResult
                kind={compare.unavailable_kind}
                gates={compare.gates}
                unavailable={compare.unavailable}
              />
            ))}
          {compare && (
            <SignificanceNote floor={compare.noise_floor} significance={compare.significance} />
          )}
          {compare && <UnavailableList items={compare.unavailable} />}
          {compare && <ConditionsBlock conditions={compare.conditions} />}
        </>
      )}
    </Panel>
  );
}
