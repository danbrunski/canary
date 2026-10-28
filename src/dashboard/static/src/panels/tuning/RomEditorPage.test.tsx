import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlBlob, controlJson } from "../../api/core";
import {
  ENTRY_K106,
  ENTRY_TUNED,
  ENTRY_UNRECORDED,
  SHA_K106,
  SHA_TUNED,
  SHA_UNRECORDED,
  TABLES_WITH_FUEL_COMP,
  fuelCompTable,
} from "../../test/romLibraryFixtures";
import {
  LEVERS_FUEL_COMP,
  LEVERS_MAF_CURVE,
  LEVERS_TIMING_TRIM,
} from "../../test/romLeverFixtures";
import {
  chooseRecent,
  findTreeTable,
  recentEntry,
  showRomView,
  treeContents,
  treeTable,
} from "../../test/romEditorViews";
import type { RomEditPreview, RomEditPreviewRequest, RomLevers } from "../../types/romEditor";
import { downloadText } from "../../util/browser";
import { Workbench, type DashboardData } from "../Workbench";
import { RomEditorPage } from "./RomEditorPage";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
  controlBlob: vi.fn(),
}));
vi.mock("../../util/browser", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../util/browser")>()),
  downloadText: vi.fn(),
  downloadBlob: vi.fn(),
}));
const control = vi.mocked(controlJson);
const blob = vi.mocked(controlBlob);
const save = vi.mocked(downloadText);

/** `@testing-library/user-event` is not a dependency: these are the three
 *  gestures the page needs, as plain events. */
const userEvent = {
  click: async (element: HTMLElement) => {
    await act(async () => {
      fireEvent.click(element);
    });
  },
  type: async (element: HTMLElement, text: string) => {
    fireEvent.change(element, { target: { value: text } });
  },
  selectOptions: async (element: HTMLElement, value: string) => {
    fireEvent.change(element, { target: { value } });
  },
};

const bodyOf = (init?: RequestInit) => (typeof init?.body === "string" ? init.body : "");

const PREVIEW_PATH = (sha: string) => `/control/rom/library/${sha}/edits/preview`;
const SPEC_YAML = "schema_version: 1\nbase: {revision: ZS44A-20260920}\nlabel: idle richer\n";

/** What the server would answer for `scale 1.02 nearest` on Fuel Comp cells
 *  (0,0) and (0,1): raw 120 -> 122, 121 -> 123. */
function previewFor(request: RomEditPreviewRequest): RomEditPreview {
  const after = fuelCompTable(1);
  after.raw = after.raw.map((row) => [...row]);
  after.raw[0][0] = 122;
  after.raw[0][1] = 123;
  after.decoded = after.raw.map((row) => row.map((value) => value * 0.78125));
  return {
    tool: "rom_edit_spec",
    tool_version: "1",
    spec: { label: request.label },
    spec_yaml: SPEC_YAML,
    spec_sha256: "5".repeat(64),
    candidate_sha256: "c".repeat(64),
    provenance: {
      kind: "simulation_candidate",
      flashable: false,
      label: request.label,
      base_sha256: SHA_TUNED,
      candidate_sha256: "c".repeat(64),
      spec_sha256: "5".repeat(64),
      cells: [
        {
          table: "fuel_compensation",
          slot: 1,
          row: 0,
          col: 0,
          address: 0xe0660,
          old: 120,
          new: 122,
        },
        {
          table: "fuel_compensation",
          slot: 1,
          row: 0,
          col: 1,
          address: 0xe0661,
          old: 121,
          new: 123,
        },
      ],
      ranges: [],
      effect: {
        outcome: "traced",
        slot: 1,
        slot_source: "default_slot",
        changed_bytes: 2,
        changed_nodes: ["fuel_compensation_lookup"],
        moved_lookups: [],
      },
    },
    tables: [after],
    levers: LEVERS_FUEL_COMP,
    sim_base: { ok: true, status: "matched", revision: "ZS44A-20260920", reason: null },
  };
}

function route(
  preview: (init?: RequestInit) => unknown = (init) => previewFor(JSON.parse(bodyOf(init))),
  extra: Record<string, unknown> = {},
) {
  const table: Record<string, unknown> = {
    "GET /control/rom/library": { entries: [ENTRY_TUNED, ENTRY_K106] },
    [`GET /control/rom/library/${SHA_TUNED}/tables`]: TABLES_WITH_FUEL_COMP,
    [`GET /control/rom/library/${SHA_K106}/tables`]: { ...TABLES_WITH_FUEL_COMP, sha256: SHA_K106 },
    [`POST ${PREVIEW_PATH(SHA_TUNED)}`]: preview,
    [`POST ${PREVIEW_PATH(SHA_K106)}`]: preview,
    [`GET /control/rom/library/${SHA_UNRECORDED}/tables`]: {
      ...TABLES_WITH_FUEL_COMP,
      sha256: SHA_UNRECORDED,
    },
    ...extra,
  };
  control.mockImplementation((path: string, _token: string, init?: RequestInit) => {
    const key = `${init?.method ?? "GET"} ${path}`;
    if (!(key in table)) return Promise.reject(new Error(`unexpected ${key}`));
    let value = table[key];
    if (typeof value === "function") value = (value as (init?: RequestInit) => unknown)(init);
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value as never);
  });
}

afterEach(() => {
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

/** The editor starts unloaded; a `?rom=<sha>` link (a reload, the desk
 *  launcher) is what loads a base at start. Most tests start from one. */
function renderEditor(sha: string | null = SHA_TUNED) {
  if (sha && !new URLSearchParams(window.location.search).get("rom")) {
    window.history.replaceState(null, "", `/?rom=${sha}`);
  }
  return render(<RomEditorPage token="t" status={null} />);
}

function cell(grid: HTMLElement, row: number, col: number) {
  return within(grid).getByRole("gridcell", { name: new RegExp(`^Row ${row}, column ${col}:`) });
}

async function openFuelComp() {
  renderEditor();
  fireEvent.click(await findTreeTable("Fuel Compensation (Map 1)"));
  return screen.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" });
}

/** Select cells (1,1)-(1,2), scale 2 % with nearest rounding, apply. */
async function applyScale(grid: HTMLElement) {
  fireEvent.mouseDown(cell(grid, 1, 1));
  fireEvent.mouseDown(cell(grid, 1, 2), { shiftKey: true });
  fireEvent.mouseUp(window);
  const operation = screen.getByRole("form", { name: "Operation" });
  await userEvent.selectOptions(within(operation).getByLabelText("Operation"), "scale");
  await userEvent.type(within(operation).getByLabelText("Scale (%)"), "2");
  await userEvent.selectOptions(within(operation).getByLabelText("Rounding"), "nearest");
  await userEvent.click(within(operation).getByRole("button", { name: "Apply to selection" }));
}

describe("ROM Editor page: base and table (W09.3)", () => {
  it("lists each image with its revision and whether it can be simulated; choosing one loads it and keeps the sha in the URL", async () => {
    route();
    renderEditor(null);
    const images = await screen.findByRole("table", { name: "Base images" });
    const rows = within(images).getAllByRole("row");
    expect(within(rows[1]).getByText("ZS44A-20260920")).toBeVisible();
    expect(within(rows[1]).getByText("can simulate")).toBeVisible();
    expect(within(rows[2]).getByText("unrecognised")).toBeVisible();
    expect(within(rows[2]).getByText("preview only")).toBeVisible();
    // Nothing was chosen, so nothing is loaded or in the URL.
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
    expect(new URLSearchParams(window.location.search).get("rom")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: /Edit from k106\.bin/ }));
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("rom")).toBe(SHA_K106),
    );
    // The Simulate slot says why it is off for an image that is not a revision.
    await showRomView("Simulate");
    expect(
      await screen.findByText(/Simulate is unavailable: base_not_a_recorded_revision/),
    ).toBeVisible();
  });

  it("opens the image the URL names", async () => {
    route();
    window.history.replaceState(null, "", `/?rom=${SHA_K106}`);
    renderEditor();
    await screen.findByRole("grid");
    expect(within(screen.getByLabelText("Loaded ROM")).getByText("k106.bin")).toBeVisible();
    expect(recentEntry(/Edit from k106\.bin/)).toHaveAttribute("aria-pressed", "true");
  });

  it("offers Fuel Targets, Fuel Compensation and K, a Map 2 copy in its family, with the Fuel Comp axis note", async () => {
    route();
    renderEditor();
    await screen.findByRole("navigation", { name: "Tables" });
    // Fuel Targets is chosen, so its family is open; Fuel Compensation's is not.
    expect(treeContents()).toEqual([
      "Fuel, 3 tables",
      "Fuel Targets, 2 maps",
      "Fuel Targets (Map 1)",
      "Fuel Targets (Map 2)",
      "K (injector constant) (Map 1)",
      "Fuel Compensation, 2 maps",
    ]);
    await userEvent.click(treeTable("Fuel Compensation (Map 1)"));
    expect(screen.getByRole("note", { name: "Fuel Compensation editor labels" })).toHaveTextContent(
      /UpRev labels these axes differently/,
    );
  });

  it("is mounted on the Workbench in place of the placeholder", async () => {
    route();
    const data = { token: "t", status: null } as unknown as DashboardData;
    render(<Workbench active="tuning-rom-editor" onNavigate={() => {}} data={data} />);
    expect(await screen.findByRole("heading", { name: "ROM Editor" })).toBeVisible();
    expect(screen.queryByText(/reserved for a later implementation/)).not.toBeInTheDocument();
  });
});

describe("ROM Editor page: selecting cells", () => {
  it("selects by click, shift-click and drag", async () => {
    route();
    const grid = await openFuelComp();
    fireEvent.mouseDown(cell(grid, 2, 2));
    expect(cell(grid, 2, 2)).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Selected: 1 cell (row 2, column 2).")).toBeVisible();

    fireEvent.mouseDown(cell(grid, 3, 4), { shiftKey: true });
    expect(screen.getByText("Selected: 6 cells (row 2–3, column 2–4).")).toBeVisible();
    expect(cell(grid, 3, 3)).toHaveAttribute("aria-selected", "true");
    expect(cell(grid, 4, 4)).toHaveAttribute("aria-selected", "false");

    fireEvent.mouseUp(window);
    fireEvent.mouseDown(cell(grid, 5, 5), { buttons: 1 });
    fireEvent.mouseEnter(cell(grid, 6, 6), { buttons: 1 });
    fireEvent.mouseUp(window);
    fireEvent.mouseEnter(cell(grid, 8, 8));
    expect(screen.getByText("Selected: 4 cells (row 5–6, column 5–6).")).toBeVisible();
  });

  it("never extends a drag unless the primary button is down, and focuses without scrolling", async () => {
    route();
    const grid = await openFuelComp();
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    try {
      fireEvent.mouseDown(cell(grid, 2, 2), { buttons: 1 });
      expect(focus).toHaveBeenCalledWith({ preventScroll: true });
      // The page scrolled under the pointer, or the mouseup was missed: other
      // cells enter with no button held. That must not extend the selection...
      fireEvent.mouseEnter(cell(grid, 6, 2), { buttons: 0 });
      expect(screen.getByText("Selected: 1 cell (row 2, column 2).")).toBeVisible();
      // ...and it ends the drag for good: a later enter with the button
      // reported down again does not resume it.
      fireEvent.mouseEnter(cell(grid, 7, 2), { buttons: 1 });
      expect(screen.getByText("Selected: 1 cell (row 2, column 2).")).toBeVisible();
      // A real drag (button held, no mouseup) extends.
      fireEvent.mouseDown(cell(grid, 3, 3), { buttons: 1 });
      fireEvent.mouseEnter(cell(grid, 4, 4), { buttons: 1 });
      expect(screen.getByText("Selected: 4 cells (row 3–4, column 3–4).")).toBeVisible();
      // Releasing off the table (outside any cell) ends it too.
      fireEvent.mouseUp(window);
      fireEvent.mouseEnter(cell(grid, 9, 9), { buttons: 1 });
      expect(screen.getByText("Selected: 4 cells (row 3–4, column 3–4).")).toBeVisible();
    } finally {
      focus.mockRestore();
    }
  });

  it("moves with arrows and extends with shift+arrows", async () => {
    route();
    const grid = await openFuelComp();
    fireEvent.keyDown(grid, { key: "ArrowDown" });
    expect(cell(grid, 1, 1)).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(grid, { key: "ArrowRight" });
    expect(screen.getByText("Selected: 1 cell (row 1, column 2).")).toBeVisible();
    fireEvent.keyDown(grid, { key: "ArrowDown", shiftKey: true });
    fireEvent.keyDown(grid, { key: "ArrowRight", shiftKey: true });
    expect(screen.getByText("Selected: 4 cells (row 1–2, column 2–3).")).toBeVisible();
    fireEvent.keyDown(grid, { key: "Escape" });
    expect(screen.getByText("Select cells in the table first.")).toBeVisible();
  });
});

describe("ROM Editor page: operation, pending edits and preview", () => {
  it("select, scale 2 % nearest, preview: sends the exact cells and shows the after values", async () => {
    route();
    const grid = await openFuelComp();
    await applyScale(grid);

    // Pending cells are marked before any preview.
    expect(cell(grid, 1, 1)).toHaveAttribute("data-pending", "true");
    // Applying stays on Tables; the view's label counts the edit.
    expect(screen.getByRole("button", { name: "Edits & Preview (1)" })).toBeVisible();
    await showRomView("Edits & Preview");
    const list = screen.getByRole("list", { name: "Pending edits" });
    expect(within(list).getByText(/scale raw ×1\.02 \(\+2 %, nearest\) on 2 cells/)).toBeVisible();

    // No label, no preview.
    expect(screen.getByRole("button", { name: "Preview" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Label for this edit set"), "idle richer");
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));

    const result = await screen.findByRole("region", { name: "Preview result" });
    const call = control.mock.calls.find(([path]) => path === PREVIEW_PATH(SHA_TUNED));
    expect(call?.[2]?.method).toBe("POST");
    expect(JSON.parse(bodyOf(call?.[2]))).toEqual({
      label: "idle richer",
      edits: [
        {
          table: "fuel_compensation",
          slot: 1,
          cells: [
            [0, 0],
            [0, 1],
          ],
          scale: 1.02,
          rounding: "nearest",
        },
      ],
    });

    // After table: the two changed cells are outlined, with their change in percent.
    const after = within(result).getByRole("table", {
      name: "Fuel Compensation (Map 1) after the edits",
    });
    const changed = within(after).getByRole("cell", {
      name: /^Row 1, column 1: 95\.31 \(changed\)/,
    });
    expect(changed).toHaveAttribute("data-changed", "true");
    expect(changed).toHaveTextContent("95.31");
    expect(changed).toHaveTextContent("+1.7 %");
    expect(within(after).getByRole("cell", { name: /^Row 1, column 3:/ })).not.toHaveAttribute(
      "data-changed",
    );

    // The cell list: raw and decoded, old -> new.
    const rows = within(
      within(result).getByRole("table", { name: "Changed cells list" }),
    ).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("row 1, col 1");
    expect(rows[1]).toHaveTextContent("120 → 122");
    expect(rows[1]).toHaveTextContent("93.75 → 95.31");
    // Reached outputs.
    expect(within(result).getByText("fuel_compensation_lookup")).toBeVisible();
  });

  it("renders a refusal by its reason", async () => {
    route(
      () =>
        new HttpError(
          400,
          JSON.stringify({
            error: "edit_refused",
            reason: "value_out_of_range",
            message: "raw 255 x 1.02 is 260",
          }),
        ),
    );
    const grid = await openFuelComp();
    await applyScale(grid);
    await showRomView("Edits & Preview");
    await userEvent.type(screen.getByLabelText("Label for this edit set"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Refused: value_out_of_range");
    expect(alert).toHaveTextContent("raw 255 x 1.02 is 260");
    expect(screen.queryByRole("region", { name: "Preview result" })).not.toBeInTheDocument();
  });

  it("renders the daemon's real 400 body: {detail: {error, kind, reason, message}}", async () => {
    route(
      () =>
        new HttpError(
          400,
          JSON.stringify({
            detail: {
              error: "edit_refused",
              kind: "edit_spec",
              reason: "no_op_edit",
              message: "edit 0 leaves every selected cell at its old value: (0, 0)",
            },
          }),
        ),
    );
    const grid = await openFuelComp();
    await applyScale(grid);
    await showRomView("Edits & Preview");
    await userEvent.type(screen.getByLabelText("Label for this edit set"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Refused: no_op_edit");
    expect(alert).toHaveTextContent("leaves every selected cell at its old value");
  });

  it("refuses in the browser a value whose raw is outside u8, and sends nothing", async () => {
    route();
    renderEditor();
    const grid = await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    fireEvent.mouseDown(cell(grid, 1, 1));
    const operation = screen.getByRole("form", { name: "Operation" });
    await userEvent.type(within(operation).getByLabelText("Value (AFR)"), "5");
    await userEvent.click(within(operation).getByRole("button", { name: "Apply to selection" }));
    expect(await within(operation).findByRole("alert")).toHaveTextContent(
      /Refused \(value_out_of_range\).*raw 376/,
    );
    // Nothing pending: the view's label carries no count.
    expect(screen.getByRole("button", { name: "Edits & Preview" })).toBeVisible();
    expect(screen.getByText("No pending edits.")).toBeInTheDocument();
    expect(control.mock.calls.some(([, , init]) => init?.method === "POST")).toBe(false);
  });

  it("shows the set-to-value chain: 12.5 AFR -> raw 151 -> 12.46 AFR", async () => {
    route();
    renderEditor();
    const grid = await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    fireEvent.mouseDown(cell(grid, 1, 1));
    const operation = screen.getByRole("form", { name: "Operation" });
    await userEvent.type(within(operation).getByLabelText("Value (AFR)"), "12.5");
    await userEvent.click(within(operation).getByRole("button", { name: "Apply to selection" }));
    await showRomView("Edits & Preview");
    expect(await screen.findByText("12.5 AFR → raw 151 → 12.46 AFR")).toBeVisible();
  });

  it("will not apply a scale without a rounding", async () => {
    route();
    const grid = await openFuelComp();
    fireEvent.mouseDown(cell(grid, 1, 1));
    const operation = screen.getByRole("form", { name: "Operation" });
    await userEvent.selectOptions(within(operation).getByLabelText("Operation"), "scale");
    await userEvent.type(within(operation).getByLabelText("Scale (%)"), "2");
    await userEvent.click(within(operation).getByRole("button", { name: "Apply to selection" }));
    expect(await within(operation).findByRole("alert")).toHaveTextContent("rounding_undeclared");
  });

  it("removes a pending edit, and refuses a second edit on the same cells", async () => {
    route();
    const grid = await openFuelComp();
    await applyScale(grid);
    // Same cells again: the second is a duplicate (E08.1 duplicate_edit).
    await userEvent.click(screen.getByRole("button", { name: "Apply to selection" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("duplicate_edit");
    expect(screen.getByRole("button", { name: "Edits & Preview (1)" })).toBeVisible();
    await showRomView("Edits & Preview");
    const list = screen.getByRole("list", { name: "Pending edits" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Remove edit 1" }));
    expect(screen.getByText("No pending edits.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Edits & Preview" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("keeps the pending edits and label per image across a reload", async () => {
    route();
    const first = renderEditor();
    fireEvent.click(await findTreeTable("Fuel Compensation (Map 1)"));
    await applyScale(screen.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" }));
    await showRomView("Edits & Preview");
    await userEvent.type(screen.getByLabelText("Label for this edit set"), "kept");
    first.unmount();

    // The reload comes back on the same view (`?view=edits`).
    renderEditor();
    const list = await screen.findByRole("list", { name: "Pending edits" });
    expect(within(list).getByText(/scale raw ×1\.02/)).toBeVisible();
    expect(screen.getByLabelText("Label for this edit set")).toHaveValue("kept");

    // Another image does not see them.
    chooseRecent(/Edit from k106\.bin/);
    await waitFor(() => expect(screen.getByText("No pending edits.")).toBeVisible());
  });
});

describe("ROM Editor page: spec download and no image", () => {
  it("downloads the spec YAML as <label>.edit-spec.yaml and offers no image or candidate", async () => {
    route();
    const grid = await openFuelComp();
    await applyScale(grid);
    await showRomView("Edits & Preview");
    await userEvent.type(screen.getByLabelText("Label for this edit set"), "idle richer");
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    await screen.findByRole("region", { name: "Preview result" });

    await userEvent.click(screen.getByRole("button", { name: "Download spec" }));
    expect(save).toHaveBeenCalledWith(
      SPEC_YAML,
      "idle_richer.edit-spec.yaml",
      "application/x-yaml",
    );

    // No image bytes are ever requested, and nothing offers them.
    expect(blob).not.toHaveBeenCalled();
    expect(control.mock.calls.some(([path]) => String(path).endsWith("/download"))).toBe(false);
    expect(screen.queryAllByRole("link")).toEqual([]);
    expect(document.querySelector("a[href], a[download]")).toBeNull();
    const downloads = screen
      .getAllByRole("button")
      .map((button) => button.textContent ?? "")
      .filter((text) => /download/i.test(text));
    expect(downloads).toEqual(["Download spec"]);
    expect(screen.queryByText(/download (the )?(image|candidate|rom)/i)).not.toBeInTheDocument();
  });
});

const LEVERS_HEADING = "What this edit can and cannot show";

/** Select, scale, label and preview, with `levers` as the daemon sent them. */
async function previewWithLevers(levers: RomLevers | null) {
  route((init) => {
    const body = previewFor(JSON.parse(bodyOf(init)));
    return levers === null ? { ...body, levers: undefined } : { ...body, levers };
  });
  const grid = await openFuelComp();
  await applyScale(grid);
  await showRomView("Edits & Preview");
  await userEvent.type(screen.getByLabelText("Label for this edit set"), "idle richer");
  await userEvent.click(screen.getByRole("button", { name: "Preview" }));
}

const shown = (block: HTMLElement) =>
  within(block)
    .getAllByRole("listitem")
    .map((item) => item.textContent);

describe("ROM Editor page: what the edit can and cannot show (E09.14b)", () => {
  it("lists the classifier's messages verbatim and in order, one per line, before Simulate", async () => {
    await previewWithLevers(LEVERS_FUEL_COMP);
    const result = await screen.findByRole("region", { name: "Preview result" });
    const block = within(result).getByRole("region", { name: LEVERS_HEADING });
    expect(within(block).getByRole("heading", { name: LEVERS_HEADING })).toBeVisible();
    // Exactly the strings, in the order the daemon sent them: not reworded,
    // grouped, filtered or counted.
    expect(LEVERS_FUEL_COMP.messages.length).toBeGreaterThan(4);
    expect(shown(block)).toEqual(LEVERS_FUEL_COMP.messages);
    // A Fuel Comp edit: the two ECU outputs get a number, the plant's wait on
    // the plant and on the closed loop (E09.14's Accept).
    expect(block).toHaveTextContent(
      "Fuel Comp -> base_fuel_schedule_word: a number can be given (for points that state only the fill byte)",
    );
    expect(block).toHaveTextContent(
      /plant not qualified: .*; closed loop: trim authority undecoded/,
    );
    // It is on the page before Simulate is offered: on Edits & Preview, ahead
    // of the Simulate view in the document, and the form is on that view.
    // (A hidden region has no accessible name, so it is found by its slot.)
    const simulate = document.querySelector<HTMLElement>('[data-slot="simulate"]');
    if (!simulate) throw new Error("no Simulate slot");
    expect(block.compareDocumentPosition(simulate) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await showRomView("Simulate");
    expect(within(simulate).getByRole("form", { name: "Simulate form" })).toBeVisible();
  });

  it("names the unvalidated torque stage for a timing trim and 'not mapped' for bytes outside the map", async () => {
    await previewWithLevers(LEVERS_TIMING_TRIM);
    let block = within(await screen.findByRole("region", { name: "Preview result" })).getByRole(
      "region",
      { name: LEVERS_HEADING },
    );
    expect(block).toHaveTextContent(
      "Timing trim A -> ignition_trim_selected: engine torque not computed in conditional_rom_comparison; torque production built, not validated",
    );
    expect(shown(block)).toEqual(LEVERS_TIMING_TRIM.messages);
    cleanup();
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");

    await previewWithLevers(LEVERS_MAF_CURVE);
    block = within(await screen.findByRole("region", { name: "Preview result" })).getByRole(
      "region",
      { name: LEVERS_HEADING },
    );
    expect(block).toHaveTextContent(/0xE27E: not mapped \(outside rom_map\.yaml\)/);
    expect(shown(block)).toEqual(LEVERS_MAF_CURVE.messages);
  });

  it("shows whatever strings it is given, unaltered", async () => {
    // Not the classifier's wording: if the page wrote any text of its own for
    // a message, or tidied this one, it would not come back equal.
    const odd = [
      "Zed -> out_b: first  (two  spaces)",
      "Alpha -> out_a: <b>markup</b> & ampersand; semicolons; and a 'quote'",
      "no change: the candidate is the baseline image",
    ];
    await previewWithLevers({ ...LEVERS_FUEL_COMP, messages: odd });
    const block = within(await screen.findByRole("region", { name: "Preview result" })).getByRole(
      "region",
      { name: LEVERS_HEADING },
    );
    expect(shown(block)).toEqual(odd);
    expect(block.querySelector("b")).toBeNull();
  });

  it("is absent only when messages is empty, and the rest of the preview is unchanged", async () => {
    // The classifier's floor is one line ("the edit reaches no output"), so an
    // empty list is not an all-clear it sent: the block is not drawn, and
    // nothing else of the preview depends on it.
    await previewWithLevers({ ...LEVERS_FUEL_COMP, messages: [] });
    const result = await screen.findByRole("region", { name: "Preview result" });
    expect(within(result).queryByRole("region", { name: LEVERS_HEADING })).not.toBeInTheDocument();
    expect(within(result).queryByText(LEVERS_HEADING)).not.toBeInTheDocument();
    expect(within(result).getByRole("button", { name: "Download spec" })).toBeVisible();
    expect(
      within(result).getByRole("table", { name: "Fuel Compensation (Map 1) after the edits" }),
    ).toBeVisible();
  });

  it("refuses a preview that carries no levers, naming the version mismatch, rather than showing an edit unclassified", async () => {
    await previewWithLevers(null);
    const alert = await screen.findByText(/response has no levers/);
    expect(alert).toHaveTextContent(/different versions/);
    expect(screen.queryByRole("region", { name: "Preview result" })).not.toBeInTheDocument();
  });

  it("still offers no image download or candidate link with the block drawn", async () => {
    await previewWithLevers(LEVERS_FUEL_COMP);
    await screen.findByRole("region", { name: LEVERS_HEADING });
    expect(blob).not.toHaveBeenCalled();
    expect(screen.queryAllByRole("link")).toEqual([]);
    expect(document.querySelector("a[href], a[download]")).toBeNull();
    const downloads = screen
      .getAllByRole("button")
      .map((button) => button.textContent ?? "")
      .filter((text) => /download/i.test(text));
    expect(downloads).toEqual(["Download spec"]);
  });
});

describe("ROM Editor page: the Simulate slot", () => {
  it("holds the Simulate form for a matched revision (W09.4) and gives the reason otherwise", async () => {
    route();
    renderEditor();
    await screen.findByRole("grid");
    await showRomView("Simulate");
    let slot = screen.getByRole("region", { name: "Simulate" });
    // A matched revision: the form is there, nothing says it is unavailable.
    expect(within(slot).getByRole("form", { name: "Simulate form" })).toBeVisible();
    expect(within(slot).queryByRole("note")).not.toBeInTheDocument();
    chooseRecent(/Edit from k106\.bin/);
    expect(await screen.findByRole("note")).toHaveTextContent(
      /Simulate is unavailable: base_not_a_recorded_revision\. The simulator needs a base recorded/,
    );
    slot = screen.getByRole("region", { name: "Simulate" });
    expect(within(slot).getByRole("button", { name: "Run simulation" })).toBeDisabled();
  });
});

/** The native file dialog is the browser's; a test hands the input a file. */
function pickFile(name: string, size = 1048576) {
  const file = new File([new Uint8Array(16)], name, { type: "application/octet-stream" });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

async function openFile(file: File) {
  await act(async () => {
    fireEvent.change(screen.getByLabelText<HTMLInputElement>("ROM file"), {
      target: { files: [file] },
    });
  });
}

const UPLOAD = (name: string) => `POST /control/rom/library?filename=${name}`;

describe("ROM Editor page: Open ROM (W09.8)", () => {
  it("with nothing stored, offers one Open ROM… button and no picker", async () => {
    route(undefined, { "GET /control/rom/library": { entries: [] } });
    renderEditor(null);
    const open = await screen.findByRole("button", { name: "Open ROM…" });
    expect(open).toBeEnabled();
    expect(screen.queryByRole("table", { name: "Base images" })).not.toBeInTheDocument();
    expect(screen.queryByText("Recent ROMs")).not.toBeInTheDocument();
    expect(screen.queryByText(/ROM Library page/)).not.toBeInTheDocument();
    // The button opens the browser's file dialog: a plain input, nothing filtered out.
    const input = screen.getByLabelText<HTMLInputElement>("ROM file");
    expect(input.type).toBe("file");
    expect(input.hasAttribute("accept")).toBe(false);
    const click = vi.spyOn(input, "click");
    fireEvent.click(open);
    expect(click).toHaveBeenCalled();
  });

  it("loads a picked file in one step: uploads it and makes the answered entry the base", async () => {
    route(undefined, {
      [UPLOAD("tuner-base.bin")]: { ...ENTRY_UNRECORDED, created: true },
    });
    renderEditor();
    await screen.findByRole("grid");
    const file = pickFile("tuner-base.bin");
    await openFile(file);
    const call = control.mock.calls.find(
      ([path]) => path === "/control/rom/library?filename=tuner-base.bin",
    );
    expect(call?.[2]).toMatchObject({ method: "POST", body: file });
    expect(new Headers(call?.[2]?.headers).get("Content-Type")).toBe("application/octet-stream");

    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("rom")).toBe(SHA_UNRECORDED),
    );
    // The header names the file and its state: preview only, with the reason on the Simulate slot.
    const loaded = await screen.findByLabelText("Loaded ROM");
    expect(within(loaded).getByText("tuner-base.bin")).toBeVisible();
    expect(within(loaded).getByText("unrecognised")).toBeVisible();
    expect(within(loaded).getByText("preview only")).toBeVisible();
    await showRomView("Simulate");
    expect(
      await screen.findByText(/Simulate is unavailable: base_not_a_recorded_revision/),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Run simulation" })).toBeDisabled();
    // Not announced as a new import step, and the control is now "another".
    expect(screen.queryByText(/already in the library/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open another ROM…" })).toBeEnabled();
    // It is also in the recent list, selected.
    expect(recentEntry(/Edit from tuner-base\.bin/)).toHaveAttribute("aria-pressed", "true");
  });

  it("a recorded revision opened from a file can simulate", async () => {
    route(undefined, { [UPLOAD("tuned.bin")]: { ...ENTRY_TUNED, created: false } });
    window.history.replaceState(null, "", `/?rom=${SHA_K106}`);
    renderEditor();
    await screen.findByRole("grid");
    await openFile(pickFile("tuned.bin"));
    const loaded = await screen.findByLabelText("Loaded ROM");
    await within(loaded).findByText("can simulate");
    expect(within(loaded).getByText("tuned.bin")).toBeVisible();
    expect(within(loaded).getByText("ZS44A-20260920")).toBeVisible();
    expect(screen.queryByText(/Simulate is unavailable/)).not.toBeInTheDocument();
  });

  it("says quietly when the file was already in the library", async () => {
    route(undefined, { [UPLOAD("again.bin")]: { ...ENTRY_K106, created: false } });
    renderEditor();
    await screen.findByRole("grid");
    await openFile(pickFile("again.bin"));
    expect(
      await screen.findByText("again.bin is already in the library; nothing new was stored."),
    ).toBeVisible();
    expect(recentEntry(/Edit from k106\.bin/)).toHaveAttribute("aria-pressed", "true");
  });

  it("shows UpRev's encrypted backup refusal by its name and keeps the current base", async () => {
    const detail =
      "UpRev encrypted backup (1,048,592 bytes, starts 32bec582): it cannot be decoded. Upload the .OsirisROM or a plain 1 MiB image instead.";
    route(undefined, {
      [UPLOAD("Xterra-backup.bin")]: new HttpError(422, JSON.stringify({ detail })),
    });
    renderEditor();
    await screen.findByRole("grid");
    await openFile(pickFile("Xterra-backup.bin", 1048592));
    expect(await screen.findByText(`Xterra-backup.bin: ${detail}`)).toBeVisible();
    expect(recentEntry(/Edit from Running tune/)).toHaveAttribute("aria-pressed", "true");
    expect(new URLSearchParams(window.location.search).get("rom")).toBe(SHA_TUNED);
    expect(screen.getByRole("grid", { name: "Fuel Targets (Map 1) cells" })).toBeVisible();
  });

  it("keeps a 413 and a 401 refusal's reason, and refuses an empty file without uploading", async () => {
    route(undefined, {
      [UPLOAD("big.bin")]: new HttpError(
        413,
        JSON.stringify({ detail: "ROM exceeds the 2 MiB upload limit" }),
      ),
      [UPLOAD("bad.bin")]: new HttpError(401, JSON.stringify({ detail: "Unauthorized" })),
    });
    renderEditor();
    await screen.findByRole("grid");
    await openFile(pickFile("big.bin"));
    expect(await screen.findByText("big.bin: ROM exceeds the 2 MiB upload limit")).toBeVisible();
    await openFile(pickFile("bad.bin"));
    expect(await screen.findByText(/^bad\.bin: /)).toBeVisible();
    expect(screen.queryByText(/^big\.bin: /)).not.toBeInTheDocument();
    await openFile(pickFile("empty.bin", 0));
    expect(screen.getByText("empty.bin is empty.")).toBeVisible();
    expect(control.mock.calls.some(([path]) => String(path).includes("empty.bin"))).toBe(false);
  });

  it("accepts a dropped file", async () => {
    route(undefined, { [UPLOAD("drop.bin")]: { ...ENTRY_UNRECORDED, created: true } });
    renderEditor();
    await screen.findByRole("grid");
    const file = pickFile("drop.bin");
    await act(async () => {
      fireEvent.drop(screen.getByRole("group", { name: "Open a ROM file" }), {
        dataTransfer: { files: [file] },
      });
    });
    await waitFor(() =>
      expect(control).toHaveBeenCalledWith(
        "/control/rom/library?filename=drop.bin",
        "t",
        expect.objectContaining({ method: "POST", body: file }),
      ),
    );
  });

  it("keeps each image's pending edits across an upload-driven switch, and leaks none", async () => {
    route(undefined, { [UPLOAD("tuner-base.bin")]: { ...ENTRY_UNRECORDED, created: true } });
    renderEditor();
    fireEvent.click(await findTreeTable("Fuel Compensation (Map 1)"));
    await applyScale(screen.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" }));
    expect(screen.getByRole("button", { name: "Edits & Preview (1)" })).toBeVisible();

    await openFile(pickFile("tuner-base.bin"));
    await within(await screen.findByLabelText("Loaded ROM")).findByText("preview only");
    await showRomView("Edits & Preview");
    await waitFor(() => expect(screen.getByText("No pending edits.")).toBeVisible());

    // Back to the first image: its edit is still there.
    chooseRecent(/Edit from Running tune/);
    const list = await screen.findByRole("list", { name: "Pending edits" });
    expect(within(list).getByText(/scale raw ×1\.02/)).toBeVisible();
  }, 30000);

  it("offers no image download after an upload", async () => {
    route(undefined, { [UPLOAD("tuner-base.bin")]: { ...ENTRY_UNRECORDED, created: true } });
    renderEditor();
    await screen.findByRole("grid");
    await openFile(pickFile("tuner-base.bin"));
    await screen.findByLabelText("Loaded ROM");
    expect(blob).not.toHaveBeenCalled();
    expect(control.mock.calls.some(([path]) => String(path).endsWith("/download"))).toBe(false);
    expect(document.querySelector("a[href], a[download]")).toBeNull();
    const downloads = screen
      .queryAllByRole("button")
      .map((button) => button.textContent ?? "")
      .filter((text) => /download/i.test(text));
    expect(downloads).toEqual([]);
  });
});

describe("ROM Editor page: starts unloaded (W09.8)", () => {
  const rowTitles = () =>
    within(screen.getByRole("table", { name: "Base images" }))
      .getAllByRole("row")
      .slice(1)
      .map((row) => within(row).getAllByRole("cell")[0]?.textContent ?? "");

  it("loads nothing at start, even with a library, and previews or simulates nothing", async () => {
    route();
    renderEditor(null);
    await screen.findByRole("table", { name: "Base images" });
    expect(screen.getByRole("button", { name: "Open ROM…" })).toBeEnabled();
    expect(screen.queryByLabelText("Loaded ROM")).not.toBeInTheDocument();
    for (const name of ["Tables", "Edit", "Pending edits", "Preview", "Simulate"]) {
      expect(screen.queryByRole("heading", { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Simulate form" })).not.toBeInTheDocument();
    expect(screen.queryAllByRole("button", { pressed: true })).toHaveLength(0);
    expect(new URLSearchParams(window.location.search).get("rom")).toBeNull();
    // Only reads: no table fetch, no POST.
    expect(control.mock.calls.map(([path]) => String(path))).toEqual(["/control/rom/library"]);
    expect(window.localStorage.length).toBe(0);
  });

  it("lists recent ROMs newest first by added_at, whatever order the API answers", async () => {
    route(undefined, {
      "GET /control/rom/library": { entries: [ENTRY_K106, ENTRY_TUNED, ENTRY_UNRECORDED] },
    });
    renderEditor(null);
    await screen.findByRole("table", { name: "Base images" });
    expect(screen.getByText("Recent ROMs")).toBeVisible();
    expect(rowTitles().map((text) => text.replace(/[0-9a-f]{12}$/, ""))).toEqual([
      "tuner-base.bin",
      "Running tune",
      "k106.bin",
    ]);
  });

  it("loads a recent ROM with one click and writes ?rom=", async () => {
    route();
    renderEditor(null);
    await userEvent.click(await screen.findByRole("button", { name: /Edit from k106\.bin/ }));
    await screen.findByRole("grid");
    expect(new URLSearchParams(window.location.search).get("rom")).toBe(SHA_K106);
    expect(within(screen.getByLabelText("Loaded ROM")).getByText("k106.bin")).toBeVisible();
  });

  it("a ?rom= link in the library loads at start", async () => {
    route();
    renderEditor(SHA_K106);
    await screen.findByRole("grid");
    expect(screen.queryByText(/is not in the library/)).not.toBeInTheDocument();
  });

  it("a ?rom= link that is not in the library stays unloaded with a quiet note", async () => {
    route();
    renderEditor("e".repeat(64));
    expect(await screen.findByText(/is not in the library/)).toBeVisible();
    expect(screen.getByText(/eeeeeeeeeeee/)).toBeVisible();
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Loaded ROM")).not.toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Base images" })).toBeVisible();
    expect(control.mock.calls.some(([path]) => String(path).includes("/tables"))).toBe(false);
  });
});

describe("ROM Editor page: the loaded header (W10.8)", () => {
  const menuTitles = (menu: HTMLElement) =>
    within(within(menu).getByRole("table", { name: "Base images" }))
      .getAllByRole("row")
      .slice(1)
      .map((row) =>
        (within(row).getAllByRole("cell")[0]?.textContent ?? "").replace(/[0-9a-f]{12}$/, ""),
      );

  it("is one line with Recent ▾ in place of the Recent ROMs table, and the menu switches the image", async () => {
    route(undefined, {
      "GET /control/rom/library": { entries: [ENTRY_K106, ENTRY_TUNED, ENTRY_UNRECORDED] },
    });
    renderEditor();
    await screen.findByRole("grid");
    // Loaded: no intro, no Recent ROMs table; the header names the image.
    expect(screen.queryByText("Recent ROMs")).not.toBeInTheDocument();
    expect(screen.queryByRole("table", { name: "Base images" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Opening a file adds it to the ROM library/)).not.toBeInTheDocument();
    const header = screen.getByRole("group", { name: "Open a ROM file" });
    expect(within(header).getByLabelText("Loaded ROM")).toHaveTextContent("Running tune");
    expect(within(header).getByRole("button", { name: "Open another ROM…" })).toBeEnabled();

    const recent = within(header).getByRole("button", { name: "Recent" });
    expect(recent).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(recent);
    expect(recent).toHaveAttribute("aria-expanded", "true");
    const menu = screen.getByRole("dialog", { name: "Recent ROMs" });
    expect(menuTitles(menu)).toEqual(["tuner-base.bin", "Running tune", "k106.bin"]);

    fireEvent.click(within(menu).getByRole("button", { name: /Edit from k106\.bin/ }));
    expect(screen.queryByRole("dialog", { name: "Recent ROMs" })).not.toBeInTheDocument();
    await waitFor(() =>
      expect(new URLSearchParams(window.location.search).get("rom")).toBe(SHA_K106),
    );
    expect(within(screen.getByLabelText("Loaded ROM")).getByText("k106.bin")).toBeVisible();
    await screen.findByRole("grid");
  });

  it("closes the menu on Escape and on a click elsewhere, not on a click inside it", async () => {
    route();
    renderEditor();
    await screen.findByRole("grid");
    const recent = screen.getByRole("button", { name: "Recent" });
    const isOpen = () => screen.queryByRole("dialog", { name: "Recent ROMs" }) !== null;
    fireEvent.click(recent);
    fireEvent.mouseDown(
      within(screen.getByRole("dialog", { name: "Recent ROMs" })).getByRole("table"),
    );
    expect(isOpen()).toBe(true);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(isOpen()).toBe(false);
    fireEvent.click(recent);
    fireEvent.mouseDown(screen.getByRole("grid"));
    expect(isOpen()).toBe(false);
    // The second press of the button closes it too.
    fireEvent.click(recent);
    fireEvent.click(recent);
    expect(isOpen()).toBe(false);
  });
});

describe("ROM Editor page: three views (W10.9)", () => {
  const views = () => screen.getByRole("group", { name: "ROM Editor views" });
  const pressed = () =>
    within(views())
      .getAllByRole("button")
      .filter((button) => button.getAttribute("aria-pressed") === "true")
      .map((button) => button.textContent);

  it("opens on Tables and writes the view beside ?rom=", async () => {
    route();
    renderEditor();
    await screen.findByRole("grid");
    expect(
      within(views())
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["Tables", "Edits & Preview", "Simulate"]);
    expect(pressed()).toEqual(["Tables"]);
    await showRomView("Simulate");
    const params = new URLSearchParams(window.location.search);
    expect(params.get("view")).toBe("simulate");
    expect(params.get("rom")).toBe(SHA_TUNED);
    expect(screen.getByRole("form", { name: "Simulate form" })).toBeVisible();
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
    await showRomView("Edits & Preview");
    expect(new URLSearchParams(window.location.search).get("view")).toBe("edits");
    expect(screen.getByRole("heading", { name: "Pending edits" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Preview" })).toBeVisible();
  });

  it("restores the view ?view= names, and falls back to Tables for anything else", async () => {
    route();
    window.history.replaceState(null, "", `/?rom=${SHA_TUNED}&view=simulate`);
    const first = renderEditor();
    expect(await screen.findByRole("form", { name: "Simulate form" })).toBeVisible();
    expect(pressed()).toEqual(["Simulate"]);
    first.unmount();

    window.history.replaceState(null, "", `/?rom=${SHA_TUNED}&view=preview`);
    renderEditor();
    expect(await screen.findByRole("grid")).toBeVisible();
    expect(pressed()).toEqual(["Tables"]);
  });

  it("keeps a half-made operation across a view switch: every view stays mounted", async () => {
    route();
    const grid = await openFuelComp();
    fireEvent.mouseDown(cell(grid, 1, 1));
    const operation = screen.getByRole("form", { name: "Operation" });
    await userEvent.selectOptions(within(operation).getByLabelText("Operation"), "scale");
    await userEvent.type(within(operation).getByLabelText("Scale (%)"), "2");
    await showRomView("Edits & Preview");
    await showRomView("Tables");
    // Queried afresh: a remounted form would be a new, empty one.
    const again = screen.getByRole("form", { name: "Operation" });
    expect(again).toBe(operation);
    expect(within(again).getByLabelText("Scale (%)")).toHaveValue("2");
    expect(screen.getByText("Selected: 1 cell (row 1, column 1).")).toBeVisible();
  });
});
