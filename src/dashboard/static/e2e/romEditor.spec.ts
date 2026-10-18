import { readFile } from "node:fs/promises";

import { expect, test, type Locator, type Page } from "@playwright/test";

import { STATUSES } from "../src/lib/romSimView";
import { LEVERS_FUEL_COMP, LEVERS_TIMING_TRIM } from "../src/test/romLeverFixtures";
import { ENTRY_K106, ENTRY_TUNED, ENTRY_UNRECORDED } from "../src/test/romLibraryFixtures";
import { buildRequest, buildSimResult } from "../src/test/romSimFixtures";
import type { RomEditPreviewRequest, RomSimRequest } from "../src/types/romEditor";
import { mockDaemon } from "./fixtures/mockDaemon";
import { overflowReport } from "./fixtures/overflow";
import {
  mockRomApi,
  specYaml,
  trimSimResult,
  type RomApi,
  type RomApiOptions,
} from "./fixtures/romEditorApi";

/**
 * The ROM Editor (W09.3, W09.4, W09.8) in a real Chromium, the daemon stubbed
 * at the network layer (`fixtures/romEditorApi.ts`), as `romMaps.spec.ts` does
 * for the ROM Maps page. Real-image runs are W09.6's and stay open.
 *
 * The viewport is short on purpose: with the 16 x 16 table partly below the
 * fold, one click on a cell once selected 7-11 cells (focus scrolled the page
 * under the held button); jsdom cannot show that.
 */
test.use({ viewport: { width: 1280, height: 600 } });

const TUNED_FILE = "ZS44A-Tuned-20260920.OsirisROM";
const FILE_BYTES = Buffer.from("synthetic ROM bytes: the stub does not read them");

async function open(page: Page, options: RomApiOptions): Promise<RomApi> {
  await mockDaemon(page);
  const api = await mockRomApi(page, options);
  await page.addInitScript(() => localStorage.setItem("canary.controlToken", "ui-test"));
  await page.goto("/#wb/tuning-rom-editor");
  return api;
}

/** Click Open ROM… and answer the browser's own file chooser. */
async function chooseFile(page: Page, name: string, buffer: Buffer = FILE_BYTES) {
  const chooser = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: /^Open (another )?ROM…$/ }).click();
  await (await chooser).setFiles({ name, mimeType: "application/octet-stream", buffer });
}

/** A cell of the editable grid (gridcells); the after table's are plain cells. */
const cellAt = (grid: Locator, row: number, col: number) =>
  grid.getByRole("gridcell", { name: new RegExp(`^Row ${row + 1}, column ${col + 1}:`) });
const afterCell = (table: Locator, row: number, col: number) =>
  table.getByRole("cell", { name: new RegExp(`^Row ${row + 1}, column ${col + 1}:`) });

/** Open the tuned ROM, pick Fuel Compensation, scale cells (1,1)-(2,3) by 2 %
 *  nearest and name the set. Returns the grid. */
async function openAndEdit(page: Page, label: string) {
  await chooseFile(page, TUNED_FILE);
  await expect(page.getByLabel("Loaded ROM")).toBeVisible();
  await page.getByRole("button", { name: "Fuel Compensation (Map 1)" }).click();
  const grid = page.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" });
  await cellAt(grid, 1, 1).click();
  await cellAt(grid, 2, 3).click({ modifiers: ["Shift"] });
  const operation = page.getByRole("form", { name: "Operation" });
  await operation.getByLabel("Operation").selectOption("scale");
  await operation.getByLabel("Scale (%)").fill("2");
  await operation.getByLabel("Rounding").selectOption("nearest");
  await operation.getByRole("button", { name: "Apply to selection" }).click();
  await page.getByLabel("Label for this edit set").fill(label);
  return grid;
}

test("starts unloaded: one Open ROM… button, Recent ROMs newest first, nothing fetched or edited", async ({
  page,
}) => {
  // The API answers oldest first; the page sorts by `added_at`.
  const api = await open(page, { library: [ENTRY_K106, ENTRY_TUNED] });

  await expect(page.getByRole("button", { name: "Open ROM…" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Recent ROMs" })).toBeVisible();
  const rows = page.getByRole("table", { name: "Base images" }).getByRole("row");
  await expect(rows).toHaveCount(3); // header + two images
  await expect(rows.nth(1)).toContainText("Running tune");
  await expect(rows.nth(2)).toContainText("k106.bin");

  // Unloaded: no base, no editor, no table request, nothing in the URL.
  await expect(page.getByLabel("Loaded ROM")).toHaveCount(0);
  await expect(page.getByRole("grid")).toHaveCount(0);
  await expect(page.getByRole("form", { name: "Operation" })).toHaveCount(0);
  await expect(page.getByRole("form", { name: "Simulate form" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Preview" })).toHaveCount(0);
  expect(new URL(page.url()).searchParams.has("rom")).toBe(false);
  expect(api.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
    "GET /control/rom/library",
  ]);
  expect(api.unexpected).toEqual([]);
});

test("open a file, select one cell then a range, scale +2 %, preview, download the spec; no image is offered", async ({
  page,
}, testInfo) => {
  const label = "light load 2 % richer";
  const api = await open(page, {
    library: [ENTRY_K106],
    uploads: { [TUNED_FILE]: { ...ENTRY_TUNED, created: true } },
  });

  await chooseFile(page, TUNED_FILE);
  // One step: loaded, named by the file, with the sha, the revision and what it can do.
  const loaded = page.getByLabel("Loaded ROM");
  await expect(loaded).toContainText(TUNED_FILE);
  // The file went up as the page sends it: raw bytes, its own name. Read the recorded call
  // only after the header shows the upload's answer, so it cannot be missing (a race).
  const upload = api.calls.find((call) => call.method === "POST");
  expect(upload?.query).toBe(`?filename=${encodeURIComponent(TUNED_FILE)}`);
  expect(upload?.contentType).toBe("application/octet-stream");
  expect(upload?.body?.equals(FILE_BYTES)).toBe(true);
  await expect(loaded).toContainText(ENTRY_TUNED.sha256.slice(0, 12));
  await expect(loaded.getByText("can simulate")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`\\?rom=${ENTRY_TUNED.sha256}`));

  await page.getByRole("button", { name: "Fuel Compensation (Map 1)" }).click();
  const grid = page.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" });
  await expect(grid.getByRole("gridcell")).toHaveCount(256);

  // One click selects exactly one cell, with the table partly below the fold.
  const box = await grid.boundingBox();
  expect(box!.y + box!.height).toBeGreaterThan(page.viewportSize()!.height);
  await cellAt(grid, 1, 1).click();
  await expect(grid.getByRole("gridcell", { selected: true })).toHaveCount(1);
  await expect(cellAt(grid, 1, 1)).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("form", { name: "Operation" }).getByRole("status")).toHaveText(
    "Selected: 1 cell (row 2, column 2).",
  );

  // Shift-click makes a rectangle: rows 2-3 x columns 2-4.
  await cellAt(grid, 2, 3).click({ modifiers: ["Shift"] });
  await expect(grid.getByRole("gridcell", { selected: true })).toHaveCount(6);
  await expect(page.getByRole("form", { name: "Operation" }).getByRole("status")).toHaveText(
    "Selected: 6 cells (row 2–3, column 2–4).",
  );

  const operation = page.getByRole("form", { name: "Operation" });
  await operation.getByLabel("Operation").selectOption("scale");
  await operation.getByLabel("Scale (%)").fill("2");
  // No default rounding: applying without one is refused by name, and adds nothing.
  await operation.getByRole("button", { name: "Apply to selection" }).click();
  await expect(operation.getByRole("alert")).toContainText("Refused (rounding_undeclared)");
  await expect(page.getByRole("list", { name: "Pending edits" })).toHaveCount(0);
  await operation.getByLabel("Rounding").selectOption("nearest");
  await operation.getByRole("button", { name: "Apply to selection" }).click();
  const pending = page.getByRole("list", { name: "Pending edits" });
  await expect(pending.getByRole("listitem")).toHaveCount(1);
  await expect(pending).toContainText("Fuel Compensation");
  await expect(pending).toContainText("6 cells");
  await expect(grid.locator("[data-pending]")).toHaveCount(6);

  // Preview needs a name for the set.
  const previewButton = page.getByRole("button", { name: "Preview", exact: true });
  await expect(previewButton).toBeDisabled();
  await page.getByLabel("Label for this edit set").fill(label);
  await previewButton.click();

  // The request carries the exact cells, the scale and the rounding.
  const [sent] = api.posted("/edits/preview") as RomEditPreviewRequest[];
  expect(sent).toEqual({
    label,
    edits: [
      {
        table: "fuel_compensation",
        slot: 1,
        cells: [
          [1, 1],
          [1, 2],
          [1, 3],
          [2, 1],
          [2, 2],
          [2, 3],
        ],
        scale: 1.02,
        rounding: "nearest",
      },
    ],
  });

  // After values: raw 122 x 1.02 = 124.44 -> 124 (decoded 96.88), +1.6 %, outlined.
  const after = page.getByRole("table", { name: "Fuel Compensation (Map 1) after the edits" });
  await expect(after).toBeVisible();
  const changed = after.locator('[data-changed="true"]');
  await expect(changed).toHaveCount(6);
  const first = afterCell(after, 1, 1);
  await expect(first).toHaveAttribute("data-changed", "true");
  await expect(first).toHaveAttribute("aria-label", "Row 2, column 2: 96.88 (changed)");
  await expect(first).toContainText("+1.6 %");
  await expect(first).toHaveAttribute("title", "raw 124 (was 122)");
  await expect(first).toHaveCSS("outline-style", "solid");
  await expect(first).toHaveCSS("outline-width", "2px");
  // A neighbour the edit did not touch is not outlined.
  const untouched = afterCell(after, 0, 0);
  await expect(untouched).not.toHaveAttribute("data-changed", "true");
  await expect(untouched).toHaveCSS("outline-style", "none");
  const cellList = page.getByRole("table", { name: "Changed cells list" });
  await expect(cellList.getByRole("row")).toHaveCount(7); // header + six cells
  await expect(cellList).toContainText("122 → 124");
  await expect(page.getByRole("region", { name: "Reached outputs" })).toContainText(
    "fuel_comp_lookup",
  );
  // What the edit can and cannot show: the classifier's lines, verbatim and in
  // order, drawn above the Simulate form (E09.14b).
  const levers = page.getByRole("region", { name: "What this edit can and cannot show" });
  await expect(levers.getByRole("listitem")).toHaveText(LEVERS_FUEL_COMP.messages);
  await expect(levers).toContainText("closed loop: trim authority undecoded");
  const leversBox = await levers.boundingBox();
  const simulateBox = await page.getByRole("region", { name: "Simulate" }).boundingBox();
  expect(leversBox?.y).toBeLessThan(simulateBox?.y ?? 0);
  await page.screenshot({ path: testInfo.outputPath("rom-editor-preview.png"), fullPage: true });

  // Download spec: a file named for the label, holding the spec the daemon returned.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "Download spec" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("light_load_2_richer.edit-spec.yaml");
  const saved = await download.path();
  expect(await readFile(saved, "utf8")).toBe(specYaml(label));

  // No way to get an image out: no image or candidate link, and the spec is the only download.
  await expect(page.getByRole("link", { name: /image|candidate|\.bin|osirisrom/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /image|candidate/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /download/i })).toHaveText("Download spec");
  await expect(page.locator("a[download], a[href^='blob:']")).toHaveCount(0);
  // Everything the page asked of the daemon: the list, the upload, the tables, the preview.
  const asked = new Set(
    api.calls.map((call) => `${call.method} ${call.path.replace(/[0-9a-f]{64}/, "<sha>")}`),
  );
  expect([...asked].sort()).toEqual([
    "GET /control/rom/library",
    "GET /control/rom/library/<sha>/tables",
    "POST /control/rom/library",
    "POST /control/rom/library/<sha>/edits/preview",
  ]);
  expect(api.unexpected).toEqual([]);
});

/** State every field of the Simulate form, as an operator would; `skip` leaves
 *  the named labels alone. The values are `buildRequest()`'s. */
async function stateForm(form: Locator, skip: string[] = []) {
  const stated: Record<string, string> = {
    "RPM edges (rpm)": "200, 600, 1000, 1400",
    "Load edges (% fill)": "3.5, 10.7, 17.8, 25, 32.2",
    "Baro (kPa)": "80",
    "IAT (°C)": "25",
    "Coolant (°C)": "88",
    "Battery (V)": "14",
    "Throttle (%)": "20",
    "Pedal flag (0-255)": "0",
    "STFT (%)": "0",
    "LTFT (%)": "0",
    "Throttle closed learned (counts)": "0",
    "Idle air learned (counts)": "0",
    "EVAP purge area (counts)": "0",
    Draws: "200",
    Seed: "20261015",
  };
  for (const [name, value] of Object.entries(stated)) {
    if (!skip.includes(name)) await form.getByLabel(name, { exact: true }).fill(value);
  }
  await form.getByRole("radio", { name: /Fuel Comp axes/ }).check();
  await form.getByLabel("Rail pressure", { exact: true }).selectOption("working_assumption");
  await form.getByRole("radio", { name: /Fresh flash resets trims/ }).check();
  await stateTiming(form);
}

/** State the ignition chain's two ECU states, which nothing logs and the form never
 *  preselects: the truck's 2026-10-08 pairing by default (`buildRequest()`'s). */
async function stateTiming(form: Locator, highDet = /^Set\b/, knock = /^Fallback\b/) {
  await form
    .getByRole("group", { name: "HI DET latch" })
    .getByRole("radio", { name: highDet })
    .check();
  await form
    .getByRole("group", { name: "Knock control" })
    .getByRole("radio", { name: knock })
    .check();
}

test("simulate: a missing field blocks Run and is named; the result puts the caveats first, labels every status and downloads", async ({
  page,
}, testInfo) => {
  const label = "light load 2 % richer";
  const result = buildSimResult();
  const api = await open(page, {
    library: [],
    uploads: { [TUNED_FILE]: { ...ENTRY_TUNED, created: true } },
    simulate: result,
  });
  await openAndEdit(page, label);

  const form = page.getByRole("form", { name: "Simulate form" });
  const run = form.getByRole("button", { name: "Run simulation" });
  // Nothing is defaulted: a fresh form cannot run, and says what it needs.
  await expect(run).toBeDisabled();
  await expect(form.getByRole("radio", { checked: true })).toHaveCount(0);
  // The edges are suggested from the base's Fuel Comp breakpoints, shown with the grid size.
  await expect(form.getByLabel("RPM edges (rpm)")).not.toHaveValue("");
  await expect(form.getByLabel("Grid size")).toContainText("of at most");

  // Everything but Baro: Run stays blocked and names exactly that field.
  await stateForm(form, ["Baro (kPa)"]);
  await expect(run).toBeDisabled();
  const blocks = form.getByRole("status", { name: "What blocks Run" });
  await expect(blocks).toHaveText("Run needs: Baro (kPa).");
  await form.getByLabel("Baro (kPa)", { exact: true }).fill("80");
  await expect(blocks).toHaveCount(0);
  await expect(run).toBeEnabled();

  await run.click();
  const results = page.getByRole("region", { name: "Simulation result" });
  await expect(results).toBeVisible();

  // The request states all 23 fields, the cells and scale as selected.
  const [sent] = api.posted("/simulate") as RomSimRequest[];
  expect(sent).toEqual({
    ...buildRequest(),
    label,
    edits: [
      {
        table: "fuel_compensation",
        slot: 1,
        cells: [
          [1, 1],
          [1, 2],
          [1, 3],
          [2, 1],
          [2, 2],
          [2, 3],
        ],
        scale: 1.02,
        rounding: "nearest",
      },
    ],
  });

  // The caveat block is the first thing in the result, before any map, table or status cell.
  const caveats = results.getByRole("region", { name: "Read this first" });
  await expect(caveats).toContainText("The plant is not qualified.");
  await expect(caveats).toContainText("structural_error_not_sampled");
  await expect(caveats).toContainText("parameter_uncertainty_only");
  const before = await caveats.evaluate((element) => {
    const root = element.closest('[role="region"][aria-label="Simulation result"]');
    return [...(root?.querySelectorAll("table, figure, [data-status]") ?? [])].filter(
      (other) => element.compareDocumentPosition(other) & Node.DOCUMENT_POSITION_PRECEDING,
    ).length;
  });
  expect(before).toBe(0);
  // ...and there is a map after it for it to precede.
  const firstMap = await caveats.evaluate((element) => {
    const figure = element
      .closest('[role="region"][aria-label="Simulation result"]')
      ?.querySelector("figure");
    return (
      !!figure && !!(element.compareDocumentPosition(figure) & Node.DOCUMENT_POSITION_FOLLOWING)
    );
  });
  expect(firstMap).toBe(true);

  // Every status has its own label.
  const map = results.getByRole("table", { name: /^Map: relative Δλ p50, open_loop pairs/ });
  await expect(map).toBeVisible();
  for (const status of STATUSES) {
    expect(await map.locator(`[data-status="${status}"]`).count(), status).toBeGreaterThan(0);
  }
  await expect(map.getByText("-1.52", { exact: true })).toHaveCount(2); // changed
  await expect(map.getByText("0", { exact: true })).toHaveCount(1); // unchanged
  await expect(map.getByText("U", { exact: true })).toBeVisible(); // unsupported
  await expect(map.getByText("P+0.40", { exact: true })).toBeVisible(); // partly supported
  await expect(map.getByText("–", { exact: true })).toHaveCount(2); // no points
  await expect(map.getByText("NR", { exact: true })).toBeVisible(); // not reached
  await expect(map.getByText("CL", { exact: true }).first()).toBeVisible(); // other loop
  await expect(map.getByText("∅", { exact: true })).toBeVisible(); // relative change undefined
  await page.screenshot({ path: testInfo.outputPath("rom-editor-result.png"), fullPage: true });

  // Download result JSON: the response, as a file named for the run.
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    results.getByRole("button", { name: "Download result JSON" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe("light_load_2_richer.sim-result.json");
  expect(JSON.parse(await readFile(await download.path(), "utf8"))).toEqual(result);
  await expect(page.getByRole("link", { name: /image|candidate|\.bin|osirisrom/i })).toHaveCount(0);
  expect(api.unexpected).toEqual([]);
});

test("timing trims: edit trim B on its value bits with the flag kept, state the latch and knock control, and the result shows the change per cell on the trims' own axes", async ({
  page,
}, testInfo) => {
  const label = "trim B -3 counts";
  const api = await open(page, {
    library: [],
    uploads: { [TUNED_FILE]: { ...ENTRY_TUNED, created: true } },
    simulate: trimSimResult("trim_b"),
  });
  await chooseFile(page, TUNED_FILE);
  await expect(page.getByLabel("Loaded ROM")).toBeVisible();
  await page.getByRole("button", { name: "Ignition timing trim B (Map 1)" }).click();
  const grid = page.getByRole("grid", { name: "Ignition timing trim B (Map 1) cells" });
  await expect(grid.getByRole("gridcell")).toHaveCount(256);
  await expect(page.getByRole("note").filter({ hasText: "decode mask 0x7F" })).toBeVisible();
  // Column 10 is in the flag block: the grid shows the value, the raw count is on hover.
  await expect(cellAt(grid, 4, 9)).toContainText("47");
  await expect(cellAt(grid, 4, 9)).toHaveAttribute("title", "raw 175");
  await cellAt(grid, 4, 9).click();
  const operation = page.getByRole("form", { name: "Operation" });
  await operation.getByLabel("Operation").selectOption("add_raw");
  await operation.getByLabel("Raw counts to add").fill("-3");
  await operation.getByRole("button", { name: "Apply to selection" }).click();
  await expect(page.getByRole("list", { name: "Pending edits" })).toContainText(
    "Ignition timing trim B: add raw -3 on 1 cell",
  );
  await page.getByLabel("Label for this edit set").fill(label);
  await page.getByRole("button", { name: "Preview", exact: true }).click();

  const [previewed] = api.posted("/edits/preview") as RomEditPreviewRequest[];
  expect(previewed).toEqual({
    label,
    edits: [{ table: "timing_trim_b", slot: 1, cells: [[4, 9]], add_raw: -3 }],
  });
  // The value bits moved 47 -> 44 and bit 7 stayed set: raw 175 -> 172.
  const after = page.getByRole("table", { name: "Ignition timing trim B (Map 1) after the edits" });
  await expect(afterCell(after, 4, 9)).toHaveAttribute("title", "raw 172 (was 175)");
  await expect(afterCell(after, 4, 9)).toHaveAttribute(
    "aria-label",
    "Row 5, column 10: 44 (changed)",
  );
  const levers = page.getByRole("region", { name: "What this edit can and cannot show" });
  await expect(levers.getByRole("listitem")).toHaveText(LEVERS_TIMING_TRIM.messages);

  // Simulate: nothing is preselected, and the two ECU states are named while missing.
  const form = page.getByRole("form", { name: "Simulate form" });
  const run = form.getByRole("button", { name: "Run simulation" });
  await expect(form.getByRole("group", { name: "HI DET latch" }).getByRole("radio")).toHaveCount(2);
  for (const radio of await form.getByRole("radio").all()) await expect(radio).not.toBeChecked();
  await stateForm(form);
  await expect(run).toBeEnabled();
  await run.click();
  const results = page.getByRole("region", { name: "Simulation result" });
  await expect(results).toBeVisible();
  const [sent] = api.posted("/simulate") as RomSimRequest[];
  expect(Object.keys(sent)).toHaveLength(23);
  expect(sent).toMatchObject({
    edits: [{ table: "timing_trim_b", slot: 1, cells: [[4, 9]], add_raw: -3 }],
    high_det: "set",
    knock_control: "fallback",
  });

  const timing = results.getByRole("region", { name: "Ignition timing" });
  await expect(timing).toContainText("HI DET latch set (warm timing reads trim B)");
  const changes = timing.getByRole("table", { name: "Ignition timing change per trim cell" });
  const cell = changes.getByRole("cell", { name: /^Row 5, column 10: -3/ });
  await expect(cell).toHaveAttribute("data-timing-mark", "changed");
  await expect(changes.locator('[data-timing-mark="changed"]')).toHaveCount(2);
  await expect(changes.getByRole("rowheader", { name: "2,000" })).toBeVisible();
  await expect(changes.getByRole("columnheader", { name: "80" })).toBeVisible();
  await expect(timing).toContainText("unsupported: spark_response_unmeasured");
  await page.screenshot({ path: testInfo.outputPath("rom-editor-timing.png"), fullPage: true });
  expect(api.unexpected).toEqual([]);
});

test("simulate: the Coarse preset fills the edges and draws visibly, and the result carries the coarse-run banner", async ({
  page,
}) => {
  const api = await open(page, {
    library: [],
    uploads: { [TUNED_FILE]: { ...ENTRY_TUNED, created: true } },
    simulate: buildSimResult(),
  });
  await openAndEdit(page, "light load 2 % richer");
  const form = page.getByRole("form", { name: "Simulate form" });
  // Nothing pressed, nothing remembered: no preset, no draws.
  await expect(form.getByRole("status", { name: "Preset state" })).toContainText(
    "No preset pressed",
  );
  await expect(form.getByLabel("Draws", { exact: true })).toHaveValue("");

  await form.getByRole("button", { name: "Coarse (quick check)" }).click();
  await expect(form.getByLabel("Draws", { exact: true })).toHaveValue("200");
  await expect(
    form
      .getByLabel("RPM edges (rpm)")
      .inputValue()
      .then((v) => v.split(", ").length),
  ).resolves.toBe(9);
  await expect(form.getByLabel("Grid size")).toContainText("8 × 8 = 64");
  await expect(form.getByRole("status", { name: "Preset state" })).toContainText(
    "filled RPM edges (9 edges, 8 cells)",
  );
  await expect(form.getByRole("group", { name: "Cost estimate" })).toContainText(
    "64 points × 200 draws",
  );
  // Editing a filled field makes it Custom.
  await form.getByLabel("Draws", { exact: true }).fill("300");
  await expect(form.getByRole("status", { name: "Preset state" })).toContainText("Custom");
  await form.getByLabel("Draws", { exact: true }).fill("200");

  await stateForm(form, ["RPM edges (rpm)", "Load edges (% fill)", "Draws"]);
  await form.getByRole("button", { name: "Run simulation" }).click();
  const results = page.getByRole("region", { name: "Simulation result" });
  await expect(results).toBeVisible();
  const banner = results.getByRole("note", { name: "Coarse run" });
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("200 draws");
  await expect(banner).toContainText("not unchanged");
  const [sent] = api.posted("/simulate") as RomSimRequest[];
  expect(sent.draws).toBe(200);
  expect(sent.rpm_edges).toHaveLength(9);
  expect(Object.keys(sent)).toHaveLength(23);
  expect(api.unexpected).toEqual([]);
});

/** The number fields "Fill typical values" fills on the Xterra, beside Baro and the rail
 *  (the last two are its placeholders). */
const TYPICAL_FILLED = [
  "IAT (°C)",
  "Coolant (°C)",
  "Battery (V)",
  "Throttle (%)",
  "Pedal flag (0-255)",
  "STFT (%)",
  "LTFT (%)",
  "EVAP purge area (counts)",
  "Throttle closed learned (counts)",
  "Idle air learned (counts)",
];

test("simulate: Fill typical values fills the stated environment with its sources, tags the two placeholders as such, leaves the choices to the operator, and a run sends 23 fields", async ({
  page,
}) => {
  const api = await open(page, {
    library: [],
    uploads: { [TUNED_FILE]: { ...ENTRY_TUNED, created: true } },
    simulate: buildSimResult(),
  });
  await openAndEdit(page, "light load 2 % richer");
  const form = page.getByRole("form", { name: "Simulate form" });
  const tags = form.locator("[data-typical]");
  const rpmEdges = await form.getByLabel("RPM edges (rpm)").inputValue();
  // Nothing is fetched or filled until the button is pressed.
  await expect(tags).toHaveCount(0);
  expect(api.calls.some((call) => call.path.endsWith("/typical-values"))).toBe(false);

  await form.getByRole("button", { name: "Fill typical values" }).click();
  const report = form.getByRole("status", { name: "Typical values state" });
  await expect(report).toContainText(
    "Filled 12 values (10 typical, 2 placeholders) from nissan-xterra-2008",
  );
  // Twelve values in thirteen fields (the rail is a choice and a number), each tagged with
  // its kind and source: eleven `typical`, two `placeholder`.
  await expect(tags).toHaveCount(13);
  await expect(tags.first()).toContainText("typical: E07.6's stated point");
  const placeholders = form.locator('[data-typical-kind="placeholder"]');
  await expect(placeholders).toHaveCount(2);
  await expect(placeholders.first()).toContainText("placeholder: PLACEHOLDER, not a measurement");
  await expect(form.getByLabel("Baro (kPa)", { exact: true })).toHaveValue("81");
  await expect(form.getByLabel("Pedal flag (0-255)", { exact: true })).toHaveValue("0");
  await expect(form.getByLabel("Rail pressure", { exact: true })).toHaveValue("stated");
  await expect(form.getByLabel("Rail pressure (kPa)", { exact: true })).toHaveValue("350");
  // The placeholders are filled and named as placeholders, and nothing is left empty.
  await expect(form.getByLabel("Throttle closed learned (counts)", { exact: true })).toHaveValue(
    "0",
  );
  await expect(report).toContainText("2 of them are placeholders, not measurements");
  await expect(report).not.toContainText("Left empty for you to state");
  await expect(report).toContainText("Pedal flag 0: bit 0 is clear");
  // The experiment and the run are untouched.
  for (const radio of await form.getByRole("radio").all()) await expect(radio).not.toBeChecked();
  await expect(form.getByLabel("RPM edges (rpm)")).toHaveValue(rpmEdges);
  await expect(form.getByLabel("Draws", { exact: true })).toHaveValue("");
  await expect(form.getByLabel("Seed", { exact: true })).toHaveValue("");
  await expect(form.getByRole("button", { name: "Run simulation" })).toBeDisabled();

  // Editing a filled field is the operator's: its tag goes, and it is what is sent.
  await form.getByLabel("Baro (kPa)", { exact: true }).fill("78");
  await expect(tags).toHaveCount(12);
  await stateForm(form, ["Baro (kPa)", ...TYPICAL_FILLED]);
  await form.getByLabel("Rail pressure", { exact: true }).selectOption("stated");
  await form.getByLabel("Rail pressure (kPa)", { exact: true }).fill("350");
  await form.getByRole("button", { name: "Run simulation" }).click();
  await expect(page.getByRole("region", { name: "Simulation result" })).toBeVisible();
  const [sent] = api.posted("/simulate") as RomSimRequest[];
  expect(Object.keys(sent)).toHaveLength(23);
  expect(sent).toMatchObject({
    baro_kpa: 78,
    rail_kpa: 350,
    pedal_flag: 0,
    ltft_pct: 2.34,
    throttle_closed_learned: 0,
    idle_air_learned: 0,
  });
  expect(api.calls.filter((call) => call.path.endsWith("/typical-values"))).toHaveLength(1);
  expect(api.unexpected).toEqual([]);
});

test.describe("at phone width (412 x 915)", () => {
  test.use({ viewport: { width: 412, height: 915 }, hasTouch: true });

  test("the preview's levers, the Simulate form after Fill typical values, and a result have no horizontal page scroll", async ({
    page,
  }) => {
    await open(page, {
      library: [],
      uploads: { [TUNED_FILE]: { ...ENTRY_TUNED, created: true } },
      // A trim edit's result, so the 16-column timing grid is on screen at this width.
      simulate: trimSimResult("trim_b"),
    });
    // Everything the page shows is checked at each stage: nothing may poke past the right edge
    // outside a box that scrolls sideways on purpose.
    const noSidewaysPage = async (stage: string) => {
      const report = await overflowReport(page);
      expect(report.documentWidth, `${stage}: document width`).toBeLessThanOrEqual(report.viewport);
      expect(report.offenders, `${stage}: elements past the right edge`).toEqual([]);
    };
    await openAndEdit(page, "light load 2 % richer");
    await page.getByRole("button", { name: "Preview", exact: true }).click();
    await expect(page.getByRole("region", { name: "Preview result" })).toBeVisible();
    await expect(
      page.getByRole("region", { name: "What this edit can and cannot show" }),
    ).toBeVisible();
    await noSidewaysPage("preview with levers");

    const form = page.getByRole("form", { name: "Simulate form" });
    await form.scrollIntoViewIfNeeded();
    await form.getByRole("button", { name: "Fill typical values" }).click();
    await expect(form.getByRole("status", { name: "Typical values state" })).toContainText(
      "Filled 12 values",
    );
    await expect(form.locator("[data-typical]")).toHaveCount(13);
    await noSidewaysPage("Simulate form after Fill typical values");

    await form.getByRole("radio", { name: /Fuel Comp axes/ }).check();
    await form.getByLabel("Rail pressure", { exact: true }).selectOption("working_assumption");
    await form.getByRole("radio", { name: /Fresh flash resets trims/ }).check();
    await stateTiming(form);
    await noSidewaysPage("Simulate form with the ignition timing state chosen");
    await form.getByLabel("RPM edges (rpm)").fill("200, 600, 1000, 1400");
    await form.getByLabel("Load edges (% fill)").fill("3.5, 10.7, 17.8, 25, 32.2");
    await form.getByLabel("Draws", { exact: true }).fill("200");
    await form.getByLabel("Seed", { exact: true }).fill("20261015");
    await form.getByRole("button", { name: "Run simulation" }).click();
    await expect(page.getByRole("region", { name: "Simulation result" })).toBeVisible();
    await noSidewaysPage("simulation result");
    // The ignition timing section: its facts wrap like the other result lists, and the
    // 16-column grid scrolls inside its own box instead of widening the page.
    const timing = page.getByRole("region", { name: "Ignition timing" });
    await timing.scrollIntoViewIfNeeded();
    await expect(timing.getByRole("table", { name: /per trim cell/ })).toBeVisible();
    const scroller = timing.locator("[data-table-scroll]").first();
    expect(await scroller.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(true);
    await noSidewaysPage("simulation result with the ignition timing grid");
  });
});

test("an unrecorded base is preview only: Simulate is off and gives the reason", async ({
  page,
}) => {
  const api = await open(page, {
    library: [ENTRY_TUNED],
    uploads: { "tuner-base.bin": { ...ENTRY_UNRECORDED, created: true } },
  });
  await chooseFile(page, "tuner-base.bin");
  const loaded = page.getByLabel("Loaded ROM");
  await expect(loaded).toContainText("tuner-base.bin");
  await expect(loaded.getByText("preview only")).toBeVisible();
  await expect(loaded.getByText("can simulate")).toHaveCount(0);

  const form = page.getByRole("form", { name: "Simulate form" });
  await expect(form.getByRole("note")).toContainText(
    "Simulate is unavailable: base_not_a_recorded_revision.",
  );
  await expect(form.getByRole("button", { name: "Run simulation" })).toBeDisabled();
  await expect(form.getByLabel("Baro (kPa)", { exact: true })).toBeDisabled();

  // It still previews: preview-only is not unusable, and the reason survives it.
  await page.getByRole("button", { name: "Fuel Compensation (Map 1)" }).click();
  const grid = page.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" });
  await cellAt(grid, 0, 0).click();
  const operation = page.getByRole("form", { name: "Operation" });
  await operation.getByLabel("Operation").selectOption("scale");
  await operation.getByLabel("Scale (%)").fill("2");
  await operation.getByLabel("Rounding").selectOption("nearest");
  await operation.getByRole("button", { name: "Apply to selection" }).click();
  await page.getByLabel("Label for this edit set").fill("tuner base probe");
  await page.getByRole("button", { name: "Preview", exact: true }).click();
  await expect(page.getByRole("button", { name: "Download spec" })).toBeVisible();
  await expect(form.getByRole("note")).toContainText("base_not_a_recorded_revision");
  await expect(form.getByRole("button", { name: "Run simulation" })).toBeDisabled();
  expect(api.posted("/simulate")).toEqual([]);
  expect(api.unexpected).toEqual([]);
});

test("UpRev's encrypted backup is refused by name and nothing is loaded", async ({ page }) => {
  const detail =
    "UpRev encrypted backup (1,048,592 bytes, starts 32bec582): it cannot be decoded. Upload the .OsirisROM or a plain 1 MiB image instead.";
  const api = await open(page, {
    library: [ENTRY_TUNED],
    uploads: { "Xterra-backup.bin": { status: 422, body: { detail } } },
  });
  await chooseFile(page, "Xterra-backup.bin");

  await expect(page.getByText(`Xterra-backup.bin: ${detail}`, { exact: true })).toBeVisible();
  await expect(page.getByLabel("Loaded ROM")).toHaveCount(0);
  await expect(page.getByRole("grid")).toHaveCount(0);
  await expect(page.getByRole("form", { name: "Operation" })).toHaveCount(0);
  expect(new URL(page.url()).searchParams.has("rom")).toBe(false);
  // The page is still the unloaded one: the primary button, and the stored image to fall back on.
  await expect(page.getByRole("button", { name: "Open ROM…" })).toBeEnabled();
  await expect(page.getByRole("table", { name: "Base images" })).toContainText("Running tune");
  expect(api.calls.filter((call) => call.path.endsWith("/tables"))).toEqual([]);
  expect(api.unexpected).toEqual([]);
});
