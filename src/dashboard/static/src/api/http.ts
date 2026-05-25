// Thin fetch wrappers around the FastAPI REST endpoints (Task 5.3).
//
// One function per route — kept minimal so a panel that needs an endpoint
// imports the named function. `fetchJson` centralizes error handling so we
// never accidentally render an HTML 500 page as if it were valid JSON.

import type {
  AnomaliesResponse,
  BehaviorLiveResponse,
  BehaviorModelsResponse,
  BindingsByChannel,
  CanCoverageResponse,
  CaptureResponse,
  CaptureTagResponse,
  DatasetHealthResponse,
  DidMapResponse,
  FeedbackResponse,
  JobLogsResponse,
  JobResponse,
  JobsResponse,
  ObdCoverageResponse,
  PreflightResponse,
  SequenceDetectorStatus,
  SessionSummary,
  SessionsResponse,
  StatusResponse,
  TelemetryLatestResponse,
  TestProfilesResponse,
  TrainingRunsResponse,
  TuningConfig,
  TuningEstimate,
  TuningResponse,
  VersionResponse,
} from "../types";
import { controlJson, fetchJson, HttpError, jsonBody } from "./core";

export { HttpError } from "./core";

export function getStatus(signal?: AbortSignal): Promise<StatusResponse> {
  return fetchJson<StatusResponse>("/status", { signal });
}

export function getConfig(signal?: AbortSignal): Promise<Record<string, unknown>> {
  return fetchJson("/config", { signal });
}

export function getAnomalies(
  since?: string,
  signal?: AbortSignal,
): Promise<AnomaliesResponse> {
  const params = since ? `?since=${encodeURIComponent(since)}` : "";
  return fetchJson<AnomaliesResponse>(`/anomalies${params}`, { signal });
}

export function getLatestTelemetry(
  n = 100,
  signal?: AbortSignal,
): Promise<TelemetryLatestResponse> {
  return fetchJson<TelemetryLatestResponse>(`/telemetry/latest?n=${n}`, { signal });
}

/** Seed shape for the live panels: the most recent sample for *every*
 *  channel, not the last N rows globally (which the high-rate CAN bus
 *  dominates, starving the ~1 Hz OBD gauges). */
export function getLatestPerChannel(
  signal?: AbortSignal,
): Promise<TelemetryLatestResponse> {
  return fetchJson<TelemetryLatestResponse>(`/telemetry/latest?per_channel=true`, {
    signal,
  });
}

export function getBehaviorModels(
  signal?: AbortSignal,
): Promise<BehaviorModelsResponse> {
  return fetchJson<BehaviorModelsResponse>("/models/behavior", { signal });
}

/** Live "you are here" overlay feed. Polled fast (~2 s) and independently
 *  of the slow static-surface poll — the trail itself lives server-side, so
 *  a slow client poll still returns a smooth history. */
export function getBehaviorLive(
  signal?: AbortSignal,
): Promise<BehaviorLiveResponse> {
  return fetchJson<BehaviorLiveResponse>("/models/behavior/live", { signal });
}

export function getBindings(signal?: AbortSignal): Promise<BindingsByChannel> {
  return fetchJson<BindingsByChannel>("/bindings", { signal });
}

export function getPreflight(
  token: string,
  signal?: AbortSignal,
): Promise<PreflightResponse> {
  return controlJson<PreflightResponse>("/control/preflight", token, { signal });
}

export function getControlVersion(
  token: string,
  signal?: AbortSignal,
): Promise<VersionResponse> {
  return controlJson<VersionResponse>("/control/version", token, { signal });
}

export function getCurrentCapture(
  token: string,
  signal?: AbortSignal,
): Promise<CaptureResponse> {
  return controlJson<CaptureResponse>("/control/captures/current", token, { signal });
}

export function getSequenceDetector(
  token: string,
  signal?: AbortSignal,
): Promise<{ sequence_detector: SequenceDetectorStatus }> {
  return controlJson<{ sequence_detector: SequenceDetectorStatus }>(
    "/control/sequence-detector",
    token,
    { signal },
  );
}

export function switchSequenceDetector(
  token: string,
  backbone: "lstm" | "tcn",
): Promise<{ sequence_detector: SequenceDetectorStatus }> {
  return controlJson<{ sequence_detector: SequenceDetectorStatus }>(
    "/control/sequence-detector",
    token,
    { method: "POST", body: jsonBody({ backbone }) },
  );
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

export function listSessions(
  token: string,
  signal?: AbortSignal,
  baseUrl?: string,
): Promise<SessionsResponse> {
  return controlJson<SessionsResponse>("/control/sessions", token, { signal }, baseUrl);
}

export function updateSessionMetadata(
  token: string,
  sessionId: string,
  metadata: Record<string, unknown>,
  merge = true,
): Promise<{ session_id: string; metadata: Record<string, unknown> }> {
  return controlJson(`/control/sessions/${encodeURIComponent(sessionId)}/metadata`, token, {
    method: "POST",
    body: jsonBody({ metadata, merge }),
  });
}

export function pinSession(
  token: string,
  sessionId: string,
  reason?: string,
): Promise<{ session: unknown; metadata: Record<string, unknown> }> {
  return controlJson(`/control/sessions/${encodeURIComponent(sessionId)}/pin`, token, {
    method: "POST",
    body: jsonBody({ reason: reason || null, metadata: {} }),
  });
}

export function unpinSession(
  token: string,
  sessionId: string,
): Promise<{ session: unknown; metadata: Record<string, unknown> }> {
  return controlJson(`/control/sessions/${encodeURIComponent(sessionId)}/unpin`, token, {
    method: "POST",
    body: jsonBody({}),
  });
}

export function deleteSession(
  token: string,
  sessionId: string,
  force: boolean,
): Promise<{ session_id: string; deleted: boolean; force: boolean }> {
  const suffix = force ? "?force=true" : "";
  return controlJson(`/control/sessions/${encodeURIComponent(sessionId)}${suffix}`, token, {
    method: "DELETE",
  });
}

export async function downloadSession(token: string, sessionId: string): Promise<Blob> {
  const response = await fetch(`/control/sessions/${encodeURIComponent(sessionId)}/export`, {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/zip" },
  });
  if (!response.ok) {
    throw new HttpError(response.status, await response.text());
  }
  return response.blob();
}

export function listJobs(
  token: string,
  signal?: AbortSignal,
  baseUrl?: string,
): Promise<JobsResponse> {
  return controlJson<JobsResponse>("/control/jobs", token, { signal }, baseUrl);
}

export function submitJob(
  token: string,
  kind: string,
  params: Record<string, unknown>,
  baseUrl?: string,
): Promise<JobResponse> {
  return controlJson<JobResponse>("/control/jobs", token, {
    method: "POST",
    body: jsonBody({ kind, params }),
  }, baseUrl);
}

export function cancelJob(token: string, jobId: string): Promise<JobResponse> {
  return controlJson<JobResponse>(`/control/jobs/${encodeURIComponent(jobId)}/cancel`, token, {
    method: "POST",
    body: jsonBody({}),
  });
}

export function getJobLogs(token: string, jobId: string): Promise<JobLogsResponse> {
  return controlJson<JobLogsResponse>(`/control/jobs/${encodeURIComponent(jobId)}/logs`, token);
}

export function uploadModelArtifact(
  token: string,
  modelType: string,
  artifactB64: string,
  manifest: Record<string, unknown>,
): Promise<{ model: Record<string, unknown> }> {
  return controlJson(`/control/models/${encodeURIComponent(modelType)}/artifact`, token, {
    method: "POST",
    body: jsonBody({ artifact_b64: artifactB64, manifest }),
  });
}

export function syncSessionToCompanion(
  companionBaseUrl: string,
  companionToken: string,
  sessionId: string,
  body: {
    pi_host?: string | null;
    token?: string | null;
    pin?: boolean;
    pin_reason?: string | null;
    index_with_config?: boolean;
  },
): Promise<{ session: SessionSummary }> {
  return controlJson(
    `/companion/sync/sessions/${encodeURIComponent(sessionId)}`,
    companionToken,
    {
      method: "POST",
      body: jsonBody(body),
    },
    companionBaseUrl,
  );
}

// --- Phase-6 panels --------------------------------------------------------

/** `/obd/coverage` is read-only + unauthenticated (Task 6.3). */
export function getObdCoverage(
  signal?: AbortSignal,
): Promise<ObdCoverageResponse> {
  return fetchJson("/obd/coverage", { signal });
}

/** `/can/coverage` — static catalog of decodable broadcast-CAN signals. */
export function getCanCoverage(
  signal?: AbortSignal,
): Promise<CanCoverageResponse> {
  return fetchJson("/can/coverage", { signal });
}

/** `/dids` is read-only + unauthenticated, like `/bindings` (Task 6.16). */
export function getDids(signal?: AbortSignal): Promise<DidMapResponse> {
  return fetchJson("/dids", { signal });
}

/** `/test-profiles` is read-only + unauthenticated (scanned YAML catalog);
 *  the picker must render before the control token is entered. */
export function getTestProfiles(
  signal?: AbortSignal,
): Promise<TestProfilesResponse> {
  return fetchJson("/test-profiles", { signal });
}

export function getDatasetHealth(
  token: string,
  signal?: AbortSignal,
): Promise<DatasetHealthResponse> {
  return controlJson("/control/datasets/health", token, { signal });
}

export function getTrainingRuns(
  token: string,
  signal?: AbortSignal,
  baseUrl?: string,
): Promise<TrainingRunsResponse> {
  return controlJson("/control/training-runs", token, { signal }, baseUrl);
}

export function getTuning(
  token: string,
  signal?: AbortSignal,
): Promise<TuningResponse> {
  return controlJson("/control/tuning", token, { signal });
}

export function updateTuning(
  token: string,
  config: TuningConfig,
  change: string,
): Promise<TuningResponse> {
  return controlJson("/control/tuning", token, {
    method: "POST",
    body: jsonBody({ config, change }),
  });
}

export function estimateTuning(
  token: string,
  config: TuningConfig,
): Promise<TuningEstimate> {
  return controlJson("/control/tuning/estimate", token, {
    method: "POST",
    body: jsonBody({ config }),
  });
}

export function revertTuning(
  token: string,
  version: number,
): Promise<TuningResponse> {
  return controlJson("/control/tuning/revert", token, {
    method: "POST",
    body: jsonBody({ version }),
  });
}

export function getFeedback(
  token: string,
  signal?: AbortSignal,
): Promise<FeedbackResponse> {
  return controlJson("/control/feedback", token, { signal });
}

export function postFeedback(
  token: string,
  event: Record<string, unknown>,
  label: string,
  reason: string | null,
): Promise<{ feedback: Record<string, unknown> }> {
  return controlJson("/control/feedback", token, {
    method: "POST",
    body: jsonBody({ event, label, reason }),
  });
}
