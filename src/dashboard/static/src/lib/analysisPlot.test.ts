import { describe, expect, it } from "vitest";

import { FLOOR, delivery, pullComparison, trimCell, trimSurface } from "../test/workbenchFixtures";
import type { NoiseFloor } from "../types/workbench";
import { comparedAsCells, deliveryScale, trimCells } from "./analysisPlot";

const bands = (errors: Array<number | null>) =>
  errors.map((error_pct, i) => ({ ...delivery().bands[0], rpm_band: `b${i}`, error_pct }));
const NO_FLOOR: NoiseFloor = { available: false, channel: "delivery_error_pct" };

describe("deliveryScale", () => {
  it("never spans less than 3 % either side, plus 15 % headroom", () => {
    const scale = deliveryScale({ noise_floor: NO_FLOOR, bands: bands([0.5, -1]) });
    expect(scale.limit).toBeCloseTo(3.45, 10);
    expect(scale.y(0)).toBe(100);
    expect(scale.y(scale.limit)).toBe(12);
    expect(scale.y(-scale.limit)).toBe(188);
  });

  it("widens to the largest error, in either direction", () => {
    expect(deliveryScale({ noise_floor: NO_FLOOR, bands: bands([4, 1]) }).limit).toBeCloseTo(
      4.6,
      10,
    );
    expect(deliveryScale({ noise_floor: NO_FLOOR, bands: bands([-5, 1]) }).limit).toBeCloseTo(
      5.75,
      10,
    );
  });

  it("widens to the whole per-cell range of the floor", () => {
    const wide = { ...FLOOR, min: -5, max: 1 };
    const scale = deliveryScale({ noise_floor: wide, bands: bands([1]) });
    expect(scale).toMatchObject({ sd: 1.49, rangeLo: -5, rangeHi: 1 });
    expect(scale.limit).toBeCloseTo(5.75, 10);
  });

  it("falls back to ±sd for a floor with no recorded range, or with half of one", () => {
    expect(
      deliveryScale({ noise_floor: { ...FLOOR, min: null, max: null, sd: 2 }, bands: [] }),
    ).toMatchObject({
      rangeLo: -2,
      rangeHi: 2,
    });
    expect(
      deliveryScale({ noise_floor: { ...FLOOR, min: -4, max: null, sd: 2 }, bands: [] }),
    ).toMatchObject({
      rangeLo: -4,
      rangeHi: 2,
    });
  });

  it("lets an unavailable floor contribute nothing, even if it carries numbers", () => {
    const stale: NoiseFloor = { ...NO_FLOOR, sd: 9, min: -9, max: 9 };
    const scale = deliveryScale({ noise_floor: stale, bands: bands([1]) });
    // -0 is still no width: compare by value, not by Object.is.
    expect([scale.sd, scale.rangeLo, scale.rangeHi].map((v) => v === 0)).toEqual([
      true,
      true,
      true,
    ]);
    expect(scale.limit).toBeCloseTo(3.45, 10);
  });

  it("counts a band with no error as zero", () => {
    expect(deliveryScale({ noise_floor: NO_FLOOR, bands: bands([null, null]) }).limit).toBeCloseTo(
      3.45,
      10,
    );
  });

  it("is 320 wide at least, then 70 per band", () => {
    expect(deliveryScale({ noise_floor: NO_FLOOR, bands: [] }).width).toBe(320);
    expect(deliveryScale({ noise_floor: NO_FLOOR, bands: bands([1, 1, 1, 1]) }).width).toBe(320);
    expect(deliveryScale({ noise_floor: NO_FLOOR, bands: bands(Array(5).fill(1)) }).width).toBe(
      350,
    );
  });
});

describe("trimCells", () => {
  const cells = (total: number) => [trimCell(total, false, 5)];
  const result = trimSurface({
    banks: { b1: { n: 5, cells: cells(1) }, b2: { n: 5, cells: cells(2) } },
    per_session: [{ session_id: "s", revision: "R", cells: { b1: cells(3) } }],
  });

  it("is the pooled bank, or the one session's cells of that bank", () => {
    expect(trimCells(result, "b2", "pooled")[0].total).toBe(2);
    expect(trimCells(result, "b1", "s")[0].total).toBe(3);
  });

  it("is empty for what the result does not hold", () => {
    expect(trimCells(null, "b1", "pooled")).toEqual([]);
    expect(trimCells(result, "b9", "pooled")).toEqual([]);
    expect(trimCells(result, "b2", "s")).toEqual([]);
    expect(trimCells(result, "b1", "other")).toEqual([]);
  });
});

describe("comparedAsCells", () => {
  it("sizes a cell by the thinner side, never sparse, carrying its delta and drop flag", () => {
    const [dropped, steady] = comparedAsCells(pullComparison());
    expect(dropped).toMatchObject({ n: 7, sparse: false, delta: -4, dropped: true });
    expect(steady).toMatchObject({ n: 9, sparse: false, delta: 1, dropped: false });
    expect(dropped.rpm_range).toEqual([3000, 3500]);
    expect(dropped.airflow_index).toBe(0);
  });

  it("keeps a cell with no samples on one side, at n 0", () => {
    const base = pullComparison();
    const [cell] = comparedAsCells(pullComparison({ cells: [{ ...base.cells[0], n_b: 0 }] }));
    expect(cell.n).toBe(0);
  });
});
