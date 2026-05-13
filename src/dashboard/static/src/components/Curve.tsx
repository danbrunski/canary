import { useEffect, useRef } from "react";

import type { LookupTableFit, PolynomialFit } from "../types";

interface CurveProps {
  fit: PolynomialFit | LookupTableFit;
  domain: [number, number];
  width?: number;
  height?: number;
}

/** Render a 1D behavior model's fitted curve to canvas.
 *
 *  Samples the fit at ``samples`` evenly-spaced x values across the fit's
 *  declared domain and draws the polyline. Stays cheap because the
 *  evaluation is closed-form for polynomials and linear-interp for the
 *  lookup table; no curve solver involved. */
export function Curve({ fit, domain, width = 240, height = 90 }: CurveProps) {
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

    const [xLo, xHi] = domain;
    const samples = 96;
    const xs: number[] = [];
    const ys: number[] = [];
    for (let i = 0; i < samples; i++) {
      const x = xLo + ((xHi - xLo) * i) / (samples - 1);
      xs.push(x);
      ys.push(evaluate(fit, x));
    }

    const yLo = Math.min(...ys);
    const yHi = Math.max(...ys);
    const ySpan = yHi - yLo || 1;
    const pad = 4;
    const usableH = height - pad * 2;

    ctx.strokeStyle = "#FFB84A";
    ctx.lineWidth = 1.75;
    ctx.beginPath();
    xs.forEach((x, i) => {
      const px = ((x - xLo) / (xHi - xLo)) * width;
      const py = pad + (1 - (ys[i] - yLo) / ySpan) * usableH;
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();

    // Axis labels (just min/max y values, no clutter)
    ctx.fillStyle = "#94a3ad";
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText(yHi.toFixed(1), 2, 10);
    ctx.fillText(yLo.toFixed(1), 2, height - 2);
  }, [fit, domain, width, height]);

  return <canvas ref={ref} width={width} height={height} />;
}

function evaluate(fit: PolynomialFit | LookupTableFit, x: number): number {
  if (fit.kind === "poly") {
    let y = 0;
    let xPow = 1;
    for (const c of fit.coefficients) {
      y += c * xPow;
      xPow *= x;
    }
    return y;
  }
  // lookup_table: piecewise-linear interpolation
  const { x_knots, y_knots } = fit;
  if (x <= x_knots[0]) return y_knots[0];
  if (x >= x_knots[x_knots.length - 1]) return y_knots[y_knots.length - 1];
  for (let i = 0; i < x_knots.length - 1; i++) {
    if (x >= x_knots[i] && x <= x_knots[i + 1]) {
      const t = (x - x_knots[i]) / (x_knots[i + 1] - x_knots[i]);
      return y_knots[i] + t * (y_knots[i + 1] - y_knots[i]);
    }
  }
  return y_knots[y_knots.length - 1];
}
