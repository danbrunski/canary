import { describe, expect, it } from "vitest";

import { WINDOWED_DID_SCHEDULER } from "../test/captureFixtures";
import type { ObdSchedulerRequestStatus, ObdSchedulerStatus } from "../types";
import { effectiveTargetHz, matchingRequest } from "./scheduler";

function request(kind: string, channel: string, hz: number): ObdSchedulerRequestStatus {
  return {
    kind,
    channel,
    configured_rate_hz: hz,
    backed_off: false,
    last_response_age_seconds: null,
  };
}

const RPM = request("pid", "RPM", 10);
const AFR = request("did", "ECM_AF_SENSOR_V_B1", 2);
const SCHEDULER = {
  requests: [RPM, AFR],
  did_profile: { window_interval_seconds: 0.25 },
} as unknown as ObdSchedulerStatus;

const FIXTURE = WINDOWED_DID_SCHEDULER;

describe("scheduler rates (R08.6)", () => {
  it("matches a stream channel case-insensitively", () => {
    expect(matchingRequest("rpm", SCHEDULER)).toBe(RPM);
    expect(matchingRequest("SPEED", SCHEDULER)).toBeUndefined();
    expect(matchingRequest("RPM", null)).toBeUndefined();
  });

  it("the requested rate is the rate the daemon resolved; null when unscheduled", () => {
    expect(effectiveTargetHz(RPM)).toBe(10);
    expect(effectiveTargetHz(AFR)).toBe(2);
    expect(effectiveTargetHz(request("pid", "X", 0))).toBeNull();
    expect(effectiveTargetHz(undefined)).toBeNull();
  });

  it.each(Object.entries(FIXTURE))(
    "%s: a windowed DID's requested rate is its window cadence, as the resolved plan says",
    (_name, scenario) => {
      const did = scenario.requests.find((row) => row.kind === "did");
      expect(did).toBeDefined();
      const member = scenario.resolved_plan.members.find((m) => m.channel === did?.channel);
      expect(member).toBeDefined();
      const requested = effectiveTargetHz(
        matchingRequest(did?.channel ?? "", { requests: scenario.requests }),
      );
      // Agrees with the backend's resolved plan (rows round to 4 places) ...
      expect(requested).toBeCloseTo(member?.rate_hz ?? NaN, 4);
      // ... is never faster than one read per window ...
      expect(requested).toBeLessThanOrEqual(1 / scenario.window_period_seconds + 1e-4);
      // ... and is not the reported interval's reciprocal, which the dashboard
      // used to show: the interval does not govern when a cycle is set.
      const interval = scenario.did_profile.window_interval_seconds;
      expect(Math.abs((requested ?? 0) - 1 / interval)).toBeGreaterThan(0.01);
    },
  );

  it("the fixture covers a weighted DID and an every-window DID", () => {
    const periods = Object.values(FIXTURE).map((scenario) => {
      const did = scenario.requests.find((row) => row.kind === "did");
      return (did?.configured_rate_hz ?? 0) * scenario.window_period_seconds;
    });
    expect(periods.some((reads) => Math.abs(reads - 1) < 1e-3)).toBe(true);
    expect(periods.some((reads) => reads < 0.99)).toBe(true);
  });
});
