/** Tire pressure units. The app shows and takes tire pressure in psi (the tire
 *  ledger, the tread-wear card); the backend's run conditions are kPa. The
 *  conversion happens here, at the edge, so a psi figure never travels as kPa. */

/** Exactly 6.894757293168361 kPa per psi (the daemon's `analog_sensors` constant). */
export const KPA_PER_PSI = 6.894757293168361;

export function psiToKpa(psi: number): number {
  return psi * KPA_PER_PSI;
}

export function kpaToPsi(kpa: number): number {
  return kpa / KPA_PER_PSI;
}

/** The range the daemon accepts for a run's stated tire pressure, mirrored from
 *  `TIRE_PRESSURE_MIN_KPA` / `TIRE_PRESSURE_MAX_KPA` in
 *  `src/analysis/dyno_comparison.py`, which is the authority: it refuses a
 *  value outside it with a 400. This copy only lets the form say so in psi
 *  before the request is made. */
export const TIRE_PRESSURE_RANGE_KPA = { min: 80, max: 600 } as const;

export type TirePsiReading =
  { state: "unstated" } | { state: "ok"; kpa: number } | { state: "refused"; message: string };

/** What the operator typed in a psi box: nothing, a pressure in kPa ready to
 *  send, or a refusal in psi's own words. A value that is only plausible as
 *  kPa or bar (200, 2.4) is refused rather than converted. */
export function readTirePsi(text: string): TirePsiReading {
  const trimmed = text.trim();
  if (!trimmed) return { state: "unstated" };
  const psi = Number(trimmed);
  if (!Number.isFinite(psi)) {
    return { state: "refused", message: `"${trimmed}" is not a number of psi` };
  }
  const kpa = psiToKpa(psi);
  if (kpa < TIRE_PRESSURE_RANGE_KPA.min || kpa > TIRE_PRESSURE_RANGE_KPA.max) {
    // Rounded inward, so every psi figure the message names is one it accepts.
    const lo = (Math.ceil(kpaToPsi(TIRE_PRESSURE_RANGE_KPA.min) * 10) / 10).toFixed(1);
    const hi = Math.floor(kpaToPsi(TIRE_PRESSURE_RANGE_KPA.max)).toFixed(0);
    return {
      state: "refused",
      message: `${trimmed} psi is outside ${lo}-${hi} psi: is it kPa or bar? Enter psi`,
    };
  }
  return { state: "ok", kpa };
}
