// Reading the OBD scheduler's per-request status (R08.6). TelemetryPanel's
// freshness chips and the capture runner's scheduler table both read a
// "requested" rate off `/status.health.obd_scheduler.requests`, and they read
// it differently for DIDs:
//
// - `effectiveTargetHz` (TelemetryPanel): a DID is only polled inside the
//   Mode 22 window, so its target is `1 / window_interval_seconds` when the
//   daemon reports a window interval, else `configured_rate_hz`.
// - `requestedRateHz` (capture runner table): `configured_rate_hz` as the
//   scheduler holds it, for every kind.
//
// Which of the two is *the* requested rate is an open operator decision
// (R08.6, after R02.3 settles the backend preview/live precedence). Both live
// here, side by side, so unifying them is a one-line change once it is made.

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

/** TelemetryPanel's target rate for a request: the DID window cadence for a
 *  windowed DID, else the configured rate. Null when unscheduled. */
export function effectiveTargetHz(
  request: ObdSchedulerRequestStatus | undefined,
  scheduler?: ObdSchedulerStatus | null,
): number | null {
  if (!request || request.configured_rate_hz <= 0) return null;
  if (request.kind === "did") {
    const interval = scheduler?.did_profile?.window_interval_seconds;
    if (interval != null && interval > 0) return 1 / interval;
  }
  return request.configured_rate_hz;
}

/** The capture runner's "requested" column: the configured rate as held by
 *  the scheduler, for every kind (see the module note). */
export function requestedRateHz(request: ObdSchedulerRequestStatus): number {
  return request.configured_rate_hz;
}
