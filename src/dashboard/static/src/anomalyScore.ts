/** Anomaly-score presentation helpers.
 *
 *  The raw `AnomalyEvent.score` is model-internal and not human-readable:
 *
 *  * Isolation Forest emits `-decision_function` — its anomaly boundary sits
 *    at ~0, so a row that's a hair positive (0.002) is flagged the same as a
 *    genuinely bad one. `threshold` is usually null for IF.
 *  * LSTM emits a reconstruction error against a fitted `threshold`.
 *
 *  Operators asked for a single normalized index + a normalized chart instead
 *  of a flood of near-zero "anomalies". This module maps a raw score to a
 *  0–100 index and a coarse severity level, kept pure so it's unit-testable
 *  without React.
 */

export type AnomalyLevel = "low" | "elevated" | "high";

export interface ScoreView {
  /** Raw model score, passed through for the detail line. */
  raw: number;
  /** 0–100 normalized anomaly index. */
  index: number;
  level: AnomalyLevel;
}

/** Linear-interpolated percentile of `values` (unsorted ok). 0 when empty. */
export function percentile(values: number[], p: number): number {
  const finite = values.filter((v) => Number.isFinite(v));
  if (finite.length === 0) return 0;
  const sorted = [...finite].sort((a, b) => a - b);
  if (sorted.length === 1) return sorted[0];
  const rank = (p / 100) * (sorted.length - 1);
  const lo = Math.floor(rank);
  const hi = Math.ceil(rank);
  if (lo === hi) return sorted[lo];
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (rank - lo);
}

/** Self-calibrating reference scale for scores that carry no threshold (IF):
 *  the 95th percentile of positive scores in the visible window, so the index
 *  tracks the observed range instead of a hard-coded constant. */
export function windowReference(scores: number[]): number {
  const positive = scores.filter((s) => Number.isFinite(s) && s > 0);
  const ref = percentile(positive, 95);
  return ref > 0 ? ref : 1;
}

/** Map one raw score to a 0–100 index + severity level.
 *
 *  When the model supplies a positive `threshold` (LSTM) we anchor the index
 *  at it with a saturating curve: score 0 → 0, score == threshold → 50,
 *  score ≫ threshold → ~100. That's assumption-free and monotonic. Without a
 *  threshold (IF) we scale against `windowRef`. Non-finite scores (the
 *  missing-features sentinel) pin to 100.
 */
export function scoreView(score: number, threshold: number | null, windowRef: number): ScoreView {
  if (!Number.isFinite(score)) {
    return { raw: score, index: 100, level: "high" };
  }
  const s = Math.max(score, 0);
  let index: number;
  if (threshold !== null && threshold > 0) {
    index = Math.round((100 * s) / (s + threshold));
  } else {
    const ref = windowRef > 0 ? windowRef : 1;
    index = Math.round(Math.min(100, (100 * s) / ref));
  }
  index = Math.max(0, Math.min(100, index));
  const level: AnomalyLevel = index < 15 ? "low" : index < 50 ? "elevated" : "high";
  return { raw: score, index, level };
}
