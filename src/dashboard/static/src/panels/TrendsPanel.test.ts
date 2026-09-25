import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { StatTrend, TrendsResponse } from "../types";
import { mockHttp } from "../test/harness";

const mocks = mockHttp(["getTrends"]);

const { TrendsPanel } = await import("./TrendsPanel");

function statTrend(overrides: Partial<StatTrend> = {}): StatTrend {
  return {
    stat: "idle_rpm_mean",
    n: 4,
    session_ids: ["s0", "s1", "s2", "s3"],
    values: [800.0, 805.0, 815.0, 820.0],
    latest_session: "s3",
    latest_value: 820.0,
    reference_mean: 800.0,
    reference_std: 10.0,
    reference_band: [790.0, 810.0],
    mann_kendall: {
      n: 4,
      s: 6,
      var_s: 16.6,
      z: 1.9,
      p_value: 0.03,
      tau: 1.0,
      trend: "increasing",
    },
    cusum: {
      n: 4,
      k: 0.5,
      h: 5.0,
      sh: 6.0,
      sl: 0.0,
      alarm: true,
      direction: "upper",
      first_alarm_index: 3,
    },
    ...overrides,
  };
}

describe("TrendsPanel", () => {
  it("prompts for a token without one", () => {
    render(createElement(TrendsPanel, { token: "" }));
    expect(screen.getByText(/Enter the control token/)).toBeInTheDocument();
    expect(mocks.getTrends).not.toHaveBeenCalled();
  });

  it("shows a loading state before the first fetch settles", () => {
    mocks.getTrends.mockReturnValue(new Promise(() => {}));
    render(createElement(TrendsPanel, { token: "secret" }));
    expect(screen.getByText(/loading trends/)).toBeInTheDocument();
  });

  it("shows the unfrozen-epoch message when unavailable", async () => {
    const body: TrendsResponse = { available: false, reason: "no epoch", stats: {} };
    mocks.getTrends.mockResolvedValue(body);
    render(createElement(TrendsPanel, { token: "secret" }));
    expect(await screen.findByText(/No reference epoch frozen yet/)).toBeInTheDocument();
  });

  it("shows an empty message when the epoch is frozen but nothing has a fingerprint", async () => {
    const body: TrendsResponse = {
      available: true,
      frozen_at: "2026-06-01T00:00:00+00:00",
      reference_sessions: 5,
      stats: {},
    };
    mocks.getTrends.mockResolvedValue(body);
    render(createElement(TrendsPanel, { token: "secret" }));
    expect(await screen.findByText(/no session has a fingerprint yet/)).toBeInTheDocument();
  });

  it("renders a stat row with latest value, band, trend verdict, and CUSUM alarm", async () => {
    const body: TrendsResponse = {
      available: true,
      frozen_at: "2026-06-01T00:00:00+00:00",
      reference_sessions: 5,
      stats: { idle_rpm_mean: statTrend() },
    };
    mocks.getTrends.mockResolvedValue(body);
    render(createElement(TrendsPanel, { token: "secret" }));

    expect(await screen.findByText("idle_rpm_mean")).toBeInTheDocument();
    expect(screen.getByText(/latest 820/)).toBeInTheDocument();
    expect(screen.getByText(/band \[790, 810\]/)).toBeInTheDocument();
    expect(screen.getByText("trend: increasing")).toBeInTheDocument();
    expect(screen.getByText("CUSUM ALARM (upper)")).toBeInTheDocument();
  });

  it("shows 'CUSUM ok' for a stat that isn't alarming", async () => {
    const quiet = statTrend({
      cusum: {
        n: 4,
        k: 0.5,
        h: 5.0,
        sh: 0.5,
        sl: 0.0,
        alarm: false,
        direction: null,
        first_alarm_index: null,
      },
      mann_kendall: {
        n: 4,
        s: 0,
        var_s: 16.6,
        z: 0.0,
        p_value: 1.0,
        tau: 0.0,
        trend: "no trend",
      },
    });
    const body: TrendsResponse = {
      available: true,
      frozen_at: "2026-06-01T00:00:00+00:00",
      reference_sessions: 5,
      stats: { stationary_stat: quiet },
    };
    mocks.getTrends.mockResolvedValue(body);
    render(createElement(TrendsPanel, { token: "secret" }));

    expect(await screen.findByText("CUSUM ok")).toBeInTheDocument();
    expect(screen.getByText("trend: no trend")).toBeInTheDocument();
  });

  it("shows an error state when the fetch fails", async () => {
    mocks.getTrends.mockRejectedValue(new Error("boom"));
    render(createElement(TrendsPanel, { token: "secret" }));
    expect(await screen.findByText("boom")).toBeInTheDocument();
  });
});
