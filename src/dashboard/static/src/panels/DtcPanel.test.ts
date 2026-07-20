import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { DtcReportResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getDtc"]);

const { DtcPanel } = await import("./DtcPanel");

const EMPTY: DtcReportResponse = {
  captured_at: null,
  stored: [],
  pending: [],
  permanent: [],
  mil_on: null,
  dtc_count: 0,
  stale: true,
};

describe("DtcPanel", () => {
  it("shows a loading state before the first fetch settles", () => {
    mocks.getDtc.mockReturnValue(new Promise(() => {}));
    render(createElement(DtcPanel));
    expect(screen.getByText(/reading DTC report/)).toBeInTheDocument();
  });

  it("renders an empty report gracefully with 'never polled'", async () => {
    mocks.getDtc.mockResolvedValue(EMPTY);
    render(createElement(DtcPanel));
    expect(await screen.findByText("never polled")).toBeInTheDocument();
    expect(screen.getByText("MIL state unknown")).toBeInTheDocument();
    expect(screen.getAllByText("none")).toHaveLength(3);
  });

  it("renders the MIL-on banner and the three code lists with description/system", async () => {
    mocks.getDtc.mockResolvedValue({
      captured_at: "2026-07-19T12:00:00+00:00",
      stored: [
        {
          code: "P0300",
          description: "Random/Multiple Cylinder Misfire Detected",
          system: "powertrain",
          generic: true,
          curated: true,
        },
      ],
      pending: [
        {
          code: "P0171",
          description: "System Too Lean (Bank 1)",
          system: "powertrain",
          generic: true,
          curated: true,
        },
      ],
      permanent: [],
      mil_on: true,
      dtc_count: 2,
      stale: false,
    } satisfies DtcReportResponse);
    render(createElement(DtcPanel));

    expect(await screen.findByText("MIL ON")).toBeInTheDocument();
    expect(screen.getByText("P0300")).toBeInTheDocument();
    expect(
      screen.getByText("Random/Multiple Cylinder Misfire Detected"),
    ).toBeInTheDocument();
    expect(screen.getAllByText("powertrain")).toHaveLength(2);
    expect(screen.getByText("P0171")).toBeInTheDocument();
    expect(screen.getByText("2 codes")).toBeInTheDocument();
    expect(screen.queryByText(/stale/)).not.toBeInTheDocument();
  });

  it("shows a stale indicator when the report is stale", async () => {
    mocks.getDtc.mockResolvedValue({
      ...EMPTY,
      captured_at: "2026-07-19T12:00:00+00:00",
      mil_on: false,
      stale: true,
    } satisfies DtcReportResponse);
    render(createElement(DtcPanel));
    expect(await screen.findByText("MIL off")).toBeInTheDocument();
    expect(screen.getByText(/stale/)).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    mocks.getDtc.mockRejectedValue(new Error("boom"));
    render(createElement(DtcPanel));
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});
