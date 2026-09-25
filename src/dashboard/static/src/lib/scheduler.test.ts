import { describe, expect, it } from "vitest";

import type { ObdSchedulerRequestStatus, ObdSchedulerStatus } from "../types";
import { effectiveTargetHz, matchingRequest, requestedRateHz } from "./scheduler";

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

describe("scheduler rates (R08.6)", () => {
  it("matches a stream channel case-insensitively", () => {
    expect(matchingRequest("rpm", SCHEDULER)).toBe(RPM);
    expect(matchingRequest("SPEED", SCHEDULER)).toBeUndefined();
    expect(matchingRequest("RPM", null)).toBeUndefined();
  });

  it("targets a windowed DID at the window cadence, a PID at its configured rate", () => {
    expect(effectiveTargetHz(AFR, SCHEDULER)).toBe(4);
    expect(effectiveTargetHz(RPM, SCHEDULER)).toBe(10);
    expect(effectiveTargetHz(AFR, { requests: [] })).toBe(2);
    expect(effectiveTargetHz(request("pid", "X", 0), SCHEDULER)).toBeNull();
    expect(effectiveTargetHz(undefined, SCHEDULER)).toBeNull();
  });

  it("the runner's requested rate is the configured rate, DID or not (decision open)", () => {
    expect(requestedRateHz(AFR)).toBe(2);
    expect(requestedRateHz(RPM)).toBe(10);
  });
});
