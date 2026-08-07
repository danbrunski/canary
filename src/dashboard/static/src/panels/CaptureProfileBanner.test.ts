import { createElement } from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CaptureProfileState } from "../types";
import { CaptureProfileBanner } from "./CaptureProfileBanner";

const ACTIVE: CaptureProfileState = {
  active: true,
  name: "fuel-map",
  description: "High-rate fueling and timing capture",
  experimental: false,
  started_at: "2026-07-31T10:00:00+00:00",
  session_dir: "_capture-fuel-map-2026-07-31T100000",
  source: "control",
  channels: ["RPM", "ENGINE_LOAD"],
  mode01: { RPM: 1, ENGINE_LOAD: 1 },
  mode22: [],
  mode01_enabled: true,
  mode22_enabled: true,
  did_interval_seconds: null,
  round_timestamps: true,
  target_rate_hz: null,
  did_continuous: false,
  expected_total_demand_hz: null,
  capacity_evidence: "",
  paused: false,
  broadcast_decode: true,
  corpus_eligible: false,
  bus_budget_override: null,
};

describe("CaptureProfileBanner (capture-profiles.md 3.1)", () => {
  it("renders nothing when no capture block is present", () => {
    render(createElement(CaptureProfileBanner, { state: null }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders nothing under default collection", () => {
    render(
      createElement(CaptureProfileBanner, {
        state: { ...ACTIVE, active: false, name: "default" },
      }),
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("names the profile and its session while a capture runs", () => {
    // The invisible-replay lesson: a non-default collection mode must never
    // be indistinguishable from normal operation.
    render(createElement(CaptureProfileBanner, { state: ACTIVE }));
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("CAPTURE: fuel-map");
    expect(banner).toHaveTextContent("_capture-fuel-map-2026-07-31T100000");
  });

  it("still reports the capture when the session directory is unknown", () => {
    render(createElement(CaptureProfileBanner, { state: { ...ACTIVE, session_dir: null } }));
    expect(screen.getByRole("status")).toHaveTextContent("rotating storage");
  });

  it("keeps experimental cadence visible outside the capture panel", () => {
    render(
      createElement(CaptureProfileBanner, {
        state: {
          ...ACTIVE,
          name: "tuning-hybrid-research",
          experimental: true,
          target_rate_hz: 4,
        },
      }),
    );
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("EXPERIMENTAL");
    expect(banner).toHaveTextContent("4 Hz/channel");
  });
});
