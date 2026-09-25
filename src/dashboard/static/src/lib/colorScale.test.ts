import { describe, expect, it } from "vitest";

import {
  DAY_TOKENS,
  SERIES_TOKENS,
  cssColor,
  seriesColor,
  seriesVar,
  withAlpha,
} from "./chartTheme";
import {
  coverage,
  coverageClass,
  diverging,
  divergingAround0,
  sequential,
  sequentialIn,
} from "./colorScale";
import { mapColor } from "./romMaps";

describe("colorScale (R08.7)", () => {
  it("sequential runs green → red and clamps", () => {
    expect(sequential(0)).toBe("rgb(80, 190, 110)");
    expect(sequential(1)).toBe("rgb(230, 80, 65)");
    expect(sequential(2)).toBe(sequential(1));
    expect(sequential(Number.NaN)).toBe(sequential(0));
    expect(sequentialIn(5, 5, 5)).toBe(sequential(0));
  });

  it("the ROM map ramp is the shared sequential one", () => {
    expect(mapColor(14.7, 11.5, 14.7)).toBe(sequential(1));
    expect(mapColor(13.1, 11.5, 14.7)).toBe(sequentialIn(13.1, 11.5, 14.7));
  });

  it("diverging keeps zero neutral whatever the extent", () => {
    expect(diverging(0)).toBe("rgb(224, 229, 234)");
    expect(divergingAround0(0, 7)).toBe(diverging(0));
    expect(divergingAround0(-7, 7)).toBe("rgb(96, 150, 235)");
    expect(divergingAround0(3, 0)).toBe(diverging(0));
  });

  it("coverage separates never-visited from thin from visited", () => {
    expect(coverageClass(0)).toBe("unvisited");
    expect(coverageClass(4)).toBe("thin");
    expect(coverageClass(5)).toBe("visited");
    expect(coverage(0, 40)).toBe("rgb(226, 232, 240)");
    expect(coverage(40, 40)).toBe("rgb(30, 64, 175)");
  });
});

describe("chartTheme (R08.7)", () => {
  it("resolves tokens for canvas, falling back to the day palette without a stylesheet", () => {
    expect(cssColor("accent")).toBe(DAY_TOKENS.accent);
    document.documentElement.style.setProperty("--color-accent", "#d99a3c");
    expect(cssColor("accent")).toBe("#d99a3c");
    document.documentElement.style.removeProperty("--color-accent");
  });

  it("has one six-series palette, cycling", () => {
    expect(SERIES_TOKENS).toHaveLength(6);
    expect(seriesVar(0)).toBe("var(--color-accent)");
    expect(seriesVar(6)).toBe(seriesVar(0));
    expect(seriesColor(1)).toBe(DAY_TOKENS["accent-2"]);
  });

  it("adds alpha to a hex colour", () => {
    expect(withAlpha("#ff6f6f", 0.1)).toBe("rgba(255, 111, 111, 0.1)");
    expect(withAlpha("#fff", 0.5)).toBe("rgba(255, 255, 255, 0.5)");
  });
});
