/**
 * R08.1: every colour class in the dashboard resolves to a theme token.
 *
 * Tailwind v4 generates nothing for a class whose colour is not in
 * `index.css`'s `@theme`, so `text-fg-muted` (never defined) rendered the
 * "muted" notes on the tuning pages at full foreground colour. Raw palette
 * classes (`text-red-400`, ...) do render, but ignore `[data-theme="night"]`.
 * Both were invisible to typecheck and to every component test.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { DAY_TOKENS } from "./lib/chartTheme";

// Read from disk (vitest's root is the dashboard directory): the vitest config
// sets `css: false`, which empties a `?raw` CSS import.
const css = readFileSync(resolve(process.cwd(), "src/index.css"), "utf8");

const sources = import.meta.glob<string>(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

const RAW_PALETTE =
  /\b(?:text|bg|border|fill|stroke|ring|outline|divide|from|to|via)-(?:red|amber|emerald|sky|slate|green|yellow|blue|gray|zinc|neutral|orange|rose)-\d{2,3}\b/g;

function themeColorTokens(): Set<string> {
  const tokens = new Set<string>();
  for (const match of css.matchAll(/--color-([a-z0-9-]+)\s*:/g)) tokens.add(match[1]);
  return tokens;
}

describe("theme classes", () => {
  it("finds the dashboard sources", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
  });

  it("uses no raw Tailwind palette colour", () => {
    const hits: string[] = [];
    for (const [path, text] of Object.entries(sources)) {
      for (const m of text.matchAll(RAW_PALETTE)) hits.push(`${path}: ${m[0]}`);
    }
    expect(hits).toEqual([]);
  });

  it("uses only semantic colour tokens the theme defines", () => {
    // `text-fg-muted` and `border-danger` both looked like tokens and were not.
    const tokens = themeColorTokens();
    expect(tokens.has("fg-dim")).toBe(true);
    const semantic =
      /\b(?:text|bg|border|fill|stroke|ring|outline)-((?:fg|elev|accent|ok|warn|bad|danger|error|success|info|muted|primary|secondary)(?:-[a-z0-9]+)*)\b/g;
    const hits: string[] = [];
    for (const [path, text] of Object.entries(sources)) {
      for (const m of text.matchAll(semantic)) {
        if (!tokens.has(m[1])) hits.push(`${path}: ${m[0]}`);
      }
    }
    expect(hits).toEqual([]);
  });

  it("keeps the canvas fallback palette equal to index.css's day tokens", () => {
    // chartTheme.DAY_TOKENS is what canvas draws with where no stylesheet is
    // loaded; it must be the day @theme block, token for token.
    const day = css.slice(css.indexOf("@theme"), css.indexOf("[data-theme"));
    const declared = Object.fromEntries(
      [...day.matchAll(/--color-([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8})/g)].map((m) => [
        m[1],
        m[2].toLowerCase(),
      ]),
    );
    expect(DAY_TOKENS).toEqual(declared);
  });

  it("gives the night theme every token the day theme has", () => {
    const night = css.slice(css.indexOf("[data-theme"));
    const nightTokens = new Set([...night.matchAll(/--color-([a-z0-9-]+)\s*:/g)].map((m) => m[1]));
    expect([...themeColorTokens()].filter((t) => !nightTokens.has(t))).toEqual([]);
  });

  it("draws charts with theme tokens, never colour literals (R08.7)", () => {
    // A literal stroke/fill ignores the night theme; use a `stroke-*`/`fill-*`
    // class or lib/chartTheme (cssVar for SVG, cssColor for canvas), and
    // lib/colorScale for value ramps.
    const literal = /\b(?:stroke|fill)(?:Style)?\s*=\s*\{?\s*["'`](?:#|rgb|hsl)/;
    const hsl = /\bhsl\(/;
    const hits = Object.entries(sources)
      .filter(([path]) => path !== "./lib/colorScale.ts" && path !== "./lib/chartTheme.ts")
      .filter(([, text]) => literal.test(text) || hsl.test(text))
      .map(([path]) => path);
    expect(hits).toEqual([]);
  });

  it("sets font sizes from the type scale, not arbitrary values (R28.8)", () => {
    // A size is a `--text-*` token in index.css's @theme (`text-body`). A fresh
    // `text-[0.74rem]` is a 26th size nobody chose; the sweep that cut 25 sizes
    // to seven tokens had to undo 392 of them. The exceptions below are the
    // sizes that sit more than 0.02rem from every token, with how often each
    // is used. It is an exact count, so the list can only shrink: use a token
    // and lower the number here, or delete the line when it reaches zero.
    const scale = [...css.matchAll(/--text-([a-z0-9-]+)\s*:\s*[0-9.]+rem/g)].map((m) => m[1]);
    expect(scale).toEqual(["micro", "caption", "small", "body-sm", "body", "lead", "title"]);

    const arbitrary: Record<string, number> = {};
    for (const text of Object.values(sources)) {
      for (const m of text.matchAll(/\btext-\[([0-9.]+(?:rem|px|em))\]/g)) {
        arbitrary[m[1]] = (arbitrary[m[1]] ?? 0) + 1;
      }
    }
    const EXCEPTIONS: Record<string, number> = {
      "0.5rem": 1,
      "0.55rem": 2,
      "0.56rem": 2,
      "0.9rem": 6,
      "0.92rem": 2,
      "0.95rem": 2,
      "1.1rem": 2,
      "1.25rem": 1,
      "1.4rem": 2,
      "1.75rem": 1,
      "10px": 3,
    };
    expect(
      arbitrary,
      "a new arbitrary font size: use a --text-* token from index.css; a count went down: lower it in EXCEPTIONS",
    ).toEqual(EXCEPTIONS);
  });

  it("writes the eyebrow and the small label once, in styles.ts (R28.8)", () => {
    // `eyebrowClass` and `smallLabelClass` were nine and ten copies of one string.
    const copy =
      /text-(?:caption uppercase tracking-\[0\.12em\]|small uppercase tracking-\[0\.08em\]) text-fg-faint/;
    const hits = Object.entries(sources)
      .filter(([path, text]) => path !== "./styles.ts" && copy.test(text))
      .map(([path]) => path);
    expect(hits).toEqual([]);
  });
});
