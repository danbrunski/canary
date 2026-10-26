import { readFileSync } from "node:fs";
import { resolve } from "node:path";

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
  contrastRatio,
  coverage,
  coverageClass,
  diverging,
  divergingAround0,
  inkOn,
  luminance,
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

// The ROM Editor's grids lay value shading under text and under the marks that
// show selection, pending edits and changes. Both themes are dark (the night
// theme is a dimmer dark, never a light one), so what has to hold is that the
// dark ink and the dark ring read on every stop of the ramps, in both.
describe("cell shading stays legible in both themes (ROM Editor)", () => {
  const css = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");
  const block = (selector: string) => {
    const start = css.indexOf(selector);
    return css.slice(start, css.indexOf("}", start));
  };
  const token = (selector: string, name: string) => {
    const match = new RegExp(`--color-${name}:\\s*(#[0-9a-fA-F]{6})`).exec(block(selector));
    if (!match) throw new Error(`no --color-${name} under ${selector}`);
    return match[1];
  };
  /** `#rrggbb` as the `rgb(r, g, b)` string `luminance` reads. */
  const rgb = (hex: string) =>
    `rgb(${[1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16)).join(", ")})`;
  const themes = [
    ["day (default dark)", "@theme"],
    ["night", '[data-theme="night"]'],
  ] as const;
  const steps = Array.from({ length: 51 }, (_, i) => i / 50);

  it.each(themes)(
    "the %s theme's ink and marks read on every value-ramp stop",
    (_name, selector) => {
      const ink = luminance(rgb(token(selector, "bg")));
      expect(ink).not.toBeNull();
      for (const t of steps) {
        const fill = luminance(sequential(t));
        expect(fill).not.toBeNull();
        // Text: WCAG AA for normal text.
        expect(contrastRatio(ink!, fill!)).toBeGreaterThanOrEqual(4.5);
        // The ring, outline and glyph marks are drawn in the same dark ink.
        expect(inkOn(sequential(t))).toBe("dark");
      }
    },
  );

  it.each(themes)("the %s theme's ink reads on every coverage shade", (_name, selector) => {
    const dark = luminance(rgb(token(selector, "bg")))!;
    const light = luminance(rgb(token(selector, "fg")))!;
    for (const count of [0, 1, 4, 5, 10, 20, 40]) {
      const fill = coverage(count, 40);
      const ink = inkOn(fill) === "dark" ? dark : light;
      expect(contrastRatio(ink, luminance(fill)!), `count ${count}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("the coverage ramp's deep end takes light ink, its pale end dark", () => {
    expect(inkOn(coverage(40, 40))).toBe("light");
    expect(inkOn(coverage(0, 40))).toBe("dark");
    expect(inkOn("not a colour")).toBe("dark");
  });

  it("measures contrast the WCAG way", () => {
    expect(contrastRatio(1, 0)).toBeCloseTo(21, 5);
    expect(luminance("rgb(255, 255, 255)")).toBeCloseTo(1, 5);
    expect(luminance("rgb(0, 0, 0)")).toBe(0);
  });
});
