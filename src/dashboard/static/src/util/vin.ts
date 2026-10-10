/** Live VIN feedback for the profile editor — a mirror of `src/vehicle/vin.py`,
 *  which stays the authority (the daemon refuses on save). ISO 3779 shape,
 *  and the North American check digit at position 9: refused on a North
 *  American VIN (first character 1–5), a warning elsewhere. */

export type VinStatus =
  | { kind: "empty" }
  | { kind: "malformed"; message: string }
  | { kind: "pass"; message: string }
  | { kind: "refused"; message: string }
  | { kind: "warning"; message: string };

const WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2];
const LETTERS: Record<string, number> = {
  A: 1, B: 2, C: 3, D: 4, E: 5, F: 6, G: 7, H: 8,
  J: 1, K: 2, L: 3, M: 4, N: 5, P: 7, R: 9,
  S: 2, T: 3, U: 4, V: 5, W: 6, X: 7, Y: 8, Z: 9,
}; // prettier-ignore

function value(ch: string): number {
  return /[0-9]/.test(ch) ? Number(ch) : (LETTERS[ch] ?? 0);
}

/** The check digit a well-formed 17-character VIN should carry. */
export function expectedCheckDigit(vin: string): string {
  let total = 0;
  for (let i = 0; i < 17; i += 1) total += value(vin[i]) * WEIGHTS[i];
  const remainder = total % 11;
  return remainder === 10 ? "X" : String(remainder);
}

export function checkVin(text: string): VinStatus {
  const vin = text.trim().toUpperCase();
  if (!vin) return { kind: "empty" };
  if (vin.length !== 17) {
    return { kind: "malformed", message: `A VIN has 17 characters; this has ${vin.length}.` };
  }
  if (!/^[A-Z0-9]+$/.test(vin)) {
    return { kind: "malformed", message: "A VIN holds only letters and digits." };
  }
  if (/[IOQ]/.test(vin)) {
    return { kind: "malformed", message: "A VIN never contains I, O or Q (they read as 1 and 0)." };
  }
  const expected = expectedCheckDigit(vin);
  if (vin[8] === expected) return { kind: "pass", message: "Check digit valid." };
  if ("12345".includes(vin[0])) {
    return {
      kind: "refused",
      message: `Check digit mismatch: position 9 is ${vin[8]}, the rest gives ${expected}. A North American VIN always carries a valid one — mistyped or misread.`,
    };
  }
  return {
    kind: "warning",
    message: `Check digit is ${vin[8]}, the North American scheme gives ${expected}; a VIN from outside North America need not carry one — check it against the plate.`,
  };
}
