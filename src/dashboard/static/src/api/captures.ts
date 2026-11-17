// The capture routes, one function per route: the manual capture (`/control/captures`) and the capture profiles (`/control/capture`).

import type {
  CaptureProfile,
  CaptureProfileState,
  CaptureProfileWarning,
  CaptureProfilesResponse,
  CaptureQuality,
  CaptureResponse,
  CaptureTagResponse,
  CurrentCaptureResponse,
} from "../types";
import { controlJson, jsonBody } from "./core";

export function getCurrentCapture(
  token: string,
  signal?: AbortSignal,
): Promise<CurrentCaptureResponse> {
  return controlJson<CurrentCaptureResponse>("/control/captures/current", token, { signal });
}

export function startCapture(
  token: string,
  body: {
    purpose?: string | null;
    protocol?: string | null;
    vehicle_state?: string | null;
    fields?: Record<string, unknown>;
  },
): Promise<CaptureResponse> {
  return controlJson<CaptureResponse>("/control/captures", token, {
    method: "POST",
    body: jsonBody(body),
  });
}

export function tagCapture(
  token: string,
  body: {
    label: string;
    kind?: string;
    // Unix seconds of the moment the event happened. Sent client-side so a
    // slow/retried POST over flaky in-vehicle signal still lands the tag at
    // the right time (the server otherwise stamps it on receipt).
    timestamp?: number;
    fields?: Record<string, unknown>;
  },
): Promise<CaptureTagResponse> {
  return controlJson<CaptureTagResponse>("/control/captures/current/tags", token, {
    method: "POST",
    body: jsonBody(body),
  });
}

export function stopCapture(
  token: string,
  body: { reason?: string | null; fields?: Record<string, unknown> },
): Promise<CaptureResponse> {
  return controlJson<CaptureResponse>("/control/captures/current/stop", token, {
    method: "POST",
    body: jsonBody(body),
  });
}

export function getCaptureProfiles(
  token: string,
  signal?: AbortSignal,
): Promise<CaptureProfilesResponse> {
  return controlJson("/control/capture/profiles", token, { signal });
}

export function saveCaptureProfile(
  token: string,
  profile: CaptureProfile,
  previousName?: string,
): Promise<{ profile: CaptureProfile; warnings: CaptureProfileWarning[] }> {
  return controlJson("/control/capture/profiles", token, {
    method: "PUT",
    body: jsonBody({ profile, previous_name: previousName ?? null }),
  });
}

export function deleteCaptureProfile(token: string, name: string): Promise<{ deleted: string }> {
  return controlJson(`/control/capture/profiles/${encodeURIComponent(name)}`, token, {
    method: "DELETE",
  });
}

export function startProfileCapture(
  token: string,
  profile: string,
  tuneRevision?: string,
): Promise<{ capture_profile: CaptureProfileState }> {
  return controlJson("/control/capture", token, {
    method: "POST",
    body: jsonBody({ profile, tune_revision: tuneRevision || null }),
  });
}

export function pauseProfileCapture(
  token: string,
  paused: boolean,
): Promise<{ capture_profile: CaptureProfileState }> {
  return controlJson("/control/capture/pause", token, {
    method: "POST",
    body: JSON.stringify({ paused }),
  });
}

/** Stopping returns the contract verdict for the capture that just ended.
 *  `capture_quality` is null when no sidecar could be written — unknown, not a
 *  pass, and the caller must render it that way. */
export function stopProfileCapture(token: string): Promise<{
  capture_profile: CaptureProfileState;
  ended_capture_dir?: string;
  capture_quality?: CaptureQuality | null;
}> {
  return controlJson("/control/capture", token, { method: "DELETE" });
}
