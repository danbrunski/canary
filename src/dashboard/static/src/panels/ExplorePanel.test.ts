import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mockHttp } from "../test/harness";
import type { BinnedAnalysis2D, BinnedAnalysisUnavailable } from "../types";

const mocks = mockHttp(["getBinnedAnalysis"]);

const { ExplorePanel } = await import("./ExplorePanel");

function binned2d(overrides: Partial<BinnedAnalysis2D> = {}): BinnedAnalysis2D {
  return {
    available: true,
    kind: "2d",
    value_channel: "obd:SHORT_FUEL_TRIM_1",
    x_channel: "obd:RPM",
    y_channel: "obd:ENGINE_LOAD",
    agg: "mean",
    sample_count: 40,
    x_edges: [0, 1000, 2000],
    y_edges: [0, 50, 100],
    rows: 2,
    cols: 2,
    cells: [
      { mean: 1.0, std: 0.2, count: 10 },
      { mean: 2.0, std: 0.3, count: 10 },
      { mean: 3.0, std: 0.4, count: 10 },
      { mean: 4.0, std: 0.5, count: 10 },
    ],
    ...overrides,
  };
}

describe("ExplorePanel", () => {
  it("runs a binned query and shows the sample count", async () => {
    mocks.getBinnedAnalysis.mockResolvedValue(binned2d());
    render(createElement(ExplorePanel));

    fireEvent.click(screen.getByRole("button", { name: "run" }));

    expect(await screen.findByText("n=40")).toBeInTheDocument();
    expect(mocks.getBinnedAnalysis).toHaveBeenCalledWith(
      expect.objectContaining({ value: "obd:SHORT_FUEL_TRIM_1", x: "obd:RPM", y: "obd:ENGINE_LOAD" }),
    );
  });

  it("shows the reason when the scope has no data", async () => {
    const unavailable: BinnedAnalysisUnavailable = {
      available: false,
      reason: "no telemetry in scope",
      value_channel: "obd:RPM",
      x_channel: "obd:RPM",
      y_channel: null,
      agg: "mean",
    };
    mocks.getBinnedAnalysis.mockResolvedValue(unavailable);
    render(createElement(ExplorePanel));

    fireEvent.click(screen.getByRole("button", { name: "run" }));

    expect((await screen.findAllByText("no telemetry in scope")).length).toBeGreaterThan(0);
  });
});
