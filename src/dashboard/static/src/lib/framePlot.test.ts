import { describe, expect, it } from "vitest";

import { frame } from "../test/workbenchFixtures";
import {
  HEAT_BINS,
  extent,
  fraction,
  heatCells,
  heatShade,
  scatterPoints,
  stripMarkers,
  stripPoints,
  timeAxis,
  timeX,
} from "./framePlot";

describe("extent", () => {
  it("is the minimum and maximum of what is finite", () => {
    expect(extent([3, 1, 2])).toEqual([1, 3]);
    expect(extent([5])).toEqual([5, 5]);
    expect(extent([-2, null, 7, null])).toEqual([-2, 7]);
  });

  it("is null when nothing is measurable", () => {
    expect(extent([])).toBeNull();
    expect(extent([null, null])).toBeNull();
    expect(extent([Number.NaN, Infinity, -Infinity])).toBeNull();
  });

  it("ignores a non-finite value beside finite ones", () => {
    expect(extent([1, Number.NaN, 4, Infinity])).toEqual([1, 4]);
  });
});

describe("fraction", () => {
  it("is 0 at the minimum and 1 at the maximum, and does not clamp beyond them", () => {
    expect(fraction(10, [10, 20])).toBe(0);
    expect(fraction(20, [10, 20])).toBe(1);
    expect(fraction(15, [10, 20])).toBe(0.5);
    expect(fraction(30, [10, 20])).toBe(2);
  });

  it("puts everything at 0 on a range with no width, never NaN", () => {
    expect(fraction(5, [5, 5])).toBe(0);
    expect(Number.isNaN(fraction(6, [5, 5]))).toBe(false);
  });
});

describe("timeAxis and timeX", () => {
  const stamps = ["2026-09-20T12:00:00Z", "2026-09-20T12:00:01Z", "2026-09-20T12:00:04Z"];

  it("spans the first row to the last, unevenly spaced rows keeping their spacing", () => {
    const axis = timeAxis(stamps);
    expect(axis.t1 - axis.t0).toBe(4000);
    expect([0, 1, 2].map((i) => timeX(axis, axis.times[i], 720))).toEqual([0, 180, 720]);
  });

  it("has a unit span for no rows, and a single row sits at 0", () => {
    const none = timeAxis([]);
    expect(none).toMatchObject({ t0: 0, t1: 1, times: [] });
    const one = timeAxis([stamps[0]]);
    expect(one.t0).toBe(one.t1);
    expect(timeX(one, one.t0, 720)).toBe(0);
  });

  it("carries an unparseable stamp as NaN rather than hiding it", () => {
    expect(Number.isNaN(timeAxis(["not a time"]).times[0])).toBe(true);
  });
});

describe("stripPoints", () => {
  const axis = timeAxis(["2026-09-20T12:00:00Z", "2026-09-20T12:00:01Z", "2026-09-20T12:00:02Z"]);

  it("scales into a 4-unit margin, y growing downward, to one decimal", () => {
    expect(stripPoints([800, 3000, 1500], axis, [800, 3000], 720, 70)).toBe(
      "0.0,66.0 360.0,4.0 720.0,46.3",
    );
  });

  it("skips a missing sample, joining its neighbours across the gap", () => {
    expect(stripPoints([800, null, 3000], axis, [800, 3000], 720, 70)).toBe("0.0,66.0 720.0,4.0");
  });

  it("is empty when every sample is missing", () => {
    expect(stripPoints([null, null, null], axis, null, 720, 70)).toBe("");
  });

  it("draws a constant channel along the floor of the strip", () => {
    expect(stripPoints([5, 5, 5], axis, [5, 5], 720, 70)).toBe("0.0,66.0 360.0,66.0 720.0,66.0");
  });
});

describe("stripMarkers", () => {
  const axis = timeAxis(["2026-09-20T12:00:00Z", "2026-09-20T12:00:10Z"]);
  const at = (second: number) => `2026-09-20T12:00:${String(second).padStart(2, "0")}Z`;

  it("keeps tags then flashes inside the span, with both ends included", () => {
    const markers = stripMarkers(
      frame({
        tags: [
          { t: at(10), label: "end" },
          { t: at(5), label: "mid" },
          { t: "2026-09-20T11:59:59Z", label: "before" },
          { t: at(11), label: "after" },
        ],
        revision_boundaries: [
          { revision: "B", flashed_at: at(0) },
          { revision: "C", flashed_at: at(12) },
        ],
      }),
      axis,
    );
    expect(markers.map((m) => `${m.kind}:${m.label}`)).toEqual([
      "tag:end",
      "tag:mid",
      "flash:flash B",
    ]);
  });

  it("drops a marker whose time cannot be read", () => {
    expect(
      stripMarkers(frame({ tags: [{ t: "garbage", label: "x" }], revision_boundaries: [] }), axis),
    ).toEqual([]);
  });
});

describe("scatterPoints", () => {
  it("places points in a margin of 5, y growing upward", () => {
    const points = scatterPoints([0, 10], [0, 10], [0, 10], [0, 10], 360, 240);
    expect(points).toEqual([
      { index: 0, cx: 5, cy: 235 },
      { index: 1, cx: 355, cy: 5 },
    ]);
  });

  it("skips a row missing either axis and keeps the row index of the rest", () => {
    const points = scatterPoints([1, null, 3, 4], [1, 2, null, 4], [1, 4], [1, 4], 360, 240);
    expect(points.map((p) => p.index)).toEqual([0, 3]);
  });

  it("stands a short y column's missing rows aside rather than plotting them", () => {
    expect(scatterPoints([1, 2, 3], [1], [1, 3], [1, 1], 360, 240).map((p) => p.index)).toEqual([
      0,
    ]);
  });

  it("puts a constant axis at its margin", () => {
    const [point] = scatterPoints([7], [7], [7, 7], [7, 7], 360, 240);
    expect([point.cx, point.cy]).toEqual([5, 235]);
  });

  it("has nowhere to place a point on an axis with no range", () => {
    const [point] = scatterPoints([7], [7], null, null, 360, 240);
    expect([point.cx, point.cy]).toEqual([0, 0]);
  });
});

describe("heatCells", () => {
  it("has the 12 x 12 grid the map is drawn on", () => {
    expect(HEAT_BINS).toBe(12);
  });

  it("puts the maximum in the last cell, and a value on a bin edge in the upper bin", () => {
    // Range 0..12, so bin edges fall on whole numbers.
    const cells = heatCells([0, 1, 11.999, 12], [0, 0, 0, 0], null, [0, 12], [0, 12]);
    expect(cells.map((c) => c.bx)).toEqual([0, 1, 11]);
    expect(cells.find((c) => c.bx === 11)?.n).toBe(2);
  });

  it("counts samples per cell when there is no colour channel", () => {
    const cells = heatCells([0, 0.1, 12], [0, 0.1, 12], null, [0, 12], [0, 12]);
    expect(cells).toEqual([
      { bx: 0, by: 0, value: 2, n: 2 },
      { bx: 11, by: 11, value: 1, n: 1 },
    ]);
  });

  it("averages the colour channel per cell, and does not count a sample that lacks it", () => {
    const cells = heatCells([0, 0.1, 0.2], [0, 0, 0], [10, 20, null], [0, 12], [0, 12]);
    expect(cells).toEqual([{ bx: 0, by: 0, value: 15, n: 2 }]);
  });

  it("skips a sample missing either axis or past the end of the colour column", () => {
    const cells = heatCells([0, null, 5, 6], [0, 1, null, 6], [1, 1, 1], [0, 12], [0, 12]);
    // Rows 1 and 2 lack an axis; row 3 has no colour value (the column is short).
    expect(cells).toHaveLength(1);
    expect(cells[0]).toMatchObject({ bx: 0, by: 0, n: 1 });
  });

  it("lists cells in the order they were first seen", () => {
    const cells = heatCells([12, 0, 12], [12, 0, 12], null, [0, 12], [0, 12]);
    expect(cells.map((c) => [c.bx, c.by])).toEqual([
      [11, 11],
      [0, 0],
    ]);
  });

  it("puts every sample in the first cell along an axis with no width", () => {
    const cells = heatCells([5, 5, 5], [0, 6, 12], null, [5, 5], [0, 12]);
    expect(cells.map((c) => c.bx)).toEqual([0, 0, 0]);
    expect(cells.map((c) => c.by)).toEqual([0, 6, 11]);
  });

  it("has no cells for an axis with no range", () => {
    expect(heatCells([1], [1], null, null, [0, 1])).toEqual([]);
    expect(heatCells([1], [1], null, [0, 1], null)).toEqual([]);
  });
});

describe("heatShade", () => {
  it("runs 0 to 1 across the cells' own range, mid-ramp with none, 0 when they all agree", () => {
    expect(heatShade(2, [2, 6])).toBe(0);
    expect(heatShade(6, [2, 6])).toBe(1);
    expect(heatShade(4, [2, 6])).toBe(0.5);
    expect(heatShade(3, null)).toBe(0.5);
    expect(heatShade(3, [3, 3])).toBe(0);
  });
});
