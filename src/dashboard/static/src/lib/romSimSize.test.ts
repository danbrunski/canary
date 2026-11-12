import { afterEach, describe, expect, it, vi } from "vitest";

import { MAX_DRAWS, formatEdges, suggestEdges } from "./romSim";
import {
  COST_FIT,
  LAPTOP_TIMINGS,
  PRESET_DESCRIPTION,
  PRESET_DRAWS,
  TIMINGS_KEPT,
  TIMINGS_STORAGE_KEY,
  cancelWording,
  coarseVerdict,
  costText,
  describeFill,
  estimateCost,
  expectedRun,
  fineReference,
  fitCost,
  hostScale,
  laptopSeconds,
  loadTimings,
  nearestTiming,
  presetFill,
  recordTiming,
  referenceLine,
} from "./romSimSize";
import { tablesWithAxes } from "../test/romSimFixtures";

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

const suggestion = () => suggestEdges(tablesWithAxes().tables);

describe("the presets", () => {
  it("take their draw counts from the documented evidence, both within MAX_DRAWS", () => {
    // E08.3 reads cell quantiles from 200 draws; W09.6's reference row is 1,000.
    expect(PRESET_DRAWS).toEqual({ coarse: 200, fine: 1000 });
    expect(PRESET_DRAWS.coarse).toBeLessThan(PRESET_DRAWS.fine);
    expect(PRESET_DRAWS.fine).toBeLessThanOrEqual(MAX_DRAWS);
  });

  it("Fine fills the suggested edges (the load edge past 100 % cut to the model) and 1,000 draws", () => {
    const s = suggestion();
    const fine = presetFill("fine", s);
    expect(fine).not.toBeNull();
    expect(fine?.draws).toBe(1000);
    expect(fine?.rpmEdges).toEqual(s.rpmEdges);
    // 16 breakpoints -> 17 edges, the last (103.125) cut to 100.
    expect(fine?.rpmEdges).toHaveLength(17);
    expect(fine?.loadEdges).toHaveLength(17);
    expect(fine?.loadEdges.at(-1)).toBe(100);
    expect(fine?.loadTrimmed).toBe(true);
    expect(fine?.points).toBe(256);
  });

  it("Coarse takes every other breakpoint on each axis: a quarter of the points", () => {
    const s = suggestion();
    const coarse = presetFill("coarse", s);
    expect(coarse?.draws).toBe(200);
    expect(coarse?.rpmEdges).toHaveLength(9);
    expect(coarse?.loadEdges).toHaveLength(9);
    expect(coarse?.points).toBe(64);
    const fine = presetFill("fine", s);
    expect((coarse?.points ?? 0) / (fine?.points ?? 1)).toBe(0.25);
    // Each grid point still sits on a breakpoint: the first cell is centred on
    // the first breakpoint (rpm 400), the second on the third (1,200).
    const rpm = s.rpm ?? [];
    const edges = coarse?.rpmEdges ?? [];
    expect((edges[0] + edges[1]) / 2).toBeCloseTo(rpm[0], 6);
    expect((edges[1] + edges[2]) / 2).toBeCloseTo(rpm[2], 6);
  });

  it("fills nothing without Fuel Comp breakpoints", () => {
    const none = suggestEdges([]);
    expect(presetFill("coarse", none)).toBeNull();
    expect(presetFill("fine", none)).toBeNull();
    expect(fineReference(none)).toEqual({ points: null, draws: 1000 });
  });

  it("says what it filled", () => {
    const fill = presetFill("coarse", suggestion());
    const text = describeFill(fill!);
    expect(text).toContain("Coarse (quick check) filled RPM edges (9 edges, 8 cells)");
    expect(text).toContain("64 points × 200 draws");
    expect(text).toContain("Nothing else was touched");
  });

  it("describes Coarse as leaving no_points cells that are not unchanged", () => {
    expect(PRESET_DESCRIPTION.coarse).toMatch(/no_points/);
    expect(PRESET_DESCRIPTION.coarse).toMatch(/NOT unchanged/);
    expect(PRESET_DESCRIPTION.coarse).toMatch(/every other/i);
  });

  it("the fine reference is the Fine preset's own size", () => {
    expect(fineReference(suggestion())).toEqual({ points: 256, draws: 1000 });
  });
});

describe("whether a run is coarse", () => {
  const fine = { points: 224, draws: 1000 };

  it("is fine at the Fine preset's draws and points or above", () => {
    expect(coarseVerdict(1000, 224, fine).coarse).toBe(false);
    expect(coarseVerdict(2000, 1024, fine).coarse).toBe(false);
  });

  it("is coarse for few draws, few points, or both, however the fields were set", () => {
    const draws = coarseVerdict(999, 224, fine);
    expect(draws).toMatchObject({ coarse: true, fewDraws: true, fewPoints: false });
    expect(draws.reasons).toEqual(["999 draws, below the Fine preset's 1,000"]);
    const points = coarseVerdict(1000, 223, fine);
    expect(points).toMatchObject({ coarse: true, fewDraws: false, fewPoints: true });
    expect(points.reasons[0]).toBe("223 points, below the Fine preset's 224");
    expect(coarseVerdict(200, 64, fine).reasons).toHaveLength(2);
  });

  it("judges only the draws when Fine's size is unknown", () => {
    expect(coarseVerdict(1000, 4, { points: null }).coarse).toBe(false);
    expect(coarseVerdict(200, 4, { points: null }).coarse).toBe(true);
  });
});

describe("the cost model", () => {
  it("is fitted from the W09.13 laptop timings only, and says how far off it is", () => {
    // The ten rows of W09.13's re-measured table (fuel-mv-2).
    expect(LAPTOP_TIMINGS).toHaveLength(10);
    expect(LAPTOP_TIMINGS).toContainEqual({ points: 224, draws: 1000, seconds: 7.7 });
    expect(LAPTOP_TIMINGS).toContainEqual({ points: 1024, draws: 2000, seconds: 42.1 });
    expect(COST_FIT.overheadS).toBeGreaterThan(2.6);
    expect(COST_FIT.overheadS).toBeLessThan(3.4);
    expect(COST_FIT.secondsPerPointDraw).toBeGreaterThan(1.8e-5);
    expect(COST_FIT.secondsPerPointDraw).toBeLessThan(2.3e-5);
    // The stated error is the real worst residual against its own rows.
    for (const row of LAPTOP_TIMINGS) {
      const error = (laptopSeconds(row.points, row.draws) - row.seconds) / row.seconds;
      expect(error).toBeGreaterThanOrEqual(COST_FIT.worstUnder - 1e-9);
      expect(error).toBeLessThanOrEqual(COST_FIT.worstOver + 1e-9);
    }
    expect(COST_FIT.worstOver).toBeLessThan(0.25);
    expect(COST_FIT.worstUnder).toBeGreaterThan(-0.25);
  });

  it("fits an exact line exactly", () => {
    const rows = [
      { points: 10, draws: 10, seconds: 2 + 0.01 * 100 },
      { points: 100, draws: 100, seconds: 2 + 0.01 * 10000 },
    ];
    const fit = fitCost(rows);
    expect(fit.overheadS).toBeCloseTo(2, 9);
    expect(fit.secondsPerPointDraw).toBeCloseTo(0.01, 9);
    expect(fit.worstOver).toBeLessThan(1e-9);
  });

  it("names the nearest measured row as a labelled reference, not a prediction", () => {
    expect(nearestTiming(224, 1000)).toEqual({ points: 224, draws: 1000, seconds: 7.7 });
    const line = referenceLine(224, 1000);
    expect(line).toContain("Reference, not a prediction");
    expect(line).toContain("the dev laptop took 7.7 s for 224 points × 1,000 draws (W09.13)");
  });
});

describe("this server's own timings", () => {
  it("are stored per browser, newest last, at most the kept count", () => {
    for (let i = 1; i <= TIMINGS_KEPT + 3; i += 1) {
      recordTiming({ points: 100, draws: 100 * i, seconds: i });
    }
    const runs = loadTimings();
    expect(runs).toHaveLength(TIMINGS_KEPT);
    expect(runs.at(-1)).toEqual({
      points: 100,
      draws: 100 * (TIMINGS_KEPT + 3),
      seconds: TIMINGS_KEPT + 3,
    });
  });

  it("drop entries that are not runs, and ignore a blocked store", () => {
    window.localStorage.setItem(
      TIMINGS_STORAGE_KEY,
      JSON.stringify({
        version: 1,
        runs: [
          { points: 64, draws: 200, seconds: 3 },
          { points: 0, draws: 200, seconds: 3 },
          { points: 64, draws: 200, seconds: -1 },
          { points: "64", draws: 200, seconds: 3 },
          null,
        ],
      }),
    );
    expect(loadTimings()).toEqual([{ points: 64, draws: 200, seconds: 3 }]);
    window.localStorage.setItem(TIMINGS_STORAGE_KEY, "{not json");
    expect(loadTimings()).toEqual([]);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(loadTimings()).toEqual([]);
    expect(() => recordTiming({ points: 1, draws: 1, seconds: 1 })).not.toThrow();
    // A run that is not a run is not stored.
    vi.restoreAllMocks();
    recordTiming({ points: 1, draws: 1, seconds: Number.NaN });
    expect(loadTimings()).toEqual([]);
  });

  it("scale the estimate: a server twice as slow as the fit is estimated at twice the fit", () => {
    const slow = LAPTOP_TIMINGS.map((row) => ({ ...row, seconds: row.seconds * 2 }));
    const scale = hostScale(slow) ?? 0;
    // Each row is off the fit by its own error; the median of ratios sits near 2.
    expect(scale).toBeGreaterThan(1.7);
    expect(scale).toBeLessThan(2.3);
    const estimate = estimateCost(224, 1000, slow);
    expect(estimate.measured?.seconds).toBeCloseTo(laptopSeconds(224, 1000) * scale, 9);
    expect(estimate.measured?.runs).toBe(LAPTOP_TIMINGS.length);
  });

  it("have no scale before the first run, and the estimate then falls back to the reference", () => {
    expect(hostScale([])).toBeNull();
    const estimate = estimateCost(64, 200, []);
    expect(estimate.measured).toBeNull();
    const text = costText(estimate);
    expect(text.headline).toContain("64 points × 200 draws = 12,800 point-draws");
    expect(text.headline).toContain("Not yet measured on this server");
    expect(text.detail).toContain("Reference, not a prediction");
    expect(text.detail).toContain("Fit: ");
    expect(text.detail).toContain("least squares on relative error");
  });

  it("use the stored rate when there is one, naming the runs it comes from", () => {
    const runs = [
      { points: 224, draws: 1000, seconds: 11 },
      { points: 224, draws: 1000, seconds: 11 },
    ];
    const estimate = estimateCost(224, 1000, runs);
    // 11 s against the fit's ~7.5 s at this size: about 1.5 times the fit.
    expect(estimate.measured?.seconds).toBeCloseTo(11, 9);
    const text = costText(estimate);
    expect(text.headline).toContain("About 11 s from your last 2 runs on this server");
    expect(text.headline).not.toContain("Not yet measured");
    expect(text.detail).toMatch(/This server ran \d\.\d\d× the dev-laptop fit/);
  });

  it("do not let a coarse run distort a fine estimate beyond the stated model error", () => {
    // The server is the dev laptop, and only a coarse run (a 200-draw row) has
    // been timed. The fine estimate must stay within the fit's own stated error
    // of what the laptop measured for the fine run (7.7 s).
    const coarseOnly = [{ points: 224, draws: 200, seconds: 4.1 }];
    const estimate = estimateCost(224, 1000, coarseOnly);
    const measured = 7.7;
    const off = Math.abs((estimate.measured?.seconds ?? 0) - measured) / measured;
    expect(off).toBeLessThanOrEqual(COST_FIT.worstOver - COST_FIT.worstUnder);
    // And the other way round: a fine run does not distort the coarse estimate.
    const fineOnly = [{ points: 224, draws: 1000, seconds: 7.7 }];
    const coarse = estimateCost(224, 200, fineOnly);
    const offCoarse = Math.abs((coarse.measured?.seconds ?? 0) - 4.1) / 4.1;
    expect(offCoarse).toBeLessThanOrEqual(COST_FIT.worstOver - COST_FIT.worstUnder);
    // A ratio, not a rate: a seconds-per-point-draw taken from the coarse run
    // alone (4.1 s / 44,800) would put 224 x 1,000 at 20.5 s, 2.7 times the measured 7.7 s.
    expect(estimate.measured?.seconds).toBeLessThan(10);
  });

  it("the median shrugs off one odd run", () => {
    const base = { points: 224, draws: 1000, seconds: 7.7 };
    const runs = [base, base, base, { ...base, seconds: 80 }];
    expect(hostScale(runs)).toBeCloseTo(7.7 / laptopSeconds(224, 1000), 9);
  });
});

describe("cancel", () => {
  it("says it only stops waiting, and what the server still does", () => {
    const text = cancelWording({ seconds: 8.5, measured: false });
    expect(text).toMatch(/Cancel only stops waiting/);
    expect(text).toContain("the server finishes this run (about 8.5 s");
    expect(text).toContain("before another can start");
    expect(text).toContain("not yet measured on this server");
    expect(cancelWording({ seconds: 40, measured: true })).toContain("from your earlier runs here");
  });

  it("expects the measured estimate when there is one, else the fit", () => {
    expect(expectedRun(224, 1000, [])).toEqual({
      seconds: laptopSeconds(224, 1000),
      measured: false,
    });
    const measured = expectedRun(224, 1000, [{ points: 224, draws: 1000, seconds: 11 }]);
    expect(measured.measured).toBe(true);
    expect(measured.seconds).toBeCloseTo(11, 9);
  });
});

it("formats edges the way the form does", () => {
  const fill = presetFill("coarse", suggestion());
  expect(formatEdges(fill?.rpmEdges ?? []).split(", ")).toHaveLength(9);
});
