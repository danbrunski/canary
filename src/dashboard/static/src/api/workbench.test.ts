import { afterEach, describe, expect, it, vi } from "vitest";

import { UnexpectedResponseError } from "./core";
import { previewRomEdits } from "./workbench";

afterEach(() => {
  vi.unstubAllGlobals();
});

function answer(body: unknown) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
}

/** The fields a preview always carries; `spec_yaml` and `levers` vary. */
const PREVIEW = {
  spec: {},
  spec_sha256: "5".repeat(64),
  candidate_sha256: "c".repeat(64),
  provenance: { cells: [], ranges: [], effect: null },
  tables: [],
  sim_base: { ok: false, status: "unrecognised", revision: null, reason: "sim_kit_undeclared" },
};
const REQUEST = { label: "x", edits: [] };

describe("previewRomEdits", () => {
  it("accepts a preview with no simulator and an unverified table (null levers, null spec)", async () => {
    answer({
      ...PREVIEW,
      spec_yaml: null,
      levers: null,
      export: { ok: false, reason: "table_not_verified", message: "m", tables: ["fuel_targets"] },
    });
    const preview = await previewRomEdits("t", "a".repeat(64), REQUEST);
    expect(preview.levers).toBeNull();
    expect(preview.spec_yaml).toBeNull();
    expect(preview.export.tables).toEqual(["fuel_targets"]);
  });

  it("refuses a preview without `export` as another daemon version's", async () => {
    answer({ ...PREVIEW, spec_yaml: "x", levers: null });
    await expect(previewRomEdits("t", "a".repeat(64), REQUEST)).rejects.toBeInstanceOf(
      UnexpectedResponseError,
    );
  });
});
