import { describe, expect, it } from "vitest";

import {
  formatAltitude,
  formatCoords,
  formatGpsSpeed,
  formatHeading,
  formatNumber,
  formatPercent,
  formatTemp,
  toneClass,
} from "./format";
import { numberArray, numberMap, record } from "./json";

describe("formatNumber", () => {
  it("is magnitude-aware with a configurable small-value precision", () => {
    expect(formatNumber(123.45)).toBe("123");
    expect(formatNumber(12.345)).toBe("12.3");
    expect(formatNumber(0.123)).toBe("0.12"); // Telemetry default
    expect(formatNumber(0.123, 3)).toBe("0.123"); // evidence cards
  });
});

describe("formatPercent", () => {
  it("treats null and undefined alike and keeps per-call precision", () => {
    expect(formatPercent(null)).toBe("-");
    expect(formatPercent(undefined)).toBe("-");
    expect(formatPercent(0.1234)).toBe("12.3%"); // Training default (1)
    expect(formatPercent(0.1234, 0)).toBe("12%"); // ModelHealth
  });
});

describe("toneClass", () => {
  it("maps tones to text utilities, '' when absent", () => {
    expect(toneClass("ok")).toBe("text-ok");
    expect(toneClass("warn")).toBe("text-warn");
    expect(toneClass("bad")).toBe("text-bad");
    expect(toneClass(undefined)).toBe("");
  });
});

describe("GPS/system formatters (moved from SystemPanel)", () => {
  it("formatTemp: one decimal, em-dash for null", () => {
    expect(formatTemp(72.34)).toBe("72.3 °C");
    expect(formatTemp(null)).toBe("—");
    expect(formatTemp(undefined)).toBe("—");
  });

  it("formatCoords: 5 decimals, em-dash if either is missing", () => {
    expect(formatCoords(37.12345, -122.6789)).toBe("37.12345, -122.67890");
    expect(formatCoords(null, -122.6789)).toBe("—");
    expect(formatCoords(37.12345, null)).toBe("—");
  });

  it("formatAltitude: meters to whole feet", () => {
    expect(formatAltitude(100)).toBe("328 ft");
    expect(formatAltitude(null)).toBe("—");
  });

  it("formatGpsSpeed: km/h to whole mph", () => {
    expect(formatGpsSpeed(100)).toBe("62 mph");
    expect(formatGpsSpeed(null)).toBe("—");
  });

  it("formatHeading: degrees with compass cardinal", () => {
    expect(formatHeading(0)).toBe("0° N");
    expect(formatHeading(184)).toBe("184° S");
    expect(formatHeading(null)).toBe("—");
  });
});

describe("numberArray (drift fix)", () => {
  it("drops NaN/Infinity, not just non-numbers", () => {
    expect(numberArray([1, "x", NaN, Infinity, 2])).toEqual([1, 2]);
    expect(numberArray("nope")).toEqual([]);
  });
});

describe("numberMap / record", () => {
  it("keeps only numeric entries; null when empty or non-object", () => {
    expect(numberMap({ a: 1, b: "x", c: NaN })).toEqual({ a: 1 });
    expect(numberMap({ b: "x" })).toBeNull();
    expect(numberMap([1, 2])).toBeNull();
    expect(record([1])).toBeNull();
    expect(record({ a: 1 })).toEqual({ a: 1 });
  });
});
