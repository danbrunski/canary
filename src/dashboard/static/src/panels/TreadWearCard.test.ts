import { describe, expect, it } from "vitest";

import type { TireStatus } from "../types";
import { treadHeadline } from "./TreadWearCard";

const base: TireStatus = {
  method_version: 1,
  series_start: null,
  series_reset_by: null,
  drives: [],
  current: null,
  baseline: null,
  estimate: null,
  caveats: [],
};

describe("treadHeadline", () => {
  it("says there is nothing yet before any measured drive", () => {
    expect(treadHeadline(base)).toBe("no measured drive yet");
  });

  it("asks for a gauge reading when there are drives but no baseline", () => {
    const status: TireStatus = {
      ...base,
      current: {
        circumference_m: 2.559,
        uncertainty_m: 0.0012,
        drives: 10,
        drive_scatter_pct: 0.15,
        through: "2026-10-03",
      },
    };
    expect(treadHeadline(status)).toBe("record a gauge reading to estimate tread depth");
  });

  it("names a pending baseline by its drive count", () => {
    const status: TireStatus = {
      ...base,
      baseline: {
        tread_depth_mm: 10,
        measured_on: "2026-10-03",
        event: null,
        drives: 1,
        status: "pending",
      },
    };
    expect(treadHeadline(status)).toBe("gauge reading recorded; 1 of 3 drives near it measured");
  });

  it("shows the estimated depth with its uncertainty", () => {
    const status: TireStatus = {
      ...base,
      estimate: {
        tread_depth_mm: 8.52,
        worn_since_baseline_mm: 1.48,
        uncertainty_mm: 0.21,
        drives_since_reading: 6,
        as_of: "2026-10-20",
      },
    };
    expect(treadHeadline(status)).toBe("8.5 mm ± 0.2");
  });

  it("says so when no drive has been measured since the gauge reading", () => {
    const status: TireStatus = {
      ...base,
      estimate: {
        tread_depth_mm: 12.7,
        worn_since_baseline_mm: 0,
        uncertainty_mm: 0,
        drives_since_reading: 0,
        as_of: "2026-10-04",
      },
    };
    expect(treadHeadline(status)).toBe("12.7 mm (the gauge reading; no drive measured since)");
  });
});
