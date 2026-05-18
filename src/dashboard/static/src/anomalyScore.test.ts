import { describe, expect, it } from "vitest";

import { percentile, scoreView, windowReference } from "./anomalyScore";

describe("percentile", () => {
  it("returns 0 for empty input", () => {
    expect(percentile([], 95)).toBe(0);
  });
  it("interpolates", () => {
    expect(percentile([0, 10], 50)).toBe(5);
    expect(percentile([1, 2, 3, 4], 100)).toBe(4);
  });
  it("ignores non-finite values", () => {
    expect(percentile([1, Infinity, NaN, 3], 50)).toBe(2);
  });
});

describe("windowReference", () => {
  it("falls back to 1 when there are no positive scores", () => {
    expect(windowReference([-0.1, -0.2, 0])).toBe(1);
    expect(windowReference([])).toBe(1);
  });
  it("uses p95 of positive scores", () => {
    expect(windowReference([1, 2, 3, 4, 5])).toBeCloseTo(percentile([1, 2, 3, 4, 5], 95));
  });
});

describe("scoreView", () => {
  it("anchors the index at a supplied threshold (50 at threshold)", () => {
    expect(scoreView(0.3, 0.3, 1).index).toBe(50);
    expect(scoreView(0, 0.3, 1).index).toBe(0);
    expect(scoreView(3.0, 0.3, 1).index).toBeGreaterThan(85);
  });
  it("self-calibrates against the window reference when no threshold", () => {
    // ref 0.2: score 0.1 -> 50, near-zero boundary noise -> low
    expect(scoreView(0.1, null, 0.2).index).toBe(50);
    expect(scoreView(0.002, null, 0.2).level).toBe("low");
    expect(scoreView(0.2, null, 0.2).index).toBe(100);
  });
  it("clamps negative (normal) scores to 0/low", () => {
    const v = scoreView(-0.05, null, 0.2);
    expect(v.index).toBe(0);
    expect(v.level).toBe("low");
  });
  it("pins the missing-features sentinel (non-finite) to high", () => {
    const v = scoreView(Infinity, null, 0.2);
    expect(v.index).toBe(100);
    expect(v.level).toBe("high");
  });
  it("levels: <15 low, <50 elevated, >=50 high", () => {
    expect(scoreView(0.02, null, 0.2).level).toBe("low"); // idx 10
    expect(scoreView(0.06, null, 0.2).level).toBe("elevated"); // idx 30
    expect(scoreView(0.15, null, 0.2).level).toBe("high"); // idx 75
  });
});
