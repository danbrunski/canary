import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { MonitorsResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getMonitors"]);

const { MonitorsPanel, computeMisfireRows } = await import("./MonitorsPanel");

const EMPTY: MonitorsResponse = {
  captured_at: null,
  misfire_counts: {},
  results: [],
  stale: true,
};

describe("MonitorsPanel", () => {
  it("shows a loading state before the first fetch settles", () => {
    mocks.getMonitors.mockReturnValue(new Promise(() => {}));
    render(createElement(MonitorsPanel));
    expect(screen.getByText(/reading monitor sweep/)).toBeInTheDocument();
  });

  it("renders an empty sweep gracefully with 'never polled'", async () => {
    mocks.getMonitors.mockResolvedValue(EMPTY);
    render(createElement(MonitorsPanel));
    expect(await screen.findByText("never polled")).toBeInTheDocument();
    expect(screen.getAllByText("none")).toHaveLength(2);
  });

  it("renders per-cylinder misfire counts and monitor test results", async () => {
    mocks.getMonitors.mockResolvedValue({
      captured_at: "2026-07-19T12:00:00+00:00",
      misfire_counts: { "1": 3, "2": 0 },
      results: [
        {
          monitor_id: 11,
          test_id: 1,
          unit_and_scaling_id: 16,
          test_value: 50,
          min_limit: 0,
          max_limit: 100,
          passed: true,
        },
        {
          monitor_id: 12,
          test_id: 1,
          unit_and_scaling_id: 16,
          test_value: 200,
          min_limit: 0,
          max_limit: 100,
          passed: false,
        },
      ],
      stale: false,
    } satisfies MonitorsResponse);
    render(createElement(MonitorsPanel));

    expect(await screen.findByText("Cyl 1")).toBeInTheDocument();
    expect(screen.getByText("Cyl 2")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("MID 11 / TID 1")).toBeInTheDocument();
    expect(screen.getByText("MID 12 / TID 1")).toBeInTheDocument();
    expect(screen.getByText("2 tests")).toBeInTheDocument();
    expect(screen.queryByText(/stale/)).not.toBeInTheDocument();
  });

  it("shows no delta on the very first snapshot (it IS the baseline)", async () => {
    mocks.getMonitors.mockResolvedValue({
      ...EMPTY,
      captured_at: "2026-07-19T12:00:00+00:00",
      misfire_counts: { "1": 2 },
      stale: false,
    } satisfies MonitorsResponse);
    render(createElement(MonitorsPanel));
    await screen.findByText("Cyl 1");
    expect(screen.queryByText(/^\+/)).not.toBeInTheDocument();
  });

  describe("computeMisfireRows (Task 3.5 delta-since-drive-start)", () => {
    it("has zero delta for a cylinder with no baseline yet", () => {
      const rows = computeMisfireRows({ "1": 2 }, {});
      expect(rows).toEqual([{ cylinder: "1", count: 2, delta: 0 }]);
    });

    it("computes a positive delta when the count grew past the baseline", () => {
      const rows = computeMisfireRows({ "1": 5, "2": 0 }, { "1": 2, "2": 0 });
      expect(rows).toEqual([
        { cylinder: "1", count: 5, delta: 3 },
        { cylinder: "2", count: 0, delta: 0 },
      ]);
    });

    it("sorts rows numerically by cylinder", () => {
      const rows = computeMisfireRows({ "10": 1, "2": 1 }, {});
      expect(rows.map((r) => r.cylinder)).toEqual(["2", "10"]);
    });
  });

  it("shows a stale indicator when the sweep is stale", async () => {
    mocks.getMonitors.mockResolvedValue({
      ...EMPTY,
      captured_at: "2026-07-19T12:00:00+00:00",
      stale: true,
    } satisfies MonitorsResponse);
    render(createElement(MonitorsPanel));
    expect(await screen.findByText(/stale/)).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    mocks.getMonitors.mockRejectedValue(new Error("boom"));
    render(createElement(MonitorsPanel));
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});
