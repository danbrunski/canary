import { useEffect, useMemo, useState } from "react";

import { controlJson } from "../../api/core";
import { Panel } from "../../components/Panel";
import type { TuningContext } from "../../hooks/useTuningContext";
import {
  describeRefusal,
  getDeliveryError,
  getLogs,
  getPullComparison,
  getTiming,
  getTrimSurface,
  type Cell,
  type DeliveryErrorResponse,
  type LogsResponse,
  type NoiseFloor,
  type PullComparisonResponse,
  type Refusal,
  type SessionChoice,
  type TimingResponse,
  type TrimSurfaceResponse,
} from "../../lib/workbenchApi";
import { buttonClass, cn, inputClass, selectClass } from "../../styles";
import type { StatusResponse } from "../../types";
import {
  CellGrid,
  ConditionsBlock,
  EmptyResult,
  RefusalNotice,
  UnavailableList,
} from "./WorkbenchNotices";

/** The measured-AFR sources, named — never a silent default. The ECM's own λ
 *  and the A/F-voltage path disagree by ~3.8 %, so the page makes the
 *  operator pick a side and every result carries the side picked. */
export const MEASURED_SOURCES: ReadonlyArray<{ id: string; label: string }> = [
  { id: "obd:O2_WR_LAMBDA_S1", label: "ECM λ — Mode 01 wide-range O2 (obd:O2_WR_LAMBDA_S1)" },
  { id: "semantic:measured_afr_b1", label: "A/F sensor voltage, bank 1, calibrated transfer" },
  { id: "semantic:measured_afr_b2", label: "A/F sensor voltage, bank 2, calibrated transfer" },
];

type View = "delivery" | "trim" | "timing";

async function run<T>(
  load: () => Promise<T>,
  setResult: (value: T | null) => void,
  setRefusal: (value: Refusal | null) => void,
  setBusy: (value: boolean) => void,
) {
  setBusy(true);
  setRefusal(null);
  try {
    setResult(await load());
  } catch (error) {
    setResult(null);
    setRefusal(describeRefusal(error));
  } finally {
    setBusy(false);
  }
}

// ---------------------------------------------------------------- chooser

function SessionChooser({
  token,
  choice,
  onChoice,
  runningRevision,
  selectedLogId,
}: {
  token: string;
  choice: SessionChoice;
  onChoice: (choice: SessionChoice) => void;
  runningRevision: string | null;
  selectedLogId: string | null;
}) {
  const [includeSessions, setIncludeSessions] = useState(false);
  const [logs, setLogs] = useState<LogsResponse | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  useEffect(() => {
    if (!token) return;
    getLogs(token, includeSessions)
      .then(setLogs)
      .catch((error: unknown) => setRefusal(describeRefusal(error)));
  }, [token, includeSessions]);

  const toggle = (id: string) => {
    const sessions = choice.sessions.includes(id)
      ? choice.sessions.filter((s) => s !== id)
      : [...choice.sessions, id];
    onChoice({ sessions, revision: null });
  };
  const empty = choice.sessions.length === 0 && !choice.revision;
  return (
    <Panel
      title="Sessions"
      count={
        choice.revision
          ? `all on ${choice.revision}`
          : `${choice.sessions.length} chosen`
      }
      actions={
        <label className="flex items-center gap-1.5 text-xs text-fg-dim">
          <input
            type="checkbox"
            checked={includeSessions}
            onChange={(e) => setIncludeSessions(e.target.checked)}
          />
          Include ordinary sessions
        </label>
      }
    >
      {empty && (
        <p className="m-0 mb-2 text-sm">
          Choose sessions or a revision.{" "}
          <button
            type="button"
            className={buttonClass}
            disabled={!runningRevision}
            title={runningRevision ? undefined : "The running calibration is not known yet"}
            onClick={() => runningRevision && onChoice({ sessions: [], revision: runningRevision })}
          >
            All on the running calibration{runningRevision ? ` (${runningRevision})` : ""}
          </button>
          {selectedLogId && (
            <button
              type="button"
              className={cn(buttonClass, "ml-2")}
              onClick={() => onChoice({ sessions: [selectedLogId], revision: null })}
            >
              Use the selected log
            </button>
          )}
        </p>
      )}
      {!empty && (
        <button
          type="button"
          className={cn(buttonClass, "mb-2")}
          onClick={() => onChoice({ sessions: [], revision: null })}
        >
          Clear selection
        </button>
      )}
      <RefusalNotice refusal={refusal} />
      {logs && (
        <ul className="max-h-48 space-y-0.5 overflow-auto text-xs">
          {logs.logs.map((log) => (
            <li key={log.session_id}>
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={choice.sessions.includes(log.session_id)}
                  onChange={() => toggle(log.session_id)}
                />
                <span>{log.session_id}</span>
                <span className="text-fg-faint">
                  {log.profile ?? "session"} · {log.revision.revision ?? "unknown revision"}
                </span>
              </label>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- delivery

function FloorNote({ floor }: { floor: NoiseFloor | null | undefined }) {
  if (!floor) return null;
  if (!floor.available) {
    return <p className="m-0 text-xs text-fg-faint">Noise floor: not measured for this vehicle.</p>;
  }
  return (
    <p className="m-0 text-xs text-fg-dim">
      Noise floor (null run): sd {floor.sd?.toFixed(2)} {floor.unit === "percent" ? "%" : floor.unit}
      {floor.min != null && floor.max != null
        ? `, per-cell range ${floor.min.toFixed(2)} … ${floor.max.toFixed(2)}`
        : ", per-cell range not recorded"}
      {` over ${floor.n_cells} cells. Measured under: ${floor.conditions}.`} A result inside the band
      is not distinguishable from changing nothing.
    </p>
  );
}

export function DeliveryChart({ result }: { result: DeliveryErrorResponse }) {
  const floor = result.noise_floor;
  const sd = floor.available ? (floor.sd ?? 0) : 0;
  const rangeLo = floor.available && floor.min != null ? floor.min : -sd;
  const rangeHi = floor.available && floor.max != null ? floor.max : sd;
  const values = result.bands.map((b) => b.error_pct ?? 0);
  const limit = Math.max(3, ...values.map((v) => Math.abs(v)), Math.abs(rangeLo), Math.abs(rangeHi)) * 1.15;
  const H = 200;
  const W = Math.max(320, result.bands.length * 70);
  const y = (v: number) => H / 2 - (v / limit) * (H / 2 - 12);
  const bw = W / Math.max(1, result.bands.length);
  return (
    <svg aria-label="Delivery error by rpm band" width={W} height={H + 20} className="bg-elev-2">
      {floor.available && (
        <>
          <rect
            data-floor="range"
            x={0}
            width={W}
            y={y(rangeHi)}
            height={y(rangeLo) - y(rangeHi)}
            fill="rgba(148,163,173,0.12)"
          />
          <rect
            data-floor="sd"
            x={0}
            width={W}
            y={y(sd)}
            height={y(-sd) - y(sd)}
            fill="rgba(148,163,173,0.22)"
          />
        </>
      )}
      <line x1={0} x2={W} y1={y(0)} y2={y(0)} stroke="#94a3ad" />
      {result.bands.map((band, i) => {
        const v = band.error_pct ?? 0;
        const top = Math.min(y(v), y(0));
        const inside = floor.available && v >= -sd && v <= sd;
        return (
          <g key={band.rpm_band} data-band={band.rpm_band}>
            <rect
              x={i * bw + bw * 0.2}
              width={bw * 0.6}
              y={top}
              height={Math.abs(y(v) - y(0))}
              fill={inside ? "#94a3ad" : v > 0 ? "#ffb547" : "#4fd6e0"}
            />
            <text x={i * bw + bw / 2} y={v >= 0 ? top - 3 : top + Math.abs(y(v) - y(0)) + 10} textAnchor="middle" fontSize={10} fill="#e6edf3">
              {v.toFixed(2)}% · n {band.n}
            </text>
            <text x={i * bw + bw / 2} y={H + 14} textAnchor="middle" fontSize={9} fill="#94a3ad">
              {band.rpm_band}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function DeliveryView({ token, choice }: { token: string; choice: SessionChoice }) {
  const [measured, setMeasured] = useState("");
  const [minLoad, setMinLoad] = useState("");
  const [result, setResult] = useState<DeliveryErrorResponse | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [busy, setBusy] = useState(false);
  const semantic = measured.startsWith("semantic:");
  const ready = Boolean(token && measured && (choice.sessions.length || choice.revision));
  return (
    <Panel title="Delivery error by rpm band">
      <div className="mb-3 flex flex-wrap items-end gap-2 text-xs">
        <label className="flex min-w-72 flex-col gap-1">
          Measured-AFR source
          <select
            className={selectClass}
            aria-label="Measured-AFR source"
            value={measured}
            onChange={(e) => setMeasured(e.target.value)}
          >
            <option value="">Choose a source — the two paths disagree by ~3.8 %</option>
            {MEASURED_SOURCES.map((source) => (
              <option key={source.id} value={source.id}>
                {source.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1">
          Min load (Mode 01 only)
          <input
            className={inputClass}
            inputMode="decimal"
            placeholder="none"
            disabled={semantic}
            value={semantic ? "" : minLoad}
            onChange={(e) => setMinLoad(e.target.value)}
          />
        </label>
        <button
          type="button"
          className={buttonClass}
          disabled={!ready || busy}
          onClick={() =>
            run(
              () =>
                getDeliveryError(
                  token,
                  choice,
                  measured,
                  !semantic && minLoad.trim() ? Number(minLoad) : null,
                ),
              setResult,
              setRefusal,
              setBusy,
            )
          }
        >
          {busy ? "Running…" : "Run"}
        </button>
      </div>
      <RefusalNotice refusal={refusal} />
      {result && (
        <>
          {result.available ? (
            <>
              <p className="m-0 mb-1 text-sm">
                Band-averaged {result.band_averaged_pct?.toFixed(2)} % · pooled{" "}
                {result.pooled_pct?.toFixed(2)} % · n {result.n} · measured by{" "}
                <code>{result.measured}</code>
              </p>
              <DeliveryChart result={result} />
            </>
          ) : (
            <EmptyResult kind={result.unavailable_kind} gates={result.gates} unavailable={result.unavailable} />
          )}
          <FloorNote floor={result.noise_floor} />
          <UnavailableList items={result.unavailable} />
          <ConditionsBlock
            conditions={result.conditions}
            extra={
              <>
                <dt>Measured</dt>
                <dd className="m-0">{result.measured}</dd>
                <dt>Load floor</dt>
                <dd className="m-0">{result.min_load ?? "none"}</dd>
              </>
            }
          />
        </>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- trim

function TrimView({ token, choice }: { token: string; choice: SessionChoice }) {
  const [result, setResult] = useState<TrimSurfaceResponse | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [busy, setBusy] = useState(false);
  const [bank, setBank] = useState("b1");
  const [split, setSplit] = useState("pooled");
  const ready = Boolean(token && (choice.sessions.length || choice.revision));
  const cells: Cell[] = useMemo(() => {
    if (!result) return [];
    if (split === "pooled") return result.banks[bank]?.cells ?? [];
    return result.per_session.find((s) => s.session_id === split)?.cells[bank] ?? [];
  }, [result, bank, split]);
  return (
    <Panel title="Closed-loop trim surface (STFT + LTFT)">
      <div className="mb-3 flex flex-wrap items-end gap-2 text-xs">
        <button
          type="button"
          className={buttonClass}
          disabled={!ready || busy}
          onClick={() => run(() => getTrimSurface(token, choice), setResult, setRefusal, setBusy)}
        >
          {busy ? "Running…" : "Run"}
        </button>
        {result && (
          <>
            <label className="flex flex-col gap-1">
              Bank
              <select className={selectClass} aria-label="Bank" value={bank} onChange={(e) => setBank(e.target.value)}>
                {Object.keys(result.banks).map((b) => (
                  <option key={b} value={b}>
                    {b === "b1" ? "Bank 1" : "Bank 2"}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              Split
              <select className={selectClass} aria-label="Split" value={split} onChange={(e) => setSplit(e.target.value)}>
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
            />
          ) : (
            <EmptyResult kind={result.unavailable_kind} gates={result.gates} unavailable={result.unavailable} />
          )}
          <p className="m-0 mt-1 text-xs text-fg-faint">
            A pooled surface can hide a per-session effect — the −5 % hump of 2026-09-18 existed only on
            the altitude trip. Use Split to check each session.
          </p>
          <UnavailableList items={result.unavailable} />
          <ConditionsBlock conditions={result.conditions} />
        </>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- timing

function comparedAsCells(result: PullComparisonResponse): Cell[] {
  return result.cells.map((c) => ({
    rpm_index: c.rpm_index,
    airflow_index: c.airflow_index,
    rpm_range: c.rpm_range,
    airflow_range: c.airflow_range,
    n: Math.min(c.n_a, c.n_b),
    sparse: false,
    delta: c.delta,
    dropped: c.dropped,
  }));
}

function TimingView({ token, choice }: { token: string; choice: SessionChoice }) {
  const [result, setResult] = useState<TimingResponse | null>(null);
  const [compare, setCompare] = useState<PullComparisonResponse | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<"cells" | "compare">("cells");
  const [revisions, setRevisions] = useState<string[]>([]);
  const [revA, setRevA] = useState("");
  const [revB, setRevB] = useState("");
  const [floorChannel, setFloorChannel] = useState("");
  useEffect(() => {
    if (!token || mode !== "compare") return;
    controlJson<{ history: Array<{ revision: string }> }>("/control/calibration", token)
      .then((data) => setRevisions(data.history.map((h) => h.revision)))
      .catch(() => setRevisions([]));
  }, [token, mode]);
  const ready = Boolean(token && (choice.sessions.length || choice.revision));
  return (
    <Panel
      title="Final timing per cell"
      actions={
        <div role="group" aria-label="Timing mode" className="flex gap-1">
          {(["cells", "compare"] as const).map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={mode === m}
              className={cn(buttonClass, mode === m && "border-accent text-accent")}
              onClick={() => setMode(m)}
            >
              {m === "cells" ? "Cells" : "Compare revisions"}
            </button>
          ))}
        </div>
      }
    >
      {mode === "cells" && (
        <>
          <button
            type="button"
            className={cn(buttonClass, "mb-3")}
            disabled={!ready || busy}
            onClick={() => run(() => getTiming(token, choice), setResult, setRefusal, setBusy)}
          >
            {busy ? "Running…" : "Run"}
          </button>
          <RefusalNotice refusal={refusal} />
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
              <EmptyResult kind={result.unavailable_kind} gates={result.gates} unavailable={result.unavailable} />
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
                <select className={selectClass} aria-label={label} value={value} onChange={(e) => set(e.target.value)}>
                  <option value="">choose</option>
                  {revisions.map((r) => (
                    <option key={r}>{r}</option>
                  ))}
                </select>
              </label>
            ))}
            <label className="flex flex-col gap-1">
              Noise-floor channel (optional)
              <input className={inputClass} value={floorChannel} onChange={(e) => setFloorChannel(e.target.value)} />
            </label>
            <button
              type="button"
              className={buttonClass}
              disabled={!token || !revA || !revB || revA === revB || busy}
              onClick={() =>
                run(
                  () => getPullComparison(token, revA, revB, floorChannel.trim() || null),
                  setCompare,
                  setRefusal,
                  setBusy,
                )
              }
            >
              {busy ? "Running…" : "Compare"}
            </button>
          </div>
          <RefusalNotice refusal={refusal} />
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
                />
                <p className="m-0 mt-1 text-xs text-warn">
                  {compare.cells.filter((c) => c.dropped).length} cell(s) outlined: timing dropped
                  where {compare.revision_a} had advanced — the ECM's emergency retard, the only knock
                  proxy this ECM offers.
                </p>
              </>
            ) : (
              <EmptyResult kind={compare.unavailable_kind} gates={compare.gates} unavailable={compare.unavailable} />
            ))}
          {compare && <FloorNote floor={compare.noise_floor} />}
          {compare && <UnavailableList items={compare.unavailable} />}
          {compare && <ConditionsBlock conditions={compare.conditions} />}
        </>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- page

/** W04 Analysis: derived results that may pool many sessions. Each view
 *  shows n, its gate report and conditions, and says which kind of
 *  unavailable an empty result is. */
export function AnalysisPage({
  token,
  status,
  context,
}: {
  token: string;
  status: StatusResponse | null;
  context: TuningContext;
}) {
  const [choice, setChoice] = useState<SessionChoice>({ sessions: [], revision: null });
  const [view, setView] = useState<View>("delivery");
  const runningRevision = status?.registry_revision ?? null;
  return (
    <div className="flex flex-col gap-4">
      {!token && <p className="m-0 text-sm">Enter the control token to run analyses.</p>}
      <SessionChooser
        token={token}
        choice={choice}
        onChoice={setChoice}
        runningRevision={runningRevision}
        selectedLogId={context.selectedLogId}
      />
      <div role="tablist" aria-label="Analysis views" className="flex gap-1">
        {(
          [
            ["delivery", "Delivery error"],
            ["trim", "Trim surface"],
            ["timing", "Timing"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={view === id}
            className={cn(buttonClass, view === id && "border-accent text-accent")}
            onClick={() => setView(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {view === "delivery" && <DeliveryView token={token} choice={choice} />}
      {view === "trim" && <TrimView token={token} choice={choice} />}
      {view === "timing" && <TimingView token={token} choice={choice} />}
    </div>
  );
}
