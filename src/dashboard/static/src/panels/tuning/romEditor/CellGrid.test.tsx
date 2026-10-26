/**
 * The editor's grid is shaded low to high on the shared value ramp, and
 * selection, pending edits, changes and the live overlay's marks stay legible
 * over that colour (the ink and ring contrast is measured in
 * `lib/colorScale.test.ts`).
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { mapColor } from "../../../lib/romMaps";
import { coverage, sequential } from "../../../lib/colorScale";
import type { CellSelection } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";
import type { GridOverlay } from "../../cellOverlay";
import { CellGrid } from "./CellGrid";

const AXIS = {
  name: "axis",
  raw: [1, 2, 3],
  decoded: [1, 2, 3],
  unit: "x",
  confidence: "verified" as const,
};

function table(raw: number[][], overrides: Partial<RomTableValues> = {}): RomTableValues {
  return {
    id: "t",
    name: "Table",
    slot: 1,
    raw,
    decoded: raw.map((row) => row.map((value) => value * 2)),
    unit: "u",
    axes: { rows: AXIS, cols: AXIS },
    confidence: "verified",
    evidence: "",
    ...overrides,
  };
}

const RAW = [
  [10, 20, 30],
  [40, 50, 60],
  [70, 80, 90],
];
const grid = () => screen.getByRole("table", { name: "Cells" });
const cellAt = (r: number, c: number) =>
  within(grid()).getByRole("cell", { name: new RegExp(`^Row ${r + 1}, column ${c + 1}:`) });

describe("CellGrid value shading", () => {
  it("shades each cell on the shared ramp across the table's own range", () => {
    render(<CellGrid table={table(RAW)} label="Cells" />);
    // Decoded values 20..180: the lowest is the ramp's green end, the highest its red end.
    expect(cellAt(0, 0).style.backgroundColor).toBe(sequential(0));
    expect(cellAt(2, 2).style.backgroundColor).toBe(sequential(1));
    expect(cellAt(1, 1).style.backgroundColor).toBe(mapColor(100, 20, 180));
    // Not a second palette: every cell is exactly what the Library's `mapColor` gives.
    RAW.forEach((row, r) =>
      row.forEach((raw, c) =>
        expect(cellAt(r, c).style.backgroundColor).toBe(mapColor(raw * 2, 20, 180)),
      ),
    );
    expect(cellAt(0, 0)).toHaveAttribute("data-shaded", "true");
    expect(screen.getByText("Low → high; not a safety rating")).toBeInTheDocument();
  });

  it("shades an undecoded table by its raw counts", () => {
    render(<CellGrid table={table(RAW, { decoded: null, unit: null })} label="Cells" />);
    expect(cellAt(0, 0).style.backgroundColor).toBe(mapColor(10, 10, 90));
    expect(cellAt(2, 2).style.backgroundColor).toBe(mapColor(90, 10, 90));
  });

  it("leaves a scalar and a uniform table unshaded: there is no range to show", () => {
    const { unmount } = render(<CellGrid table={table([[28715]])} label="Cells" />);
    expect(cellAt(0, 0)).not.toHaveAttribute("data-shaded");
    expect(cellAt(0, 0).style.backgroundColor).toBe("");
    unmount();
    render(
      <CellGrid
        table={table([
          [5, 5],
          [5, 5],
        ])}
        label="Cells"
      />,
    );
    expect(cellAt(1, 1)).not.toHaveAttribute("data-shaded");
    expect(screen.queryByText(/Low → high/)).not.toBeInTheDocument();
  });

  it("leaves a cell with no decoded value unshaded", () => {
    const decoded = RAW.map((row) => row.map((value): number | null => value));
    decoded[1][1] = null;
    render(<CellGrid table={table(RAW, { decoded })} label="Cells" />);
    expect(cellAt(1, 1).style.backgroundColor).toBe("");
    expect(cellAt(0, 0).style.backgroundColor).not.toBe("");
  });

  it("carries dark ink on a shaded cell and the raw count on hover", () => {
    render(<CellGrid table={table(RAW)} label="Cells" />);
    expect(cellAt(1, 1)).toHaveClass("text-bg");
    expect(cellAt(1, 1)).toHaveAttribute("title", "raw 50");
  });
});

describe("CellGrid marks over the shading", () => {
  it("shows a selection as a ring in the dark ink, with the shading and aria state intact", () => {
    const selection: CellSelection = { anchor: [1, 1], focus: [1, 2] };
    render(<CellGrid table={table(RAW)} label="Cells" selection={selection} onSelect={vi.fn()} />);
    const grid = screen.getByRole("grid", { name: "Cells" });
    const selected = within(grid).getByRole("gridcell", { name: /^Row 2, column 2:/ });
    const other = within(grid).getByRole("gridcell", { name: /^Row 1, column 1:/ });
    expect(selected).toHaveAttribute("aria-selected", "true");
    expect(other).toHaveAttribute("aria-selected", "false");
    // The fill is the value's colour either way: selection never replaces it.
    expect(selected.style.backgroundColor).toBe(mapColor(100, 20, 180));
    expect(selected.getAttribute("style")).toContain("box-shadow");
    expect(selected.getAttribute("style")).toContain("var(--color-bg)");
    expect(other.getAttribute("style")).not.toContain("box-shadow");
  });

  it("marks a pending cell with a dashed outline and a glyph, not colour alone", () => {
    render(
      <CellGrid
        table={table(RAW)}
        label="Cells"
        onSelect={vi.fn()}
        pendingCells={new Set(["0:1"])}
      />,
    );
    const pending = within(screen.getByRole("grid", { name: "Cells" })).getByRole("gridcell", {
      name: /^Row 1, column 2:/,
    });
    expect(pending).toHaveAttribute("data-pending", "true");
    expect(pending.getAttribute("style")).toContain("dashed");
    expect(pending).toHaveTextContent("✎");
    expect(pending.style.backgroundColor).toBe(mapColor(40, 20, 180));
  });

  it("outlines a changed cell and prints its change in the ink, over the shading", () => {
    const base = table(RAW);
    const after = table(RAW.map((row, r) => row.map((v, c) => (r === 0 && c === 0 ? 11 : v))));
    render(<CellGrid table={after} base={base} label="Cells" />);
    const changed = cellAt(0, 0);
    expect(changed).toHaveAttribute("data-changed", "true");
    expect(changed).toHaveTextContent("+10.0 %");
    expect(changed.getAttribute("style")).toContain("solid");
    expect(changed.style.backgroundColor).not.toBe("");
    expect(within(changed).getByText("+10.0 %")).not.toHaveClass("text-accent-2");
    expect(cellAt(0, 1)).not.toHaveAttribute("data-changed");
  });

  it("keeps the old theme marks on a cell that has no shading to read against", () => {
    const base = table([[5, 5]]);
    // Every decoded value equal: there is no range, so no shading.
    const after = table([[5, 6]], { decoded: [[10, 10]] });
    render(<CellGrid table={after} base={base} label="Cells" pendingCells={new Set(["0:0"])} />);
    expect(cellAt(0, 1)).toHaveClass("outline-accent-2");
    expect(cellAt(0, 0)).toHaveClass("outline-warn");
  });
});

describe("CellGrid live overlay", () => {
  const position = { rows: [0, 1], columns: [0, 1], row: 0, column: 0 };
  const overlay = (over: Partial<GridOverlay> = {}): GridOverlay => ({
    position,
    ecm: { rows: [1], columns: [2], row: 1, column: 2 },
    coverage: null,
    dwell: null,
    ...over,
  });

  it("marks the inferred cell, its neighbours and the ECM's cell, with names and glyphs", () => {
    const { container } = render(<CellGrid table={table(RAW)} label="Cells" overlay={overlay()} />);
    expect(container.querySelectorAll('[data-trace="nearest"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-trace="neighbor"]')).toHaveLength(3);
    expect(container.querySelectorAll('[data-ecm="cell"]')).toHaveLength(1);
    expect(cellAt(0, 0)).toHaveAttribute(
      "aria-label",
      expect.stringContaining("nearest inferred cell"),
    );
    expect(cellAt(0, 1)).toHaveAttribute("aria-label", expect.stringContaining("surrounding cell"));
    expect(cellAt(1, 2)).toHaveAttribute("aria-label", expect.stringContaining("ECM cell"));
    expect(cellAt(0, 0)).toHaveTextContent("●");
    expect(cellAt(1, 2)).toHaveTextContent("ECM");
    // Value shading stays under the marks.
    expect(cellAt(1, 2).style.backgroundColor).toBe(mapColor(120, 20, 180));
  });

  it("replaces value shading with sample-count shading, never-visited distinct from thin", () => {
    const counts = [
      [0, 3, 12],
      [40, 0, 0],
      [0, 0, 0],
    ];
    render(
      <CellGrid
        table={table(RAW)}
        label="Cells"
        overlay={overlay({ position: null, ecm: null, coverage: counts, dwell: counts })}
      />,
    );
    expect(cellAt(0, 0)).toHaveAttribute("data-coverage", "unvisited");
    expect(cellAt(0, 1)).toHaveAttribute("data-coverage", "thin");
    expect(cellAt(0, 2)).toHaveAttribute("data-coverage", "visited");
    expect(cellAt(0, 1).style.backgroundColor).toBe(coverage(3, 40));
    // The deepest shade takes light ink: dark ink would not read on it.
    expect(cellAt(1, 0)).toHaveClass("text-fg");
    expect(cellAt(0, 1)).toHaveClass("text-bg");
    expect(cellAt(0, 1)).toHaveAttribute("title", expect.stringContaining("3 samples"));
    expect(screen.getByText(/hover a cell for its count/)).toBeInTheDocument();
    expect(screen.queryByText(/Low → high/)).not.toBeInTheDocument();
  });

  it("keeps the selection ring and the ECM ring together, selection outermost", () => {
    render(
      <CellGrid
        table={table(RAW)}
        label="Cells"
        selection={{ anchor: [1, 2], focus: [1, 2] }}
        onSelect={vi.fn()}
        overlay={overlay()}
      />,
    );
    const both = within(screen.getByRole("grid", { name: "Cells" })).getByRole("gridcell", {
      name: /^Row 2, column 3:/,
    });
    const shadow = both.style.boxShadow || both.getAttribute("style") || "";
    expect(shadow.indexOf("--color-bg")).toBeGreaterThanOrEqual(0);
    expect(shadow.indexOf("--color-bg")).toBeLessThan(shadow.indexOf("--color-series-5"));
  });

  it("draws a large grid for the drive presentation", () => {
    render(<CellGrid table={table(RAW)} label="Cells" large />);
    expect(cellAt(0, 0)).toHaveClass("min-w-24");
    expect(grid()).toHaveClass("text-xl");
  });
});

describe("CellGrid interaction is unchanged by the shading", () => {
  it("selects with a click and extends with shift, as before", () => {
    const onSelect = vi.fn();
    render(<CellGrid table={table(RAW)} label="Cells" onSelect={onSelect} />);
    const gridEl = screen.getByRole("grid", { name: "Cells" });
    fireEvent.mouseDown(within(gridEl).getByRole("gridcell", { name: /^Row 1, column 1:/ }));
    expect(onSelect).toHaveBeenLastCalledWith({ anchor: [0, 0], focus: [0, 0] });
  });
});
