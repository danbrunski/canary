import type { Page, Route } from "@playwright/test";

import type { RomEdit, RomEditPreview, RomSimResult } from "../../src/types/romEditor";
import type { RomLibraryEntry, RomTablesResponse } from "../../src/types/romLibrary";
import { LEVERS_FUEL_COMP } from "../../src/test/romLeverFixtures";
import { buildSimResult, fuelCompWithAxes, tablesWithAxes } from "../../src/test/romSimFixtures";

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
  /** The simulate answer. */
  simulate?: RomSimResult | Failure;
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

/** What `rom_editor.preview_edits` answers for `scale` / `set_raw` / `add_raw`
 *  edits on Fuel Compensation (Map 1): the after table, the cell list old ->
 *  new, the spec as YAML. Only scale + nearest edits are computed here; the
 *  stub refuses anything else, so a page that sent something else fails loudly. */
export function previewFor(
  sha: string,
  request: { label: string; edits: RomEdit[] },
  matched: boolean,
): RomEditPreview {
  const before = fuelCompWithAxes();
  const after = { ...before, raw: before.raw.map((row) => [...row]) };
  const cells: RomEditPreview["provenance"]["cells"] = [];
  for (const edit of request.edits) {
    if (edit.table !== "fuel_compensation" || edit.slot !== 1) {
      throw new Error(`stub previews only Fuel Compensation (Map 1), got ${edit.table}`);
    }
    if (edit.scale === undefined || edit.rounding !== "nearest") {
      throw new Error("stub previews only `scale` with `rounding: nearest`");
    }
    for (const [row, col] of edit.cells ?? []) {
      const old = before.raw[row]?.[col] ?? 0;
      const next = Math.round(old * edit.scale);
      after.raw[row][col] = next;
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
  }
  const scale = 0.78125;
  after.decoded = after.raw.map((row) => row.map((value) => value * scale));
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
      changed_nodes: ["fuel_comp_lookup"],
      moved_lookups: [{ node: "target_afr_lookup", tables: ["fuel_targets"] }],
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
    spec_sha256: specSha,
    candidate_sha256: candidate,
    provenance,
    tables: [after],
    levers: LEVERS_FUEL_COMP,
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
    const tables = /^\/control\/rom\/library\/([0-9a-f]{64})\/tables$/.exec(path);
    if (method === "GET" && tables) {
      return reply(route, (options.tables ?? tablesFor)(tables[1]));
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
