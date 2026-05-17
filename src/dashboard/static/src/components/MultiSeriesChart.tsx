import { useEffect, useRef } from "react";

export interface ChartSeries {
  key: string;
  color: string;
  values: number[];
}

interface MultiSeriesChartProps {
  series: ChartSeries[];
  width?: number;
  height?: number;
}

/** Multi-channel trend chart on a single canvas (Task 6.10). Each series is
 *  normalized to its *own* min/max so channels on wildly different scales
 *  (RPM vs. throttle %) share one plot legibly — the legend carries the
 *  real units/values. One canvas, no chart-lib dep, consistent with the
 *  Sparkline approach; redraws cheaply on each telemetry tick. */
export function MultiSeriesChart({
  series,
  width = 640,
  height = 240,
}: MultiSeriesChartProps) {
  const ref = useRef<HTMLCanvasElement>(null);

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
      let lo = Infinity;
      let hi = -Infinity;
      for (const v of vals) {
        if (!Number.isFinite(v)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) continue;
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
  }, [series, width, height]);

  return <canvas ref={ref} className="block w-full" />;
}
