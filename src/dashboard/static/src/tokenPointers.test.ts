import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Entry for the control token moved from the Control panel to the header
// pill (Task 5.1) and ~11 placeholders kept pointing at the old place. A
// locked panel renders `TokenRequired`; this fails on a hand-written pointer
// so the next one cannot drift the same way.

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return sources(path);
    return /\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name) ? [path] : [];
  });
}

describe("control-token pointers", () => {
  const files = sources(join(__dirname));

  it("scans the source tree", () => {
    expect(files.length).toBeGreaterThan(100);
  });

  it("no source points at the Control panel for the token", () => {
    const hits = files.filter((f) => readFileSync(f, "utf8").includes("(Control panel)"));
    expect(hits).toEqual([]);
  });

  it("no source hand-writes an 'Enter the control token' placeholder", () => {
    const stale = /Enter the (Pi )?(control )?token/;
    const hits = files.filter((f) => stale.test(readFileSync(f, "utf8")));
    expect(hits).toEqual([]);
  });
});
