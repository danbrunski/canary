import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { STATUSES } from "../../../lib/romSimView";
import {
  buildCellTable,
  buildRunContext,
  output,
  buildSimResult,
  BASE_SHA,
  CANDIDATE_SHA,
} from "../../../test/romSimFixtures";
import type { RomSimResult, SimCell, SimRunContext } from "../../../types/romEditor";
import { SimResults } from "./SimResults";

function show(
  options: {
    result?: RomSimResult;
    run?: SimRunContext;
    stale?: boolean;
    onDownload?: () => void;
  } = {},
) {
  return render(
    <SimResults
      result={options.result ?? buildSimResult()}
      run={options.run ?? buildRunContext()}
      stale={options.stale ?? false}
      onDownload={options.onDownload ?? (() => {})}
    />,
  );
}

const lambdaMap = () =>
  screen.getByRole("table", { name: /^Map: relative Δλ p50, open_loop pairs/ });
const buttons = (scope: HTMLElement) => within(scope).getAllByRole("button");
const byStatus = (scope: HTMLElement, status: string) =>
  buttons(scope).filter((button) => button.getAttribute("data-status") === status);

describe("the caveat block", () => {
  it("comes before the first map in the DOM", () => {
    show();
    const caveats = screen.getByRole("region", { name: "Read this first" });
    const firstMap = screen.getAllByRole("table")[0];
    const figure = screen.getAllByRole("figure")[0];
    expect(
      caveats.compareDocumentPosition(firstMap) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(caveats.compareDocumentPosition(figure) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // And nothing that is a table, a grid or a map sits before it.
    const result = screen.getByRole("region", { name: "Simulation result" });
    const before = [...result.querySelectorAll("table, figure, [data-status]")].filter(
      (element) => caveats.compareDocumentPosition(element) & Node.DOCUMENT_POSITION_PRECEDING,
    );
    expect(before).toEqual([]);
  });

  it("says why the plant is not qualified, and the two standing caveats", () => {
    show();
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(within(caveats).getByText("The plant is not qualified.")).toBeVisible();
    expect(caveats).toHaveTextContent("no qualification verdict yet (baseline validation pending)");
    expect(caveats).toHaveTextContent("Nothing below is a qualified prediction");
    expect(within(caveats).getByText("structural_error_not_sampled")).toBeVisible();
    expect(caveats).toHaveTextContent(/Structural error is not sampled/);
    expect(within(caveats).getByText("parameter_uncertainty_only")).toBeVisible();
    expect(caveats).toHaveTextContent(/Parameter uncertainty only/);
  });

  it("says what E08.7 has shown: items 1-4 in the suite, not on this page, the truck scoring owed", () => {
    show();
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(caveats).toHaveTextContent(/known answers 1–4/);
    expect(caveats).toHaveTextContent(/reproduced in the test suite through the CLI path/);
    expect(caveats).toHaveTextContent(/not been run through this page on real images yet/);
    expect(caveats).toHaveTextContent(/post-flash truck drives \(item 5\) is still owed/);
  });

  it("lists every other caveat, every assumption of the result and the design, every unresolved", () => {
    const result = buildSimResult();
    show({ result });
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(within(caveats).getByText("synthetic_example_not_a_measurement")).toBeVisible();
    for (const text of [
      ...result.assumptions,
      ...(result.design.assumptions ?? []),
      ...result.unresolved,
    ]) {
      expect(within(caveats).getByText(text)).toBeVisible();
    }
  });

  it("states a plant that is qualified only by the absence of the caveat", () => {
    const result = buildSimResult();
    result.cells.caveats = result.cells.caveats.filter(
      (item) => !item.startsWith("plant_not_qualified"),
    );
    result.caveats = result.cells.caveats;
    show({ result });
    expect(screen.getByRole("region", { name: "Read this first" })).toHaveTextContent(
      /the run states no plant_not_qualified caveat/,
    );
  });

  it("prints both image identities, the edit list, coverage, draws and seed", () => {
    show();
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(within(caveats).getByText(BASE_SHA)).toBeVisible();
    expect(within(caveats).getByText(CANDIDATE_SHA)).toBeVisible();
    expect(caveats).toHaveTextContent("Running tune (ZS44A-20260920)");
    const edits = within(caveats).getByRole("list", { name: "Edit list" });
    expect(edits).toHaveTextContent("Fuel Compensation: scale raw ×1.02 (+2 %, nearest) on 1 cell");
    expect(caveats).toHaveTextContent("2 cells changed in 2 bytes, reaching fuel_comp_lookup");
    expect(caveats).toHaveTextContent("2 edited cells of fuel_compensation slot 1");
    expect(within(caveats).getByRole("list", { name: "Moved lookups" })).toHaveTextContent(
      "moved lookup target_afr_lookup (fuel_targets), off this grid",
    );
    expect(caveats).toHaveTextContent("10 points on a fuel_comp grid");
    expect(caveats).toHaveTextContent("10 of 12 cells visited");
    expect(caveats).toHaveTextContent("load_below_model_floor: 40 pairs");
    expect(caveats).toHaveTextContent("delivered_lambda 82.0 %");
    expect(caveats).toHaveTextContent(/draws\s*200/);
    expect(caveats).toHaveTextContent(/seed\s*20261015/);
    expect(caveats).toHaveTextContent(/candidate events\s*ecu_reflash/);
  });

  it("notes when the pending edits have moved on since the run", () => {
    show({ stale: true });
    expect(screen.getByRole("status")).toHaveTextContent(
      /pending edits have changed since this run/,
    );
  });
});

describe("every status has a label or hatch", () => {
  it("renders each status of sim_cells.STATUSES in the lambda map", () => {
    show();
    const map = lambdaMap();
    for (const status of STATUSES) {
      expect(byStatus(map, status).length, status).toBeGreaterThan(0);
    }
    // The label text of each, as the report prints it.
    expect(within(map).getAllByText("-1.52")).toHaveLength(2); // changed (the two edited cells)
    expect(within(map).getByText("0")).toBeVisible(); // unchanged
    expect(within(map).getByText("U")).toBeVisible(); // unsupported
    expect(within(map).getByText("P+0.40")).toBeVisible(); // partly supported
    expect(within(map).getAllByText("–").length).toBe(2); // no points
    expect(within(map).getByText("NR")).toBeVisible(); // not reached
    expect(within(map).getAllByText("CL").length).toBeGreaterThan(0); // another loop
    expect(within(map).getByText("∅")).toBeVisible(); // relative change undefined
  });

  it("names the status in each cell's accessible name and in the legend", () => {
    show();
    const map = lambdaMap();
    for (const [status, words] of [
      ["unsupported", /unsupported \(computed only: -0\.6 %\)/],
      ["partly_supported", /partly supported \+0\.4 %, 60\.0 % supported/],
      ["no_points", /no points/],
      ["not_reached", /not reached/],
      ["other_loop", /other loop: its pairs are CL/],
      ["undefined", /relative change undefined/],
    ] as const) {
      const first = byStatus(map, status)[0];
      expect(first.getAttribute("aria-label"), status).toMatch(words);
    }
    const legend = screen.getByRole("group", { name: /^Legend: relative Δλ p50, open_loop/ });
    for (const word of [
      "changed",
      "unchanged",
      "unsupported",
      "partly supported",
      "no points",
      "not reached",
      "other loop",
      "relative change undefined",
      "edited",
      "moved breakpoint",
    ]) {
      expect(within(legend).getByText(word)).toBeVisible();
    }
  });

  it("colours only a computed value: every other status is a hatch", () => {
    show();
    const map = lambdaMap();
    for (const status of [
      "unsupported",
      "partly_supported",
      "no_points",
      "not_reached",
      "other_loop",
      "undefined",
    ]) {
      for (const button of byStatus(map, status)) {
        expect(button.style.backgroundImage, status).toMatch(/gradient/);
      }
    }
    for (const status of ["changed", "unchanged"]) {
      for (const button of byStatus(map, status)) {
        expect(button.style.backgroundImage, status).toBe("");
        expect(button.style.backgroundColor, status).toMatch(/^rgb\(/);
      }
    }
    // The same value gets the same colour: the two edited, richer cells.
    const cell = (row: number, col: number) => buttons(map)[row * 4 + col];
    expect(cell(0, 0).style.backgroundColor).toBe(cell(2, 2).style.backgroundColor);
    expect(cell(0, 0).style.backgroundColor).not.toBe(cell(1, 3).style.backgroundColor);
  });

  it("outlines edited cells and marks the moved breakpoint on its axis and cells", () => {
    show();
    const map = lambdaMap();
    const edited = buttons(map).filter((button) => button.getAttribute("data-edited") === "true");
    expect(edited).toHaveLength(2);
    for (const button of edited) expect(button.style.boxShadow).toMatch(/inset/);
    expect(buttons(map).filter((b) => b.getAttribute("data-axis-moved") === "true")).toHaveLength(
      1,
    );
    expect(within(map).getAllByLabelText("breakpoint moves")).toHaveLength(1);
    expect(within(map).getByRole("rowheader", { name: /1200/ })).toHaveTextContent("▲");
  });
});

describe("a changed cell whose median is exactly 0", () => {
  it("reads '0*', not unchanged's '0', and the legend explains it", () => {
    const result = buildSimResult();
    const target = result.cells.cells[2 * 4 + 2];
    const block = output("changed", { delta: [-0.01, 0, 0.01] });
    target.by_loop = [{ ...target.by_loop[0], cell: block }];
    show({ result });
    const map = lambdaMap();
    const starred = buttons(map)[2 * 4 + 2];
    expect(starred).toHaveAttribute("data-status", "changed");
    expect(starred).toHaveTextContent("0*");
    expect(starred.getAttribute("aria-label")).toMatch(/changed, median exactly 0/);
    const plain = buttons(map)[1];
    expect(plain).toHaveAttribute("data-status", "unchanged");
    expect(plain.textContent).toBe("0");
    const legend = screen.getByRole("group", { name: /^Legend: relative Δλ p50, open_loop/ });
    expect(within(legend).getByText("changed, median 0")).toBeVisible();
    expect(legend).toHaveTextContent(/some pairs move but the middle value is exactly 0/);
  });

  it("is not in the legend of a map with no such cell", () => {
    show();
    const legend = screen.getByRole("group", { name: /^Legend: relative Δλ p50, open_loop/ });
    expect(within(legend).queryByText("changed, median 0")).not.toBeInTheDocument();
  });
});

describe("closed loop and the setpoint read their own headline", () => {
  it("draws a map for each loop with pairs, the closed-loop one in the trim", () => {
    show();
    const trim = screen.getByRole("table", {
      name: /^Map: Δtotal fuel trim p50, closed_loop pairs/,
    });
    // (1,2) is unsupported with a computed trim, shown as text on a hatch.
    const computed = byStatus(trim, "unsupported")[0];
    expect(computed).toHaveTextContent("U");
    expect(computed).toHaveTextContent("-0.80");
    // (1,3): a supported trim, coloured.
    const supported = byStatus(trim, "changed")[0];
    expect(supported).toHaveTextContent("+0.90");
    expect(supported.style.backgroundColor).toMatch(/^rgb\(/);
    // Open-loop-only cells are "other loop" here, not a trim of zero.
    expect(byStatus(trim, "other_loop").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("table", {
        name: /^Map: relative Δtarget φ p50, closed_loop_setpoint_moved/,
      }),
    ).toBeVisible();
  });
});

describe("a cell's detail", () => {
  it("shows points, pairs, weight, quantiles, regime mix, supported share and reasons on click", () => {
    show();
    expect(screen.queryByRole("region", { name: "Cell detail" })).not.toBeInTheDocument();
    fireEvent.click(byStatus(lambdaMap(), "partly_supported")[0]);
    const detail = screen.getByRole("region", { name: "Cell detail" });
    expect(detail).toHaveTextContent("Cell (row 1, column 4) · 400 rpm · 28.6 %");
    expect(detail).toHaveTextContent("n 1 point · 40 pairs · weight 0.25 grid weight");
    expect(detail).toHaveTextContent("closed_loop 25.0 %, lean_target 75.0 %");
    const outputs = within(detail).getByRole("table", { name: "Cell outputs" });
    const row = within(outputs).getByRole("row", { name: /delivered_lambda/ });
    expect(row).toHaveTextContent("partly supported");
    expect(row).toHaveTextContent("over supported_pairs: 24 pairs, weight 0.25");
    expect(row).toHaveTextContent("relative p05/p50/p95 0 / +0.4 / +0.9 %");
    expect(row).toHaveTextContent("supported 60.0 %, unsupported 40.0 %");
    expect(row).toHaveTextContent("reasons (40.0 %, 16 pairs): fill_byte_outside_plant_range");
    const loops = within(detail).getByRole("list", { name: "Cell loops" });
    expect(loops).toHaveTextContent("open_loop 100.0 % → λ (delivered_lambda): partly supported");
  });

  it("shows the edit and both loops of a mixed cell, and says an unvisited cell was never visited", () => {
    show();
    fireEvent.click(buttons(lambdaMap())[0]); // (0,0), edited
    expect(screen.getByRole("region", { name: "Cell detail" })).toHaveTextContent(
      "Edited: raw 120 → 122",
    );
    const mixed = buttons(lambdaMap())[7]; // (1,3)
    fireEvent.click(mixed);
    const loops = within(screen.getByRole("region", { name: "Cell detail" })).getByRole("list", {
      name: "Cell loops",
    });
    expect(within(loops).getAllByRole("listitem")).toHaveLength(2);
    expect(loops).toHaveTextContent(
      "closed_loop 30.0 % → total fuel trim (fuel_trim_total_pct): changed",
    );
    fireEvent.click(byStatus(lambdaMap(), "no_points")[0]);
    expect(screen.getByRole("region", { name: "Cell detail" })).toHaveTextContent(
      "No points: the design never visited this cell.",
    );
  });
});

describe("the views render only the grid being looked at", () => {
  it("switches between the lambda, width, ΔBFS and Δpulse maps", () => {
    show();
    const tablesBefore = screen
      .getAllByRole("table")
      .filter((t) => t.getAttribute("aria-label")?.startsWith("Map:"));
    expect(tablesBefore).toHaveLength(3); // open loop, closed loop, setpoint moved

    fireEvent.click(screen.getByRole("button", { name: "p05–p95 width" }));
    const width = screen
      .getAllByRole("table")
      .filter((t) => t.getAttribute("aria-label")?.startsWith("Map:"));
    expect(width).toHaveLength(1);
    expect(width[0]).toHaveAccessibleName(/p05–p95 width of relative Δλ, open_loop pairs/);
    expect(screen.queryByRole("table", { name: /^Map: relative Δλ p50/ })).not.toBeInTheDocument();
    expect(within(width[0]).getAllByText("1.00").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "ΔBFS" }));
    expect(screen.getByRole("table", { name: /^Map: relative ΔBFS p50, all pairs/ })).toBeVisible();
    expect(
      within(screen.getByRole("table", { name: /^Map: relative ΔBFS/ })).getAllByText("+2.01")
        .length,
    ).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: "Δpulse" }));
    const pulse = screen.getByRole("table", { name: /^Map: Δpulse p50, all pairs/ });
    expect(within(pulse).getAllByText("+0.05").length).toBeGreaterThan(0);
    // The pulse is unsupported in the closed-loop-free cell (0,2): a label and a computed value.
    expect(byStatus(pulse, "unsupported")[0]).toHaveTextContent("+0.02");
  });

  it("renders a full 16 x 16 grid for the viewed map only", () => {
    const result = buildSimResult();
    const base = buildCellTable();
    const filler = base.cells[1];
    const cells: SimCell[] = [];
    const bps = Array.from({ length: 16 }, (_, i) => i + 1);
    for (let row = 0; row < 16; row += 1) {
      for (let col = 0; col < 16; col += 1) {
        cells.push({
          ...filler,
          row,
          col,
          row_breakpoint: bps[row],
          col_breakpoint: bps[col],
          row_value: bps[row] * 400,
          col_value: bps[col] * 6.25,
        });
      }
    }
    result.cells = {
      ...base,
      rows: {
        ...base.rows,
        raw: bps,
        breakpoints: bps,
        values: bps.map((v) => v * 400),
        cell_ranges: bps.map(() => [null, null]),
      },
      cols: {
        ...base.cols,
        raw: bps,
        breakpoints: bps,
        values: bps.map((v) => v * 6.25),
        cell_ranges: bps.map(() => [null, null]),
      },
      cells,
      overlay: { ...base.overlay, edited_cells: [], axis_edits: [] },
    };
    show({ result });
    const maps = screen
      .getAllByRole("table")
      .filter((t) => t.getAttribute("aria-label")?.startsWith("Map:"));
    // Every cell here is open loop: one lambda map, 256 cell buttons, nothing else.
    expect(maps.map((m) => within(m).getAllByRole("button").length)).toEqual([256]);
    fireEvent.click(screen.getByRole("button", { name: "Δpulse" }));
    const only = screen
      .getAllByRole("table")
      .filter((t) => t.getAttribute("aria-label")?.startsWith("Map:"));
    expect(only).toHaveLength(1);
  });
});

describe("design, provenance and the download", () => {
  it("prints the design summary and the hashes below the maps", () => {
    show();
    const section = screen.getByRole("region", { name: "Design and provenance" });
    const maps = screen.getByRole("region", { name: "Maps" });
    expect(maps.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(section).toHaveTextContent("grid, ZS44A-20260920");
    expect(section).toHaveTextContent("20261015");
    expect(section).toHaveTextContent("fuel-mv-3");
    expect(section).toHaveTextContent("working_assumption");
    expect(within(section).getAllByText("d".repeat(64)).length).toBeGreaterThan(0);
    const counts = within(section).getByRole("table", { name: "Status counts" });
    expect(counts).toHaveTextContent("delivered_lambda");
  });

  it("offers the result as a JSON file, and no image", () => {
    const onDownload = vi.fn();
    show({ onDownload });
    fireEvent.click(screen.getByRole("button", { name: "Download result JSON" }));
    expect(onDownload).toHaveBeenCalledTimes(1);
    const downloads = screen
      .getAllByRole("button")
      .map((button) => button.textContent ?? "")
      .filter((text) => /download/i.test(text));
    expect(downloads).toEqual(["Download result JSON"]);
    expect(screen.queryAllByRole("link")).toEqual([]);
    expect(screen.queryByText(/download (the )?(image|candidate|rom)\b/i)).not.toBeInTheDocument();
  });
});
