/**
 * Layering rules for the dashboard sources, enforced rather than documented.
 *
 * - R08.4: routes live in `api/`, one function per route. Panels and `lib/`
 *   used to call `controlJson` inline, which is how `/control/calibration`
 *   came to be typed twice and `/control/capture/profiles` re-implemented.
 * - R08.5: `mem:<NAME>` streams as `obd:mem:<NAME>`; only
 *   `lib/romMaps.streamKey` writes that rule down.
 */
import { describe, expect, it } from "vitest";

const sources = import.meta.glob<string>(["./**/*.{ts,tsx}", "!./**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

function offenders(pattern: RegExp, allowed: (path: string) => boolean): string[] {
  return Object.entries(sources)
    .filter(([path, text]) => !allowed(path) && pattern.test(text))
    .map(([path]) => path);
}

describe("dashboard layering", () => {
  it("calls controlJson and fetchJson only from api/", () => {
    expect(offenders(/\b(?:controlJson|fetchJson)\b/, (p) => p.startsWith("./api/"))).toEqual([]);
  });

  it("builds obd:mem: stream keys only in lib/romMaps", () => {
    expect(offenders(/obd:mem:/, (p) => p === "./lib/romMaps.ts")).toEqual([]);
  });
});
