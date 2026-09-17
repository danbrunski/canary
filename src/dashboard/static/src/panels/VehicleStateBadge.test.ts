import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { VehicleState } from "../types";
import { VehicleStateBadge } from "./VehicleStateBadge";

function state(overrides: Partial<VehicleState>): VehicleState {
  return {
    state: "engine_running",
    rpm: 1500,
    coolant_temp: 90,
    run_time: 120,
    obd_freshness_seconds: 0.5,
    can_freshness_seconds: 0.2,
    replay_id: null,
    derived_at: "2026-09-17T20:00:00+00:00",
    ...overrides,
  };
}

describe("VehicleStateBadge (Task 1.1)", () => {
  it.each([
    ["engine_running", "engine running", "text-ok"],
    ["ignition_only", "ignition only", "text-accent"],
    ["engine_off", "engine off", "text-fg-dim"],
    ["no_data", "no data", "text-bad"],
    ["replay", "replay", "text-accent-2"],
    ["unknown", "unknown", "text-bad"],
  ] as const)("renders %s distinctly", (raw, label, tone) => {
    render(
      createElement(VehicleStateBadge, {
        vehicleState: state({ state: raw }),
      }),
    );
    const value = screen.getByText(label);
    expect(value).toBeInTheDocument();
    expect(value.className).toContain(tone);
  });

  it("shows an alert placeholder when the field is absent", () => {
    render(createElement(VehicleStateBadge, { vehicleState: null }));
    expect(screen.getByText("—").className).toContain("text-bad");
  });
});
