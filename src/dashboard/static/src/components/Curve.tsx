import { useEffect, useRef } from "react";

import { SEVERITY_TOKEN } from "../lib/behaviorOverlay";
import { prepareCanvas } from "../lib/canvas";
import { cssColor, useThemeVersion, withAlpha } from "../lib/chartTheme";
import type {
  BehaviorSeverity,
  DeviationConfig,
  LookupTableFit,
  PolynomialFit,
  RobustBaselineFit,
} from "../types";

/** Every 1D fit shape the backend can serve (`Fit1D`). */
export type CurveFit = PolynomialFit | LookupTableFit | RobustBaselineFit;

/** Live "you are here" overlay for a 1D surface. ``trail`` is the recent
 *  rolling history (server-side); points outside the domain or with a null
 *  axis/actual are skipped by the renderer. */
export interface CurveLive {
  x: number | null;
  actual: number | null;
  expected: number | null;
  severity: BehaviorSeverity;
  trail: { x: number | null; actual: number | null }[];
}

interface CurveProps {
  fit: CurveFit;
  domain: [number, number];
  /** Drives the warning/critical envelope shading (absolute bands only —
   *  relative bands need a per-x local std the client doesn't have). */
  deviation?: DeviationConfig | null;
  live?: CurveLive | null;
  width?: number;
  height?: number;
}

/** Render a 1D behavior model's fitted curve, its deviation envelope, and
 *  the live operating point + recent trajectory.
 *
 *  The fit is sampled closed-form (polynomial) or linear-interp (lookup
 *  table), so the whole thing — curve, ± band fills, trail polyline, marker
 *  — is a handful of cheap canvas paths redrawn on each 2 s live poll. The
 *  y-range is fitted over the curve *and* the band *and* the live values so
 *  an excursion past "normal" is visible rather than clipped off-canvas. */
export function Curve({ fit, domain, deviation, live, width = 240, height = 90 }: CurveProps) {
  const ref = useRef<HTMLCanvasElement>(null);
  const themeVersion = useThemeVersion();

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const ctx = prepareCanvas(canvas, width, height);
    if (!ctx) return;

    const [xLo, xHi] = domain;
    const xSpan = xHi - xLo || 1;
    const samples = 96;
    const xs: number[] = [];
    const ys: number[] = [];
    for (let i = 0; i < samples; i++) {
      const x = xLo + (xSpan * i) / (samples - 1);
      xs.push(x);
      ys.push(evaluate(fit, x));
    }

    // Absolute-mode bands shade the curve ± the configured deltas. Relative
    // mode would need the local std per x (only the daemon has it), so we
    // skip the fill there and still draw the marker/trail.
    const absolute = deviation?.kind === "absolute";
    const warnBand = absolute ? (deviation?.warning_band ?? null) : null;
    const critBand = absolute ? (deviation?.critical_band ?? null) : null;

    // Collect every value that must stay on-canvas.
    const inDomain = (x: number) => x >= xLo && x <= xHi;
    const yCandidates = [...ys];
    const outer = critBand ?? warnBand;
    if (outer !== null) {
      for (const y of ys) yCandidates.push(y + outer, y - outer);
    }
    const trailPts = (live?.trail ?? []).filter(
      (p): p is { x: number; actual: number } => p.x !== null && p.actual !== null && inDomain(p.x),
    );
    for (const p of trailPts) yCandidates.push(p.actual);
    if (live && live.x !== null && live.actual !== null && inDomain(live.x)) {
      yCandidates.push(live.actual);
    }
    if (live?.expected !== null && live?.expected !== undefined) {
      yCandidates.push(live.expected);
    }
    let yLo = Math.min(...yCandidates);
    let yHi = Math.max(...yCandidates);
    const margin = (yHi - yLo || 1) * 0.06;
    yLo -= margin;
    yHi += margin;
    const ySpan = yHi - yLo || 1;
    const pad = 4;
    const usableH = height - pad * 2;

    const xToPx = (x: number) => ((x - xLo) / xSpan) * width;
    const yToPy = (v: number) => pad + (1 - (v - yLo) / ySpan) * usableH;

    const band = (delta: number, fill: string) => {
      ctx.fillStyle = fill;
      ctx.beginPath();
      xs.forEach((x, i) => {
        const px = xToPx(x);
        const py = yToPy(ys[i] + delta);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      for (let i = xs.length - 1; i >= 0; i--) {
        ctx.lineTo(xToPx(xs[i]), yToPy(ys[i] - delta));
      }
      ctx.closePath();
      ctx.fill();
    };
    // Critical first (wider), warning on top — nested envelopes.
    if (critBand !== null) band(critBand, withAlpha(cssColor("bad"), 0.1));
    if (warnBand !== null) band(warnBand, withAlpha(cssColor("warn"), 0.12));

    // Fitted curve.
    ctx.strokeStyle = cssColor("accent");
    ctx.lineWidth = 1.75;
    ctx.beginPath();
    xs.forEach((x, i) => {
      const px = xToPx(x);
      const py = yToPy(ys[i]);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();

    // Recent trajectory — faint polyline through the trail.
    if (trailPts.length >= 2) {
      ctx.strokeStyle = withAlpha(cssColor("fg"), 0.28);
      ctx.lineWidth = 1;
      ctx.beginPath();
      trailPts.forEach((p, i) => {
        const px = xToPx(p.x);
        const py = yToPy(p.actual);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }

    // Current operating point.
    if (live && live.x !== null && live.actual !== null && inDomain(live.x)) {
      const px = xToPx(live.x);
      const py = yToPy(live.actual);
      // Deviation tick: marker → expected.
      if (live.expected !== null && live.expected !== undefined) {
        ctx.strokeStyle = cssColor(SEVERITY_TOKEN[live.severity]);
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(px, py);
        ctx.lineTo(px, yToPy(live.expected));
        ctx.stroke();
      }
      ctx.fillStyle = cssColor(SEVERITY_TOKEN[live.severity]);
      ctx.beginPath();
      ctx.arc(px, py, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = cssColor("bg");
      ctx.lineWidth = 1;
      ctx.stroke();
    } else if (live && live.x !== null) {
      // Off-map: clamp a hollow chevron to the near edge so the operator
      // sees "we're driving past the fitted envelope," not a blank chart.
      const atHi = live.x > xHi;
      const px = atHi ? width - 5 : 5;
      ctx.strokeStyle = cssColor("fg-dim");
      ctx.lineWidth = 1.25;
      ctx.beginPath();
      ctx.moveTo(atHi ? px - 5 : px + 5, height / 2 - 5);
      ctx.lineTo(px, height / 2);
      ctx.lineTo(atHi ? px - 5 : px + 5, height / 2 + 5);
      ctx.stroke();
    }

    // Axis labels (min/max y only — no clutter).
    ctx.fillStyle = cssColor("fg-dim");
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText(yHi.toFixed(1), 2, 10);
    ctx.fillText(yLo.toFixed(1), 2, height - 2);
  }, [fit, domain, deviation, live, width, height, themeVersion]);

  return <canvas ref={ref} width={width} height={height} />;
}

function evaluate(fit: CurveFit, x: number): number {
  if (fit.kind === "poly") {
    let y = 0;
    let xPow = 1;
    for (const c of fit.coefficients) {
      y += c * xPow;
      xPow *= x;
    }
    return y;
  }
  // robust_baseline claims no x relationship, so the expectation is flat.
  // Falling through to the lookup-table branch would read knots it does not
  // have and take the whole dashboard down with it.
  if (fit.kind === "robust_baseline") return fit.median;
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
