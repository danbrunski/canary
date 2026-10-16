import { useMemo, useState } from "react";

import { getTrimSurface } from "../../api/workbench";
import { Panel } from "../../components/Panel";
import { useAnalysisRun } from "../../hooks/useWorkbench";
import { trimCells } from "../../lib/analysisPlot";
import { buttonClass, selectClass } from "../../styles";
import type { Cell, SessionChoice, TrimSurfaceResponse } from "../../types/workbench";
import {
  CellGrid,
  ConditionsBlock,
  EmptyResult,
  RefusalNotice,
  UnavailableList,
} from "./WorkbenchNotices";

/** The closed-loop trim surface (STFT + LTFT) of the chosen sessions, by bank,
 *  pooled or split by session. Owns its own run and its bank/split choice. */
export function TrimView({ token, choice }: { token: string; choice: SessionChoice }) {
  const { result, refusal, busy, run } = useAnalysisRun<TrimSurfaceResponse>();
  const [bank, setBank] = useState("b1");
  const [split, setSplit] = useState("pooled");
  const ready = Boolean(token && (choice.sessions.length || choice.revision));
  const cells: Cell[] = useMemo(() => trimCells(result, bank, split), [result, bank, split]);
  return (
    <Panel title="Closed-loop trim surface (STFT + LTFT)">
      <div className="mb-3 flex flex-wrap items-end gap-2 text-xs">
        <button
          type="button"
          className={buttonClass}
          disabled={!ready || busy}
          onClick={() => run((signal) => getTrimSurface(token, choice, signal))}
        >
          {busy ? "Running…" : "Run"}
        </button>
        {result && (
          <>
            <label className="flex flex-col gap-1">
              Bank
              <select
                className={selectClass}
                aria-label="Bank"
                value={bank}
                onChange={(e) => setBank(e.target.value)}
              >
                {Object.keys(result.banks).map((b) => (
                  <option key={b} value={b}>
                    {b === "b1" ? "Bank 1" : "Bank 2"}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              Split
              <select
                className={selectClass}
                aria-label="Split"
                value={split}
                onChange={(e) => setSplit(e.target.value)}
              >
                <option value="pooled">Pooled</option>
                {result.per_session.map((s) => (
                  <option key={s.session_id} value={s.session_id}>
                    {s.session_id}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
      </div>
      <RefusalNotice refusal={refusal} />
      {result && (
        <>
          {result.available ? (
            <CellGrid
              cells={cells}
              rpmEdges={result.axes.rpm_edges}
              airflowEdges={result.axes.airflow_edges}
              valueKey="total"
              unit={`% total correction; rows ${result.axes.rpm}, columns ${result.axes.airflow}`}
              label="Trim surface"
              scale="diverging"
            />
          ) : (
            <EmptyResult
              kind={result.unavailable_kind}
              gates={result.gates}
              unavailable={result.unavailable}
            />
          )}
          <p className="m-0 mt-1 text-xs text-fg-faint">
            A pooled surface can hide a per-session effect — the −5 % hump of 2026-09-18 existed
            only on the altitude trip. Use Split to check each session.
          </p>
          <UnavailableList items={result.unavailable} />
          <ConditionsBlock conditions={result.conditions} />
        </>
      )}
    </Panel>
  );
}
