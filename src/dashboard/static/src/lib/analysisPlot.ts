// The pure side of the Analysis views (R19.3): the delivery-error chart's
// scale and the shaping of results into the cell grid the Trim and Timing
// views draw.

import type {
  Cell,
  DeliveryErrorResponse,
  PullComparisonResponse,
  TrimSurfaceResponse,
} from "../types/workbench";

export const DELIVERY_CHART_HEIGHT = 200;
const DELIVERY_BAND_WIDTH = 70;
const DELIVERY_MIN_WIDTH = 320;
/** The chart spans at least this many percent either side of zero. */
const DELIVERY_MIN_SPAN_PCT = 3;
const DELIVERY_HEADROOM = 1.15;

export interface DeliveryScale {
  /** Half-width of the noise floor's sd band, 0 with no floor. */
  sd: number;
  /** The floor's per-cell range, or ±sd when none was recorded. */
  rangeLo: number;
  rangeHi: number;
  /** Percent shown at the top and bottom edge of the plot. */
  limit: number;
  width: number;
  /** Pixels for an error of `value` percent: zero on the centre line. */
  y: (value: number) => number;
}

/** The delivery chart's scale: wide enough for the largest error and the
 *  whole noise-floor range, never under ±3 %, and a band with no error
 *  counts as zero. An unavailable floor contributes nothing. */
export function deliveryScale(
  result: Pick<DeliveryErrorResponse, "noise_floor" | "bands">,
): DeliveryScale {
  const floor = result.noise_floor;
  const sd = floor.available ? (floor.sd ?? 0) : 0;
  const rangeLo = floor.available && floor.min != null ? floor.min : -sd;
  const rangeHi = floor.available && floor.max != null ? floor.max : sd;
  const values = result.bands.map((b) => b.error_pct ?? 0);
  const limit =
    Math.max(
      DELIVERY_MIN_SPAN_PCT,
      ...values.map((v) => Math.abs(v)),
      Math.abs(rangeLo),
      Math.abs(rangeHi),
    ) * DELIVERY_HEADROOM;
  const H = DELIVERY_CHART_HEIGHT;
  return {
    sd,
    rangeLo,
    rangeHi,
    limit,
    width: Math.max(DELIVERY_MIN_WIDTH, result.bands.length * DELIVERY_BAND_WIDTH),
    y: (v) => H / 2 - (v / limit) * (H / 2 - 12),
  };
}

/** The cells of one bank, pooled over the sessions or those of one session
 *  (none for a bank that session lacks). */
export function trimCells(result: TrimSurfaceResponse | null, bank: string, split: string): Cell[] {
  if (!result) return [];
  if (split === "pooled") return result.banks[bank]?.cells ?? [];
  return result.per_session.find((s) => s.session_id === split)?.cells[bank] ?? [];
}

/** A revision comparison as the grid draws a result: each cell sized by the
 *  thinner of its two sides, never sparse, carrying its delta and whether
 *  timing dropped. */
export function comparedAsCells(result: PullComparisonResponse): Cell[] {
  return result.cells.map((c) => ({
    rpm_index: c.rpm_index,
    airflow_index: c.airflow_index,
    rpm_range: c.rpm_range,
    airflow_range: c.airflow_range,
    n: Math.min(c.n_a, c.n_b),
    sparse: false,
    delta: c.delta,
    dropped: c.dropped,
  }));
}
