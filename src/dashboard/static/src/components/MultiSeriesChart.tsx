import { useEffect, useMemo, useRef, useState } from "react";

import { buttonClass, cn } from "../styles";

export interface ChartSeries {
  key: string;
  color: string;
  values: number[];
}

interface MultiSeriesChartProps {
  series: ChartSeries[];
  width?: number;
  height?: number;
  /** ISO-8601 timestamp per sample index, same length as each series'
   *  `values` (Task 5.5) — enables X-axis time labels, a timestamped hover
   *  readout, and a timestamp column in the CSV export. Omitted in the live
   *  ChartsPanel (its ring buffers don't carry timestamps); required for
   *  session-mode charting, where the backend read always has them. */
  timestamps?: string[];
  /** `"independent"` (default, Task 6.10's original behavior): each series
   *  normalizes to its own min/max, so wildly different scales (RPM vs.
   *  throttle %) both fill the plot height. `"shared"` (Task 5.5): every
   *  series normalizes against one global min/max, so relative magnitude
   *  between series is actually comparable — independent normalization
   *  otherwise hides that e.g. two channels moved by the same amount. */
  yAxisMode?: "independent" | "shared";
  /** Show the export toolbar (PNG/CSV of the plotted window). Defaults to
   *  true; the caller can hide it if it wants its own export affordance. */
  showExport?: boolean;
}

function seriesExtent(values: number[]): [number, number] | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  return Number.isFinite(lo) && Number.isFinite(hi) ? [lo, hi] : null;
}

function formatTimeLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function downloadBlob(content: string, filename: string, mime: string): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** Multi-channel trend chart on a single canvas (Task 6.10; ergonomics —
 *  shared/dual Y axis, hover cursor, time labels, PNG/CSV export — Task
 *  5.5). One canvas, no chart-lib dep, consistent with the Sparkline
 *  approach; redraws cheaply on each telemetry tick. */
export function MultiSeriesChart({
  series,
  width = 640,
  height = 240,
  timestamps,
  yAxisMode = "independent",
  showExport = true,
}: MultiSeriesChartProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const sampleCount = useMemo(() => Math.max(0, ...series.map((s) => s.values.length)), [series]);

  const sharedExtent = useMemo(() => {
    if (yAxisMode !== "shared") return null;
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of series) {
      const extent = seriesExtent(s.values);
      if (!extent) continue;
      if (extent[0] < lo) lo = extent[0];
      if (extent[1] > hi) hi = extent[1];
    }
    return Number.isFinite(lo) && Number.isFinite(hi) ? ([lo, hi] as [number, number]) : null;
  }, [series, yAxisMode]);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = width * dpr;
    canvas.height = height * dpr;
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);

    // Horizontal gridlines (quartiles) — orientation only; the legend has
    // the numbers.
    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) {
      const y = (height * i) / 4;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(width, y);
      ctx.stroke();
    }

    const pad = 4;
    for (const s of series) {
      const vals = s.values;
      if (vals.length < 2) continue;
      const extent = yAxisMode === "shared" ? sharedExtent : seriesExtent(vals);
      if (!extent) continue;
      const [lo, hi] = extent;
      const span = hi - lo || 1;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      vals.forEach((v, i) => {
        const x = (width * i) / (vals.length - 1);
        const norm = (v - lo) / span; // 0..1
        const y = pad + (1 - norm) * (height - pad * 2);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }

    // Hover crosshair.
    if (hoverIndex !== null && sampleCount > 1) {
      const x = (width * hoverIndex) / (sampleCount - 1);
      ctx.strokeStyle = "rgba(255,255,255,0.35)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();
    }

    // X-axis time labels (only when timestamps were supplied — the live
    // ChartsPanel's ring buffers don't carry them).
    if (timestamps && timestamps.length > 1) {
      ctx.fillStyle = "rgba(255,255,255,0.45)";
      ctx.font = "10px system-ui, sans-serif";
      const ticks = 4;
      for (let i = 0; i <= ticks; i++) {
        const idx = Math.round((i / ticks) * (timestamps.length - 1));
        const x = (width * idx) / (timestamps.length - 1);
        const label = formatTimeLabel(timestamps[idx]);
        const align = i === 0 ? "left" : i === ticks ? "right" : "center";
        ctx.textAlign = align;
        ctx.fillText(label, x, height - 4);
      }
    }
  }, [series, width, height, timestamps, yAxisMode, sharedExtent, hoverIndex, sampleCount]);

  const handleMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    if (sampleCount < 2) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    setHoverIndex(Math.round(frac * (sampleCount - 1)));
  };

  const exportPng = () => {
    const canvas = ref.current;
    if (!canvas) return;
    const a = document.createElement("a");
    a.href = canvas.toDataURL("image/png");
    a.download = "chart.png";
    a.click();
  };

  const exportCsv = () => {
    const header = ["index", ...(timestamps ? ["timestamp"] : []), ...series.map((s) => s.key)];
    const rows: string[] = [header.join(",")];
    for (let i = 0; i < sampleCount; i++) {
      const cells = [
        String(i),
        ...(timestamps ? [timestamps[i] ?? ""] : []),
        ...series.map((s) => (s.values[i] ?? "").toString()),
      ];
      rows.push(cells.join(","));
    }
    downloadBlob(rows.join("\n"), "chart.csv", "text/csv");
  };

  return (
    <div className="flex flex-col gap-1">
      <div className="relative">
        <canvas
          ref={ref}
          className="block w-full"
          onMouseMove={handleMove}
          onMouseLeave={() => setHoverIndex(null)}
        />
        {hoverIndex !== null && (
          <div className="pointer-events-none absolute top-1 right-1 rounded bg-elev-2/90 px-2 py-1 text-[0.68rem] tabular-nums text-fg-dim shadow">
            {timestamps?.[hoverIndex] && (
              <div className="mb-0.5 text-fg">{formatTimeLabel(timestamps[hoverIndex])}</div>
            )}
            {series.map((s) => (
              <div key={s.key} className="flex items-center gap-1.5">
                <span
                  className="inline-block h-2 w-2 rounded-full"
                  style={{ background: s.color }}
                />
                <span>{s.key}</span>
                <b className="text-fg">{s.values[hoverIndex] ?? "—"}</b>
              </div>
            ))}
          </div>
        )}
      </div>
      {showExport && series.length > 0 && (
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className={cn(buttonClass, "px-2 py-0.5 text-[0.7rem]")}
            onClick={exportPng}
          >
            export PNG
          </button>
          <button
            type="button"
            className={cn(buttonClass, "px-2 py-0.5 text-[0.7rem]")}
            onClick={exportCsv}
          >
            export CSV
          </button>
        </div>
      )}
    </div>
  );
}
