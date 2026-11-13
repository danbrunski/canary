// How the two `/status` views become the one `StatusResponse` the panels read
// (R29.9).
//
// The full payload is built on the Pi at 1.3-2.6 s, so the dashboard polls the
// compact projection every few seconds and the full one on a slow cadence. The
// panels keep reading one object: the last full payload with the compact
// fields laid over it. Only fields the compact view *carries* are overlaid, so
// an overlay can never invent a field, and a field a poll does not refresh is
// the full payload's, at most one full-poll interval old.
//
// What each view owns:
//   compact (refreshed every poll): vehicle_state, free_mb, emitter_counters,
//     passive, capture_profile.{name, active}, the health counters
//     (system, telemetry_sinks, can0, obd_scheduler counters),
//     detectors.inference.
//   full only (refreshed on the slow cadence, faster while a panel that shows
//     it is mounted, and at once when `compactChangeKey` moves): everything
//     else, among them latest_session, model_versions, model_provenance,
//     profile, gps, imu, trip, replay, last_capture, the calibration fields,
//     tuning_readiness, accepted_baseline_overrides, network, health.power,
//     health.inference_latency_ms, health.obd_scheduler.requests and
//     capture_profile.rows_written.

import type { CompactStatusResponse, StatusResponse } from "../types";

/** The top-level keys of `CompactStatusResponse` that carry state. `view` and
 *  `served_at` describe the response, not the daemon. */
const OVERLAY_KEYS = [
  "free_mb",
  "vehicle_state",
  "emitter_counters",
  "passive",
  "capture_profile",
  "health",
  "detectors",
] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The blocks the compact view *narrows*: it carries some of their keys and the
 *  full payload carries the rest, so they merge key by key. Every other block
 *  compact carries, it carries **whole** (the daemon builds both views from one
 *  object and the compact one copies the block verbatim: `vehicle_state`,
 *  `emitter_counters`, `passive`, `health.system`, `health.can0`, and each
 *  scheduler and inference field that is itself a map, such as
 *  `profile_start_withheld`, `receive` and `missed_by_reason`), so it replaces
 *  the full payload's copy. A merge there would leave behind a key the daemon
 *  has dropped since the last full poll: `passive.ecm_session_claims`
 *  `{foreign: 5}` over a compact `{}` must read `{}`, not `{foreign: 5}`. The
 *  paths are the ones `compact_status` builds (`src/dashboard/status.py`). */
const NARROWED = new Set([
  "health",
  "health.obd_scheduler",
  "health.telemetry_sinks.*",
  "detectors",
  "detectors.inference",
  "capture_profile",
]);

/** Blocks keyed by something the daemon adds and removes (the telemetry sinks):
 *  compact carries every key, so a key missing from it is gone, and each member
 *  is narrowed (compact carries three of its fields). */
const KEYED = new Set(["health.telemetry_sinks"]);

/** `over` laid on `base`, at `path` in the status object. A block in
 *  `NARROWED` merges key by key and a `KEYED` one by member; any other value in
 *  `over` replaces, **including a `null` over a scalar**: a reading that went
 *  from 850 to null (the engine stopped answering) is no longer 850. A `null`
 *  over a record, an array or a missing field replaces nothing: that is the
 *  compact view reporting a whole block it does not carry as `null` ("not
 *  instrumented"), which must not erase the full payload's richer copy of it.
 *  An absent (`undefined`) value replaces nothing. */
function overlay(base: unknown, over: unknown, path: string): unknown {
  if (over === undefined) return base;
  if (over === null) {
    return base === undefined || isRecord(base) || Array.isArray(base) ? base : null;
  }
  if (!isRecord(over)) return over;
  const prior = isRecord(base) ? base : {};
  if (KEYED.has(path)) {
    return Object.fromEntries(
      Object.entries(over).map(([key, value]) => [key, overlay(prior[key], value, `${path}.*`)]),
    );
  }
  if (!NARROWED.has(path) || !isRecord(base)) return over;
  const merged: Record<string, unknown> = { ...prior };
  for (const [key, value] of Object.entries(over)) {
    merged[key] = overlay(prior[key], value, `${path}.${key}`);
  }
  return merged;
}

/** The last full payload with the compact view's fresher fields laid over it.
 *
 *  `capture_profile` is the one block whose *identity* can change under the
 *  overlay: compact carries only `{name, active}`, and laid over the previous
 *  profile's full state it would show the new name beside the old profile's
 *  `channels`, `session_dir` and `target_rate_hz` (a start or a switch), or an
 *  ended capture beside its `started_at` and `rows_written`. When the name or
 *  the active flag differs from the full payload's, the plan fields are dropped
 *  and the panels read `{name, active}` alone until the full payload that
 *  `compactChangeKey` asks for lands. */
export function overlayCompact(
  full: StatusResponse,
  compact: CompactStatusResponse,
): StatusResponse {
  const merged: Record<string, unknown> = { ...full };
  for (const key of OVERLAY_KEYS) merged[key] = overlay(merged[key], compact[key], key);
  const was: unknown = full.capture_profile;
  const now: unknown = compact.capture_profile;
  if (isRecord(was) && isRecord(now) && (was.name !== now.name || was.active !== now.active)) {
    merged.capture_profile = { ...now };
  }
  return merged as unknown as StatusResponse;
}

/** What the daemon is doing, in a few words. A change in it means the full
 *  payload has changed too (a capture starting rewrites `capture_profile`, and
 *  its end `last_capture`; a replay starting is `replay`; the vehicle state is
 *  one of the `tuning_readiness` chips), so `useStatus` fetches the full block
 *  at once rather than waiting out its interval. */
export function compactChangeKey(compact: CompactStatusResponse): string {
  const { vehicle_state: vehicle, capture_profile: capture, passive } = compact;
  return [
    vehicle?.state,
    vehicle?.replay_id,
    capture?.active,
    capture?.name,
    passive?.enabled,
    passive?.ecm_session_owner,
  ].join("|");
}
