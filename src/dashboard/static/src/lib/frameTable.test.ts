import { describe, expect, it } from "vitest";

import { frame } from "../test/workbenchFixtures";
import {
  OVERSCAN,
  ROW_HEIGHT,
  VIEWPORT_ROWS,
  formatFrameValue,
  frameCsv,
  frameCsvName,
  sortedRowOrder,
  visibleRowWindow,
} from "./frameTable";

describe("formatFrameValue", () => {
  it("shows three places without trailing zeros, whole numbers from 1000, blank when missing", () => {
    expect(formatFrameValue(12.5)).toBe("12.5");
    expect(formatFrameValue(3)).toBe("3");
    expect(formatFrameValue(0.12345)).toBe("0.123");
    expect(formatFrameValue(999.9996)).toBe("1000");
    expect(formatFrameValue(1000.4)).toBe("1000");
    expect(formatFrameValue(-1234.5)).toBe("-1235");
    expect(formatFrameValue(null)).toBe("");
    expect(formatFrameValue(undefined)).toBe("");
    expect(formatFrameValue(Number.NaN)).toBe("");
  });
});

describe("sortedRowOrder", () => {
  const rows = frame({
    timestamps: ["a", "b", "c", "d"],
    values: { x: [30, null, 10, 20], y: [null, null, 1, 2] },
  });

  it("is the frame's own order by time, or its reverse, without disturbing the frame", () => {
    expect(sortedRowOrder(rows, { key: "timestamp", dir: 1 })).toEqual([0, 1, 2, 3]);
    expect(sortedRowOrder(rows, { key: "timestamp", dir: -1 })).toEqual([3, 2, 1, 0]);
    expect(rows.timestamps).toEqual(["a", "b", "c", "d"]);
  });

  it("sorts a channel by value with a missing value last whichever way it runs", () => {
    expect(sortedRowOrder(rows, { key: "x", dir: 1 })).toEqual([2, 3, 0, 1]);
    expect(sortedRowOrder(rows, { key: "x", dir: -1 })).toEqual([0, 3, 2, 1]);
  });

  it("keeps the rows with a value ahead of those without, among several gaps", () => {
    const order = sortedRowOrder(rows, { key: "y", dir: 1 });
    expect(order.slice(0, 2)).toEqual([2, 3]);
    expect(new Set(order.slice(2))).toEqual(new Set([0, 1]));
  });

  it("orders equal values by their original order", () => {
    const ties = frame({ timestamps: ["a", "b", "c"], values: { x: [1, 1, 1] } });
    expect(sortedRowOrder(ties, { key: "x", dir: 1 })).toEqual([0, 1, 2]);
    expect(sortedRowOrder(ties, { key: "x", dir: -1 })).toEqual([0, 1, 2]);
  });
});

describe("visibleRowWindow", () => {
  const span = VIEWPORT_ROWS + 2 * OVERSCAN;

  it("renders the rows in view plus the overscan below at the top of the scroll", () => {
    expect(visibleRowWindow(0, 500)).toEqual({ first: 0, last: span });
  });

  it("starts the overscan above the first row in view", () => {
    expect(visibleRowWindow(100 * ROW_HEIGHT, 500)).toEqual({ first: 80, last: 80 + span });
  });

  it("stays at the top for the first rows of overscan and for an overscrolled negative position", () => {
    expect(visibleRowWindow(OVERSCAN * ROW_HEIGHT - 1, 500).first).toBe(0);
    expect(visibleRowWindow(-30, 500).first).toBe(0);
  });

  it("changes only on a whole row of scroll", () => {
    expect(visibleRowWindow(100 * ROW_HEIGHT + ROW_HEIGHT - 1, 500)).toEqual(
      visibleRowWindow(100 * ROW_HEIGHT, 500),
    );
    expect(visibleRowWindow(101 * ROW_HEIGHT, 500).first).toBe(81);
  });

  it("ends at the last row, and covers a short table whole", () => {
    expect(visibleRowWindow(499 * ROW_HEIGHT, 500)).toEqual({ first: 479, last: 500 });
    expect(visibleRowWindow(0, 5)).toEqual({ first: 0, last: 5 });
    expect(visibleRowWindow(0, 0)).toEqual({ first: 0, last: 0 });
  });

  it("renders nothing, with the spacer taking the room, when scrolled past the end", () => {
    const { first, last } = visibleRowWindow(10_000 * ROW_HEIGHT, 500);
    expect(first).toBeGreaterThan(last);
    expect(last).toBe(500);
  });
});

describe("frameCsv", () => {
  const rows = frame({
    columns: [
      { spec: "obd:RPM", unit: "rpm" },
      { spec: 'tag,with"quote', unit: null },
    ],
    timestamps: ["t0", "t1", "t2"],
    values: { "obd:RPM": [1, null, 3], 'tag,with"quote': [4, 5, 6] },
  });

  it("heads each channel with its unit, quotes what needs it, and leaves a missing value empty", () => {
    expect(frameCsv(rows, [0, 1, 2])).toBe(
      ['timestamp,obd:RPM (rpm),"tag,with""quote"', "t0,1,4", "t1,,5", "t2,3,6", ""].join("\n"),
    );
  });

  it("writes exactly the rows given, in the order given", () => {
    expect(frameCsv(rows, [2, 0])).toBe(
      ['timestamp,obd:RPM (rpm),"tag,with""quote"', "t2,3,6", "t0,1,4", ""].join("\n"),
    );
    expect(frameCsv(rows, [])).toBe('timestamp,obd:RPM (rpm),"tag,with""quote"\n');
  });

  it("names the file by session and regime", () => {
    expect(frameCsvName(frame())).toBe("_capture-tuning-2026-09-20T120000-all.csv");
    expect(
      frameCsvName(frame({ regime_report: { regime: "fuel_cut", before: 1, after: 1 } })),
    ).toBe("_capture-tuning-2026-09-20T120000-fuel_cut.csv");
  });
});
