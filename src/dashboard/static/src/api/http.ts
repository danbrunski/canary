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
  BinnedAggregation,
  BinnedAnalysisResponse,
  CanCoverageResponse,
  CaptureResponse,
  CaptureProfile,
  CaptureProfilesResponse,
  CaptureProfileState,
  CaptureProfileWarning,
  CaptureTagResponse,
  CompanionStatusResponse,
  CustomPid,
  CustomPidImportResponse,
  CustomPidsResponse,
  DatasetHealthResponse,
  DidMapResponse,
  DtcReportResponse,
  FeedbackResponse,
  JobLogsResponse,
  JobResponse,
  JobResultResponse,
  JobsResponse,
  LlmAssistCardsResponse,
  MonitorsResponse,
  ObdCoverageResponse,
  PowerKeepaliveResponse,
  PowerStatusResponse,
  PreflightResponse,
  ReplayStatusResponse,
  SequenceDetectorStatus,
  SessionChartResponse,
  SessionDigestResponse,
  SessionSummary,
  SessionsResponse,
  StatusResponse,
  TelemetryLatestResponse,
  TestProfilesResponse,
  TrainingRun,
  TrainingRunsResponse,
  TrendsResponse,
  TripSummaryResponse,
  TuningConfig,
  TuningEstimate,
  TuningResponse,
  VehicleProfile,
  VehicleProfilesResponse,
  VersionResponse,
} from "../types";
import {
  apiUrl,
  controlJson,
  fetchJson,
  fetchOrNetworkError,
  HttpError,
  jsonBody,
} from "./core";

export { HttpError, NetworkError } from "./core";

export function getStatus(signal?: AbortSignal): Promise<StatusResponse> {
  return fetchJson<StatusResponse>("/status", { signal });
}

/** Task 6.35 — only the companion app answers this (same-origin, no
 *  `baseUrl`/token): its reachability is the "am I running as the local
 *  library, not the live Pi dashboard" signal `useConnectivityMode` reads. */
export function getCompanionStatus(
  signal?: AbortSignal,
): Promise<CompanionStatusResponse> {
  return fetchJson<CompanionStatusResponse>("/companion/status", { signal });
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

/** `GET /control/replay` (Task 2.1) — current runtime-replay state; also
 *  mirrored at `/status.replay` for the unauthenticated banner (Task 1.2),
 *  but `ReplayCard` uses this authenticated route so its own poll doesn't
 *  depend on the public status payload staying in sync. */
export function getReplay(
  token: string,
  signal?: AbortSignal,
): Promise<ReplayStatusResponse> {
  return controlJson<ReplayStatusResponse>("/control/replay", token, { signal });
}

/** `POST /control/replay` — refused with 409 while `vehicle_state ==
 *  "engine_running"` (7.2, operator-approved: replay is a bench/desk tool).
 *  The 409 `detail` is rendered verbatim by the caller — no client-side
 *  second-guessing of the daemon's refusal text. */
export function startReplay(
  token: string,
  body: { session_id: string; speed?: number; loop?: boolean },
): Promise<ReplayStatusResponse> {
  return controlJson<ReplayStatusResponse>("/control/replay", token, {
    method: "POST",
    body: jsonBody(body),
  });
}

export function stopReplay(token: string): Promise<ReplayStatusResponse> {
  return controlJson<ReplayStatusResponse>("/control/replay/stop", token, {
    method: "POST",
    body: jsonBody({}),
  });
}

export function listSessions(
  token: string,
  signal?: AbortSignal,
  baseUrl?: string,
  captures = false,
): Promise<SessionsResponse> {
  const suffix = captures ? "?captures=true" : "";
  return controlJson<SessionsResponse>(`/control/sessions${suffix}`, token, { signal }, baseUrl);
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

export function stopProfileCapture(token: string): Promise<{ capture_profile: CaptureProfileState }> {
  return controlJson("/control/capture", token, { method: "DELETE" });
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
  const response = await fetchOrNetworkError(
    `/control/sessions/${encodeURIComponent(sessionId)}/export`,
    { headers: { Authorization: `Bearer ${token}`, Accept: "application/zip" } },
  );
  if (!response.ok) {
    throw new HttpError(response.status, await response.text());
  }
  return response.blob();
}

/** Task 5.6a: GPX/KML export of a session's captured GPS track. Uses a raw
 *  `fetch` (not `controlJson`) like `downloadSession` above, since the
 *  response body is the file itself, not JSON. */
async function downloadSessionTrack(
  token: string,
  sessionId: string,
  format: "gpx" | "kml",
  baseUrl?: string,
): Promise<Blob> {
  const response = await fetchOrNetworkError(
    apiUrl(`/control/sessions/${encodeURIComponent(sessionId)}/gps.${format}`, baseUrl),
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok) {
    throw new HttpError(response.status, await response.text());
  }
  return response.blob();
}

export function downloadSessionGpx(
  token: string,
  sessionId: string,
  baseUrl?: string,
): Promise<Blob> {
  return downloadSessionTrack(token, sessionId, "gpx", baseUrl);
}

export function downloadSessionKml(
  token: string,
  sessionId: string,
  baseUrl?: string,
): Promise<Blob> {
  return downloadSessionTrack(token, sessionId, "kml", baseUrl);
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

export function getJob(token: string, jobId: string): Promise<JobResponse> {
  return controlJson<JobResponse>(`/control/jobs/${encodeURIComponent(jobId)}`, token);
}

export function getJobLogs(token: string, jobId: string): Promise<JobLogsResponse> {
  return controlJson<JobLogsResponse>(`/control/jobs/${encodeURIComponent(jobId)}/logs`, token);
}

/** `GET /control/jobs/{id}/result` (Task 4.1) — the structured report a
 *  `replay_window` job writes on success; `result` reads `null` until the
 *  job's report file lands (still running/queued). 404s for any other job
 *  kind, same as `describeError` already renders. */
export function getJobResult(token: string, jobId: string): Promise<JobResultResponse> {
  return controlJson<JobResultResponse>(`/control/jobs/${encodeURIComponent(jobId)}/result`, token);
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

/** `/obd/dtc` is read-only + unauthenticated, like `/obd/coverage` (Task
 *  1.5) — the cached stored/pending/permanent DTC report. No clear-codes
 *  endpoint exists; this API surface is read-only by design. */
export function getDtc(signal?: AbortSignal): Promise<DtcReportResponse> {
  return fetchJson("/obd/dtc", { signal });
}

/** `/obd/monitors` (Task 3.1) — cached Mode 06 on-board monitor sweep:
 *  per-cylinder misfire counts + raw monitor test results. Read-only, like
 *  `/obd/dtc` above. */
export function getMonitors(signal?: AbortSignal): Promise<MonitorsResponse> {
  return fetchJson("/obd/monitors", { signal });
}

/** `/analysis/llm-assist/cards` (Task S.2) — public/read-only like
 *  `/obd/monitors` above. `enabled: false` (the default; no producer
 *  exists yet) is the expected response, not an error. */
export function getLlmAssistCards(signal?: AbortSignal): Promise<LlmAssistCardsResponse> {
  return fetchJson("/analysis/llm-assist/cards", { signal });
}

/** `/sessions/{id}/trip-summary` is read-only + unauthenticated, like
 *  `/obd/dtc` (Task 1.5) — computed on demand from the stored session. */
export function getTripSummary(
  sessionId: string,
  signal?: AbortSignal,
): Promise<TripSummaryResponse> {
  return fetchJson(`/sessions/${encodeURIComponent(sessionId)}/trip-summary`, { signal });
}

/** `/control/sessions/{id}/digest` is authenticated (auth-consistency
 *  follow-up to Task 5.6) — it aggregates the vehicle ledger and
 *  per-session fingerprint z-scores, both otherwise only readable via
 *  authenticated `/control/*` routes (see getTrends below). Computed on
 *  demand if the trip-end hook hasn't already written
 *  `digest.json`. */
export function getSessionDigest(
  token: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionDigestResponse> {
  return controlJson(`/control/sessions/${encodeURIComponent(sessionId)}/digest`, token, {
    signal,
  });
}

/** `/analysis/binned` (Task 5.1) is read-only + unauthenticated, same
 *  posture as `/sessions/{id}/trip-summary` — no secrets, just historical
 *  telemetry binned on demand. `y`/`yBins` omitted -> a 1D result. */
export function getBinnedAnalysis(
  params: {
    value: string;
    x: string;
    y?: string;
    xBins?: number;
    yBins?: number;
    agg?: BinnedAggregation;
    session?: string;
    since?: string;
    until?: string;
  },
  signal?: AbortSignal,
): Promise<BinnedAnalysisResponse> {
  const query = new URLSearchParams({ value: params.value, x: params.x });
  if (params.y) query.set("y", params.y);
  if (params.xBins != null) query.set("x_bins", String(params.xBins));
  if (params.yBins != null) query.set("y_bins", String(params.yBins));
  if (params.agg) query.set("agg", params.agg);
  if (params.session) query.set("session", params.session);
  if (params.since) query.set("since", params.since);
  if (params.until) query.set("until", params.until);
  return fetchJson<BinnedAnalysisResponse>(`/analysis/binned?${query.toString()}`, { signal });
}

/** `/control/sessions/{id}/chart` (Task 5.3) — per-channel time-series
 *  arrays for a stored session, authenticated like the other
 *  `/control/sessions/{id}/...` routes; `baseUrl` lets the companion app's
 *  local-library backend serve the identical shape (its own docstring's
 *  stale "chart" promise, `src/companion/library.py`). */
export function getSessionChart(
  token: string,
  sessionId: string,
  channels: string[],
  resampleHz?: number,
  baseUrl?: string,
  signal?: AbortSignal,
): Promise<SessionChartResponse> {
  const query = new URLSearchParams({ channels: channels.join(",") });
  if (resampleHz != null) query.set("resample_hz", String(resampleHz));
  return controlJson<SessionChartResponse>(
    `/control/sessions/${encodeURIComponent(sessionId)}/chart?${query.toString()}`,
    token,
    { signal },
    baseUrl,
  );
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

/** Task 6.36 — persist the operator's approve/unapprove decision for a
 *  training run in the shared ledger (companion or Pi, per `baseUrl`). */
export function approveTrainingRun(
  token: string,
  runId: string,
  approved: boolean,
  baseUrl?: string,
): Promise<{ run: TrainingRun }> {
  return controlJson(
    `/control/training-runs/${encodeURIComponent(runId)}/approve`,
    token,
    { method: "POST", body: jsonBody({ approved }) },
    baseUrl,
  );
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

export function getProfiles(
  token: string,
  signal?: AbortSignal,
): Promise<VehicleProfilesResponse> {
  return controlJson<VehicleProfilesResponse>("/control/profiles", token, { signal });
}

export function updateProfileName(
  token: string,
  slug: string,
  name: string,
): Promise<{ profile: VehicleProfile }> {
  return controlJson<{ profile: VehicleProfile }>(
    `/control/profiles/${encodeURIComponent(slug)}/name`,
    token,
    { method: "POST", body: jsonBody({ name }) },
  );
}

/** `/control/trends` (Task 4.5) — per-stat session-fingerprint drift over
 *  the frozen reference epoch. Auth-gated like the other `/control/*`
 *  read routes. */
export function getTrends(
  token: string,
  signal?: AbortSignal,
): Promise<TrendsResponse> {
  return controlJson<TrendsResponse>("/control/trends", token, { signal });
}

export function getPowerStatus(
  token: string,
  signal?: AbortSignal,
): Promise<PowerStatusResponse> {
  return controlJson<PowerStatusResponse>("/control/power", token, { signal });
}

export function setKeepAlive(
  token: string,
  minutes: number,
): Promise<PowerKeepaliveResponse> {
  return controlJson<PowerKeepaliveResponse>("/control/power/keepalive", token, {
    method: "POST",
    body: jsonBody({ minutes }),
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
  // Task S.3 (llm-assist-and-compute-roadmap.md): the operator's working
  // theory + an optional clarifying question/answer pair, attached
  // alongside the verdict. All optional — omitted callers are unaffected.
  hypothesis?: string | null,
  clarifyingQuestion?: string | null,
  clarifyingAnswer?: string | null,
): Promise<{ feedback: Record<string, unknown> }> {
  return controlJson("/control/feedback", token, {
    method: "POST",
    body: jsonBody({
      event,
      label,
      reason,
      hypothesis: hypothesis ?? null,
      clarifying_question: clarifyingQuestion ?? null,
      clarifying_answer: clarifyingAnswer ?? null,
    }),
  });
}

export function getCustomPids(
  token: string,
  signal?: AbortSignal,
): Promise<CustomPidsResponse> {
  return controlJson("/control/custom-pids", token, { signal });
}

export function addCustomPid(
  token: string,
  pid: CustomPid,
): Promise<{ custom_pid: CustomPid }> {
  return controlJson("/control/custom-pids", token, {
    method: "POST",
    body: jsonBody(pid),
  });
}

export function deleteCustomPid(
  token: string,
  name: string,
): Promise<{ name: string; deleted: boolean }> {
  return controlJson(`/control/custom-pids/${encodeURIComponent(name)}`, token, {
    method: "DELETE",
  });
}

export function importCustomPidsCsv(
  token: string,
  csvText: string,
): Promise<CustomPidImportResponse> {
  return controlJson("/control/custom-pids/import", token, {
    method: "POST",
    body: jsonBody({ csv_text: csvText }),
  });
}
