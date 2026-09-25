import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Panel } from "../../components/Panel";
import { useLocalStorageJson } from "../../hooks/useLocalStorage";
import type { TuningContext } from "../../hooks/useTuningContext";
import {
  REGIMES,
  describeRefusal,
  formatDuration,
  getFrame,
  getLogs,
  toCsv,
  type FrameResponse,
  type LogSummary,
  type LogsResponse,
  type Refusal,
  type Regime,
} from "../../lib/workbenchApi";
import { buttonClass, cn, inputClass, selectClass } from "../../styles";
import { RefusalNotice } from "./WorkbenchNotices";

/** J1979 names every OBD-II vehicle answers — a starting set, not a vehicle
 *  fact. The operator edits it; the choice persists. */
const DEFAULT_SPECS = "obd:RPM,obd:MAF,obd:COMMANDED_EQUIV_RATIO,obd:O2_WR_LAMBDA_S1";
const SPECS_KEY = "canary.tuning.logSpecs.v1";
const ROW_HEIGHT = 24;
const VIEWPORT_ROWS = 18;
const OVERSCAN = 20;

function downloadText(content: string, filename: string) {
  const blob = new Blob([content], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------- browser

export function LogBrowser({
  token,
  selectedId,
  onSelect,
}: {
  token: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [includeSessions, setIncludeSessions] = useState(false);
  const [data, setData] = useState<LogsResponse | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setRefusal(null);
    getLogs(token, includeSessions, controller.signal)
      .then(setData)
      .catch((error: unknown) => {
        if ((error as { name?: string }).name !== "AbortError") setRefusal(describeRefusal(error));
      });
    return () => controller.abort();
  }, [token, includeSessions]);

  return (
    <Panel
      title="Captures"
      count={data ? `${data.logs.length}${data.capped ? ` of ${data.total}` : ""}` : undefined}
      actions={
        <label className="flex items-center gap-1.5 text-xs text-fg-dim">
          <input
            type="checkbox"
            checked={includeSessions}
            onChange={(event) => setIncludeSessions(event.target.checked)}
          />
          Include ordinary sessions
        </label>
      }
    >
      {!token && <p className="m-0 text-sm">Enter the control token to browse logs.</p>}
      <RefusalNotice refusal={refusal} />
      {data && data.logs.length === 0 && (
        <p className="m-0 text-sm text-fg-dim">
          {includeSessions
            ? "No sessions recorded yet."
            : "No captures yet — start one on Logging, or include ordinary sessions."}
        </p>
      )}
      {data && data.logs.length > 0 && (
        <div className="max-h-80 overflow-auto">
          <table aria-label="Capture browser" className="w-full text-left text-xs">
            <thead className="text-fg-faint">
              <tr>
                <th>Profile</th>
                <th>Start</th>
                <th>Duration</th>
                <th>Tune revision</th>
                <th>Quality</th>
                <th>End reason</th>
                <th className="text-right">Rows</th>
              </tr>
            </thead>
            <tbody>
              {data.logs.map((log) => (
                <LogRow
                  key={log.session_id}
                  log={log}
                  selected={log.session_id === selectedId}
                  onSelect={onSelect}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function LogRow({
  log,
  selected,
  onSelect,
}: {
  log: LogSummary;
  selected: boolean;
  onSelect: (id: string) => void;
}) {
  return (
    <tr
      className={cn(
        "cursor-pointer border-t border-border-soft hover:bg-elev-2",
        selected && "bg-elev-2 text-accent",
      )}
      aria-selected={selected}
      onClick={() => onSelect(log.session_id)}
    >
      <td className="py-1">{log.profile ?? (log.is_capture ? "unknown" : "session")}</td>
      <td title={log.session_id}>
        {log.started_at ? new Date(log.started_at).toLocaleString() : log.session_id}
      </td>
      <td title={log.duration_source ?? "unknown"}>{formatDuration(log.duration_s)}</td>
      <td>
        {log.revision.revision ?? "unknown"}
        <span className="text-fg-faint"> · {log.revision.source.replace(/_/g, " ")}</span>
        {log.revision_boundaries.length > 0 && (
          <span className="ml-1 text-warn" title="A flash falls inside this log">
            ⚑ flash inside
          </span>
        )}
      </td>
      <td
        className={
          log.quality?.verdict === "pass" ? "text-ok" : log.quality ? "text-warn" : "text-fg-faint"
        }
      >
        {log.quality?.verdict ?? "unknown"}
      </td>
      <td>{log.end_reason?.replace(/_/g, " ") ?? (log.is_capture ? "open" : "—")}</td>
      <td className="text-right font-num">{log.row_count?.toLocaleString() ?? "unknown"}</td>
    </tr>
  );
}

// ---------------------------------------------------------------- viewer

type Presentation = "table" | "visual";

export function LogViewer({ token, sessionId }: { token: string; sessionId: string }) {
  const [specsText, setSpecsText] = useLocalStorageJson(SPECS_KEY, DEFAULT_SPECS);
  const [draft, setDraft] = useState(specsText);
  const [grid, setGrid] = useState("1s");
  const [regime, setRegime] = useState<Regime>("all");
  const [offset, setOffset] = useState(0);
  const [presentation, setPresentation] = useState<Presentation>("table");
  const [frame, setFrame] = useState<FrameResponse | null>(null);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  const [loading, setLoading] = useState(false);

  const specs = useMemo(
    () =>
      specsText
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    [specsText],
  );

  useEffect(() => {
    setOffset(0);
  }, [sessionId, specsText, grid, regime]);

  useEffect(() => {
    if (!token || specs.length === 0) return;
    const controller = new AbortController();
    setLoading(true);
    setRefusal(null);
    getFrame(token, sessionId, { specs, grid, regime, offset }, controller.signal)
      .then((next) => setFrame(next))
      .catch((error: unknown) => {
        if ((error as { name?: string }).name === "AbortError") return;
        setFrame(null);
        setRefusal(describeRefusal(error));
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [token, sessionId, specs, grid, regime, offset]);

  return (
    <Panel
      title={sessionId}
      actions={
        <div role="group" aria-label="Presentation" className="flex gap-1">
          {(["table", "visual"] as const).map((mode) => (
            <button
              key={mode}
              type="button"
              className={cn(buttonClass, presentation === mode && "border-accent text-accent")}
              aria-pressed={presentation === mode}
              onClick={() => setPresentation(mode)}
            >
              {mode === "table" ? "Table" : "Visual"}
            </button>
          ))}
        </div>
      }
    >
      <form
        className="mb-3 flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setSpecsText(draft);
        }}
      >
        <label className="flex min-w-72 flex-1 flex-col gap-1 text-xs">
          Channels (comma-separated specs)
          <input className={inputClass} value={draft} onChange={(e) => setDraft(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Grid
          <select className={selectClass} value={grid} onChange={(e) => setGrid(e.target.value)}>
            {["100ms", "250ms", "500ms", "1s", "5s"].map((g) => (
              <option key={g}>{g}</option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs">
          Regime
          <select
            className={selectClass}
            aria-label="Regime"
            value={regime}
            onChange={(e) => setRegime(e.target.value as Regime)}
          >
            {REGIMES.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className={buttonClass}>
          Apply
        </button>
      </form>
      <RefusalNotice refusal={refusal} />
      {loading && <p className="m-0 text-xs text-fg-faint">Loading…</p>}
      {frame && <FrameNotes frame={frame} onOffset={setOffset} />}
      {frame && presentation === "table" && <FrameTable frame={frame} />}
      {frame && presentation === "visual" && <FrameVisual frame={frame} />}
    </Panel>
  );
}

function FrameNotes({ frame, onOffset }: { frame: FrameResponse; onOffset: (n: number) => void }) {
  const report = frame.regime_report;
  const end = frame.offset + frame.rows_returned;
  return (
    <div className="mb-2 space-y-1 text-xs text-fg-dim">
      <p className="m-0">
        Revision {frame.revision.revision ?? "unknown"} ({frame.revision.source.replace(/_/g, " ")})
        {frame.profile && ` · profile ${frame.profile}`} · grid {frame.grid}
      </p>
      {frame.revision_boundaries.length > 0 && (
        <p role="alert" className="m-0 text-warn">
          A flash falls inside this log (
          {frame.revision_boundaries
            .map((b) => `${b.revision} at ${new Date(b.flashed_at).toLocaleString()}`)
            .join(", ")}
          ). The registry labels the whole log by its start, so figures across the marker mix two
          calibrations.
        </p>
      )}
      {frame.missing.map((item) => (
        <p key={item.spec} className="m-0 text-bad">
          {item.reason}
        </p>
      ))}
      {report.available === false && (
        <p className="m-0 text-bad">Regime filter unavailable: {report.reason}</p>
      )}
      <p className="m-0">
        {frame.freshness.dropped.toLocaleString()} of{" "}
        {frame.freshness.cells_with_any.toLocaleString()} cells dropped by the freshness bound
        (every channel must be sampled in the cell)
        {report.regime !== "all" &&
          report.available !== false &&
          ` · regime ${report.regime.replace(/_/g, " ")} kept ${report.after} of ${report.before}` +
            (report.unknown ? ` (${report.unknown} unclassifiable)` : "")}
      </p>
      {(frame.capped || frame.offset > 0) && (
        <p className="m-0 text-warn">
          Showing rows {frame.offset + 1}–{end} of {frame.rows_total.toLocaleString()} (cap{" "}
          {frame.row_cap.toLocaleString()} per page).{" "}
          {frame.offset > 0 && (
            <button
              type="button"
              className="underline"
              onClick={() => onOffset(Math.max(0, frame.offset - frame.row_cap))}
            >
              Previous
            </button>
          )}{" "}
          {frame.capped && (
            <button type="button" className="underline" onClick={() => onOffset(end)}>
              Next
            </button>
          )}
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- table

function formatValue(value: number | null | undefined): string {
  if (value === null || value === undefined) return "";
  return Math.abs(value) >= 1000 ? value.toFixed(0) : value.toFixed(3).replace(/\.?0+$/, "");
}

export function FrameTable({ frame }: { frame: FrameResponse }) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: "timestamp", dir: 1 });
  const [scrollTop, setScrollTop] = useState(0);
  const scroller = useRef<HTMLDivElement>(null);
  const specs = frame.columns.map((c) => c.spec);

  const order = useMemo(() => {
    const index = frame.timestamps.map((_, i) => i);
    if (sort.key === "timestamp") return sort.dir === 1 ? index : index.reverse();
    const column = frame.values[sort.key] ?? [];
    return index.sort((a, b) => {
      const va = column[a];
      const vb = column[b];
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      return (va - vb) * sort.dir;
    });
  }, [frame, sort]);

  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
  const last = Math.min(order.length, first + VIEWPORT_ROWS + OVERSCAN * 2);
  const visible = order.slice(first, last);

  const exportCsv = useCallback(() => {
    const header = [
      "timestamp",
      ...frame.columns.map((c) => (c.unit ? `${c.spec} (${c.unit})` : c.spec)),
    ];
    const rows = order.map((i) => [
      frame.timestamps[i],
      ...specs.map((s) => frame.values[s]?.[i] ?? null),
    ]);
    downloadText(toCsv(header, rows), `${frame.session_id}-${frame.regime_report.regime}.csv`);
  }, [frame, order, specs]);

  const toggle = (key: string) =>
    setSort((current) => ({ key, dir: current.key === key ? (current.dir === 1 ? -1 : 1) : 1 }));
  const arrow = (key: string) => (sort.key === key ? (sort.dir === 1 ? " ▲" : " ▼") : "");

  if (frame.rows_total === 0) {
    return <p className="m-0 text-sm text-fg-dim">No rows in this view.</p>;
  }
  return (
    <div>
      <div className="mb-1 flex justify-end">
        <button type="button" className={buttonClass} onClick={exportCsv}>
          Export CSV ({order.length.toLocaleString()} rows)
        </button>
      </div>
      <div
        ref={scroller}
        className="overflow-auto"
        style={{ maxHeight: ROW_HEIGHT * VIEWPORT_ROWS + 28 }}
        onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      >
        <table aria-label="Log table" className="w-full text-left text-xs font-num">
          <thead className="sticky top-0 bg-elev text-fg-faint">
            <tr>
              <th>
                <button type="button" onClick={() => toggle("timestamp")}>
                  time{arrow("timestamp")}
                </button>
              </th>
              {frame.columns.map((column) => (
                <th key={column.spec}>
                  <button type="button" onClick={() => toggle(column.spec)}>
                    {column.spec}
                    {column.unit ? ` (${column.unit})` : ""}
                    {arrow(column.spec)}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {first > 0 && (
              <tr aria-hidden="true" style={{ height: first * ROW_HEIGHT }}>
                <td colSpan={specs.length + 1} />
              </tr>
            )}
            {visible.map((i) => (
              <tr
                key={i}
                data-row
                style={{ height: ROW_HEIGHT }}
                className="border-t border-border-soft"
              >
                <td>{new Date(frame.timestamps[i]).toLocaleTimeString()}</td>
                {specs.map((spec) => (
                  <td key={spec}>{formatValue(frame.values[spec]?.[i])}</td>
                ))}
              </tr>
            ))}
            {last < order.length && (
              <tr aria-hidden="true" style={{ height: (order.length - last) * ROW_HEIGHT }}>
                <td colSpan={specs.length + 1} />
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- visual

const STRIP_W = 720;
const STRIP_H = 70;
const COLORS = ["#4fd6e0", "#ffb547", "#9be15d", "#ff6b8b", "#b18cff", "#f2f2f2"];

function extent(values: Array<number | null>): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (v === null || !Number.isFinite(v)) continue;
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  return Number.isFinite(lo) ? [lo, hi] : null;
}

export function FrameVisual({ frame }: { frame: FrameResponse }) {
  const times = useMemo(() => frame.timestamps.map((t) => Date.parse(t)), [frame]);
  const t0 = times[0] ?? 0;
  const t1 = times[times.length - 1] ?? t0 + 1;
  const tx = (t: number) => ((t - t0) / (t1 - t0 || 1)) * STRIP_W;
  const markers = [
    ...frame.tags.map((tag) => ({ t: Date.parse(tag.t), label: tag.label, kind: "tag" as const })),
    ...frame.revision_boundaries.map((b) => ({
      t: Date.parse(b.flashed_at),
      label: `flash ${b.revision}`,
      kind: "flash" as const,
    })),
  ].filter((m) => m.t >= t0 && m.t <= t1);

  if (frame.rows_total === 0) {
    return <p className="m-0 text-sm text-fg-dim">No rows in this view.</p>;
  }
  return (
    <div className="space-y-3">
      <div aria-label="Time-series strips" className="space-y-1">
        {frame.columns.map((column, index) => {
          const values = frame.values[column.spec] ?? [];
          const range = extent(values);
          const ty = (v: number) =>
            range ? STRIP_H - 4 - ((v - range[0]) / (range[1] - range[0] || 1)) * (STRIP_H - 8) : 0;
          const points = values
            .map((v, i) => (v === null ? null : `${tx(times[i]).toFixed(1)},${ty(v).toFixed(1)}`))
            .filter(Boolean)
            .join(" ");
          return (
            <figure key={column.spec} className="m-0">
              <figcaption className="text-[0.65rem] text-fg-dim">
                {column.spec}
                {column.unit ? ` (${column.unit})` : ""}
                {range && ` · ${formatValue(range[0])}–${formatValue(range[1])}`}
              </figcaption>
              <svg
                width="100%"
                viewBox={`0 0 ${STRIP_W} ${STRIP_H}`}
                preserveAspectRatio="none"
                className="h-16 bg-elev-2"
              >
                <polyline
                  points={points}
                  fill="none"
                  stroke={COLORS[index % COLORS.length]}
                  strokeWidth={1.2}
                />
                {markers.map((m, i) => (
                  <line
                    key={`${m.kind}-${i}`}
                    data-marker={m.kind}
                    x1={tx(m.t)}
                    x2={tx(m.t)}
                    y1={0}
                    y2={STRIP_H}
                    stroke={m.kind === "flash" ? "#ff6b8b" : "#94a3ad"}
                    strokeDasharray={m.kind === "flash" ? undefined : "3 3"}
                  >
                    <title>{m.label}</title>
                  </line>
                ))}
              </svg>
            </figure>
          );
        })}
      </div>
      <ScatterView frame={frame} />
    </div>
  );
}

function ScatterView({ frame }: { frame: FrameResponse }) {
  const specs = frame.columns.map((c) => c.spec);
  const [x, setX] = useState(specs[0] ?? "");
  const [y, setY] = useState(specs[1] ?? specs[0] ?? "");
  const [z, setZ] = useState(specs[2] ?? "");
  const [mode, setMode] = useState<"scatter" | "heat">("scatter");
  useEffect(() => {
    if (!specs.includes(x)) setX(specs[0] ?? "");
    if (!specs.includes(y)) setY(specs[1] ?? specs[0] ?? "");
    if (z && !specs.includes(z)) setZ("");
  }, [specs, x, y, z]);

  const xs = frame.values[x] ?? [];
  const ys = frame.values[y] ?? [];
  const zs = z ? (frame.values[z] ?? []) : [];
  const xr = extent(xs);
  const yr = extent(ys);
  const zr = extent(zs);
  const W = 360;
  const H = 240;
  const px = (v: number) => (xr ? ((v - xr[0]) / (xr[1] - xr[0] || 1)) * (W - 10) + 5 : 0);
  const py = (v: number) => (yr ? H - 5 - ((v - yr[0]) / (yr[1] - yr[0] || 1)) * (H - 10) : 0);
  const color = (v: number | null) =>
    v === null || !zr
      ? "#4fd6e0"
      : `hsl(${240 - ((v - zr[0]) / (zr[1] - zr[0] || 1)) * 240}, 80%, 60%)`;

  const heat = useMemo(() => {
    if (mode !== "heat" || !xr || !yr) return [];
    const bins = 12;
    const sums = new Map<string, { sum: number; n: number }>();
    xs.forEach((xv, i) => {
      const yv = ys[i];
      const zv = z ? zs[i] : 1;
      if (xv === null || yv === null || zv === null || zv === undefined) return;
      const bx = Math.min(bins - 1, Math.floor(((xv - xr[0]) / (xr[1] - xr[0] || 1)) * bins));
      const by = Math.min(bins - 1, Math.floor(((yv - yr[0]) / (yr[1] - yr[0] || 1)) * bins));
      const key = `${bx}:${by}`;
      const cell = sums.get(key) ?? { sum: 0, n: 0 };
      cell.sum += zv;
      cell.n += 1;
      sums.set(key, cell);
    });
    return [...sums.entries()].map(([key, cell]) => {
      const [bx, by] = key.split(":").map(Number);
      return { bx, by, value: z ? cell.sum / cell.n : cell.n, n: cell.n, bins };
    });
  }, [mode, xs, ys, zs, z, xr, yr]);
  const heatRange = extent(heat.map((c) => c.value));

  return (
    <figure className="m-0">
      <div className="mb-1 flex flex-wrap items-end gap-2 text-xs">
        {(
          [
            ["X", x, setX],
            ["Y", y, setY],
          ] as const
        ).map(([label, value, set]) => (
          <label key={label} className="flex flex-col gap-1">
            {label}
            <select
              className={selectClass}
              aria-label={`${label} axis`}
              value={value}
              onChange={(e) => set(e.target.value)}
            >
              {specs.map((s) => (
                <option key={s}>{s}</option>
              ))}
            </select>
          </label>
        ))}
        <label className="flex flex-col gap-1">
          {mode === "heat" ? "Cell value (mean)" : "Colour"}
          <select
            className={selectClass}
            aria-label="Colour channel"
            value={z}
            onChange={(e) => setZ(e.target.value)}
          >
            <option value="">{mode === "heat" ? "sample count" : "none"}</option>
            {specs.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className={buttonClass}
          onClick={() => setMode(mode === "scatter" ? "heat" : "scatter")}
        >
          {mode === "scatter" ? "Heat map" : "Scatter"}
        </button>
      </div>
      <svg
        aria-label={mode === "scatter" ? "Scatter" : "Heat map"}
        width={W}
        height={H}
        className="bg-elev-2"
      >
        {mode === "scatter" &&
          xs.map((xv, i) => {
            const yv = ys[i];
            if (xv === null || yv === null || yv === undefined) return null;
            return (
              <circle
                key={i}
                cx={px(xv)}
                cy={py(yv)}
                r={1.8}
                fill={color(z ? (zs[i] ?? null) : null)}
              />
            );
          })}
        {mode === "heat" &&
          heat.map((cell) => {
            const w = W / cell.bins;
            const h = H / cell.bins;
            const t = heatRange
              ? (cell.value - heatRange[0]) / (heatRange[1] - heatRange[0] || 1)
              : 0.5;
            return (
              <rect
                key={`${cell.bx}:${cell.by}`}
                x={cell.bx * w}
                y={H - (cell.by + 1) * h}
                width={w}
                height={h}
                fill={`hsl(${240 - t * 240}, 80%, 55%)`}
              >
                <title>{`${formatValue(cell.value)} (n ${cell.n})`}</title>
              </rect>
            );
          })}
      </svg>
      <figcaption className="text-[0.65rem] text-fg-faint">
        {y} against {x}
        {z
          ? `, ${mode === "heat" ? "cell mean of" : "coloured by"} ${z}`
          : mode === "heat"
            ? ", cell sample count"
            : ""}
        {xr && ` · x ${formatValue(xr[0])}–${formatValue(xr[1])}`}
        {yr && ` · y ${formatValue(yr[0])}–${formatValue(yr[1])}`}
      </figcaption>
    </figure>
  );
}

// ---------------------------------------------------------------- page

/** W04 Logs: the capture browser and, once a log is selected, that log in a
 *  table or visual presentation. Stands alone — the selected log comes from
 *  the group's persisted context, so `#wb/tuning-logs` opens it directly. */
export function LogsPage({ token, context }: { token: string; context: TuningContext }) {
  return (
    <div className="flex flex-col gap-4">
      <LogBrowser
        token={token}
        selectedId={context.selectedLogId}
        onSelect={(id) => context.setSelectedLogId(id)}
      />
      {context.selectedLogId && token && (
        <LogViewer token={token} sessionId={context.selectedLogId} />
      )}
    </div>
  );
}
