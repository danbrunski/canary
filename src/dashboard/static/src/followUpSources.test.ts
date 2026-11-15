import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { describe, expect, it } from "vitest";

/** Operator, 2026-11-15: a failed read after an action is loud. `refresh()`
 *  resolves to an outcome and never rejects, so a call that discards the
 *  outcome (a bare `await refresh()`, `void refresh()`, a `Promise.all([...])`
 *  of them, a `.then(...)` chain) drops a failure on the floor and leaves a
 *  stale view as if the action had changed nothing. An action's follow-up is
 *  `await reloaded(...)` (hand-rolled), `then: refresh` (the runner checks the
 *  outcome), or the outcome is read (`combineOutcomes`, `followUpFailureText`,
 *  `requireFresh`). This fails on any other call of a `refresh…()` in
 *  `src/panels`, `src/hooks` and `src/components`; background re-reads that
 *  are quiet on purpose are listed in `QUIET`, each with its reason. */

/** `file:call` -> why that discarded outcome is deliberate. */
const QUIET: Record<string, string> = {
  "hooks/useStatus.ts:refreshFull()":
    "a background re-read when the compact status changes: not an action, quiet like every poll",
  "panels/TestSessionPanel.tsx:refreshBoth()":
    "the 'Re-run preflight' button: a manual re-read, no action preceded it",
  "panels/ControlPanel.tsx:refreshControl()": "the manual Refresh button: no action preceded it",
};

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\./.test(name) ? [path] : [];
  });
}

function stripComments(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, " "))
    .replace(/\/\/.*$/gm, "");
}

const CALL = /\b[\w.]*[rR]efresh\w*\(\)/g;
/** The outcome is consumed: handed to a helper that checks it, or returned or
 *  assigned for the caller to use. */
const CONSUMED =
  /\b(?:reloaded|combineOutcomes|followUpFailureText|requireFresh)\(|(?:\breturn|=)\s*(?:await\s+)?$/;

/** Calls of `refresh…()` whose outcome is discarded, as `call` strings. */
export function discardedRefreshes(source: string): string[] {
  const text = stripComments(source);
  const found: string[] = [];
  for (const match of text.matchAll(CALL)) {
    const before = text.slice(0, match.index);
    const start = Math.max(
      before.lastIndexOf(";"),
      before.lastIndexOf("{"),
      before.lastIndexOf("}"),
    );
    const prefix = before.slice(start + 1).replace(/\s+/g, " ");
    if (!CONSUMED.test(prefix)) found.push(match[0]);
  }
  return found;
}

describe("a follow-up read's outcome is never dropped", () => {
  it("no panel, hook or component discards a refresh() outcome unlisted", () => {
    const root = __dirname;
    const offenders: string[] = [];
    for (const dir of ["panels", "hooks", "components"]) {
      for (const path of sources(join(root, dir))) {
        const file = relative(root, path);
        for (const call of discardedRefreshes(readFileSync(path, "utf8"))) {
          const key = `${file}:${call}`;
          if (!(key in QUIET)) offenders.push(key);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it("every listed quiet call still exists (a stale entry hides nothing)", () => {
    for (const key of Object.keys(QUIET)) {
      const [file, call] = key.split(":");
      expect(discardedRefreshes(readFileSync(join(__dirname, file), "utf8"))).toContain(call);
    }
  });

  it("catches the shapes it exists for", () => {
    const bad = [
      "await refresh();",
      "  await polled.refresh();",
      "void refresh();",
      "if (changed) void refreshFull();",
      "await Promise.all([a.refresh(), b.refresh()]);",
      "await refresh().then(() => 1);",
      "refresh().catch(() => undefined);",
      "const f = () => refresh();",
    ];
    for (const line of bad) {
      expect(discardedRefreshes(`async () => { ${line} }`), line).not.toEqual([]);
    }
  });

  it("passes the shapes that use the outcome", () => {
    const good = [
      "await reloaded(refresh());",
      "await reloaded(a.refresh(), b.refresh());",
      "const lost = followUpFailureText(await polled.refresh());",
      "return combineOutcomes(...(await Promise.all([a.refresh(), b.refresh()])));",
      "const read = await refresh();",
      "// await refresh();",
      "/* void refresh(); */",
    ];
    for (const line of good) {
      expect(discardedRefreshes(`async () => { ${line} }`), line).toEqual([]);
    }
  });
});
