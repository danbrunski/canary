// How big a ROM simulation is, and what that costs (W09.9): the Coarse and
// Fine presets, the "this run is coarse" verdict the result carries, the
// cost estimate shown next to Run, and the timings this browser keeps from
// its own earlier runs.
//
// What this makes impossible rather than documented:
// - a preset only fills the form's fields (draws and the two edge lists) and
//   the form says what it filled. Nothing here is a default: `MAX_DRAWS`
//   bounds both presets, the choice of preset is never stored, and `draws`
//   is still a required request field (`romSim.checkSimForm`);
// - a low run cannot pass as a fine one: `coarseVerdict` is computed from the
//   run's own draws and point count against the Fine preset's, so a hand-set
//   run is judged exactly like a clicked one;
// - the host's speed is never assumed: the estimate is the dev laptop's
//   measured fit scaled by what THIS server's earlier runs took, and without
//   such runs the page says "not yet measured" next to a labelled reference.

import {
  MAX_DRAWS,
  edgesFromBreakpoints,
  formatEdges,
  gridPoints,
  trimLoadEdges,
  type BreakpointSuggestion,
} from "./romSim";

// -- the presets ----------------------------------------------------------------------------

export type SimPreset = "coarse" | "fine";

/** The draw counts the presets fill.
 *
 *  Coarse 200: the draw count E08 reads cell quantiles from. E08.3's
 *  Fuel-Comp-restore entry records that "three draws give only a coarse
 *  per-cell spread, so cell quantiles should be read from" the 200-draw run,
 *  and the E08.5/E08.7 real-image runs and W09.6's low row all use 200. At
 *  224 points W09.6 measured 3.3 s for 200 draws against 5.7 s for 1,000: the
 *  child's start-up, not the draws, dominates a small run, so fewer than 200
 *  would save almost nothing.
 *
 *  Fine 1,000: E08.3's stated speed target (1,000 draws) and the draw count
 *  W09.6 measured as its reference row (224 points x 1,000 draws, 5.7 s on
 *  the dev laptop). 2,000 is `MAX_DRAWS`, the cap, not a recommendation. */
export const PRESET_DRAWS: Record<SimPreset, number> = { coarse: 200, fine: 1000 };

export const PRESET_LABEL: Record<SimPreset, string> = {
  coarse: "Coarse (quick check)",
  fine: "Fine",
};

export const PRESET_SOURCE =
  "Draw counts: Coarse 200 is the count E08 reads cell quantiles from (E08.3: three draws give only a coarse per-cell spread) and the low row of W09.6's timings; Fine 1,000 is E08.3's speed target and W09.6's reference row. Neither is a default: you pick one, it fills the fields below, and you can edit them.";

export const PRESET_DESCRIPTION: Record<SimPreset, string> = {
  coarse: `Every other Fuel Comp breakpoint on each axis (about a quarter of the grid points) and ${PRESET_DRAWS.coarse} draws. Table cells with no visited point show no_points, which is NOT unchanged: nothing was simulated there. The result carries a coarse-run banner.`,
  fine: `All of the suggested Fuel Comp breakpoints, as the form suggests them, and ${PRESET_DRAWS.fine.toLocaleString("en-US")} draws.`,
};

export interface PresetFill {
  preset: SimPreset;
  rpmEdges: number[];
  loadEdges: number[];
  draws: number;
  /** Design points: (rpm cells) x (load cells). */
  points: number;
  /** The load edges were cut to the fill model's 0-100 % (what the
   *  "Trim load edges" button does), because the suggestion runs past it. */
  loadTrimmed: boolean;
}

function everyOther<T>(items: T[]): T[] {
  return items.filter((_, index) => index % 2 === 0);
}

/** What a preset fills, from the base image's suggestion; null when the image
 *  gives no breakpoints to derive edges from. Coarse takes every other
 *  breakpoint of each axis and derives the edges from those (so each grid
 *  point still sits on a breakpoint); Fine is the suggested edges as they
 *  are. Both cut the load edges to 0-100 % (the model's span), as the Trim
 *  button would, and say so. */
export function presetFill(preset: SimPreset, suggestion: BreakpointSuggestion): PresetFill | null {
  let rpmEdges: number[] | null;
  let loadEdges: number[] | null;
  if (preset === "coarse") {
    rpmEdges = suggestion.rpm ? edgesFromBreakpoints(everyOther(suggestion.rpm)) : null;
    loadEdges = suggestion.load ? edgesFromBreakpoints(everyOther(suggestion.load)) : null;
  } else {
    rpmEdges = suggestion.rpmEdges;
    loadEdges = suggestion.loadEdges;
  }
  if (!rpmEdges || !loadEdges) return null;
  const trimmed = trimLoadEdges(loadEdges);
  return {
    preset,
    rpmEdges,
    loadEdges: trimmed,
    draws: Math.min(PRESET_DRAWS[preset], MAX_DRAWS),
    points: gridPoints(rpmEdges, trimmed),
    loadTrimmed: formatEdges(trimmed) !== formatEdges(loadEdges),
  };
}

/** The fields a preset filled, in words (shown after the click). */
export function describeFill(fill: PresetFill): string {
  return `${PRESET_LABEL[fill.preset]} filled RPM edges (${fill.rpmEdges.length} edges, ${fill.rpmEdges.length - 1} cells), load edges (${fill.loadEdges.length} edges, ${fill.loadEdges.length - 1} cells${fill.loadTrimmed ? ", cut to the model's 0–100 %" : ""}) and draws (${fill.draws.toLocaleString("en-US")}): ${fill.points.toLocaleString("en-US")} points × ${fill.draws.toLocaleString("en-US")} draws. Nothing else was touched.`;
}

/** The Fine preset's size for this base, the yardstick a run is judged against. */
export interface FineReference {
  /** Null when the image gave no breakpoints to size a Fine grid from. */
  points: number | null;
  draws: number;
}

export function fineReference(suggestion: BreakpointSuggestion): FineReference {
  return {
    points: presetFill("fine", suggestion)?.points ?? null,
    draws: PRESET_DRAWS.fine,
  };
}

// -- is a run coarse? -----------------------------------------------------------------------

export interface CoarseVerdict {
  coarse: boolean;
  /** Fewer draws than Fine's. */
  fewDraws: boolean;
  /** Fewer points than Fine's (false when Fine's size is not known). */
  fewPoints: boolean;
  reasons: string[];
}

/** A run is coarse when its draws or its point count is below the Fine
 *  preset's, however the fields got their values. */
export function coarseVerdict(
  draws: number,
  points: number,
  fine: Pick<FineReference, "points"> & Partial<Pick<FineReference, "draws">>,
): CoarseVerdict {
  const fineDraws = fine.draws ?? PRESET_DRAWS.fine;
  const fewDraws = draws < fineDraws;
  const fewPoints = fine.points !== null && points < fine.points;
  const reasons: string[] = [];
  if (fewDraws) {
    reasons.push(
      `${draws.toLocaleString("en-US")} draws, below the Fine preset's ${fineDraws.toLocaleString("en-US")}`,
    );
  }
  if (fewPoints && fine.points !== null) {
    reasons.push(
      `${points.toLocaleString("en-US")} points, below the Fine preset's ${fine.points.toLocaleString("en-US")}`,
    );
  }
  return { coarse: fewDraws || fewPoints, fewDraws, fewPoints, reasons };
}

// -- the cost model -------------------------------------------------------------------------

export interface TimedRun {
  points: number;
  draws: number;
  seconds: number;
}

/** W09.6's re-measured table (2026-10-17, plant fuel-mv-2): the real `spawn`
 *  child (start-up included) on the dev laptop (Intel Core Ultra 9 185H,
 *  WSL2, 1-minute load 0.6-1.6), the mean of two or three runs per row. The
 *  size is the grid's nominal cell count, which is what the form can state
 *  before a run; the design that actually runs is smaller (201 of 224, 231 of
 *  256, 945 of 1,024, 53 of 56), because points outside the identified fit
 *  are not placed. The rows: a Coarse-preset grid (8 x 7), the form's Fuel
 *  Comp grid (16 x 14) and the 16 x 16 and 32 x 32 ones. The only timings the
 *  reference line may quote. The fuel-mv-1 table (8.5 s at the form grid and
 *  1,000 draws, 51 s at the cap) was taken at the plant's 0.7 working
 *  volumetric efficiency and is gone. */
export const LAPTOP_TIMINGS: readonly TimedRun[] = [
  { points: 56, draws: 200, seconds: 2.8 },
  { points: 56, draws: 1000, seconds: 3.9 },
  { points: 224, draws: 200, seconds: 3.3 },
  { points: 224, draws: 1000, seconds: 5.7 },
  { points: 224, draws: 2000, seconds: 9.6 },
  { points: 256, draws: 200, seconds: 3.9 },
  { points: 256, draws: 1000, seconds: 7.6 },
  { points: 1024, draws: 1000, seconds: 21.9 },
  { points: 1024, draws: 2000, seconds: 34.2 },
];

export interface CostFit {
  /** The fixed part: child start-up, imports, building the design. */
  overheadS: number;
  /** Seconds per (point x draw). */
  secondsPerPointDraw: number;
  /** Worst error of the fit against its own rows, as fractions of the
   *  measured time (predicted - measured) / measured. */
  worstUnder: number;
  worstOver: number;
}

/** t = overhead + rate x points x draws, least squares on the relative error
 *  (each row weighted 1 / t), so a 3 s row counts as much as a 34 s one. */
export function fitCost(rows: readonly TimedRun[]): CostFit {
  let sw = 0;
  let sx = 0;
  let sxx = 0;
  let st = 0;
  let sxt = 0;
  for (const row of rows) {
    const w = 1 / (row.seconds * row.seconds);
    const x = row.points * row.draws;
    sw += w;
    sx += w * x;
    sxx += w * x * x;
    st += w * row.seconds;
    sxt += w * x * row.seconds;
  }
  const rate = (sw * sxt - sx * st) / (sw * sxx - sx * sx);
  const overhead = (st - rate * sx) / sw;
  const errors = rows.map(
    (row) => (overhead + rate * row.points * row.draws - row.seconds) / row.seconds,
  );
  return {
    overheadS: overhead,
    secondsPerPointDraw: rate,
    worstUnder: Math.min(0, ...errors),
    worstOver: Math.max(0, ...errors),
  };
}

export const COST_FIT: CostFit = fitCost(LAPTOP_TIMINGS);

/** The dev laptop's modelled seconds for a run of this size. */
export function laptopSeconds(points: number, draws: number, fit: CostFit = COST_FIT): number {
  return fit.overheadS + fit.secondsPerPointDraw * points * draws;
}

/** The fit, as the page states it. */
export function describeFit(fit: CostFit = COST_FIT): string {
  const pct = (value: number) => `${value > 0 ? "+" : ""}${Math.round(value * 100)} %`;
  return `${fit.overheadS.toFixed(1)} s + ${fit.secondsPerPointDraw.toExponential(1)} s × points × draws, fitted to the ${LAPTOP_TIMINGS.length} dev-laptop timings in W09.6 (least squares on relative error; against those rows it is off by ${pct(fit.worstUnder)} to ${pct(fit.worstOver)}, and no run under 56 points was timed)`;
}

/** The measured row closest to this size in points x draws (on a log scale). */
export function nearestTiming(points: number, draws: number): TimedRun {
  const size = Math.max(1, points * draws);
  let best = LAPTOP_TIMINGS[0];
  let bestGap = Infinity;
  for (const row of LAPTOP_TIMINGS) {
    const gap = Math.abs(Math.log(size / (row.points * row.draws)));
    if (gap < bestGap) {
      best = row;
      bestGap = gap;
    }
  }
  return best;
}

export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds)) return "unknown";
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 120) return `${Math.round(seconds)} s`;
  return `${Math.round(seconds / 60)} min`;
}

export function formatCount(count: number): string {
  return count.toLocaleString("en-US");
}

/** The laptop reference sentence: a measured row, labelled as a reference. */
export function referenceLine(points: number, draws: number): string {
  const row = nearestTiming(points, draws);
  return `Reference, not a prediction: the dev laptop took ${row.seconds} s for ${formatCount(row.points)} points × ${formatCount(row.draws)} draws (W09.6); its fit gives about ${formatDuration(laptopSeconds(points, draws))} for ${formatCount(points)} × ${formatCount(draws)}.`;
}

// -- this browser's own timings -------------------------------------------------------------

export const TIMINGS_STORAGE_KEY = "canary.romSim.timings";
/** The most recent runs kept; the scale is their median. */
export const TIMINGS_KEPT = 8;

function validRun(value: unknown): value is TimedRun {
  if (typeof value !== "object" || value === null) return false;
  const run = value as Record<string, unknown>;
  return (
    typeof run.points === "number" &&
    typeof run.draws === "number" &&
    typeof run.seconds === "number" &&
    run.points > 0 &&
    run.draws > 0 &&
    run.seconds > 0 &&
    [run.points, run.draws, run.seconds].every(Number.isFinite)
  );
}

/** The runs this browser remembers for this server (localStorage is per
 *  origin); empty when absent, unreadable or shaped wrongly. */
export function loadTimings(): TimedRun[] {
  try {
    const text = window.localStorage.getItem(TIMINGS_STORAGE_KEY);
    if (!text) return [];
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null) return [];
    const record = parsed as Record<string, unknown>;
    if (record.version !== 1 || !Array.isArray(record.runs)) return [];
    return record.runs.filter(validRun).map(({ points, draws, seconds }) => ({
      points,
      draws,
      seconds,
    }));
  } catch {
    return [];
  }
}

/** Keep a finished run's size and wall time. Only a run that returned a
 *  result belongs here: a failed or abandoned one says nothing about how long
 *  the work takes. */
export function recordTiming(run: TimedRun): void {
  if (!validRun(run)) return;
  try {
    const runs = [...loadTimings(), run].slice(-TIMINGS_KEPT);
    window.localStorage.setItem(TIMINGS_STORAGE_KEY, JSON.stringify({ version: 1, runs }));
  } catch {
    // Storage blocked or full: the estimate just stays on the reference.
  }
}

export function forgetTimings(): void {
  try {
    window.localStorage.removeItem(TIMINGS_STORAGE_KEY);
  } catch {
    // Blocked: nothing to forget.
  }
}

/** How much slower (or faster) this server ran than the dev-laptop fit, as
 *  the median of observed / modelled over its earlier runs. A ratio against
 *  the model, not seconds per point-draw, so a coarse run (mostly overhead)
 *  and a fine one (mostly draws) calibrate the same number; what is left is
 *  the model's own error, stated by `describeFit`. Null with no runs. */
export function hostScale(runs: readonly TimedRun[]): number | null {
  const ratios = runs
    .filter((run) => run.seconds > 0)
    .map((run) => run.seconds / laptopSeconds(run.points, run.draws))
    .sort((a, b) => a - b);
  if (ratios.length === 0) return null;
  const middle = Math.floor(ratios.length / 2);
  const median =
    ratios.length % 2 === 1 ? ratios[middle] : (ratios[middle - 1] + ratios[middle]) / 2;
  return median;
}

export interface CostEstimate {
  points: number;
  draws: number;
  /** points x draws. */
  work: number;
  /** From this server's earlier runs; null before it has any. */
  measured: { seconds: number; scale: number; runs: number } | null;
  /** The dev laptop's fit for this size, always available. */
  laptopS: number;
}

export function estimateCost(
  points: number,
  draws: number,
  runs: readonly TimedRun[],
): CostEstimate {
  const laptopS = laptopSeconds(points, draws);
  const scale = hostScale(runs);
  return {
    points,
    draws,
    work: points * draws,
    measured: scale === null ? null : { seconds: laptopS * scale, scale, runs: runs.length },
    laptopS,
  };
}

/** The wording of the estimate. `measured` ones name their basis; without
 *  runs the page says so and offers the labelled reference. */
export function costText(estimate: CostEstimate): { headline: string; detail: string } {
  const work = `${formatCount(estimate.points)} points × ${formatCount(estimate.draws)} draws = ${formatCount(estimate.work)} point-draws`;
  const fit = describeFit();
  if (estimate.measured) {
    const m = estimate.measured;
    return {
      headline: `${work}. About ${formatDuration(m.seconds)} from your last ${m.runs} ${m.runs === 1 ? "run" : "runs"} on this server.`,
      detail: `This server ran ${m.scale.toFixed(2)}× the dev-laptop fit (median of those runs). Fit: ${fit}.`,
    };
  }
  return {
    headline: `${work}. Not yet measured on this server.`,
    detail: `${referenceLine(estimate.points, estimate.draws)} Fit: ${fit}.`,
  };
}

/** What a run in flight is expected to take, for the Cancel wording. */
export function expectedRun(
  points: number,
  draws: number,
  runs: readonly TimedRun[],
): { seconds: number; measured: boolean } {
  const estimate = estimateCost(points, draws, runs);
  return estimate.measured
    ? { seconds: estimate.measured.seconds, measured: true }
    : { seconds: estimate.laptopS, measured: false };
}

/** The daemon stops any workbench job at this many seconds
 *  (`workbench_jobs.DIGEST_TIMEOUT_SECONDS`). */
export const SERVER_JOB_LIMIT_S = 900;

/** Cancel is "stop waiting", and says so: the browser drops the request, but
 *  `workbench_jobs.run_off_loop` holds the job behind `asyncio.shield`, no
 *  route cancels it, and the workbench runs one job at a time. */
export function cancelWording(expected: { seconds: number; measured: boolean }): string {
  const basis = expected.measured
    ? "from your earlier runs here"
    : "the dev-laptop fit; not yet measured on this server";
  return `Cancel only stops waiting: the server finishes this run (about ${formatDuration(expected.seconds)}, ${basis}) before another can start. No route stops it sooner, and it is bounded at ${SERVER_JOB_LIMIT_S} s.`;
}
