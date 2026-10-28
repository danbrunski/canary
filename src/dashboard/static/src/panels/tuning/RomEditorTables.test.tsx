/**
 * One page for viewing and editing ROM tables (ROM Maps merged into the ROM
 * Editor, 2026-10-26): every table the image's map declares but the axes,
 * both timing trims and Fuel Compensation and Fuel Targets among them, picked
 * from a tree grouped as the map files them (W10.10), a slotted table's copies
 * under one family (W10.14), and drawn shaded low to high; Map 1 is edited,
 * the rest are looked at.
 */
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { controlJson } from "../../api/core";
import { mapColor } from "../../lib/romMaps";
import {
  ENTRY_TUNED,
  SHA_TUNED,
  TABLES_ALL_2D,
  TABLES_ALL_SLOTS,
  TABLES_WITH_CURVES,
} from "../../test/romLibraryFixtures";
import { treeContents, treeTable } from "../../test/romEditorViews";
import type { RomTablesResponse } from "../../types/romLibrary";
import { RomEditorPage } from "./RomEditorPage";

vi.mock("../../api/core", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api/core")>()),
  controlJson: vi.fn(),
}));
const control = vi.mocked(controlJson);

function route(tables: RomTablesResponse) {
  control.mockImplementation(async (path: string) => {
    if (path === "/control/rom/library") return { entries: [ENTRY_TUNED] };
    if (path === `/control/rom/library/${SHA_TUNED}/tables`) return tables;
    throw new Error(`unexpected ${path}`);
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});

async function open(tables: RomTablesResponse = TABLES_ALL_2D) {
  route(tables);
  window.history.replaceState(null, "", `/?rom=${SHA_TUNED}`);
  render(<RomEditorPage token="t" status={null} />);
  return screen.findByRole("navigation", { name: "Tables" });
}

const shadedCells = (grid: HTMLElement) => within(grid).getAllByRole("gridcell");

describe("ROM Editor: every table, shaded", () => {
  it("offers Fuel Targets, Fuel Compensation, both timing trims and the base timing map, by group", async () => {
    const picker = await open();
    expect(treeContents()).toEqual([
      "Fuel, 2 tables",
      "Fuel Targets, 2 maps",
      "Fuel Targets (Map 1)",
      "Fuel Targets (Map 2)",
      "Fuel Compensation (Map 1)",
      "Ignition, 3 tables",
      "Timing Main (trim A) (Map 1)",
      "Timing High Det (trim B) (Map 1)",
      "Base timing rate map (Map 1)",
    ]);
    // A single-copy table is named by its tree label, a slotted copy by its
    // slot under the family; the detail column's heading is the full label.
    const targets = within(picker).getByRole("button", { name: "Map 1" });
    expect(targets).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("heading", { name: "Fuel Targets (Map 1)" })).toBeVisible();
    fireEvent.click(within(picker).getByRole("button", { name: "Fuel Compensation" }));
    expect(targets).not.toHaveAttribute("aria-current");
    expect(screen.getByRole("heading", { name: "Fuel Compensation (Map 1)" })).toBeVisible();
    expect(screen.getByRole("grid", { name: "Fuel Compensation (Map 1) cells" })).toBeVisible();
  });

  it("shades each table's cells on the Library's ramp, whichever table is picked", async () => {
    await open();
    for (const [name, label] of [
      ["Fuel Targets (Map 1)", "Fuel Targets (Map 1) cells"],
      ["Timing Main (trim A) (Map 1)", "Timing Main (trim A) (Map 1) cells"],
      ["Timing High Det (trim B) (Map 1)", "Timing High Det (trim B) (Map 1) cells"],
      ["Fuel Compensation (Map 1)", "Fuel Compensation (Map 1) cells"],
    ] as const) {
      fireEvent.click(treeTable(name));
      const grid = screen.getByRole("grid", { name: label });
      const cells = shadedCells(grid);
      expect(cells.length).toBeGreaterThan(8);
      expect(
        cells.every((cell) => cell.hasAttribute("data-shaded")),
        name,
      ).toBe(true);
      const colours = new Set(cells.map((cell) => cell.style.backgroundColor));
      expect(colours.size, `${name} has a range of colours`).toBeGreaterThan(1);
    }
    // Fuel Targets: AFR is shaded by its decoded value, the lowest AFR (richest) at the green end.
    fireEvent.click(treeTable("Fuel Targets (Map 1)"));
    const targets = shadedCells(screen.getByRole("grid", { name: "Fuel Targets (Map 1) cells" }));
    const values = targets.map((cell) => 1881.6 / Number(/raw (\d+)/.exec(cell.title)?.[1]));
    const min = Math.min(...values);
    const max = Math.max(...values);
    targets.forEach((cell, i) =>
      expect(cell.style.backgroundColor).toBe(mapColor(values[i], min, max)),
    );
  });

  it("shades the timing trim by its masked value, flag bit and all on hover", async () => {
    await open();
    fireEvent.click(treeTable("Timing Main (trim A) (Map 1)"));
    const grid = screen.getByRole("grid", { name: "Timing Main (trim A) (Map 1) cells" });
    const first = within(grid).getByRole("gridcell", { name: /^Row 1, column 1:/ });
    // Raw 171 = 43 + the bit-7 flag: shown and shaded as 43, the lowest value.
    expect(first).toHaveTextContent("43");
    expect(first).toHaveAttribute("title", "raw 171");
    expect(first.style.backgroundColor).toBe(mapColor(43, 43, 58));
  });

  it("keeps the BFS axis in the editor's ms labels over the shading", async () => {
    await open();
    const grid = await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    const headers = within(grid)
      .getAllByRole("columnheader")
      .map((header) => header.textContent);
    expect(headers.slice(1, 4)).toEqual(["8.76", "11.46", "15.67"]);
  });

  it("looks at a table nobody edits, without offering an edit", async () => {
    await open();
    fireEvent.click(treeTable("Base timing rate map (Map 1)"));
    const grid = screen.getByRole("table", { name: "Base timing rate map (Map 1) cells" });
    expect(
      within(grid)
        .getAllByRole("cell")
        .every((cell) => cell.hasAttribute("data-shaded")),
    ).toBe(true);
    expect(
      screen.getByText(/Viewing only: the ROM map does not mark Base timing rate map editable/),
    ).toBeVisible();
    expect(screen.queryByRole("form", { name: "Operation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("grid", { name: /Base timing/ })).not.toBeInTheDocument();
    // The edit tables are still editable.
    fireEvent.click(treeTable("Fuel Compensation (Map 1)"));
    expect(screen.getByRole("form", { name: "Operation" })).toBeVisible();
  });

  it("shows another slot's copy for looking at, editing only Map 1", async () => {
    await open();
    await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    fireEvent.click(treeTable("Fuel Targets (Map 2)"));
    expect(screen.getByRole("table", { name: "Fuel Targets (Map 2) cells" })).toBeVisible();
    expect(screen.getByText(/edits and the simulator cover Map 1 only/)).toBeVisible();
    expect(screen.queryByRole("form", { name: "Operation" })).not.toBeInTheDocument();
    // The single-copy tables stay where they were: no slot hides them.
    expect(treeTable("Base timing rate map (Map 1)")).toBeVisible();
    // Back to Map 1: editing returns.
    fireEvent.click(treeTable("Fuel Targets (Map 1)"));
    expect(screen.getByRole("form", { name: "Operation" })).toBeVisible();
  });

  it("lists all five map slots in each family and looks at Map 5, editing nothing there", async () => {
    await open(TABLES_ALL_SLOTS);
    await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    expect(screen.queryByLabelText("Map slot")).not.toBeInTheDocument();
    expect(treeContents()).toEqual([
      "Fuel, 2 tables",
      "Fuel Targets, 5 maps",
      "Fuel Targets (Map 1)",
      "Fuel Targets (Map 2)",
      "Fuel Targets (Map 3)",
      "Fuel Targets (Map 4)",
      "Fuel Targets (Map 5)",
      "Fuel Compensation, 5 maps",
      "Ignition, 3 tables",
      "Timing Main (trim A) (Map 1)",
      "Timing High Det (trim B) (Map 1)",
      "Base timing rate map (Map 1)",
    ]);
    fireEvent.click(treeTable("Fuel Targets (Map 5)"));
    const grid = screen.getByRole("table", { name: "Fuel Targets (Map 5) cells" });
    expect(
      within(grid)
        .getAllByRole("cell")
        .every((cell) => cell.hasAttribute("data-shaded")),
    ).toBe(true);
    expect(screen.getByText(/edits and the simulator cover Map 1 only/)).toBeVisible();
    expect(screen.queryByRole("form", { name: "Operation" })).not.toBeInTheDocument();
    expect(screen.queryByRole("grid")).not.toBeInTheDocument();
  });

  it("warns that UpRev labels Fuel Compensation's axes differently in a slot other than Map 1", async () => {
    await open(TABLES_ALL_SLOTS);
    await screen.findByRole("grid", { name: "Fuel Targets (Map 1) cells" });
    fireEvent.click(treeTable("Fuel Compensation (Map 3)"));
    const note = screen.getByRole("note", { name: "Fuel Compensation editor labels" });
    expect(note).toHaveTextContent(/UpRev labels these axes differently/);
    expect(note).toHaveTextContent(/Grid positions agree/);
    // The warning is the table's own: Fuel Targets in the same slot carries none.
    fireEvent.click(treeTable("Fuel Targets (Map 3)"));
    expect(screen.queryByRole("note", { name: /editor labels/ })).not.toBeInTheDocument();
  });

  it("needs a token: Open ROM is disabled and nothing is fetched", () => {
    route(TABLES_ALL_2D);
    render(<RomEditorPage token="" status={null} />);
    expect(screen.getByText("Enter the control token to use the ROM Editor.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Open ROM…" })).toBeDisabled();
    expect(control).not.toHaveBeenCalled();
  });

  it("lists every table but the axes: a curve is a one-row grid and a scalar one cell", async () => {
    await open(TABLES_WITH_CURVES);
    expect(treeContents()).toEqual([
      "Fuel, 2 tables",
      "Fuel Targets, 2 maps",
      "Fuel Targets (Map 1)",
      "Fuel Targets (Map 2)",
      "K (injector constant) (Map 1)",
      "Airflow, 2 tables",
      "Throttle effective area",
      "Idle air learning cap",
    ]);
    fireEvent.click(treeTable("Throttle effective area"));
    const curve = screen.getByRole("table", { name: "Throttle effective area cells" });
    expect(within(curve).getAllByRole("cell")).toHaveLength(32);
    expect(screen.getByText(/Columns: Throttle effective-area curve axis/)).toBeVisible();
    expect(
      screen.getByText(/Viewing only: the ROM map does not mark Throttle effective area editable/),
    ).toBeVisible();
    fireEvent.click(treeTable("Idle air learning cap"));
    const scalar = screen.getByRole("table", { name: "Idle air learning cap cells" });
    expect(within(scalar).getAllByRole("cell")).toHaveLength(1);
    expect(screen.queryByRole("form", { name: "Operation" })).not.toBeInTheDocument();
  });

  it("says Opening a file adds it to the library before anything is loaded, and not after", async () => {
    route(TABLES_ALL_2D);
    render(<RomEditorPage token="t" status={null} />);
    const intro = /Opening a file adds it to the ROM library, kept by its hash/;
    expect(await screen.findByText(intro)).toBeVisible();
    fireEvent.click(await screen.findByRole("button", { name: /Edit from/ }));
    await screen.findByRole("navigation", { name: "Tables" });
    expect(screen.queryByText(intro)).not.toBeInTheDocument();
  });
});
