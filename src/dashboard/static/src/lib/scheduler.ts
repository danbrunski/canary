// Reading the OBD scheduler's per-request status (R08.6). TelemetryPanel's
// freshness chips and the capture runner's scheduler table both show a
// "requested" rate off `/status.health.obd_scheduler.requests`, and both read
// it here, through `effectiveTargetHz`.
//
// For a windowed DID the requested rate is the window cadence it is actually
// polled at (operator decision 2026-09-25, R00). The daemon already reports
// exactly that as `configured_rate_hz`: since R02.3 one rate policy
// (`_request_rate_hz`) serves the live scheduler, the resolved plan and the
// capture preview, and for a DID in a Mode 22 window it is one read per
// window period (profile cycle, then config cycle, then profile interval,
// then config interval), scaled down by a fractional rate weight.
//
// The dashboard used to re-derive it as `1 / did_profile.window_interval_seconds`.
// That field is the end-to-start *interval* only, so under a cycle it named a
// cadence nothing polls at (a 60 s interval beside a 0.5 s cycle read as
// 0.017 Hz for a DID read at 2 Hz), and it ignored the weight. Re-deriving a
// backend policy here is how the two drifted; `scheduler.test.ts` pins this
// against a resolved plan the backend generates
// (`tests/test_dashboard_scheduler_fixture.py`).

import type { ObdSchedulerRequestStatus, ObdSchedulerStatus } from "../types";

/** The scheduler request serving a stream channel, matched case-insensitively
 *  on the request's channel name. */
export function matchingRequest(
  channel: string,
  scheduler?: ObdSchedulerStatus | null,
): ObdSchedulerRequestStatus | undefined {
  const wanted = channel.toUpperCase();
  return scheduler?.requests?.find((request) => request.channel.toUpperCase() === wanted);
}

/** The requested rate for a scheduler request: the rate the daemon resolved,
 *  which for a windowed DID is its window cadence (see the module note).
 *  Null when the request is unscheduled. */
export function effectiveTargetHz(request: ObdSchedulerRequestStatus | undefined): number | null {
  if (!request || !(request.configured_rate_hz > 0)) return null;
  return request.configured_rate_hz;
}
