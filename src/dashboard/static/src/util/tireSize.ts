/** Tire size designations (the sidewall code) to an unloaded diameter.
 *
 *  Metric (P-metric, LT, ST and Euro): `285/75R16` is a 285 mm section width,
 *  a sidewall 75 % of that, on a 16 in rim, so the diameter is
 *  2 × 285 × 0.75 + 16 × 25.4 = 833.9 mm. Flotation: `33x12.50R15` states the
 *  33 in diameter outright. Both are nominal, new and unloaded; the loaded
 *  rolling circumference is a separate, measured profile fact. */

const MM_PER_INCH = 25.4;

/** Sizes offered in the picker; any other well-formed size can be typed. */
export const COMMON_TIRE_SIZES: readonly string[] = [
  "195/65R15",
  "205/55R16",
  "215/60R16",
  "215/55R17",
  "225/45R17",
  "225/65R17",
  "235/45R18",
  "235/65R17",
  "245/40R18",
  "235/75R15",
  "245/75R16",
  "265/75R16",
  "285/75R16",
  "315/75R16",
  "265/70R16",
  "265/70R17",
  "285/70R17",
  "315/70R17",
  "265/65R18",
  "275/65R18",
  "285/65R18",
  "275/60R20",
  "275/55R20",
  "31x10.50R15",
  "33x12.50R15",
  "35x12.50R15",
  "33x12.50R17",
  "35x12.50R17",
  "37x12.50R17",
  "35x12.50R20",
];

export type TireSize =
  | { kind: "empty" }
  | { kind: "invalid"; message: string }
  | { kind: "ok"; size: string; diameterM: number; circumferenceM: number };

// Prefix, width / aspect, construction (R, ZR, D, B or a dash), rim; a load
// index, speed rating or load range may follow after a space.
const METRIC =
  /^(P|LT|ST|T)?\s*(\d{3})\s*\/\s*(\d{2})\s*(ZR|R|D|B|-)?\s*(\d{2}(?:\.\d)?)(?:\s+.*)?$/i;
const FLOTATION =
  /^(\d{2}(?:\.\d{1,2})?)\s*X\s*(\d{1,2}(?:\.\d{1,2})?)\s*(R|D|B|-)?\s*(\d{2}(?:\.\d)?)\s*(LT)?(?:\s+.*)?$/i;

/** Round to the millimeter the profile stores. */
const meters = (mm: number) => Math.round(mm) / 1000;

/** Parse a sidewall size into its nominal unloaded diameter, in meters. */
export function parseTireSize(text: string): TireSize {
  const compact = text.trim().toUpperCase();
  if (!compact) return { kind: "empty" };
  const metric = METRIC.exec(compact);
  if (metric) {
    const [, prefix = "", width, aspect, construction = "R", rim] = metric;
    const w = Number(width);
    const a = Number(aspect);
    const r = Number(rim);
    if (w < 100 || w > 455 || a < 20 || a > 95 || r < 10 || r > 30) {
      return { kind: "invalid", message: `${compact}: not a tire size this picker knows` };
    }
    const diameterMm = (2 * w * a) / 100 + r * MM_PER_INCH;
    return ok(`${prefix}${w}/${a}${construction}${rim}`, diameterMm);
  }
  const flotation = FLOTATION.exec(compact);
  if (flotation) {
    const [, diameter, width, construction = "R", rim, lt = ""] = flotation;
    const d = Number(diameter);
    const r = Number(rim);
    if (d <= r || d < 20 || d > 54 || Number(width) <= 0) {
      return { kind: "invalid", message: `${compact}: not a tire size this picker knows` };
    }
    return ok(`${diameter}x${width}${construction}${rim}${lt}`, d * MM_PER_INCH);
  }
  return {
    kind: "invalid",
    message: `${text.trim()}: expected a size such as 285/75R16 or 33x12.50R15`,
  };
}

function ok(size: string, diameterMm: number): TireSize {
  const diameterM = meters(diameterMm);
  return {
    kind: "ok",
    size,
    diameterM,
    circumferenceM: Math.round(Math.PI * diameterMm) / 1000,
  };
}
