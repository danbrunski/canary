import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type {
  BehaviorLiveResponse,
  BehaviorModelsResponse,
  BehaviorSeverity,
} from "../types";
import { BehaviorModelsPanel } from "./BehaviorModelsPanel";

const FIT_BASE = {
  sample_count: 1859,
  r_squared: null,
  sessions: ["2026-07-18T202456"],
  fitted_at: "2026-07-27T06:09:06.144540+00:00",
  training_data_hash: "sha256:6db79cbccb866817",
};

/** Every 1D fit kind the backend can serve, plus a 2D grid. Mirrors what
 *  `GET /models/behavior` returned from the truck on 2026-07-31. */
const MODELS: BehaviorModelsResponse = {
  schema_version: 1,
  models: [
    {
      name: "warm_idle_rpm_baseline",
      kind: "surface_1d",
      output: "obd:RPM",
      output_unit: "rpm",
      axis: { name: "observation", channel: "obd:RPM", unit: "rpm", domain: [450, 2450] },
      operating_mode: "idle",
      version: 2,
      fit: { ...FIT_BASE, kind: "robust_baseline", median: 780, mad: 24, p01: 690, p99: 900 },
      deviation: {
        kind: "absolute",
        warning_band: 100,
        critical_band: 250,
        reference_std: null,
      },
      notes: null,
    },
    {
      name: "cat_warmup",
      kind: "surface_1d",
      output: "obd:CATALYST_TEMP_B1S1",
      output_unit: "C",
      axis: { name: "run_time", channel: "obd:RUN_TIME", unit: "s", domain: [0, 705] },
      operating_mode: null,
      version: 1,
      fit: { ...FIT_BASE, kind: "lookup_table", x_knots: [0, 300, 705], y_knots: [20, 400, 620] },
      deviation: null,
      notes: null,
    },
    {
      name: "timing_advance_vs_rpm_load",
      kind: "surface_2d",
      output: "obd:TIMING_ADVANCE",
      output_unit: "deg",
      axes: [
        { name: "rpm", channel: "obd:RPM", unit: "rpm", bin_edges: [500, 1500, 2500] },
        { name: "load", channel: "obd:ENGINE_LOAD", unit: "%", bin_edges: [0, 50, 100] },
      ],
      operating_mode: null,
      version: 1,
      grid_version: 1,
      min_cell_count: 10,
      min_cell_dwell_seconds: 1,
      min_cell_sessions: 2,
      fit: {
        ...FIT_BASE,
        kind: "grid_binned_mean",
        cells: [
          { mean: 12, std: 2, count: 40 },
          { mean: 18, std: 3, count: 30 },
          { mean: 22, std: 2, count: 25 },
          { mean: 28, std: 4, count: 12 },
        ],
      },
      deviation: null,
      notes: null,
    },
  ] as unknown as BehaviorModelsResponse["models"],
};

function liveEntry(kind: "surface_1d" | "surface_2d", severity: BehaviorSeverity = "ok") {
  return {
    kind,
    x: null,
    y: null,
    actual: null,
    expected: null,
    deviation: null,
    severity,
    mode_match: false,
    // The parked truck reports a long all-null series — no operating point,
    // but plenty of samples.
    series: Array.from({ length: 12 }, () => ({
      t: "2026-08-01T00:16:02.565123Z",
      x: null,
      y: null,
      actual: null,
      expected: null,
      deviation: null,
      severity,
    })),
  };
}

const LIVE: BehaviorLiveResponse = {
  observed: 128,
  timestamp: "2026-08-01T00:19:23.347526Z",
  operating_mode: "all",
  models: {
    warm_idle_rpm_baseline: liveEntry("surface_1d"),
    cat_warmup: liveEntry("surface_1d"),
    timing_advance_vs_rpm_load: liveEntry("surface_2d"),
  },
} as unknown as BehaviorLiveResponse;

describe("BehaviorModelsPanel", () => {
  it("renders every fit kind the backend serves", () => {
    // A robust_baseline fit carries neither coefficients nor knots. Reading
    // knots off it threw and took the whole dashboard to a white screen,
    // because one panel's crash unmounts the app.
    render(createElement(BehaviorModelsPanel, { data: MODELS, error: null, live: LIVE }));

    expect(screen.getByText("warm_idle_rpm_baseline")).toBeInTheDocument();
    expect(screen.getByText("cat_warmup")).toBeInTheDocument();
    expect(screen.getByText("timing_advance_vs_rpm_load")).toBeInTheDocument();
  });

  it("renders without the live overlay", () => {
    render(createElement(BehaviorModelsPanel, { data: MODELS, error: null, live: null }));
    expect(screen.getByText("warm_idle_rpm_baseline")).toBeInTheDocument();
  });
});
