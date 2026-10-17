import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { buildCellTable, output } from "../test/romSimFixtures";
import {
  CHANGED_ZERO_LABEL,
  DESIGN_ASSUMPTION_PREFIX,
  PLANT_ASSUMPTION_PREFIX,
  COLOURED_MARKS,
  MARKS,
  MARK_LABELS,
  MARK_WORDS,
  STATUSES,
  cellLines,
  describePick,
  extentOf,
  groupAssumptions,
  hasChangedZero,
  hatchStyle,
  inCell,
  mapsForView,
  markStyle,
  marksInUse,
  movedAxes,
  quantileLines,
  supportLines,
  type Mark,
  type Pick,
} from "./romSimView";

/** `sim_cells.STATUSES` from the module itself, so a status added there and
 *  not here fails this test rather than rendering as an unlabelled cell. */
function pythonStatuses(): string[] {
  const source = readFileSync(resolve(process.cwd(), "../../analysis/sim_cells.py"), "utf8");
  const found = /STATUSES: tuple\[CellStatus, \.\.\.\] = \(([\s\S]*?)\)/.exec(source);
  expect(found).not.toBeNull();
  return [...(found?.[1] ?? "").matchAll(/"(\w+)"/g)].map((match) => match[1]);
}

const table = buildCellTable();
const cellAt = (row: number, col: number) => table.cells[row * table.cols.breakpoints.length + col];

function map(view: Parameters<typeof mapsForView>[1], key: string) {
  const found = mapsForView(table, view).maps.find((item) => item.key === key);
  expect(found).toBeDefined();
  return found!;
}

describe("statuses", () => {
  it("are sim_cells.STATUSES, in order", () => {
    expect([...STATUSES]).toEqual(pythonStatuses());
  });

  it("each have a word; each non-value status has its own label", () => {
    for (const mark of MARKS) expect(MARK_WORDS[mark]).toBeTruthy();
    const labels = Object.values(MARK_LABELS);
    expect(new Set(labels).size).toBe(labels.length);
    expect(COLOURED_MARKS).toEqual(["changed", "unchanged"]);
  });
});

describe("the maps of each view", () => {
  it("draws one lambda map per loop that has pairs, open loop first", () => {
    const { maps, absent } = mapsForView(table, "lambda");
    expect(maps.map((item) => item.key)).toEqual([
      "headline-open_loop",
      "headline-closed_loop",
      "headline-closed_loop_setpoint_moved",
    ]);
    expect(maps.map((item) => item.unit)).toEqual(["%", "% points", "%"]);
    expect(absent).toEqual([]);
  });

  it("says which map is missing when a loop has no pairs", () => {
    const openOnly = {
      ...table,
      cells: table.cells.map((cell) => ({
        ...cell,
        by_loop: cell.by_loop.filter((block) => block.loop === "open_loop"),
      })),
    };
    const { maps, absent } = mapsForView(openOnly, "lambda");
    expect(maps.map((item) => item.key)).toEqual(["headline-open_loop"]);
    expect(absent).toHaveLength(2);
    expect(absent[0]).toMatch(/no closed_loop pairs in this run/);
  });

  it("draws the width for open loop only, and ΔBFS and Δpulse over all pairs", () => {
    expect(mapsForView(table, "width").maps.map((item) => [item.key, item.scale])).toEqual([
      ["width-open_loop", "sequential"],
    ]);
    expect(mapsForView(table, "bfs").maps.map((item) => item.key)).toEqual([
      "output-base_fuel_schedule",
    ]);
    expect(mapsForView(table, "pulse").maps.map((item) => [item.key, item.unit])).toEqual([
      ["output-injector_pulse_ms", "ms"],
    ]);
    const noPulse = {
      ...table,
      outputs: table.outputs.filter((name) => name !== "injector_pulse_ms"),
    };
    expect(mapsForView(noPulse, "pulse").absent).toEqual(["the table has no injector_pulse_ms"]);
  });
});

describe("what a lambda map cell shows", () => {
  const lambda = () => map("lambda", "headline-open_loop");

  it("gives every status its own mark", () => {
    const marks = table.cells.map((cell) => lambda().pick(cell).mark);
    expect(marks).toEqual([
      "changed", // (0,0) -1.52 %
      "unchanged", // (0,1)
      "unsupported", // (0,2) computed only
      "partly_supported", // (0,3)
      "no_points", // (1,0) never visited
      "not_reached", // (1,1)
      "other_loop", // (1,2) every pair closed loop
      "changed", // (1,3) mixed: the open-loop pairs
      "undefined", // (2,0) every base 0
      "other_loop", // (2,1) setpoint moved
      "changed", // (2,2)
      "no_points", // (2,3)
    ]);
    expect(new Set(marks)).toEqual(new Set([...STATUSES, "other_loop", "undefined"]));
  });

  it("colours only a computed value: relative lambda in percent, negative is richer", () => {
    const changed = lambda().pick(cellAt(0, 0));
    expect(changed.value).toBeCloseTo(-1.52, 6);
    expect(cellLines(changed, cellAt(0, 0), true)).toEqual(["-1.52"]);
    expect(cellLines(lambda().pick(cellAt(0, 1)), cellAt(0, 1), true)).toEqual(["0"]);
  });

  it("shows an unsupported cell's computed value as text, never as a value", () => {
    const pick = lambda().pick(cellAt(0, 2));
    expect(pick.mark).toBe("unsupported");
    expect(pick.value).toBeNull();
    expect(pick.textValue).toBeCloseTo(-0.6, 6);
    expect(cellLines(pick, cellAt(0, 2), true)).toEqual(["U", "-0.60"]);
  });

  it("labels a partly supported cell with its value and supported share, never a fill", () => {
    const pick = lambda().pick(cellAt(0, 3));
    expect(cellLines(pick, cellAt(0, 3), true)).toEqual(["P+0.40", "· 60%"]);
    const style = markStyle(pick, "diverging", 5);
    expect(style.backgroundImage).toContain("repeating-linear-gradient");
    expect(style.backgroundColor).toBe(hatchStyle("partly_supported").backgroundColor);
  });

  it("labels the rest: no points, not reached, other loop (naming it), undefined", () => {
    expect(cellLines(lambda().pick(cellAt(1, 0)), cellAt(1, 0), true)).toEqual(["–"]);
    expect(cellLines(lambda().pick(cellAt(1, 1)), cellAt(1, 1), true)).toEqual(["NR"]);
    expect(cellLines(lambda().pick(cellAt(1, 2)), cellAt(1, 2), true)).toEqual(["CL"]);
    expect(cellLines(lambda().pick(cellAt(2, 1)), cellAt(2, 1), true)).toEqual(["SP"]);
    expect(cellLines(lambda().pick(cellAt(2, 0)), cellAt(2, 0), true)).toEqual(["∅"]);
  });

  it("describes a cell in one sentence, with its edit and moved breakpoint", () => {
    const text = describePick(table, cellAt(2, 2), lambda().pick(cellAt(2, 2)), "%");
    expect(text).toContain("Row 3, column 3 (1200 rpm, 21.4 %)");
    expect(text).toContain("changed -1.52 %");
    expect(text).toContain("edited, raw 128 to 131");
    expect(text).toContain("a breakpoint moves");
    expect(describePick(table, cellAt(1, 1), lambda().pick(cellAt(1, 1)), "%")).toContain(
      "not reached",
    );
  });
});

describe("closed loop reads its own headline", () => {
  const closed = () => map("lambda", "headline-closed_loop");

  it("shows the total trim, in percentage points, not a lambda effect", () => {
    expect(closed().note).toMatch(/no relative change/);
    const computed = closed().pick(cellAt(1, 2));
    expect(computed.mark).toBe("unsupported");
    expect(computed.textValue).toBeCloseTo(-0.8, 6);
    const supported = closed().pick(cellAt(1, 3));
    expect(supported.mark).toBe("changed");
    expect(supported.value).toBeCloseTo(0.9, 6);
    // Open-loop-only cells are the other loop here, not a trim of zero.
    expect(closed().pick(cellAt(0, 0)).mark).toBe("other_loop");
  });

  it("shows a moved setpoint as the change of the commanded phi", () => {
    const phi = map("lambda", "headline-closed_loop_setpoint_moved");
    const pick = phi.pick(cellAt(2, 1));
    expect(pick.mark).toBe("changed");
    // target_phi has a relative delta, like lambda: +3.1 % of the base setpoint.
    expect(pick.value).toBeCloseTo(3.1, 6);
  });
});

describe("the other maps", () => {
  it("the width is the p05-p95 spread of the relative change, in percent", () => {
    const width = map("width", "width-open_loop").pick(cellAt(0, 0));
    expect(width.mark).toBe("changed");
    expect(width.value).toBeCloseTo(1.0, 6);
  });

  it("ΔBFS is relative, Δpulse is milliseconds", () => {
    expect(map("bfs", "output-base_fuel_schedule").pick(cellAt(0, 0)).value).toBeCloseTo(2.01, 6);
    expect(map("pulse", "output-injector_pulse_ms").pick(cellAt(0, 0)).value).toBeCloseTo(0.052, 6);
    expect(map("pulse", "output-injector_pulse_ms").pick(cellAt(0, 2)).mark).toBe("unsupported");
  });
});

describe("colour", () => {
  const ramp = (value: number | null, mark: Mark = "changed"): Pick => ({
    mark,
    value,
    textValue: null,
    source: null,
    output: "x",
  });

  it("is centred at zero: unchanged is the neutral, equal and opposite are mirrored", () => {
    const neutral = markStyle(ramp(0, "unchanged"), "diverging", 2).backgroundColor;
    expect(markStyle(ramp(0), "diverging", 2).backgroundColor).toBe(neutral);
    const down = markStyle(ramp(-2), "diverging", 2).backgroundColor;
    const up = markStyle(ramp(2), "diverging", 2).backgroundColor;
    expect(down).not.toBe(up);
    expect(down).not.toBe(neutral);
  });

  it("gives every non-value mark a hatch, never a ramp colour", () => {
    for (const mark of MARKS.filter((item) => !COLOURED_MARKS.includes(item))) {
      const style = markStyle(ramp(null, mark), "diverging", 2);
      expect(style.backgroundImage).toMatch(/gradient/);
      expect(style).toEqual(hatchStyle(mark as Exclude<Mark, "changed" | "unchanged">));
    }
  });

  it("takes the extent from coloured cells only", () => {
    const picks: Pick[] = [
      ramp(1.5),
      ramp(-3),
      { ...ramp(99, "partly_supported") },
      { ...ramp(null, "unsupported"), textValue: 50 },
    ];
    expect(extentOf(picks)).toBe(3);
    expect(extentOf([])).toBe(0);
    expect(marksInUse(picks)).toEqual(["changed", "unsupported", "partly_supported"]);
  });
});

describe("detail lines and formatting", () => {
  it("states quantiles, the basis they are over, and why a share is unsupported", () => {
    const oc = cellAt(0, 3).outputs.delivered_lambda;
    const lines = [...quantileLines(oc, "delivered_lambda"), ...supportLines(oc)];
    expect(lines).toEqual(
      expect.arrayContaining([
        "over supported_pairs: 24 pairs, weight 0.25",
        expect.stringMatching(/^Δ p05\/p50\/p95 0 \/ \+0\.004 \/ \+0\.009/),
        expect.stringMatching(/^relative p05\/p50\/p95 0 \/ \+0\.4 \/ \+0\.9 %/),
        "supported 60.0 %, unsupported 40.0 %",
        "changed share of supported weight 100.0 %",
        "supported pairs show: changed",
        "reasons (40.0 %, 16 pairs): fill_byte_outside_plant_range",
      ]),
    );
  });

  it("keeps cell text short", () => {
    expect(inCell(0)).toBe("0");
    expect(inCell(1.234)).toBe("+1.23");
    expect(inCell(-12.34)).toBe("-12.3");
    expect(inCell(123.4)).toBe("+123");
    expect(inCell(5, false)).toBe("5.00");
  });

  it("lists the moved axes", () => {
    expect([...movedAxes(table)]).toEqual(["rows:2"]);
  });
});

describe("a changed cell whose median is exactly 0", () => {
  // Some pairs move, most do not: status `changed`, p50 exactly 0.
  const zeroTable = () => {
    const copy = structuredClone(table);
    const target = copy.cells[2 * 4 + 2];
    const block = output("changed", { delta: [-0.01, 0, 0.01] });
    target.by_loop = [{ ...target.by_loop[0], cell: block }];
    return copy;
  };
  const lambdaOf = (t: typeof table) =>
    mapsForView(t, "lambda").maps.find((item) => item.key === "headline-open_loop")!;

  it("does not print the '0' of an unchanged cell", () => {
    const changed = zeroTable();
    const pick = lambdaOf(changed).pick(changed.cells[2 * 4 + 2]);
    expect(pick.mark).toBe("changed");
    expect(pick.value).toBe(0);
    expect(cellLines(pick, changed.cells[2 * 4 + 2], true)).toEqual([CHANGED_ZERO_LABEL]);
    expect(CHANGED_ZERO_LABEL).not.toBe("0");
    // The unchanged cell keeps its plain 0.
    const unchanged = lambdaOf(changed).pick(changed.cells[1]);
    expect(unchanged.mark).toBe("unchanged");
    expect(cellLines(unchanged, changed.cells[1], true)).toEqual(["0"]);
  });

  it("says so in words, and the legend knows when a map has one", () => {
    const changed = zeroTable();
    const map = lambdaOf(changed);
    const picks = changed.cells.map((cell) => map.pick(cell));
    expect(describePick(changed, changed.cells[2 * 4 + 2], picks[2 * 4 + 2], "%")).toContain(
      "changed, median exactly 0 % (some pairs move)",
    );
    expect(hasChangedZero(picks)).toBe(true);
    const plain = lambdaOf(table);
    expect(hasChangedZero(table.cells.map((cell) => plain.pick(cell)))).toBe(false);
  });
});

describe("the assumption prefixes", () => {
  const python = (file: string, name: string): string => {
    const source = readFileSync(resolve(process.cwd(), "../../analysis", file), "utf8");
    const found = new RegExp(`^${name} = "([^"]*)"`, "m").exec(source);
    expect(found).not.toBeNull();
    return found?.[1] ?? "";
  };

  it("are the ones the simulator writes, read from its modules", () => {
    expect(PLANT_ASSUMPTION_PREFIX).toBe(python("paired_scenarios.py", "PLANT_ASSUMPTION_PREFIX"));
    expect(DESIGN_ASSUMPTION_PREFIX).toBe(python("sim_montecarlo.py", "DESIGN_ASSUMPTION_PREFIX"));
  });

  it("split the list into groups, each string once, unprefixed ones last", () => {
    const plant = `${PLANT_ASSUMPTION_PREFIX}rail_pressure = 350`;
    const design = `${DESIGN_ASSUMPTION_PREFIX}MAF solved at VE 0.7`;
    expect(groupAssumptions([design, "plain", plant, plant, "plain"])).toEqual({
      plant: [plant],
      design: [design],
      other: ["plain"],
    });
    expect(groupAssumptions([])).toEqual({ plant: [], design: [], other: [] });
  });
});
