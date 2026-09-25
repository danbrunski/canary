import { useCallback, useMemo, useState } from "react";

import { Panel } from "../../components/Panel";
import { SegmentedControl, type SegmentOption } from "../../components/SegmentedControl";
import type { TuningContext } from "../../hooks/useTuningContext";
import {
  getCalibration,
  getDeliveryError,
  getPullComparison,
  getTiming,
  getTrimSurface,
} from "../../api/workbench";
import { useControlResource } from "../../hooks/useControlResource";
import { useAnalysisRun, useLogs } from "../../hooks/useWorkbench";
import type {
  Cell,
  DeliveryErrorResponse,
  DeliveryPairing,
  DelaySensitivity,
  NoiseFloor,
  PullComparisonResponse,
  SessionChoice,
  Significance,
  TimingResponse,
  TrimSurfaceResponse,
} from "../../types/workbench";
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
const VIEWS: ReadonlyArray<SegmentOption<View>> = [
  { id: "delivery", label: "Delivery error" },
  { id: "trim", label: "Trim surface" },
  { id: "timing", label: "Timing" },
];

type TimingMode = "cells" | "compare";
const TIMING_MODES: ReadonlyArray<SegmentOption<TimingMode>> = [
  { id: "cells", label: "Cells" },
  { id: "compare", label: "Compare revisions" },
];

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
  const { data: logs, error: refusal } = useLogs(token, includeSessions);

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
      count={choice.revision ? `all on ${choice.revision}` : `${choice.sessions.length} chosen`}
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
      Noise floor (null run): sd {floor.sd?.toFixed(2)}{" "}
      {floor.unit === "percent" ? "%" : floor.unit}
      {floor.min != null && floor.max != null
        ? `, per-cell range ${floor.min.toFixed(2)} … ${floor.max.toFixed(2)}`
        : ", per-cell range not recorded"}
      {` over ${floor.n_cells} cells. Measured under: ${floor.conditions}.`} A result inside the
      band is not distinguishable from changing nothing.
    </p>
  );
}

/** The floor verdict for a comparison: a refused floor is named as refused,
 *  with both regimes, and never drawn as a band the deltas could sit inside. */
function SignificanceNote({
  floor,
  significance,
}: {
  floor: NoiseFloor | null | undefined;
  significance: Significance | null | undefined;
}) {
  if (significance && !significance.available) {
    return (
      <p className="m-0 text-xs text-fg-dim" role="note" aria-label="Noise floor refused">
        Noise floor refused for {significance.channel}:{" "}
        {significance.floor_conditions
          ? `measured under "${significance.floor_conditions}", but this comparison is "${significance.requested_conditions}".`
          : significance.reason}{" "}
        No cell here has a significance verdict.
      </p>
    );
  }
  return <FloorNote floor={floor} />;
}

export function DeliveryChart({ result }: { result: DeliveryErrorResponse }) {
  const floor = result.noise_floor;
  const sd = floor.available ? (floor.sd ?? 0) : 0;
  const rangeLo = floor.available && floor.min != null ? floor.min : -sd;
  const rangeHi = floor.available && floor.max != null ? floor.max : sd;
  const values = result.bands.map((b) => b.error_pct ?? 0);
  const limit =
    Math.max(3, ...values.map((v) => Math.abs(v)), Math.abs(rangeLo), Math.abs(rangeHi)) * 1.15;
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
            className="fill-fg-dim/10"
          />
          <rect
            data-floor="sd"
            x={0}
            width={W}
            y={y(sd)}
            height={y(-sd) - y(sd)}
            className="fill-fg-dim/20"
          />
        </>
      )}
      <line x1={0} x2={W} y1={y(0)} y2={y(0)} className="stroke-fg-dim" />
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
              className={inside ? "fill-fg-dim" : v > 0 ? "fill-accent" : "fill-accent-2"}
            />
            <text
              x={i * bw + bw / 2}
              y={v >= 0 ? top - 3 : top + Math.abs(y(v) - y(0)) + 10}
              textAnchor="middle"
              fontSize={10}
              className="fill-fg"
            >
              {v.toFixed(2)}% · n {band.n}
            </text>
            <text
              x={i * bw + bw / 2}
              y={H + 14}
              textAnchor="middle"
              fontSize={9}
              className="fill-fg-dim"
            >
              {band.rpm_band}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

/** What "same instant" means for these numbers, stated rather than implied. */
function PairingNote({ pairing }: { pairing: DeliveryPairing }) {
  const delay = pairing.transport_delay_s;
  const bracket = pairing.transport_delay_bracket_s;
  return (
    <p className="m-0 mb-1 text-xs text-fg-dim" role="note" aria-label="Pairing">
      Target <code>{pairing.target_spec}</code> paired with <code>{pairing.measured_spec}</code> by
      acquisition time within {(pairing.max_separation_s * 1000).toFixed(0)} ms
      {pairing.transport_delay_modelled && delay != null
        ? `, with the λ read nearest t + ${(delay * 1000).toFixed(0)} ms (transport delay${
            bracket ? `, ${bracket[0]}–${bracket[1]} s supported` : ""
          })`
        : "; transport delay is not modelled on this route"}
      ; only a target steady within {pairing.target_step_tolerance} λ for{" "}
      {pairing.steady_target_dwell_s} s and {pairing.post_fuel_cut_holdoff_s} s clear of fuel cut
      counts.
    </p>
  );
}

/** A delay-bracket result that does not hold is said, never smoothed over. */
function DelayStability({ sensitivity }: { sensitivity: DelaySensitivity | null }) {
  if (!sensitivity || sensitivity.stable) return null;
  return (
    <p className="m-0 mb-1 text-xs text-accent-2" role="alert" aria-label="Delay sensitivity">
      Unstable across the transport-delay bracket: {sensitivity.reason} (
      {sensitivity.at
        .map(
          (row) =>
            `${row.delay_s} s: ${row.band_averaged_pct == null ? "none" : `${row.band_averaged_pct.toFixed(2)} %`}`,
        )
        .join(", ")}
      ).
    </p>
  );
}

function DeliveryView({ token, choice }: { token: string; choice: SessionChoice }) {
  const [measured, setMeasured] = useState("");
  const [minLoad, setMinLoad] = useState("");
  const { result, refusal, busy, run } = useAnalysisRun<DeliveryErrorResponse>();
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
            run((signal) =>
              getDeliveryError(
                token,
                choice,
                measured,
                !semantic && minLoad.trim() ? Number(minLoad) : null,
                signal,
              ),
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
              <PairingNote pairing={result.pairing} />
              <DelayStability sensitivity={result.delay_sensitivity} />
              <DeliveryChart result={result} />
            </>
          ) : (
            <EmptyResult
              kind={result.unavailable_kind}
              gates={result.gates}
              unavailable={result.unavailable}
            />
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
  const { result, refusal, busy, run } = useAnalysisRun<TrimSurfaceResponse>();
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
      <SegmentedControl label="Analysis views" options={VIEWS} value={view} onChange={setView} />
      {view === "delivery" && <DeliveryView token={token} choice={choice} />}
      {view === "trim" && <TrimView token={token} choice={choice} />}
      {view === "timing" && <TimingView token={token} choice={choice} />}
    </div>
  );
}
