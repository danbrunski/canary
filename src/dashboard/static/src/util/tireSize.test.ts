import { describe, expect, it } from "vitest";

import { COMMON_TIRE_SIZES, parseTireSize } from "./tireSize";

describe("parseTireSize", () => {
  it.each([
    // 2 × width × aspect + rim × 25.4, to the millimeter
    ["285/75R16", "285/75R16", 0.834],
    ["LT285/75R16", "LT285/75R16", 0.834],
    ["lt 285 / 75 r16 126/123R", "LT285/75R16", 0.834],
    ["265/75R16", "265/75R16", 0.804], // the 2013 profile's hand-worked figure
    ["P225/45ZR17", "P225/45ZR17", 0.634],
    ["235/75-15", "235/75-15", 0.734],
    // flotation sizes state the diameter in inches
    ["33x12.50R15", "33x12.50R15", 0.838],
    ["35X12.50R17LT", "35x12.50R17LT", 0.889],
  ])("%s is %s at %f m", (text, size, diameterM) => {
    const result = parseTireSize(text);
    expect(result).toMatchObject({ kind: "ok", size, diameterM });
  });

  it("gives the unloaded circumference alongside", () => {
    const result = parseTireSize("285/75R16");
    expect(result.kind === "ok" && result.circumferenceM).toBe(2.62);
  });

  it("tells empty from malformed", () => {
    expect(parseTireSize("  ").kind).toBe("empty");
    expect(parseTireSize("285/75R1").kind).toBe("invalid");
    expect(parseTireSize("285/15R16").kind).toBe("invalid");
    expect(parseTireSize("15x33R16").kind).toBe("invalid");
  });

  it("parses every size it offers", () => {
    for (const size of COMMON_TIRE_SIZES) expect(parseTireSize(size).kind).toBe("ok");
  });
});
