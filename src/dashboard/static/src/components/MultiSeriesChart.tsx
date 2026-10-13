import { useEffect, useMemo, useRef, useState } from "react";

import { fitWidth, useContainerWidth } from "../hooks/useContainerWidth";
import { prepareCanvas } from "../lib/canvas";
import { cssColor, useThemeVersion, withAlpha } from "../lib/chartTheme";
import { buttonClass, cn } from "../styles";
import { downloadText } from "../util/browser";

export interface ChartSeries {
  key: string;
  color: string;
  values: number[];
}

interface MultiSeriesChartProps {
  series: ChartSeries[];
  /** Upper bound on the drawn width in CSS px. The chart fills its
   *  container (P2.5), so omit this to span the panel at any width. */
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
  /** Controlled readout position (sample index), so several charts stacked
   *  one series per row read the same instant together (P4.2). Leave both
   *  unset for a chart that keeps its own. */
  cursor?: number | null;
  onCursorChange?: (index: number | null) => void;
}

/** Fallback drawing width before the container has been measured (and in
 *  jsdom, which has no ResizeObserver). */
const DEFAULT_WIDTH = 640;

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

/** Multi-channel trend chart on a single canvas (Task 6.10; ergonomics —
 *  shared/dual Y axis, hover cursor, time labels, PNG/CSV export — Task
 *  5.5). One canvas, no chart-lib dep, consistent with the Sparkline
 *  approach; redraws cheaply on each telemetry tick.
 *
 *  The readout follows a pointer, not hover (P4.2): a mouse reads as it
 *  moves and clears on leaving; a finger reads where it touches or drags
 *  sideways (vertical drags still scroll the page), and the readout stays
 *  until the next touch or its clear button, since a phone has no "leave". */
export function MultiSeriesChart({
  series,
  width,
  height = 240,
  timestamps,
  yAxisMode = "independent",
  showExport = true,
  cursor,
  onCursorChange,
}: MultiSeriesChartProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [frame, measured] = useContainerWidth<HTMLDivElement>();
  const drawWidth = fitWidth(measured, width, DEFAULT_WIDTH);
  const [ownCursor, setOwnCursor] = useState<number | null>(null);
  const controlled = onCursorChange !== undefined;
  const hoverIndex = controlled ? (cursor ?? null) : ownCursor;
  const setHoverIndex = controlled ? onCursorChange : setOwnCursor;
  // A touch readout stays put after the finger lifts; a mouse one does not.
  const [touchHeld, setTouchHeld] = useState(false);
  const themeVersion = useThemeVersion();

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
    const width = drawWidth;
    const ctx = prepareCanvas(canvas, width, height);
    if (!ctx) return;
    const fg = cssColor("fg");

    // Horizontal gridlines (quartiles) — orientation only; the legend has
    // the numbers.
    ctx.strokeStyle = withAlpha(fg, 0.06);
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
      ctx.strokeStyle = withAlpha(fg, 0.35);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, height);
      ctx.stroke();
    }

    // X-axis time labels (only when timestamps were supplied — the live
    // ChartsPanel's ring buffers don't carry them).
    if (timestamps && timestamps.length > 1) {
      ctx.fillStyle = withAlpha(fg, 0.45);
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
  }, [
    series,
    drawWidth,
    height,
    timestamps,
    yAxisMode,
    sharedExtent,
    hoverIndex,
    sampleCount,
    themeVersion,
  ]);

  const pointAt = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (sampleCount < 2) return;
    const rect = e.currentTarget.getBoundingClientRect();
    if (!(rect.width > 0)) return;
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    setHoverIndex(Math.round(frac * (sampleCount - 1)));
  };
  const handleDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    setTouchHeld(e.pointerType !== "mouse");
    pointAt(e);
  };
  const handleLeave = (e: React.PointerEvent<HTMLCanvasElement>) => {
    // A lifted finger "leaves" too; keep its readout so it can be read.
    if (e.pointerType === "mouse") setHoverIndex(null);
  };
  const clearReadout = () => {
    setTouchHeld(false);
    setHoverIndex(null);
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
    downloadText(rows.join("\n"), "chart.csv", "text/csv");
  };

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div ref={frame} className="relative min-w-0">
        <canvas
          ref={ref}
          className="block max-w-full touch-pan-y"
          onPointerDown={handleDown}
          onPointerMove={pointAt}
          onPointerLeave={handleLeave}
        />
        {hoverIndex !== null && (
          <div
            className={cn(
              "absolute top-1 right-1 rounded bg-elev-2/90 px-2 py-1 text-[0.68rem] tabular-nums text-fg-dim shadow",
              "max-[600px]:text-[0.8rem]",
              touchHeld ? "pointer-events-auto" : "pointer-events-none",
            )}
            data-testid="chart-readout"
          >
            {touchHeld && (
              <button
                type="button"
                className="float-right -mr-1 -mt-0.5 ml-2 min-h-6 min-w-6 text-fg-dim"
                aria-label="clear readout"
                onClick={clearReadout}
              >
                ✕
              </button>
            )}
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
