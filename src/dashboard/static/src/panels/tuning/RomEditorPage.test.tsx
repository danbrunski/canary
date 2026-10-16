import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { HttpError, controlBlob, controlJson } from "../../api/core";
import {
  ENTRY_K106,
  ENTRY_TUNED,
  SHA_K106,
  SHA_TUNED,
  TABLES_WITH_FUEL_COMP,
  fuelCompTable,
} from "../../test/romLibraryFixtures";
import type { RomEditPreview, RomEditPreviewRequest } from "../../types/romEditor";
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
    sim_base: { ok: true, status: "matched", revision: "ZS44A-20260920", reason: null },
  };
}

function route(
  preview: (init?: RequestInit) => unknown = (init) => previewFor(JSON.parse(bodyOf(init))),
) {
  const table: Record<string, unknown> = {
    "GET /control/rom/library": { entries: [ENTRY_TUNED, ENTRY_K106] },
    [`GET /control/rom/library/${SHA_TUNED}/tables`]: TABLES_WITH_FUEL_COMP,
    [`GET /control/rom/library/${SHA_K106}/tables`]: { ...TABLES_WITH_FUEL_COMP, sha256: SHA_K106 },
    [`POST ${PREVIEW_PATH(SHA_TUNED)}`]: preview,
    [`POST ${PREVIEW_PATH(SHA_K106)}`]: preview,
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

function cell(grid: HTMLElement, row: number, col: number) {
  return within(grid).getByRole("gridcell", { name: new RegExp(`^Row ${row}, column ${col}:`) });
}

async function openFuelComp() {
  render(<RomEditorPage token="t" status={null} />);
  fireEvent.click(await screen.findByRole("button", { name: "Fuel Compensation (Map 1)" }));
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
  it("lists each image with its revision and whether it can be simulated, and keeps the sha in the URL", async () => {
    route();
    render(<RomEditorPage token="t" status={null} />);
    const images = await screen.findByRole("table", { name: "Base images" });
    const rows = within(images).getAllByRole("row");
    expect(within(rows[1]).getByText("ZS44A-20260920")).toBeVisible();
    expect(within(rows[1]).getByText("can simulate")).toBeVisible();
    expect(within(rows[2]).getByText("unrecognised")).toBeVisible();
    expect(within(rows[2]).getByText("preview only")).toBeVisible();
    expect(new URLSearchParams(window.location.search).get("rom")).toBe(SHA_TUNED);
    await screen.findByRole("grid");

    await userEvent.click(screen.getByRole("button", { name: /Edit from k106\.bin/ }));
    expect(new URLSearchParams(window.location.search).get("rom")).toBe(SHA_K106);
    // The Simulate slot says why it is off for an image that is not a revision.
    expect(
      await screen.findByText(/Simulate is unavailable: base_not_a_recorded_revision/),
    ).toBeVisible();
  });

  it("opens the image the URL names", async () => {
    route();
    window.history.replaceState(null, "", `/?rom=${SHA_K106}`);
    render(<RomEditorPage token="t" status={null} />);
    await screen.findByRole("grid");
    expect(screen.getByRole("button", { name: /Edit from k106\.bin/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("offers Fuel Targets, Fuel Compensation and K in Map 1 only, with the Fuel Comp axis note", async () => {
    route();
    render(<RomEditorPage token="t" status={null} />);
    const group = await screen.findByRole("group", { name: "Table" });
    expect(
      within(group)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual([
      "Fuel Targets (Map 1)",
      "K (injector constant) (Map 1)",
      "Fuel Compensation (Map 1)",
    ]);
    await userEvent.click(within(group).getByRole("button", { name: "Fuel Compensation (Map 1)" }));
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
    fireEvent.mouseDown(cell(grid, 5, 5));
    fireEvent.mouseEnter(cell(grid, 6, 6));
    fireEvent.mouseUp(window);
    fireEvent.mouseEnter(cell(grid, 8, 8));
    expect(screen.getByText("Selected: 4 cells (row 5–6, column 5–6).")).toBeVisible();
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

    const list = screen.getByRole("list", { name: "Pending edits" });
    expect(within(list).getByText(/scale raw ×1\.02 \(\+2 %, nearest\) on 2 cells/)).toBeVisible();
    // Pending cells are marked before any preview.
    expect(cell(grid, 1, 1)).toHaveAttribute("data-pending", "true");

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
    await userEvent.type(screen.getByLabelText("Label for this edit set"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Preview" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Refused: value_out_of_range");
    expect(alert).toHaveTextContent("raw 255 x 1.02 is 260");
    expect(screen.queryByRole("region", { name: "Preview result" })).not.toBeInTheDocument();
  });

  it("refuses in the browser a value whose raw is outside u8, and sends nothing", async () => {
    route();
    render(<RomEditorPage token="t" status={null} />);
    const grid = await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    fireEvent.mouseDown(cell(grid, 1, 1));
    const operation = screen.getByRole("form", { name: "Operation" });
    await userEvent.type(within(operation).getByLabelText("Value (AFR)"), "5");
    await userEvent.click(within(operation).getByRole("button", { name: "Apply to selection" }));
    expect(await within(operation).findByRole("alert")).toHaveTextContent(
      /Refused \(value_out_of_range\).*raw 376/,
    );
    expect(screen.getByText("No pending edits.")).toBeVisible();
    expect(control.mock.calls.some(([, , init]) => init?.method === "POST")).toBe(false);
  });

  it("shows the set-to-value chain: 12.5 AFR -> raw 151 -> 12.46 AFR", async () => {
    route();
    render(<RomEditorPage token="t" status={null} />);
    const grid = await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    fireEvent.mouseDown(cell(grid, 1, 1));
    const operation = screen.getByRole("form", { name: "Operation" });
    await userEvent.type(within(operation).getByLabelText("Value (AFR)"), "12.5");
    await userEvent.click(within(operation).getByRole("button", { name: "Apply to selection" }));
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
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: "Remove edit 1" }));
    expect(screen.getByText("No pending edits.")).toBeVisible();
  });

  it("keeps the pending edits and label per image across a reload", async () => {
    route();
    const first = render(<RomEditorPage token="t" status={null} />);
    fireEvent.click(await screen.findByRole("button", { name: "Fuel Compensation (Map 1)" }));
    await applyScale(screen.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" }));
    await userEvent.type(screen.getByLabelText("Label for this edit set"), "kept");
    first.unmount();

    render(<RomEditorPage token="t" status={null} />);
    const list = await screen.findByRole("list", { name: "Pending edits" });
    expect(within(list).getByText(/scale raw ×1\.02/)).toBeVisible();
    expect(screen.getByLabelText("Label for this edit set")).toHaveValue("kept");

    // Another image does not see them.
    await screen.findByRole("grid");
    await userEvent.click(screen.getByRole("button", { name: /Edit from k106\.bin/ }));
    await waitFor(() => expect(screen.getByText("No pending edits.")).toBeVisible());
  });
});

describe("ROM Editor page: spec download and no image", () => {
  it("downloads the spec YAML as <label>.edit-spec.yaml and offers no image or candidate", async () => {
    route();
    const grid = await openFuelComp();
    await applyScale(grid);
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

describe("ROM Editor page: the Simulate slot", () => {
  it("is empty for a matched revision (W09.4 fills it) and gives the reason otherwise", async () => {
    route();
    render(<RomEditorPage token="t" status={null} />);
    await screen.findByRole("grid");
    const slot = screen.getByRole("region", { name: "Simulate" });
    expect(slot).toBeEmptyDOMElement();
    await userEvent.click(screen.getByRole("button", { name: /Edit from k106\.bin/ }));
    expect(await screen.findByRole("note")).toHaveTextContent(
      /Simulate is unavailable: base_not_a_recorded_revision\. The simulator needs a base recorded/,
    );
  });
});
