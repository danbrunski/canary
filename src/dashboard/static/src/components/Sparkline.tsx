import { useEffect, useRef } from "react";

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  stroke?: string;
  /** When set, the y-axis is fixed to [min, max] instead of auto-scaled. */
  min?: number;
  max?: number;
}

/** Canvas sparkline. The dashboard would render hundreds of these if it
 *  used SVG — canvas is one node per chart, no GC churn, fast to redraw on
 *  every WebSocket tick. ~50 LOC; not worth pulling a chart library for. */
export function Sparkline({
  values,
  width = 120,
  height = 28,
  stroke = "#7CFFCB",
  min,
  max,
}: SparklineProps) {
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
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, width, height);
    if (values.length < 2) return;

    const lo = min ?? Math.min(...values);
    const hi = max ?? Math.max(...values);
    const span = hi - lo || 1;
    const stepX = width / (values.length - 1);
    const pad = 2;
    const usableH = height - pad * 2;

    ctx.lineWidth = 1.5;
    ctx.strokeStyle = stroke;
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = i * stepX;
      const y = pad + (1 - (v - lo) / span) * usableH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }, [values, width, height, stroke, min, max]);

  return <canvas ref={ref} width={width} height={height} />;
}
