/**
 * Layering rules for the dashboard sources, enforced rather than documented.
 *
 * - R08.4: routes live in `api/`, one function per route. Panels and `lib/`
 *   used to call `controlJson` inline, which is how `/control/calibration`
 *   came to be typed twice and `/control/capture/profiles` re-implemented.
 * - R08.5: `mem:<NAME>` streams as `obd:mem:<NAME>`; only
 *   `lib/romMaps.streamKey` writes that rule down.
 * - R19: the Logs and Analysis pages compose views that import their owners.
 *   The page files keep the old import paths as direct re-exports, so a view
 *   importing its own page would put a cycle back; and the plot-data helpers
 *   in `lib/` stay free of React so they stay testable as numbers.
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

  it("keeps the Logs and Analysis page facades out of every module but the workbench", () => {
    // Static, dynamic and type imports of either page file.
    const page = /(?:from\s*|import\s*\(\s*)["'][^"']*\/(?:LogsPage|AnalysisPage)["']/;
    expect(
      offenders(
        page,
        (p) => p === "./panels/Workbench.tsx" || /\/(?:LogsPage|AnalysisPage)\.tsx$/.test(p),
      ),
    ).toEqual([]);
  });

  it("keeps the log and analysis plot-data helpers free of React and of the UI layers", () => {
    const helpers = ["./lib/framePlot.ts", "./lib/frameTable.ts", "./lib/analysisPlot.ts"];
    const ui =
      /from\s*["'](?:react|react-dom|\.\.\/(?:components|hooks|panels|api))(?:\/[^"']*)?["']/;
    const impure = helpers.filter((path) => ui.test(sources[path] ?? ""));
    // A renamed helper would silently stop being checked.
    expect(helpers.filter((path) => !(path in sources))).toEqual([]);
    expect(impure).toEqual([]);
  });
});
