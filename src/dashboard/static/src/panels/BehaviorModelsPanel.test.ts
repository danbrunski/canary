import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { BehaviorLiveResponse, BehaviorModelsResponse, BehaviorSeverity } from "../types";
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
      conditions: [
        {
          kind: "max_abs_rate_of_change",
          channel: "did:ECM_RPM__roc",
          max_abs_per_second: 75,
        },
      ],
      fit: {
        ...FIT_BASE,
        tune_revision: "ZS44A-k106",
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
    expect(screen.getByText("tune ZS44A-k106")).toBeInTheDocument();
    expect(screen.getByText(/did:ECM_RPM__roc ≤ ±75\/s/)).toBeInTheDocument();
  });

  it("renders without the live overlay", () => {
    render(createElement(BehaviorModelsPanel, { data: MODELS, error: null, live: null }));
    expect(screen.getByText("warm_idle_rpm_baseline")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// B03.8 — status surfaces: empty, demoted, failed, off-map, eligible.
// ---------------------------------------------------------------------------

const ARTIFACT = {
  version: 2,
  fingerprint: "sha256:1a2394e4ca01ebdcc81fb74c752d35a590ed",
  fitted_at: "2026-07-27T06:09:06.923608+00:00",
  training_data_hash: "sha256:6db79cbccb866817",
  tune_revision: null,
  schema_contract: true,
  grid_version: null,
};

function statusModel(
  name: string,
  serving_status: Record<string, unknown>,
  validation_scope: Record<string, unknown>,
) {
  return {
    name,
    kind: "surface_1d",
    output: "obd:TIMING_ADVANCE",
    output_unit: "deg",
    axis: { name: "rpm", channel: "obd:RPM", unit: "rpm", domain: [400, 7000] },
    operating_mode: null,
    version: 2,
    fit: { ...FIT_BASE, kind: "poly", coefficients: [10] },
    deviation: { kind: "absolute", warning_band: 5, critical_band: 15, reference_std: null },
    notes: null,
    serving_status,
    artifact: { ...ARTIFACT, name },
    validation_scope,
  };
}

const STATUS_MODELS = {
  schema_version: 2,
  models: [
    statusModel(
      "eligible_model",
      {
        state: "active",
        reasons: [],
        lifecycle: "validated",
        permitted_uses: ["alert", "display", "learned_feature"],
        policy: null,
      },
      {
        verdict: "passed",
        tune_revision: "ZS44A-20260920",
        operating_mode: null,
        test_drives: ["d1", "d2"],
      },
    ),
    statusModel(
      "timing_advance_vs_rpm_iat",
      {
        state: "diagnostic_only",
        reasons: ["policy:demoted", "legacy_artifact:no_serving_eligibility_contract"],
        lifecycle: "demoted",
        permitted_uses: ["display"],
        policy: {
          model: "timing_advance_vs_rpm_iat",
          lifecycle: "demoted",
          permitted_uses: ["display"],
          reason: "RPM/IAT alone omits load",
          decided: "2026-09-21",
          evidence: "review",
        },
      },
      { verdict: "none", tune_revision: null, operating_mode: null, held_out_rows: 0 },
    ),
    statusModel(
      "failed_model",
      {
        state: "diagnostic_only",
        reasons: ["validation_failed:skill_vs_baseline:0.02<0.3"],
        lifecycle: "unvalidated",
        permitted_uses: ["display", "learned_feature"],
        policy: null,
      },
      {
        verdict: "failed",
        failures: ["skill_vs_baseline:0.02<0.3"],
        tune_revision: "ZS44A-20260920",
        operating_mode: null,
      },
    ),
  ],
} as unknown as BehaviorModelsResponse;

function liveTick(overrides: Record<string, unknown>) {
  return {
    kind: "surface_1d",
    x: 1500,
    y: null,
    actual: 12,
    expected: 10,
    deviation: 2,
    severity: "ok",
    mode_match: true,
    series: [],
    reason: null,
    lifecycle: "validated",
    permitted_uses: ["alert", "display", "learned_feature"],
    abstain_reason: null,
    ...overrides,
  };
}

describe("BehaviorModelsPanel status (B03.8)", () => {
  it("says so when no surfaces are fitted", () => {
    render(
      createElement(BehaviorModelsPanel, {
        data: { schema_version: null, models: [] },
        error: null,
        live: null,
      }),
    );
    expect(screen.getByText(/no surfaces fitted yet/)).toBeInTheDocument();
  });

  it("renders eligible, demoted and failed models with identity and scope", () => {
    const live = {
      observed: 3,
      timestamp: "2026-09-21T12:00:00Z",
      operating_mode: "city",
      models: {
        eligible_model: liveTick({}),
        timing_advance_vs_rpm_iat: liveTick({
          lifecycle: "demoted",
          permitted_uses: ["display"],
          abstain_reason: "not_alerting:demoted",
          severity: "critical",
        }),
        failed_model: liveTick({
          expected: null,
          deviation: null,
          actual: 12,
          reason: "off_map",
          lifecycle: "unvalidated",
          abstain_reason: "off_map",
        }),
      },
    } as unknown as BehaviorLiveResponse;
    render(createElement(BehaviorModelsPanel, { data: STATUS_MODELS, error: null, live }));

    const eligible = screen.getByTestId("behavior-status-eligible_model");
    expect(eligible).toHaveTextContent("validated");
    expect(eligible).toHaveTextContent(/^validated\s*alerting/);
    expect(eligible).toHaveTextContent("uses: alert, display, learned_feature");
    expect(eligible).toHaveTextContent("artifact v2");
    expect(eligible).toHaveTextContent("tune unknown");
    expect(eligible).toHaveTextContent("passed · 2 test drives · on ZS44A-20260920");

    const demoted = screen.getByTestId("behavior-status-timing_advance_vs_rpm_iat");
    expect(demoted).toHaveTextContent("demoted");
    expect(demoted).toHaveTextContent("not alerting");
    expect(demoted).toHaveTextContent("demoted: RPM/IAT alone omits load");
    expect(demoted).toHaveTextContent("validation: none recorded");
    expect(screen.getByText(/abstaining — not_alerting:demoted/)).toBeInTheDocument();

    const failed = screen.getByTestId("behavior-status-failed_model");
    expect(failed).toHaveTextContent("unvalidated");
    expect(failed).toHaveTextContent(
      "why not alerting: validation_failed:skill_vs_baseline:0.02<0.3",
    );
    expect(failed).toHaveTextContent("failed");
    // Off-map is never a clean ok reading.
    expect(screen.getByText(/off-map — operating point outside/)).toBeInTheDocument();
  });

  it("names an ineligible tick instead of calling it off-map", () => {
    const live = {
      observed: 1,
      timestamp: null,
      operating_mode: "city",
      models: {
        eligible_model: liveTick({
          expected: null,
          deviation: null,
          reason: "rate_exceeded:obd:RPM",
          abstain_reason: "rate_exceeded:obd:RPM",
        }),
      },
    } as unknown as BehaviorLiveResponse;
    render(createElement(BehaviorModelsPanel, { data: STATUS_MODELS, error: null, live }));
    expect(screen.getByText(/no prediction — rate_exceeded:obd:RPM/)).toBeInTheDocument();
  });
});
