import { useEffect, useRef } from "react";

import { fitWidth, useContainerWidth } from "../hooks/useContainerWidth";
import { prepareCanvas } from "../lib/canvas";
import { cssColor, useThemeVersion, withAlpha, type ColorToken } from "../lib/chartTheme";

interface SparklineProps {
  values: number[];
  width?: number;
  height?: number;
  /** Line colour, as a theme token so it follows the night theme. */
  stroke?: ColorToken;
  /** When set, the y-axis is fixed to [min, max] instead of auto-scaled. */
  min?: number;
  max?: number;
  /** Draw a dim dashed horizontal reference line at this y-value (same units
   *  as `values`). Use with fixed min/max for a meaningful "threshold" mark. */
  baseline?: number;
}

/** Canvas sparkline. The dashboard would render hundreds of these if it
 *  used SVG — canvas is one node per chart, no GC churn, fast to redraw on
 *  every WebSocket tick. ~50 LOC; not worth pulling a chart library for. */
export function Sparkline({
  values,
  width: requestedWidth = 120,
  height = 28,
  stroke = "accent",
  min,
  max,
  baseline,
}: SparklineProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  // Fills its container up to the requested width (P2.5): a phone column
  // narrower than the request shrinks the drawing instead of clipping it.
  const [frame, measured] = useContainerWidth<HTMLDivElement>();
  const width = fitWidth(measured, requestedWidth, requestedWidth);
  const themeVersion = useThemeVersion();

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = prepareCanvas(canvas, width, height);
    if (!ctx) return;
    if (values.length < 2) return;

    const lo = min ?? Math.min(...values);
    const hi = max ?? Math.max(...values);
    const span = hi - lo || 1;
    const stepX = width / (values.length - 1);
    const pad = 2;
    const usableH = height - pad * 2;

    if (baseline !== undefined && baseline >= lo && baseline <= hi) {
      const by = pad + (1 - (baseline - lo) / span) * usableH;
      ctx.save();
      ctx.lineWidth = 1;
      ctx.strokeStyle = withAlpha(cssColor("fg-dim"), 0.45);
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(0, by);
      ctx.lineTo(width, by);
      ctx.stroke();
      ctx.restore();
    }

    ctx.lineWidth = 1.5;
    ctx.strokeStyle = cssColor(stroke);
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = i * stepX;
      const y = pad + (1 - (v - lo) / span) * usableH;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }, [values, width, height, stroke, min, max, baseline, themeVersion]);

  return (
    <div ref={frame} className="min-w-0 max-w-full" style={{ width: requestedWidth }}>
      <canvas ref={ref} className="block" width={width} height={height} />
    </div>
  );
}
