import { describe, expect, it } from "vitest";

import { buildTimingBlock, tablesWithAxes } from "../test/romSimFixtures";
import type { IgnitionTimingBlock } from "../types/romEditor";
import {
  claimLine,
  pointSummary,
  reasonCounts,
  signed,
  stateText,
  timingGrid,
  timingMark,
  timingReasonText,
} from "./romSimTiming";

const TABLES = tablesWithAxes().tables;

describe("timingMark", () => {
  it("draws a cell the block does not list as no change", () => {
    expect(timingMark(null)).toMatchObject({ kind: "unchanged", text: "·" });
  });

  it("draws a change as signed degrees, and says when no clamp was checked", () => {
    const [cell] = buildTimingBlock("trim_b").cells ?? [];
    expect(timingMark(cell ?? null)).toEqual({
      kind: "changed",
      text: "-3",
      title: "-3 deg if no clamp binds",
    });
    expect(signed(2)).toBe("+2");
    expect(signed(-3)).toBe("-3");
    expect(signed(0)).toBe("0");
  });

  it("never draws a trim the stated latch does not read as 0", () => {
    const [cell] = buildTimingBlock("trim_a").cells ?? [];
    expect(cell?.delta).toBe(0);
    expect(timingMark(cell ?? null)).toMatchObject({ kind: "not_read", text: "NR" });
    expect(timingMark(cell ?? null).title).toContain("trim_a_not_read:high_det_latch_set");
  });

  it("draws a real change as its degrees when a trim edited beside it is not read", () => {
    // Trim A and B both -2 across the flag edge, the latch set: B is read, so the cell
    // changes by -2 and trim A's edit is named in the title, never drawn over the number.
    const cells = buildTimingBlock("both").cells ?? [];
    expect(cells).toHaveLength(8);
    for (const cell of cells) {
      expect(cell.delta).toBe(-2);
      expect(timingMark(cell)).toEqual({
        kind: "changed",
        text: "-2",
        title:
          "-2 deg if no clamp binds; also edited here but not read: trim_a_not_read:high_det_latch_set",
      });
    }
  });

  it("draws NR only when the cell's delta is 0", () => {
    const [cell] = buildTimingBlock("both").cells ?? [];
    expect(timingMark(cell ? { ...cell, delta: 0 } : null)).toMatchObject({
      kind: "not_read",
      text: "NR",
    });
    expect(timingMark(cell ?? null).kind).toBe("changed");
  });

  it("draws an unevaluable cell as U with its reasons", () => {
    const mark = timingMark({
      row: 0,
      col: 0,
      rpm_word: 32,
      x: 8,
      base: null,
      candidate: null,
      delta: null,
      trim_term_delta: null,
      status: "unevaluable",
      reasons: ["trim_term_floor_may_bind"],
      table_read: "a",
    });
    expect(mark).toMatchObject({ kind: "unevaluable", text: "U" });
    expect(mark.title).toContain("trim_term_floor_may_bind");
  });
});

describe("timingGrid", () => {
  it("labels the 16 x 16 grid from the base's own trim axes and places each listed cell", () => {
    const grid = timingGrid(buildTimingBlock("trim_b"), TABLES);
    expect(grid).not.toBeNull();
    expect(grid?.labelsFrom).toBe("trim axes");
    expect(grid?.mismatch).toBeNull();
    expect(grid?.rowLabels).toHaveLength(16);
    expect(grid?.colLabels).toHaveLength(16);
    // rpm = axis byte x 50, X = the axis byte itself.
    expect(grid?.rowLabels[4]).toBe("2,000");
    expect(grid?.colLabels[9]).toBe("80");
    expect(grid?.cells.flat().filter(Boolean)).toHaveLength(2);
    expect(grid?.cells[4]?.[9]?.delta).toBe(-3);
    expect(grid?.cells[0]?.[0]).toBeNull();
  });

  it("falls back to the cells' own coordinates when the base has no trim table, and says so", () => {
    const grid = timingGrid(buildTimingBlock("trim_b"), []);
    expect(grid?.labelsFrom).toBe("cells");
    expect(grid?.rowLabels[4]).toBe("rpm word 160");
    expect(grid?.colLabels[9]).toBe("X 80");
    expect(grid?.rowLabels[0]).toBe("1");
    expect(grid?.mismatch).toBeNull();
  });

  it("refuses to label from axes that disagree with the cells", () => {
    const block = buildTimingBlock("trim_b");
    const cells = (block.cells ?? []).map((cell, i) => (i === 1 ? { ...cell, x: 81 } : cell));
    const grid = timingGrid({ ...block, cells }, TABLES);
    expect(grid?.labelsFrom).toBe("cells");
    expect(grid?.mismatch).toContain("1 listed cell is at coordinates the base's trim axes");
  });

  it("places the eight cells of a both-trims edit and counts them as the backend does", () => {
    const block = buildTimingBlock("both");
    const grid = timingGrid(block, TABLES);
    expect(grid?.cells.flat().filter(Boolean)).toHaveLength(8);
    expect(grid?.cells[7]?.slice(3, 11).every((cell) => cell?.delta === -2)).toBe(true);
    expect(block.summary).toMatchObject({ changed: 8, changed_trim_not_read: 8, unevaluable: 0 });
    // A trim the ECM reads, below the term's floor, is unevaluable and not "not read".
    expect(buildTimingBlock("floor").summary).toMatchObject({
      changed: 0,
      changed_trim_not_read: 0,
      unevaluable: 1,
    });
  });

  it("is null for an unevaluable block: no grid, so no 0 to misread", () => {
    expect(timingGrid(buildTimingBlock("fuel_comp"), TABLES)).toBeNull();
  });
});

describe("the reasons", () => {
  it("counts each reason over the trim cells and the design pairs", () => {
    const counts = reasonCounts(buildTimingBlock("trim_b"));
    expect(counts).toEqual([
      {
        reason: "clamp_headroom_unknown",
        cells: 2,
        pairs: 0,
        text: expect.stringContaining("50-degree cap"),
      },
    ]);
    const k = reasonCounts(buildTimingBlock("fuel_comp"));
    expect(k.map((item) => item.reason)).toEqual([
      "base_timing_load_term_unmodelled",
      "timing_input_changed:fuel_comp_lookup",
    ]);
    expect(k.find((item) => item.reason.startsWith("timing_input"))).toMatchObject({
      cells: 0,
      pairs: 200,
    });
  });

  it("has a sentence for the families the block names, and none for a stranger", () => {
    expect(timingReasonText("trim_a_not_read:high_det_latch_set")).toContain(
      "Trim A was edited here, but the HI DET latch is set, so the ECM reads trim B warm",
    );
    expect(timingReasonText("trim_b_not_read:high_det_latch_clear")).toContain(
      "the HI DET latch is clear, so the ECM reads trim A warm",
    );
    expect(timingReasonText("trim_b_not_read:coolant_at_or_below_56c")).toContain("56 °C");
    expect(timingReasonText("timing_input_changed:air_charge_tp")).toContain("air_charge_tp");
    expect(timingReasonText("unknown_bytes_changed")).toContain("no node");
    expect(timingReasonText("effects_unevaluable:wrong_program")).toContain("wrong_program");
    expect(timingReasonText("a_reason_from_a_newer_daemon")).toBeNull();
  });

  it("counts the design's pairs by status and how many show a change", () => {
    const block = buildTimingBlock("fuel_comp");
    expect(pointSummary(block.points)).toEqual({
      pairs: 200,
      points: 2,
      byStatus: { unevaluable: 200 },
      numeric: 0,
      changed: 0,
    });
    // An evaluated block's pairs carry a number (0 here), so "how many change" has an answer.
    expect(pointSummary(buildTimingBlock("trim_b").points)).toMatchObject({
      pairs: 200,
      numeric: 200,
      changed: 0,
    });
    expect(pointSummary(undefined)).toEqual({
      pairs: 0,
      points: 0,
      byStatus: {},
      numeric: 0,
      changed: 0,
    });
  });
});

describe("words", () => {
  it("states the latch and knock control as the run used them", () => {
    const block = buildTimingBlock("trim_b") as IgnitionTimingBlock & {
      state: NonNullable<IgnitionTimingBlock["state"]>;
    };
    expect(stateText(block.state)).toBe(
      "HI DET latch set (warm timing reads trim B), knock control in fallback (knock sensor fault)",
    );
    expect(
      stateText({ high_det: false, knock_control: "active", source: "stated: ROM Editor" }),
    ).toBe("HI DET latch clear (warm timing reads trim A), knock control active");
  });

  it("states a torque or knock claim as the refusal the block carries", () => {
    expect(claimLine(buildTimingBlock("trim_b").torque)).toBe(
      "unsupported: spark_response_unmeasured: spark has no measured torque response (+1 more)",
    );
    expect(claimLine({ status: "supported", reasons: [] })).toBe("supported");
  });
});
