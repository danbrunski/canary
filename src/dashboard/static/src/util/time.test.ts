import { describe, expect, it } from "vitest";

import { formatClockDuration, formatElapsed } from "./time";

describe("formatClockDuration (R08.5)", () => {
  it("defaults to a running clock: floored, hours when nonzero, padded minutes", () => {
    expect(formatClockDuration(65.9)).toBe("01:05");
    expect(formatClockDuration(3725)).toBe("1:02:05");
    expect(formatClockDuration(-4)).toBe("00:00");
    expect(formatClockDuration(null)).toBe("—");
    expect(formatClockDuration(Number.NaN)).toBe("—");
  });

  it("pads hours on request (the tuning bar's capture clock)", () => {
    expect(formatClockDuration(60, { hours: "always" })).toBe("00:01:00");
    expect(formatClockDuration(36125, { hours: "always" })).toBe("10:02:05");
  });

  it("rounds with unpadded minutes for log durations", () => {
    const log = { rounding: "round", padMinutes: false, missing: "unknown" } as const;
    expect(formatClockDuration(65.6, log)).toBe("1:06");
    expect(formatClockDuration(undefined, log)).toBe("unknown");
  });

  it("formatElapsed is the default clock since an ISO start", () => {
    const start = "2026-09-21T12:00:00Z";
    expect(formatElapsed(start, Date.parse(start) + 3_725_400)).toBe("1:02:05");
    expect(formatElapsed("garbage", 0)).toBe("—");
  });
});
