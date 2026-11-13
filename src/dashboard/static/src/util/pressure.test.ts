import { describe, expect, it } from "vitest";

import { KPA_PER_PSI, kpaToPsi, psiToKpa, readTirePsi } from "./pressure";

describe("tire pressure units", () => {
  it("converts psi to kPa and back", () => {
    expect(psiToKpa(35)).toBeCloseTo(241.3165, 4);
    expect(psiToKpa(1)).toBe(KPA_PER_PSI);
    expect(kpaToPsi(psiToKpa(33.5))).toBeCloseTo(33.5, 12);
  });

  it("reads an empty box as not stated, never as zero", () => {
    expect(readTirePsi("")).toEqual({ state: "unstated" });
    expect(readTirePsi("   ")).toEqual({ state: "unstated" });
  });

  it("sends psi as kPa, unrounded, so a record converted the same way matches exactly", () => {
    expect(readTirePsi(" 35 ")).toEqual({ state: "ok", kpa: 35 * KPA_PER_PSI });
  });

  it("refuses a kPa or bar figure in psi's own words", () => {
    for (const typed of ["241", "2.4", "-5", "0"]) {
      const reading = readTirePsi(typed);
      expect(reading.state, typed).toBe("refused");
      expect(reading).toMatchObject({ message: expect.stringContaining("11.7-87 psi") });
      expect(reading).toMatchObject({ message: expect.stringContaining("kPa or bar") });
    }
    expect(readTirePsi("abc")).toMatchObject({ state: "refused" });
    expect(readTirePsi("Infinity")).toMatchObject({ state: "refused" });
  });

  it("accepts the edges of the range the daemon accepts", () => {
    // 80 kPa is 11.603 psi, so the lowest one-decimal figure accepted is 11.7
    // (11.6 psi is 79.98 kPa), and the message names that one.
    expect(readTirePsi("11.7").state).toBe("ok");
    expect(readTirePsi("11.6").state).toBe("refused");
    // A trail pressure is a real pressure.
    expect(readTirePsi("12")).toEqual({ state: "ok", kpa: 12 * KPA_PER_PSI });
    expect(readTirePsi("14.5").state).toBe("ok");
    expect(readTirePsi("87").state).toBe("ok");
    expect(readTirePsi("87.1").state).toBe("refused");
  });
});
