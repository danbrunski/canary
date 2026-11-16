import { describe, expect, it } from "vitest";

import {
  formatClockDuration,
  formatDateTime,
  formatDuration,
  formatElapsed,
  formatTimestamp,
} from "./time";

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

describe("formatDateTime (R28.7)", () => {
  const iso = "2026-11-15T15:04:05Z";
  const bare = new Date(iso).toLocaleString();

  it("formats an ISO stamp the way the bare toLocaleString copies did", () => {
    expect(formatDateTime(iso)).toBe(bare);
  });

  it("reads a number as epoch seconds by default and milliseconds on request", () => {
    const seconds = Date.parse(iso) / 1000;
    expect(formatDateTime(seconds)).toBe(bare);
    expect(formatDateTime(Date.parse(iso), { unit: "milliseconds" })).toBe(bare);
  });

  it("has a medium form for the paired-device list", () => {
    const medium = new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
    expect(formatDateTime(iso, { style: "medium" })).toBe(medium);
    expect(medium).not.toBe(bare);
  });

  it("says missing for null, undefined and an empty string, and lets the caller name it", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime("")).toBe("—");
    expect(formatDateTime(null, { missing: "never" })).toBe("never");
    expect(formatDateTime("", { missing: "session-1" })).toBe("session-1");
  });

  it('shows an unparseable value as itself, never "Invalid Date"', () => {
    expect(formatDateTime("not a date")).toBe("not a date");
    expect(formatDateTime(Number.NaN)).toBe("NaN");
  });

  it("treats zero as a real time, not a missing one", () => {
    expect(formatDateTime(0)).toBe(new Date(0).toLocaleString());
  });
});

describe("formatTimestamp (R28.7)", () => {
  it("is the locale time of day", () => {
    const iso = "2026-11-15T15:04:05Z";
    expect(formatTimestamp(iso)).toBe(new Date(iso).toLocaleTimeString());
  });

  it("takes epoch milliseconds, which is what Date takes", () => {
    const ms = Date.parse("2026-11-15T15:04:05Z");
    expect(formatTimestamp(ms)).toBe(new Date(ms).toLocaleTimeString());
  });

  it("falls back to the raw value, which its old catch never did", () => {
    expect(formatTimestamp("garbage")).toBe("garbage");
  });
});

describe("formatDuration (the two variants, R28.7)", () => {
  it("default (TripPanel): floored, minutes always shown, a dash for null or non-finite", () => {
    expect(formatDuration(65.9)).toBe("1m 5s");
    expect(formatDuration(5)).toBe("0m 5s");
    expect(formatDuration(-3)).toBe("0m 0s");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(Number.NaN)).toBe("—");
  });

  it("collapseZero (PowerPanel): rounded, no 0m prefix, 0s for null or not positive", () => {
    const opts = { collapseZero: true };
    expect(formatDuration(65.9, opts)).toBe("1m 6s");
    expect(formatDuration(5.4, opts)).toBe("5s");
    expect(formatDuration(null, opts)).toBe("0s");
    expect(formatDuration(0, opts)).toBe("0s");
    expect(formatDuration(-3, opts)).toBe("0s");
    expect(formatDuration(Number.NaN, opts)).toBe("0s");
  });
});
