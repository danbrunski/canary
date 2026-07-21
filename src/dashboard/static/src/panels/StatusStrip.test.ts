import { createElement } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AcceptedBaselineOverrideSummary, StatusResponse } from "../types";
import { StatusStrip } from "./StatusStrip";

function status(
  overrides: StatusResponse["accepted_baseline_overrides"],
): StatusResponse {
  return {
    storage_path: "/tmp/canary",
    free_mb: 2048,
    latest_session: "s1",
    latest_session_path: "/tmp/canary/s1",
    model_versions: {},
    vehicle: "2008 Nissan Xterra",
    emitter_counters: null,
    health: null,
    accepted_baseline_overrides: overrides,
  };
}

const OVERRIDE: AcceptedBaselineOverrideSummary = {
  target: "obd:SHORT_FUEL_TRIM_1",
  channel: "obd:SHORT_FUEL_TRIM_1",
  behavior_model: null,
  operating_mode: "idle",
  accepted_center: 12.0,
  band: {
    kind: "directional",
    tolerance: 3.0,
    worse_direction: "positive",
    worse_warning: 3.0,
    worse_critical: 6.0,
    better_direction: "ignore",
  },
  declared_by: "dan",
  declared_at: "2026-05-17",
  review_by: "2026-11-17",
  reason: "known weak #3 injector; owner not replacing",
  expired: false,
};

describe("StatusStrip — accepted-baseline overrides (2.3)", () => {
  it("is not shown when the daemon hasn't wired the field", () => {
    render(
      createElement(StatusStrip, {
        status: null,
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.queryByText("overrides")).not.toBeInTheDocument();
  });

  it("always shows the active count, even with zero overrides", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 0, overrides: [] }),
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.getByText("overrides")).toBeInTheDocument();
    expect(screen.getByText("0 active")).toBeInTheDocument();
  });

  it("expands to per-override provenance on click", () => {
    render(
      createElement(StatusStrip, {
        status: status({ active: 1, overrides: [OVERRIDE] }),
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.queryByText(/known weak #3 injector/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("1 active"));
    expect(screen.getByText(/known weak #3 injector/)).toBeInTheDocument();
    expect(screen.getByText(/review by 2026-11-17/)).toBeInTheDocument();
  });

  it("flags an expired override instead of hiding it", () => {
    render(
      createElement(StatusStrip, {
        status: status({
          active: 0,
          overrides: [{ ...OVERRIDE, expired: true }],
        }),
        channels: new Map(),
        events: [],
      }),
    );
    expect(screen.getByText(/expired/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("0 active"));
    expect(screen.getByText("needs re-confirmation")).toBeInTheDocument();
  });
});
