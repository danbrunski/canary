// Behavioural tests for the canvas charts (Sparkline, Curve, Heatmap). jsdom
// has no canvas, so each test records what the chart asks of a 2D context:
// the calls it makes and the colours it sets.
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DAY_TOKENS } from "../lib/chartTheme";
import { Curve } from "./Curve";
import { Heatmap } from "./Heatmap";
import { Sparkline } from "./Sparkline";

interface Recorder {
  calls: Array<[string, unknown[]]>;
  styles: string[];
  count: (name: string) => number;
}

function recordingContext(): Recorder & Record<string, unknown> {
  const calls: Array<[string, unknown[]]> = [];
  const styles: string[] = [];
  const target: Record<string, unknown> = {};
  const ctx = new Proxy(target, {
    get(obj, prop: string) {
      if (prop === "calls") return calls;
      if (prop === "styles") return styles;
      if (prop === "count") return (name: string) => calls.filter(([n]) => n === name).length;
      if (prop in obj) return obj[prop];
      return (...args: unknown[]) => {
        calls.push([prop, args]);
      };
    },
    set(obj, prop: string, value: unknown) {
      if (prop === "fillStyle" || prop === "strokeStyle") styles.push(`${prop}=${String(value)}`);
      obj[prop] = value;
      return true;
    },
  });
  return ctx as Recorder & Record<string, unknown>;
}

let ctx: Recorder & Record<string, unknown>;
let original: PropertyDescriptor | undefined;

beforeEach(() => {
  ctx = recordingContext();
  original = Object.getOwnPropertyDescriptor(HTMLCanvasElement.prototype, "getContext");
  Object.defineProperty(HTMLCanvasElement.prototype, "getContext", {
    configurable: true,
    value: () => ctx,
  });
  vi.stubGlobal("devicePixelRatio", 2);
});

afterEach(() => {
  if (original) Object.defineProperty(HTMLCanvasElement.prototype, "getContext", original);
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
  document.documentElement.style.removeProperty("--color-accent");
});

describe("Sparkline", () => {
  it("sizes the backing store for the device pixel ratio", () => {
    const { container } = render(<Sparkline values={[1, 2, 3]} width={100} height={20} />);
    const canvas = container.querySelector("canvas")!;
    expect(canvas.width).toBe(200);
    expect(canvas.height).toBe(40);
    expect(canvas.style.width).toBe("100px");
  });

  it("draws one path vertex per value", () => {
    render(<Sparkline values={[1, 5, 3, 4]} />);
    expect(ctx.count("moveTo")).toBe(1);
    expect(ctx.count("lineTo")).toBe(3);
    expect(ctx.count("stroke")).toBe(1);
  });

  it("draws nothing for fewer than two values", () => {
    render(<Sparkline values={[1]} />);
    expect(ctx.count("stroke")).toBe(0);
  });

  it("adds a dashed baseline only when it is inside the range", () => {
    render(<Sparkline values={[0, 10]} min={0} max={10} baseline={5} />);
    expect(ctx.count("setLineDash")).toBe(1);
    expect(ctx.count("stroke")).toBe(2);
  });
});

describe("chart colours follow the theme (R08.7)", () => {
  it("draws with the theme tokens and repaints when the theme flips", async () => {
    render(<Sparkline values={[1, 2]} />);
    expect(ctx.styles).toContain(`strokeStyle=${DAY_TOKENS.accent}`);
    await act(async () => {
      document.documentElement.style.setProperty("--color-accent", "#d99a3c");
      document.documentElement.dataset.theme = "night";
      await Promise.resolve();
    });
    expect(ctx.styles.at(-1)).toBe("strokeStyle=#d99a3c");
  });

  it("colours a live marker by its severity token", () => {
    render(
      <Heatmap
        cells={[{ mean: 1, std: 0, count: 2 }]}
        rows={1}
        cols={1}
        xEdges={[0, 1]}
        yEdges={[0, 1]}
        live={{ x: 0.5, y: 0.5, severity: "warning" }}
      />,
    );
    expect(ctx.styles).toContain(`strokeStyle=${DAY_TOKENS.warn}`);
  });
});

describe("Heatmap", () => {
  const cell = (mean: number, count = 3) => ({ mean, std: 0, count });

  it("says when no cell is populated", () => {
    render(<Heatmap cells={[cell(0, 0), cell(0, 0)]} rows={1} cols={2} />);
    expect(ctx.calls.find(([n]) => n === "fillText")?.[1][0]).toBe("no cells populated yet");
  });

  it("fills one rectangle per cell and outlines the live cell", () => {
    render(
      <Heatmap
        cells={[cell(1), cell(2), cell(3, 0), cell(4)]}
        rows={2}
        cols={2}
        xEdges={[0, 1, 2]}
        yEdges={[0, 1, 2]}
        live={{ x: 0.5, y: 1.5, severity: "critical" }}
      />,
    );
    expect(ctx.count("fillRect")).toBe(4);
    expect(ctx.count("strokeRect")).toBe(1);
  });
});

describe("Curve", () => {
  const poly = { kind: "poly", coefficients: [1, 0.5] } as never;

  it("samples the fit across the domain", () => {
    render(<Curve fit={poly} domain={[0, 10]} />);
    // 96 samples: one moveTo + 95 lineTo for the curve.
    expect(ctx.count("lineTo")).toBe(95);
  });

  it("shades nested absolute envelopes", () => {
    render(
      <Curve
        fit={poly}
        domain={[0, 10]}
        deviation={{ kind: "absolute", warning_band: 1, critical_band: 2, reference_std: null }}
      />,
    );
    expect(ctx.count("fill")).toBe(2);
  });

  it("marks an off-map operating point at the near edge", () => {
    render(
      <Curve
        fit={poly}
        domain={[0, 10]}
        live={{ x: 20, actual: 3, expected: 2, severity: "ok", trail: [] }}
      />,
    );
    expect(ctx.count("arc")).toBe(0);
    // Curve (95) + chevron (2).
    expect(ctx.count("lineTo")).toBe(97);
  });

  it("draws a flat robust baseline without reading knots", () => {
    render(<Curve fit={{ kind: "robust_baseline", median: 4 } as never} domain={[0, 1]} />);
    expect(ctx.count("stroke")).toBeGreaterThan(0);
  });
});
