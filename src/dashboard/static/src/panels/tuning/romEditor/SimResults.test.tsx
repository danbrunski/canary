import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { groupAssumptions, STATUSES } from "../../../lib/romSimView";
import {
  buildCellTable,
  buildRunContext,
  buildTimingBlock,
  output,
  buildSimResult,
  tablesWithAxes,
  BASE_SHA,
  CANDIDATE_SHA,
  SIM_ASSUMPTIONS,
  VE_ASSUMPTION,
} from "../../../test/romSimFixtures";
import type { RomSimResult, SimCell, SimRunContext } from "../../../types/romEditor";
import type { RomTableValues } from "../../../types/romLibrary";
import { SimResults } from "./SimResults";

function show(
  options: {
    result?: RomSimResult;
    run?: SimRunContext;
    stale?: boolean;
    tables?: RomTableValues[];
    onDownload?: () => void;
  } = {},
) {
  return render(
    <SimResults
      result={options.result ?? buildSimResult()}
      run={options.run ?? buildRunContext()}
      stale={options.stale ?? false}
      tables={options.tables}
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
    for (const text of [...result.assumptions, ...result.unresolved]) {
      expect(within(caveats).getByText(text)).toBeVisible();
    }
  });

  it("groups the assumptions by their prefix and shows the held VE once", () => {
    show();
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(VE_ASSUMPTION).toMatch(
      /^plant working assumption: manifold_pressure \(volumetric_efficiency\) = 0\.7, bracket \[0\.4, 1\.0\], held at that value \(/,
    );
    // `caveats` repeats it (E08.10); the page shows it once.
    expect(buildSimResult().caveats.filter((item) => item === VE_ASSUMPTION)).toHaveLength(1);
    expect(within(caveats).getAllByText(VE_ASSUMPTION)).toHaveLength(1);
    const group = (name: RegExp) => within(caveats).getByRole("heading", { name }).parentElement!;
    expect(within(group(/^Plant working assumptions/)).getByText(VE_ASSUMPTION)).toBeVisible();
    expect(within(group(/^Plant working assumptions/)).getAllByRole("listitem")).toHaveLength(2);
    expect(within(group(/^The design's own assumptions/)).getAllByRole("listitem")).toHaveLength(1);
    expect(within(group(/^Other assumptions/)).getAllByRole("listitem")).toHaveLength(1);
  });

  it("shows a string that is in both the caveats and the assumptions once, and no other twice", () => {
    const result = buildSimResult();
    const twice = "design: stated twice by the run";
    result.assumptions = [...result.assumptions, twice, twice];
    result.caveats = [...result.caveats, twice, "a plain caveat", "a plain caveat"];
    show({ result });
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(within(caveats).getAllByText(twice)).toHaveLength(1);
    expect(within(caveats).getAllByText("a plain caveat")).toHaveLength(1);
    for (const text of SIM_ASSUMPTIONS) {
      expect(within(caveats).getAllByText(text)).toHaveLength(1);
    }
    // Every list item in the block is a distinct string.
    const items = within(caveats)
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "")
      .filter((text) => text.length > 30);
    expect(new Set(items).size).toBe(items.length);
  });

  it("falls back to a plain list for assumptions with no known prefix", () => {
    const result = buildSimResult();
    result.assumptions = ["rail pressure is stated", "pedal flag is stated"];
    result.caveats = [...result.cells.caveats.slice(0, 4), ...result.assumptions];
    show({ result });
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(within(caveats).getByRole("heading", { name: /^Other assumptions/ })).toBeVisible();
    expect(within(caveats).getAllByText("rail pressure is stated")).toHaveLength(1);
    expect(
      within(caveats).getByRole("heading", { name: /^Plant working assumptions.*\(0\)/ }),
    ).toBeVisible();
    expect(groupAssumptions(result.assumptions)).toEqual({
      plant: [],
      design: [],
      other: result.assumptions,
    });
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

describe("the coarse-run banner (W09.9)", () => {
  /** A result of this size: the figures the banner reads are the result's own. */
  function sized(draws: number, points: number): RomSimResult {
    const result = buildSimResult();
    return {
      ...result,
      cells: { ...result.cells, draws },
      design: { ...result.design, points },
    };
  }
  const fine = (points: number | null) => ({
    ...buildRunContext(),
    fineReference: { points, draws: 1000 },
  });
  const banner = () => screen.queryByRole("note", { name: "Coarse run" });

  it("shows for a run with few draws and few points, stating both and the E08.3 wording", () => {
    show({ result: sized(200, 64), run: fine(224) });
    const note = banner() as HTMLElement;
    expect(note).toBeVisible();
    expect(note).toHaveTextContent("200 draws × 64 points");
    expect(note).toHaveTextContent("200 draws, below the Fine preset's 1,000");
    expect(note).toHaveTextContent("64 points, below the Fine preset's 224");
    expect(note).toHaveTextContent("Quantiles over few draws are noisy");
    expect(note).toHaveTextContent(
      "E08.3 recorded that three draws give only a coarse per-cell spread",
    );
    expect(note).toHaveTextContent("Fine preset runs 1,000 draws on 224 points");
  });

  it("says no_points is not unchanged, from the result's own counts", () => {
    show({ result: sized(1000, 64), run: fine(224) });
    const note = banner() as HTMLElement;
    expect(note).toHaveTextContent("Cells with no visited point read no_points");
    expect(note).toHaveTextContent("not unchanged");
    expect(note).toHaveTextContent(/\(\d+ of 12 cells here\)/);
    // The cells themselves keep their no-points label under a coarse result.
    const map = lambdaMap();
    expect(byStatus(map, "no_points").length).toBeGreaterThan(0);
    expect(byStatus(map, "no_points")[0].getAttribute("aria-label")).toMatch(/no points/);
    expect(byStatus(map, "no_points")[0].textContent).toBe("–");
    expect(byStatus(map, "no_points")[0].textContent).not.toBe("0");
  });

  it("shows for a hand-set low draw count alone, with no claim about unvisited cells", () => {
    show({ result: sized(50, 224), run: fine(224) });
    const note = banner() as HTMLElement;
    expect(note).toHaveTextContent("50 draws × 224 points");
    expect(note).not.toHaveTextContent("Cells with no visited point");
  });

  it("shows for a low point count alone", () => {
    show({ result: sized(1000, 100), run: fine(224) });
    expect(banner()).toHaveTextContent("100 points, below the Fine preset's 224");
  });

  it("does not show for a fine run", () => {
    const { unmount } = show({ result: sized(1000, 224), run: fine(224) });
    expect(banner()).not.toBeInTheDocument();
    unmount();
    show({ result: sized(2000, 1024), run: fine(224) });
    expect(banner()).not.toBeInTheDocument();
  });

  it("is above the maps when it shows", () => {
    show({ result: sized(200, 64), run: fine(224) });
    const note = banner() as HTMLElement;
    const maps = screen.getByRole("region", { name: "Maps" });
    expect(note.compareDocumentPosition(maps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("region", { name: "Read this first" })).toBeVisible();
  });

  it("judges only the draws when the Fine grid's size is unknown", () => {
    show({ result: sized(1000, 4), run: fine(null) });
    expect(banner()).not.toBeInTheDocument();
  });
});

describe("the WOT column caveat", () => {
  const withFlag = (wotColumnEdited: boolean, pedal_flag: number): SimRunContext => {
    const run = buildRunContext();
    return { ...run, wotColumnEdited, request: { ...run.request, pedal_flag } };
  };
  const caveats = () => screen.getByRole("region", { name: "Read this first" });
  const LINE =
    "The WOT column was edited with pedal flag 0: this run cannot show that edit's effect.";

  it("is a line in the caveat block when the last column was edited with bit 0 clear", () => {
    show({ run: withFlag(true, 0) });
    expect(within(caveats()).getByText(LINE)).toBeVisible();
  });

  it("states the flag it was run with", () => {
    show({ run: withFlag(true, 6) });
    expect(within(caveats()).getByText(LINE.replace("flag 0", "flag 6"))).toBeVisible();
  });

  it("is absent when bit 0 was set, or the last column was not edited", () => {
    const { unmount } = show({ run: withFlag(true, 1) });
    expect(caveats()).not.toHaveTextContent("The WOT column was edited");
    unmount();
    show({ run: withFlag(false, 0) });
    expect(caveats()).not.toHaveTextContent("The WOT column was edited");
  });
});

describe("the ignition timing section (E09.7)", () => {
  const withTiming = (kind: Parameters<typeof buildTimingBlock>[0]): RomSimResult => ({
    ...buildSimResult(),
    ignition_timing: buildTimingBlock(kind),
  });
  const section = () => screen.getByRole("region", { name: "Ignition timing" });
  const grid = () => within(section()).getByRole("table", { name: /per trim cell/ });
  const marks = (kind: string) =>
    Array.from(grid().querySelectorAll(`[data-timing-mark="${kind}"]`));

  it("comes after the caveats and the maps, never before them", () => {
    show({ result: withTiming("trim_b"), tables: tablesWithAxes().tables });
    const caveats = screen.getByRole("region", { name: "Read this first" });
    expect(
      caveats.compareDocumentPosition(section()) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      screen.getAllByRole("figure")[0]?.compareDocumentPosition(section()) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows the stated state, the change per edited cell on the trims' axes, and 'no change' elsewhere", () => {
    show({ result: withTiming("trim_b"), tables: tablesWithAxes().tables });
    expect(section()).toHaveTextContent(
      "HI DET latch set (warm timing reads trim B), knock control in fallback (knock sensor fault)",
    );
    expect(section()).toHaveTextContent(
      "coolant 88 °C (your stated coolant), pedal flag bit 0 clear",
    );
    expect(section()).toHaveTextContent("256 in the grid: 2 changed, 0 edited but not read");
    expect(marks("changed").map((cell) => cell.textContent)).toEqual(["-3", "-3"]);
    expect(marks("unchanged")).toHaveLength(254);
    expect(marks("not_read")).toHaveLength(0);
    // The cell is named by its place and what it says, with its axes beside it.
    expect(within(grid()).getByRole("cell", { name: /^Row 4, column 4: -3/ })).toBeVisible();
    expect(within(grid()).getByRole("rowheader", { name: "1,600" })).toBeVisible();
    expect(within(grid()).getByRole("columnheader", { name: "32" })).toBeVisible();
    expect(section()).toHaveTextContent("unsupported: spark_response_unmeasured");
    expect(section()).toHaveTextContent("It is no torque claim and no knock claim.");
  });

  it("explains every reason it names, in the block's own words and a sentence", () => {
    show({ result: withTiming("trim_b"), tables: tablesWithAxes().tables });
    const reasons = within(section()).getByRole("list", { name: "Timing reasons" });
    expect(within(reasons).getAllByRole("listitem")).toHaveLength(1);
    expect(reasons).toHaveTextContent("clamp_headroom_unknown");
    expect(reasons).toHaveTextContent("2 trim cells");
    expect(reasons).toHaveTextContent(/50-degree cap and the -7 floor/);
  });

  it("draws an edited trim the stated latch does not read as NR with its reason, never as 0", () => {
    show({ result: withTiming("trim_a"), tables: tablesWithAxes().tables });
    expect(marks("not_read").map((cell) => cell.textContent)).toEqual(["NR", "NR"]);
    expect(marks("changed")).toHaveLength(0);
    expect(Array.from(grid().querySelectorAll("td")).some((cell) => cell.textContent === "0")).toBe(
      false,
    );
    const reasons = within(section()).getByRole("list", { name: "Timing reasons" });
    expect(reasons).toHaveTextContent("trim_a_not_read:high_det_latch_set");
    expect(reasons).toHaveTextContent(
      "Trim A was edited here, but the HI DET latch is set, so the ECM reads trim B warm",
    );
    expect(section()).toHaveTextContent("2 edited but not read");
  });

  it("gives no grid for an edit outside the trims: unevaluable, with the node that reaches timing", () => {
    show({ result: withTiming("fuel_comp"), tables: tablesWithAxes().tables });
    expect(within(section()).queryByRole("table", { name: /per trim cell/ })).toBeNull();
    expect(section()).toHaveTextContent("unevaluable");
    expect(section()).toHaveTextContent("no number for this edit; reasons below");
    const reasons = within(section()).getByRole("list", { name: "Timing reasons" });
    expect(reasons).toHaveTextContent("timing_input_changed:fuel_comp_lookup");
    expect(reasons).toHaveTextContent(/reaches ignition timing through fuel_comp_lookup/);
    expect(reasons).not.toHaveTextContent("base_timing_load_term_unmodelled");
    expect(reasons).toHaveTextContent("charge_word_1b24_unresolved");
    expect(reasons).toHaveTextContent("charge_word_inputs_moved");
    expect(reasons).toHaveTextContent("inputs moved: 0xFFFF1736 via X, 0xFFFF175C via BFS");
    expect(reasons).toHaveTextContent("200 design pairs");
    expect(reasons).toHaveTextContent("term_279a_unmodelled");
    expect(reasons).toHaveTextContent("150 design pairs");
    expect(reasons).toHaveTextContent("50 design pairs");
    expect(section()).toHaveTextContent(
      "200 (point, draw) pairs on 2 points: range 150, unevaluable 50",
    );
    // The ranged pairs are said apart; no pair has an exact number, so 0 show a change.
    expect(section()).toHaveTextContent("0 show a change, 150 known only as a range");
  });

  it("draws a range cell as its ends, counts it, and says what makes it a range", () => {
    show({ result: withTiming("range"), tables: tablesWithAxes().tables });
    expect(marks("range").map((cell) => cell.textContent)).toEqual(["-3..+1"]);
    expect(marks("changed").map((cell) => cell.textContent)).toEqual(["-2"]);
    expect(marks("unevaluable")).toHaveLength(0);
    const cell = within(grid()).getByRole("cell", { name: /^Row 4, column 4: -3\.\.\+1/ });
    expect(cell).toHaveAttribute("data-timing-mark", "range");
    expect(section()).toHaveTextContent(
      "256 in the grid: 2 changed (1 known only as a range), 0 edited but not read, 0 unevaluable",
    );
    expect(section()).toHaveTextContent("50 show a change, 150 known only as a range");
    const legend = within(section()).getByRole("list", { name: "Legend: ignition timing" });
    expect(legend.querySelector('[data-mark="range"]')).not.toBeNull();
    const reasons = within(section()).getByRole("list", { name: "Timing reasons" });
    expect(reasons).toHaveTextContent(/swept over its plausible band/);
    // A base-table edit leaves 1B24's inputs alone: no moved inputs to name.
    expect(reasons).not.toHaveTextContent("inputs moved");
  });

  it("asks how many design pairs show a change only when pairs carry a number", () => {
    show({ result: withTiming("trim_b"), tables: tablesWithAxes().tables });
    expect(section()).toHaveTextContent("200 (point, draw) pairs on 2 points");
    expect(section()).toHaveTextContent("0 show a change");
  });

  it("draws a real change as its degrees when a trim edited beside it is not read, and says so on hover", () => {
    // Trim A and trim B both -2 on row 8, columns 4-11, the latch set: the ECM reads
    // trim B, so every cell changes by -2; trim A's edit is named, not drawn over it.
    show({ result: withTiming("both"), tables: tablesWithAxes().tables });
    expect(marks("changed").map((cell) => cell.textContent)).toEqual(Array(8).fill("-2"));
    expect(marks("not_read")).toHaveLength(0);
    const cell = within(grid()).getByRole("cell", { name: /^Row 8, column 4: -2/ });
    expect(cell).toHaveAttribute("data-timing-mark", "changed");
    expect(cell).toHaveAttribute(
      "title",
      "-2 deg if no clamp binds; also edited here but not read: trim_a_not_read:high_det_latch_set",
    );
    // The summary and the grid now say the same thing.
    expect(section()).toHaveTextContent("256 in the grid: 8 changed, 8 edited but not read");
    const reasons = within(section()).getByRole("list", { name: "Timing reasons" });
    expect(reasons).toHaveTextContent("trim_a_not_read:high_det_latch_set");
    expect(reasons).toHaveTextContent("clamp_headroom_unknown");
  });

  it("does not call a trim the ECM reads, set below the term's floor, edited but not read", () => {
    show({ result: withTiming("floor"), tables: tablesWithAxes().tables });
    expect(marks("unevaluable").map((cell) => cell.textContent)).toEqual(["U"]);
    expect(section()).toHaveTextContent(
      "256 in the grid: 0 changed, 0 edited but not read, 1 unevaluable",
    );
    const reasons = within(section()).getByRole("list", { name: "Timing reasons" });
    expect(reasons).toHaveTextContent("trim_term_floor_may_bind");
  });

  it("labels the grid from the cells when the base's trims are not at hand, and says it did", () => {
    show({ result: withTiming("trim_b"), tables: [] });
    expect(within(grid()).getByRole("rowheader", { name: "rpm word 128" })).toBeVisible();
    expect(within(grid()).getByRole("columnheader", { name: "X 32" })).toBeVisible();
    expect(section()).toHaveTextContent("rpm words");
  });

  it("scrolls the grid inside its own box and wraps the facts in the result's own list style", () => {
    show({ result: withTiming("trim_b"), tables: tablesWithAxes().tables });
    expect(grid().closest("[data-table-scroll]")).not.toBeNull();
    const facts = section().querySelector("dl");
    expect(facts?.className).toContain("grid-cols-[max-content_minmax(0,1fr)]");
  });

  it("lists what is held equal, folded away", () => {
    show({ result: withTiming("trim_b"), tables: tablesWithAxes().tables });
    const folded = section().querySelector("details");
    expect(folded).not.toBeNull();
    expect(folded).toHaveTextContent("What is held equal on both ROMs (4)");
    expect(folded).toHaveTextContent("every chain term but the trim term 0xFFFF2793");
  });
});
