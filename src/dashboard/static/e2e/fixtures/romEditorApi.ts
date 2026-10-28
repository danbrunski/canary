import type { Page, Route } from "@playwright/test";

import type {
  RomEdit,
  RomEditPreview,
  RomSimResult,
  RomTypicalValues,
} from "../../src/types/romEditor";
import type {
  RomLibraryEntry,
  RomTableValues,
  RomTablesResponse,
} from "../../src/types/romLibrary";
import type { RomTraceContext } from "../../src/types/workbench";
import { LEVERS_FUEL_COMP, LEVERS_TIMING_TRIM } from "../../src/test/romLeverFixtures";
import {
  buildSimResult,
  buildTimingBlock,
  fuelCompWithAxes,
  tablesWithAxes,
  timingTrimTable,
} from "../../src/test/romSimFixtures";
import { SHIPPED_TYPICAL } from "../../src/test/romTypicalFixtures";

/**
 * The ROM library, tables, preview and simulate routes of the daemon, stubbed
 * for `romEditor.spec.ts`. Register after `mockDaemon` (a later route wins
 * over its `/control/**` catch-all).
 *
 * The payloads are the vitest fixtures (`src/test/romLibraryFixtures.ts`,
 * `romSimFixtures.ts`), imported rather than copied, so the browser spec and
 * the component tests cannot drift apart. The preview payload has the shape of
 * `docs/headless-api.md` "POST /control/rom/library/{sha}/edits/preview", with
 * the after values computed from the request the page really sent.
 */

/** A route answer: a JSON body, or an HTTP error with its body. */
export interface Failure {
  status: number;
  body: unknown;
}

export interface RomApiOptions {
  /** `GET /control/rom/library`, in the order the API answers. */
  library: RomLibraryEntry[];
  /** `POST /control/rom/library?filename=...`: the answer per file name. */
  uploads?: Record<string, (RomLibraryEntry & { created?: boolean }) | Failure>;
  /** `GET /control/rom/library/{sha}/tables`. */
  tables?: (sha: string) => RomTablesResponse;
  /** `GET /control/rom/library/{sha}/trace`: the live overlay's inputs. Unset,
   *  the stub answers the 409 a vehicle without a viewer definition gives. */
  trace?: (sha: string) => RomTraceContext | Failure;
  /** The simulate answer. */
  simulate?: RomSimResult | Failure;
  /** `GET /control/rom/editor/typical-values`: the Xterra's real declared values
   *  (`romTypicalValues.fixture.json`) unless a spec says otherwise. */
  typical?: RomTypicalValues | Failure;
}

/** One request the stub saw. */
export interface RomApiCall {
  method: string;
  path: string;
  query: string;
  contentType: string | null;
  body: Buffer | null;
}

export interface RomApi {
  calls: RomApiCall[];
  /** Requests no route above answers: a spec asserts this stays empty. */
  unexpected: string[];
  /** The parsed JSON bodies posted to a path ending in `suffix`. */
  posted: (suffix: string) => unknown[];
}

const isFailure = (value: unknown): value is Failure =>
  typeof value === "object" && value !== null && "status" in value && "body" in value;

/** The library's Fuel Comp tables for an image whose axes decode (the Simulate
 *  form suggests its edges from them). */
export function tablesFor(sha: string): RomTablesResponse {
  return { ...tablesWithAxes(), sha256: sha };
}

/** A simulate answer for a trim edit (E09.7): the default result with the
 *  `ignition_timing` block of a trim B edit under HI DET set (a number per cell),
 *  or of the same edit to trim A (read nowhere under that latch). */
export function trimSimResult(trim: "trim_b" | "trim_a" = "trim_b"): RomSimResult {
  return { ...buildSimResult(), ignition_timing: buildTimingBlock(trim) };
}

/** What `rom_editor.preview_edits` answers for edits on Fuel Compensation
 *  (Map 1) -- `scale` + `nearest` -- and on the timing trims (Map 1) --
 *  `add_raw`, on the value bits with the flag bit kept (`rom_edit_spec`): the
 *  after tables, the cell list old -> new, the spec as YAML. The stub refuses
 *  anything else, so a page that sent something else fails loudly. */
export function previewFor(
  sha: string,
  request: { label: string; edits: RomEdit[] },
  matched: boolean,
): RomEditPreview {
  const after: RomTableValues[] = [];
  const cells: RomEditPreview["provenance"]["cells"] = [];
  const touched = (id: string, before: RomTableValues): RomTableValues => {
    let found = after.find((table) => table.id === id);
    if (!found) {
      found = { ...before, raw: before.raw.map((row) => [...row]) };
      after.push(found);
    }
    return found;
  };
  for (const edit of request.edits) {
    if (edit.table === "fuel_compensation" && edit.slot === 1) {
      if (edit.scale === undefined || edit.rounding !== "nearest") {
        throw new Error("stub previews only `scale` with `rounding: nearest`");
      }
      const before = fuelCompWithAxes();
      const table = touched("fuel_compensation", before);
      for (const [row, col] of edit.cells ?? []) {
        const old = before.raw[row]?.[col] ?? 0;
        const next = Math.round(old * edit.scale);
        table.raw[row][col] = next;
        cells.push({
          table: "fuel_compensation",
          slot: 1,
          row,
          col,
          address: 0xe0660 + row * 16 + col,
          old,
          new: next,
        });
      }
    } else if (
      (edit.table === "timing_trim_a" || edit.table === "timing_trim_b") &&
      edit.slot === 1
    ) {
      if (edit.add_raw === undefined) throw new Error("stub previews only `add_raw` on a trim");
      const before = timingTrimTable(edit.table === "timing_trim_a" ? "a" : "b");
      const table = touched(edit.table, before);
      for (const [row, col] of edit.cells ?? []) {
        const old = before.raw[row]?.[col] ?? 0;
        // The value bits move and the flag bit stays; a result outside 0..127 is refused.
        const value = (old & 0x7f) + edit.add_raw;
        if (value < 0 || value > 0x7f) throw new Error(`stub: ${value} is outside 0..127`);
        const next = (old & 0x80) | value;
        table.raw[row][col] = next;
        cells.push({
          table: edit.table,
          slot: 1,
          row,
          col,
          address: (edit.table === "timing_trim_a" ? 0xe0040 : 0xe0140) + row * 16 + col,
          old,
          new: next,
        });
      }
    } else {
      throw new Error(
        `stub previews only Fuel Compensation and the timing trims (Map 1), got ${edit.table}`,
      );
    }
  }
  for (const table of after) {
    table.decoded = table.raw.map((row) =>
      row.map((value) => (table.id === "fuel_compensation" ? value * 0.78125 : value & 0x7f)),
    );
  }
  const candidate = "c".repeat(64);
  const specSha = "5".repeat(64);
  const provenance = {
    tool: "rom_edit_spec",
    kind: "simulation_candidate",
    flashable: false,
    route: "edit_spec",
    label: request.label,
    base: {
      source: "revision",
      revision: matched ? "ZS44A-20260920" : null,
      file: `${sha}.bin`,
      image_sha256: sha,
      image_format: "plain",
      container_sha256: null,
    },
    base_sha256: sha,
    candidate_sha256: candidate,
    spec_sha256: specSha,
    cells,
    ranges: [],
    effect: {
      outcome: "traced",
      slot: 1,
      slot_source: "default_slot",
      changed_bytes: cells.length,
      changed_nodes: after.some((table) => table.id === "fuel_compensation")
        ? ["fuel_comp_lookup"]
        : ["timing_trim_lookup"],
      moved_lookups: after.some((table) => table.id === "fuel_compensation")
        ? [{ node: "target_afr_lookup", tables: ["fuel_targets"] }]
        : [],
    },
  };
  return {
    tool: "rom_editor",
    tool_version: "1",
    spec: {
      schema_version: 1,
      base: matched
        ? { revision: "ZS44A-20260920", file: null, sha256: null }
        : { revision: null, file: `${sha}.bin`, sha256: sha },
      label: request.label,
      edits: request.edits,
    },
    spec_yaml: specYaml(request.label),
    export: { ok: true, reason: null, message: null, tables: [] },
    spec_sha256: specSha,
    candidate_sha256: candidate,
    provenance,
    tables: after,
    levers: after.some((table) => table.id === "fuel_compensation")
      ? LEVERS_FUEL_COMP
      : LEVERS_TIMING_TRIM,
    sim_base: matched
      ? { ok: true, status: "matched", revision: "ZS44A-20260920", reason: null }
      : {
          ok: false,
          status: "unrecognised",
          revision: null,
          reason: "base_not_a_recorded_revision",
        },
  };
}

/** The spec the stub says it saved, with the label so a wrong download shows. */
export function specYaml(label: string): string {
  return [
    "# CANary ROM Editor edit spec (E08.1). Stubbed by e2e/fixtures/romEditorApi.ts.",
    "schema_version: 1",
    "base: {revision: ZS44A-20260920}",
    `label: ${label}`,
    "",
  ].join("\n");
}

export async function mockRomApi(page: Page, options: RomApiOptions): Promise<RomApi> {
  const calls: RomApiCall[] = [];
  const unexpected: string[] = [];
  const reply = (route: Route, answer: unknown) =>
    isFailure(answer)
      ? route.fulfill({
          status: answer.status,
          contentType: "application/json",
          body: JSON.stringify(answer.body),
        })
      : route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(answer),
        });

  await page.route(/\/control\/rom\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();
    const path = url.pathname;
    calls.push({
      method,
      path,
      query: url.search,
      contentType: request.headers()["content-type"] ?? null,
      body: request.postDataBuffer(),
    });
    if (request.headers()["authorization"] !== "Bearer ui-test") {
      return reply(route, { status: 401, body: { detail: "Unauthorized" } });
    }
    if (method === "GET" && path === "/control/rom/library") {
      return reply(route, { entries: options.library, count: options.library.length });
    }
    if (method === "POST" && path === "/control/rom/library") {
      const filename = url.searchParams.get("filename") ?? "";
      const answer = options.uploads?.[filename];
      if (!answer) {
        unexpected.push(`${method} ${path}?filename=${filename}`);
        return reply(route, { status: 500, body: { detail: "no upload expected" } });
      }
      return reply(route, answer);
    }
    if (method === "GET" && path === "/control/rom/editor/typical-values") {
      return reply(route, options.typical ?? SHIPPED_TYPICAL);
    }
    const tables = /^\/control\/rom\/library\/([0-9a-f]{64})\/tables$/.exec(path);
    if (method === "GET" && tables) {
      return reply(route, (options.tables ?? tablesFor)(tables[1]));
    }
    const trace = /^\/control\/rom\/library\/([0-9a-f]{64})\/trace$/.exec(path);
    if (method === "GET" && trace) {
      return reply(
        route,
        options.trace?.(trace[1]) ?? {
          status: 409,
          body: { detail: "ROM map definition unavailable for this vehicle: rom_viewer.yaml" },
        },
      );
    }
    const preview = /^\/control\/rom\/library\/([0-9a-f]{64})\/edits\/preview$/.exec(path);
    if (method === "POST" && preview) {
      const sha = preview[1];
      const entry = [...options.library, ...Object.values(options.uploads ?? {})].find(
        (item): item is RomLibraryEntry => !isFailure(item) && item.sha256 === sha,
      );
      const matched = entry?.revision_match.status === "matched";
      return reply(
        route,
        previewFor(sha, JSON.parse(request.postData() ?? "{}") as never, matched),
      );
    }
    const simulate = /^\/control\/rom\/library\/([0-9a-f]{64})\/simulate$/.exec(path);
    if (method === "POST" && simulate) {
      return reply(route, options.simulate ?? buildSimResult());
    }
    unexpected.push(`${method} ${path}`);
    return reply(route, { status: 500, body: { detail: "unexpected request" } });
  });

  return {
    calls,
    unexpected,
    posted: (suffix) =>
      calls
        .filter((call) => call.method === "POST" && call.path.endsWith(suffix))
        .map((call) => JSON.parse(call.body?.toString("utf8") ?? "null") as unknown),
  };
}
