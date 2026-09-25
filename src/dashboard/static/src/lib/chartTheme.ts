// One chart palette that follows the theme (R08.7). Canvas and SVG charts
// hard-coded 30+ hex literals: near-copies of tokens (`#FFB84A` for accent
// `#ffb547`, `#ef4444` for bad `#ff6f6f`), a grey (`#94a3ad`) that was no
// token at all, and two different six-colour series palettes. None of them
// followed `[data-theme="night"]`.
//
// - SVG and DOM: reference the token itself — a Tailwind `stroke-*`/`fill-*`
//   class, or `cssVar(token)` in a style — so the browser re-resolves it when
//   the theme flips.
// - Canvas cannot read a CSS variable: `cssColor(token)` resolves it through
//   `getComputedStyle` at draw time, and `useThemeVersion()` changes when the
//   theme does, so a canvas effect that lists it redraws.

import { useEffect, useState } from "react";

export type ColorToken =
  | "bg"
  | "elev"
  | "elev-2"
  | "elev-3"
  | "fg"
  | "fg-dim"
  | "fg-faint"
  | "accent"
  | "accent-2"
  | "ok"
  | "warn"
  | "bad"
  | "border"
  | "border-soft"
  | "series-5"
  | "series-6";

/** The day palette, as `index.css` declares it. Used only where no stylesheet
 *  is loaded (jsdom); themeClasses.test.ts fails if it drifts from the CSS. */
export const DAY_TOKENS: Readonly<Record<ColorToken, string>> = {
  bg: "#0b0f14",
  elev: "#141a21",
  "elev-2": "#1b232c",
  "elev-3": "#232d38",
  fg: "#eef3f8",
  "fg-dim": "#8a98a5",
  "fg-faint": "#5b6772",
  accent: "#ffb547",
  "accent-2": "#4fd6e0",
  ok: "#5fe3b3",
  warn: "#ffd166",
  bad: "#ff6f6f",
  border: "#29333d",
  "border-soft": "#20272f",
  "series-5": "#b18cff",
  "series-6": "#9be15d",
};

/** The six chart series, in order. ChartsPanel and the Logs strips used to
 *  carry two different six-colour arrays. */
export const SERIES_TOKENS: readonly ColorToken[] = [
  "accent",
  "accent-2",
  "ok",
  "bad",
  "series-5",
  "series-6",
];

/** A token as a CSS value for SVG/DOM styles; follows the theme live. */
export function cssVar(token: ColorToken): string {
  return `var(--color-${token})`;
}

/** A token's current value for canvas drawing (`#rrggbb`). */
export function cssColor(token: ColorToken): string {
  if (typeof document !== "undefined") {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(`--color-${token}`)
      .trim();
    if (value) return value;
  }
  return DAY_TOKENS[token];
}

/** `#rrggbb` (or `#rgb`) at `alpha` opacity, for canvas fills and bands. */
export function withAlpha(hex: string, alpha: number): string {
  const body = hex.replace("#", "");
  const full = body.length === 3 ? [...body].map((c) => c + c).join("") : body;
  const n = Number.parseInt(full.slice(0, 6), 16);
  if (!Number.isFinite(n)) return hex;
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/** Series `index` as an SVG/DOM colour. */
export function seriesVar(index: number): string {
  return cssVar(SERIES_TOKENS[index % SERIES_TOKENS.length]);
}

/** Series `index` resolved for canvas. */
export function seriesColor(index: number): string {
  return cssColor(SERIES_TOKENS[index % SERIES_TOKENS.length]);
}

/** Changes whenever `<html data-theme>` does. List it in a canvas draw
 *  effect's dependencies so a theme flip repaints with the new tokens. */
export function useThemeVersion(): number {
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const observer = new MutationObserver(() => setVersion((v) => v + 1));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    return () => observer.disconnect();
  }, []);
  return version;
}
