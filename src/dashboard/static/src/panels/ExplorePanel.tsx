import { useState } from "react";

import { getBinnedAnalysis } from "../api/http";
import { Heatmap } from "../components/Heatmap";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { buttonClass, cn, inputClass, placeholderClass, selectClass } from "../styles";
import type { BinnedAggregation, BinnedAnalysisResponse } from "../types";

const AGGREGATIONS: BinnedAggregation[] = ["mean", "median", "p95", "count"];

/** Ad-hoc map view (Task 5.2): pick a value/x[/y] channel and see the
 *  binned aggregation over the DuckDB parquet layer — "trims by rpm×load"
 *  without a refit. Read-only, backed by `GET /analysis/binned` (Task 5.1).
 *  Reuses `Heatmap.tsx` for 2D results, the same component
 *  `BehaviorModelsPanel` renders fitted surfaces with. */
export function ExplorePanel() {
  const [value, setValue] = useState("obd:SHORT_FUEL_TRIM_1");
  const [x, setX] = useState("obd:RPM");
  const [y, setY] = useState("obd:ENGINE_LOAD");
  const [xBins, setXBins] = useState("10");
  const [yBins, setYBins] = useState("10");
  const [agg, setAgg] = useState<BinnedAggregation>("mean");
  const [session, setSession] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [result, setResult] = useState<BinnedAnalysisResponse | null>(null);
  const { notice, setNotice, busy, run } = useAsyncAction();

  const submit = () =>
    run(
      "explore",
      async () => {
        const next = await getBinnedAnalysis({
          value,
          x,
          y: y.trim() ? y.trim() : undefined,
          xBins: Number(xBins) || undefined,
          yBins: y.trim() ? Number(yBins) || undefined : undefined,
          agg,
          session: session.trim() || undefined,
          since: since.trim() || undefined,
          until: until.trim() || undefined,
        });
        setResult(next);
        if (!next.available) {
          setNotice({ kind: "error", text: next.reason });
        }
      },
      { okText: null },
    );

  return (
    <Panel title="Explore" count={result?.available ? `n=${result.sample_count}` : undefined}>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-2">
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          value channel
          <input
            className={inputClass}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder="obd:SHORT_FUEL_TRIM_1"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          x channel
          <input
            className={inputClass}
            value={x}
            onChange={(e) => setX(e.target.value)}
            placeholder="obd:RPM"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          y channel (optional)
          <input
            className={inputClass}
            value={y}
            onChange={(e) => setY(e.target.value)}
            placeholder="obd:ENGINE_LOAD"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          x bins
          <input
            className={inputClass}
            type="number"
            min={2}
            max={64}
            value={xBins}
            onChange={(e) => setXBins(e.target.value)}
          />
        </label>
        {y.trim() && (
          <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
            y bins
            <input
              className={inputClass}
              type="number"
              min={2}
              max={64}
              value={yBins}
              onChange={(e) => setYBins(e.target.value)}
            />
          </label>
        )}
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          aggregation
          <select
            className={selectClass}
            value={agg}
            onChange={(e) => setAgg(e.target.value as BinnedAggregation)}
          >
            {AGGREGATIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          session (optional)
          <input
            className={inputClass}
            value={session}
            onChange={(e) => setSession(e.target.value)}
            placeholder="2026-07-19T000000"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          since (optional)
          <input
            className={inputClass}
            value={since}
            onChange={(e) => setSince(e.target.value)}
            placeholder="2026-07-01"
          />
        </label>
        <label className="flex flex-col gap-1 text-[0.72rem] text-fg-dim">
          until (optional)
          <input
            className={inputClass}
            value={until}
            onChange={(e) => setUntil(e.target.value)}
            placeholder="2026-07-19"
          />
        </label>
      </div>

      <button
        type="button"
        className={cn(buttonClass, "mt-3 px-3 py-1.5")}
        disabled={Boolean(busy) || !value.trim() || !x.trim()}
        onClick={submit}
      >
        {busy ? "running…" : "run"}
      </button>

      {notice && <Notice notice={notice} className="mt-2" />}

      <div className="mt-3">
        {result === null && (
          <p className={placeholderClass}>pick channels and run to see the binned result.</p>
        )}
        {result && !result.available && <p className={placeholderClass}>{result.reason}</p>}
        {result && result.available && result.kind === "2d" && (
          <Heatmap
            cells={result.cells}
            rows={result.rows}
            cols={result.cols}
            width={420}
            height={260}
            caption={`${result.value_channel} (${result.agg}) by ${result.x_channel} × ${result.y_channel}`}
          />
        )}
        {result && result.available && result.kind === "1d" && (
          <div className="flex flex-col gap-1">
            <p className="text-[0.72rem] text-fg-dim">
              {result.value_channel} ({result.agg}) by {result.x_channel}
            </p>
            <div className="flex items-end gap-1" style={{ height: 140 }}>
              {result.cells.map((cell, i) => {
                const max = Math.max(1, ...result.cells.map((c) => Math.abs(c.mean)));
                const h = Math.max(2, (Math.abs(cell.mean) / max) * 120);
                return (
                  <div
                    key={i}
                    className="flex-1 rounded-t bg-accent/70"
                    style={{ height: h }}
                    title={`[${result.x_edges[i].toFixed(1)}, ${result.x_edges[i + 1].toFixed(1)}] mean=${cell.mean.toFixed(3)} n=${cell.count}`}
                  />
                );
              })}
            </div>
          </div>
        )}
      </div>
    </Panel>
  );
}
