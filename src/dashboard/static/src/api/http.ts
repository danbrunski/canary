// Thin fetch wrappers around the FastAPI REST endpoints (Task 5.3).
//
// One function per route — kept minimal so a panel that needs an endpoint
// imports the named function. `fetchJson` centralizes error handling so we
// never accidentally render an HTML 500 page as if it were valid JSON.

import type {
  TireAction,
  TireStatus,
  VehicleLedgerDraft,
  VehicleLedgerEvent,
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
  CaptureQuality,
  CaptureProfileWarning,
  CaptureTagResponse,
  CurrentCaptureResponse,
  CompanionDiscoverResponse,
  CompanionStatusResponse,
  CustomPid,
  CustomPidImportResponse,
  CustomPidsResponse,
  DatasetHealthResponse,
  DidMapResponse,
  DtcReportResponse,
  FeedbackResponse,
  IdentityResponse,
  PiLink,
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
  VehicleProfileEditResponse,
  VehicleProfilesResponse,
  VehicleProfileUpdate,
  VinReadResult,
  VersionResponse,
} from "../types";
import type {
  ClearCodesResult,
  DiagnosticFunctionsResponse,
  ReadCodesResult,
} from "../types/diagnostics";
import type { NetworkResponse, ProvisioningWindowResponse } from "../types/network";
import { controlBlob, controlJson, fetchJson, jsonBody, queryString, requireFields } from "./core";

export { HttpError, NetworkError, UnexpectedResponseError } from "./core";

/** Key-cycle pairing (`src/control/key_cycle_pairing.py`): no token needed,
 *  since these are how a device gets one. The request secret rides a header so
 *  access logs never record it. */
export interface PairingRequestCreated {
  id: string;
  secret: string;
}

export type PairingStep = "set_acc" | "turn_on" | "back_to_acc" | "unavailable";

export interface PairingRequestStatus {
  id: string;
  status: "waiting" | "approved" | "conflict" | "expired";
  step?: PairingStep;
  detail: string;
  vehicle_state: string | null;
  expires_in_s: number;
  /** Present once, on the poll that collects an approval. */
  token?: string;
  device?: { id: string; name: string };
}

export function createPairingRequest(name: string): Promise<PairingRequestCreated> {
  return fetchJson<PairingRequestCreated>("/pair/request", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: jsonBody({ name }),
  });
}

export function getPairingRequest(
  request: PairingRequestCreated,
  signal?: AbortSignal,
): Promise<PairingRequestStatus> {
  return fetchJson<PairingRequestStatus>(`/pair/request/${encodeURIComponent(request.id)}`, {
    headers: { "X-Pairing-Secret": request.secret },
    signal,
  });
}

export function cancelPairingRequest(request: PairingRequestCreated): Promise<unknown> {
  return fetchJson(`/pair/request/${encodeURIComponent(request.id)}`, {
    method: "DELETE",
    headers: { "X-Pairing-Secret": request.secret },
    keepalive: true,
  });
}

export function getStatus(signal?: AbortSignal): Promise<StatusResponse> {
  return fetchJson<StatusResponse>("/status", { signal });
}

/** `GET /identity` (no token): who answers on this origin. `host_kind`
 *  tells a Pi from a desk daemon, which `useConnectivityMode` reads. */
export function getIdentity(signal?: AbortSignal): Promise<IdentityResponse> {
  return fetchJson<IdentityResponse>("/identity", { signal });
}

/** `GET /companion/status` (no token), same origin as the dashboard. A daemon
 *  with `companion.enabled` answers it alongside `/status`; it is a 404 when
 *  companion mode is off. A daemon that answers only this (no `/status`) is
 *  the offline local library, the `useConnectivityMode` fallback. */
export function getCompanionStatus(signal?: AbortSignal): Promise<CompanionStatusResponse> {
  return fetchJson<CompanionStatusResponse>("/companion/status", { signal });
}

/** `GET /companion/discover`: search the network for Pis (a few seconds).
 *  `sweep` widens the search to a subnet scan. */
export function discoverPis(
  token: string,
  sweep = false,
  signal?: AbortSignal,
): Promise<CompanionDiscoverResponse> {
  return controlJson<CompanionDiscoverResponse>(
    `/companion/discover${queryString({ sweep: sweep ? "true" : "false" })}`,
    token,
    { signal },
  );
}

/** `POST /companion/pi`: link the companion to the Pi at `baseUrl`. A 400
 *  carries `detail` when nothing answers there. */
export function selectPi(token: string, baseUrl: string): Promise<{ pi: PiLink }> {
  return controlJson<{ pi: PiLink }>("/companion/pi", token, {
    method: "POST",
    body: jsonBody({ base_url: baseUrl }),
  });
}

/** `DELETE /companion/pi`: unlink. */
export function clearPi(token: string): Promise<{ pi: PiLink | null }> {
  return controlJson<{ pi: PiLink | null }>("/companion/pi", token, { method: "DELETE" });
}

export function getAnomalies(since?: string, signal?: AbortSignal): Promise<AnomaliesResponse> {
  const params = since ? `?since=${encodeURIComponent(since)}` : "";
  return fetchJson<AnomaliesResponse>(`/anomalies${params}`, { signal });
}

/** Seed shape for the live panels: the most recent sample for *every*
 *  channel, not the last N rows globally (which the high-rate CAN bus
 *  dominates, starving the ~1 Hz OBD gauges). */
export function getLatestPerChannel(signal?: AbortSignal): Promise<TelemetryLatestResponse> {
  return fetchJson<TelemetryLatestResponse>(`/telemetry/latest?per_channel=true`, {
    signal,
  });
}

export function getBehaviorModels(signal?: AbortSignal): Promise<BehaviorModelsResponse> {
  return fetchJson<BehaviorModelsResponse>("/models/behavior", { signal });
}

/** Live "you are here" overlay feed. Polled fast (~2 s) and independently
 *  of the slow static-surface poll — the trail itself lives server-side, so
 *  a slow client poll still returns a smooth history. */
export function getBehaviorLive(signal?: AbortSignal): Promise<BehaviorLiveResponse> {
  return fetchJson<BehaviorLiveResponse>("/models/behavior/live", { signal });
}

export function getBindings(signal?: AbortSignal): Promise<BindingsByChannel> {
  return fetchJson<BindingsByChannel>("/bindings", { signal });
}

export function getPreflight(token: string, signal?: AbortSignal): Promise<PreflightResponse> {
  return controlJson<PreflightResponse>("/control/preflight", token, { signal });
}

export function getControlVersion(token: string, signal?: AbortSignal): Promise<VersionResponse> {
  return controlJson<VersionResponse>("/control/version", token, { signal });
}

export function getCurrentCapture(
  token: string,
  signal?: AbortSignal,
): Promise<CurrentCaptureResponse> {
  return controlJson<CurrentCaptureResponse>("/control/captures/current", token, { signal });
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
export function getReplay(token: string, signal?: AbortSignal): Promise<ReplayStatusResponse> {
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

export async function listSessions(
  token: string,
  signal?: AbortSignal,
  baseUrl?: string,
  captures = false,
): Promise<SessionsResponse> {
  const path = `/control/sessions${captures ? "?captures=true" : ""}`;
  const body = await controlJson<SessionsResponse>(path, token, { signal }, baseUrl);
  return requireFields(path, body, ["sessions"]);
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

export function downloadSession(token: string, sessionId: string, baseUrl?: string): Promise<Blob> {
  return controlBlob(
    `/control/sessions/${encodeURIComponent(sessionId)}/export`,
    token,
    { headers: { Accept: "application/zip" } },
    baseUrl,
  );
}

/** Task 5.6a: GPX/KML export of a session's captured GPS track. The body is
 *  the file itself, so `controlBlob`, not `controlJson`. */
function downloadSessionTrack(
  token: string,
  sessionId: string,
  format: "gpx" | "kml",
  baseUrl?: string,
): Promise<Blob> {
  return controlBlob(
    `/control/sessions/${encodeURIComponent(sessionId)}/gps.${format}`,
    token,
    {},
    baseUrl,
  );
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
  return controlJson<JobResponse>(
    "/control/jobs",
    token,
    {
      method: "POST",
      body: jsonBody({ kind, params }),
    },
    baseUrl,
  );
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
export async function getObdCoverage(signal?: AbortSignal): Promise<ObdCoverageResponse> {
  const body = await fetchJson<ObdCoverageResponse>("/obd/coverage", { signal });
  return requireFields("/obd/coverage", body, ["rows", "custom_pids", "undecoded_supported"]);
}

/** `/can/coverage` — static catalog of decodable broadcast-CAN signals. */
export function getCanCoverage(signal?: AbortSignal): Promise<CanCoverageResponse> {
  return fetchJson("/can/coverage", { signal });
}

/** `/dids` is read-only + unauthenticated, like `/bindings` (Task 6.16). */
export function getDids(signal?: AbortSignal): Promise<DidMapResponse> {
  return fetchJson("/dids", { signal });
}

/** `/obd/dtc` is read-only + unauthenticated, like `/obd/coverage` (Task
 *  1.5) — the cached stored/pending/permanent DTC report. No clear-codes
 *  endpoint exists; this API surface is read-only by design. */
export async function getDtc(signal?: AbortSignal): Promise<DtcReportResponse> {
  const body = await fetchJson<DtcReportResponse>("/obd/dtc", { signal });
  return requireFields("/obd/dtc", body, ["stored", "pending", "permanent"]);
}

/** `/obd/monitors` (Task 3.1) — cached Mode 06 on-board monitor sweep:
 *  per-cylinder misfire counts + raw monitor test results. Read-only, like
 *  `/obd/dtc` above. */
export async function getMonitors(signal?: AbortSignal): Promise<MonitorsResponse> {
  const body = await fetchJson<MonitorsResponse>("/obd/monitors", { signal });
  return requireFields("/obd/monitors", body, ["misfire_counts", "results"]);
}

/** `/analysis/llm-assist/cards` (Task S.2) — public/read-only like
 *  `/obd/monitors` above. `enabled: false` (the default; no producer
 *  exists yet) is the expected response, not an error. */
export function getLlmAssistCards(signal?: AbortSignal): Promise<LlmAssistCardsResponse> {
  return fetchJson("/analysis/llm-assist/cards", { signal });
}

/** `/sessions/{id}/trip-summary` is read-only + unauthenticated, like
 *  `/obd/dtc` (Task 1.5) — computed on demand from the stored session. */
export async function getTripSummary(
  sessionId: string,
  signal?: AbortSignal,
): Promise<TripSummaryResponse> {
  const path = `/sessions/${encodeURIComponent(sessionId)}/trip-summary`;
  const body = await fetchJson<TripSummaryResponse>(path, { signal });
  return requireFields(path, body, ["summary"]);
}

/** `/control/sessions/{id}/digest` is authenticated (auth-consistency
 *  follow-up to Task 5.6) — it aggregates the vehicle ledger and
 *  per-session fingerprint z-scores, both otherwise only readable via
 *  authenticated `/control/*` routes (see getTrends below). Computed on
 *  demand if the trip-end hook hasn't already written
 *  `digest.json`. */
export async function getSessionDigest(
  token: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionDigestResponse> {
  const path = `/control/sessions/${encodeURIComponent(sessionId)}/digest`;
  const body = await controlJson<SessionDigestResponse>(path, token, { signal });
  return requireFields(path, body, [
    "anomaly_counts_by_source",
    "fingerprint_top_movers",
    "new_dtcs",
    "new_ledger_events",
  ]);
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
  // `value` and `x` are required by the route (FastAPI `Query(...)`): always
  // send them, even empty, so a cleared field reads as the route's own
  // refusal rather than a missing parameter. Only the optional ones go
  // through queryString, which drops blanks.
  const required = new URLSearchParams({ value: params.value, x: params.x }).toString();
  const optional = queryString({
    y: params.y,
    x_bins: params.xBins,
    y_bins: params.yBins,
    agg: params.agg,
    session: params.session,
    since: params.since,
    until: params.until,
  });
  const rest = optional ? `&${optional.slice(1)}` : "";
  return fetchJson<BinnedAnalysisResponse>(`/analysis/binned?${required}${rest}`, { signal });
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
  const query = queryString({ channels: channels.join(","), resample_hz: resampleHz });
  return controlJson<SessionChartResponse>(
    `/control/sessions/${encodeURIComponent(sessionId)}/chart${query}`,
    token,
    { signal },
    baseUrl,
  );
}

/** `/test-profiles` is read-only + unauthenticated (scanned YAML catalog);
 *  the picker must render before the control token is entered. */
export function getTestProfiles(signal?: AbortSignal): Promise<TestProfilesResponse> {
  return fetchJson("/test-profiles", { signal });
}

export async function getDatasetHealth(
  token: string,
  signal?: AbortSignal,
): Promise<DatasetHealthResponse> {
  const body = await controlJson<DatasetHealthResponse>("/control/datasets/health", token, {
    signal,
  });
  return requireFields("/control/datasets/health", body, [
    "sessions",
    "mode_coverage",
    "channel_availability",
    "readiness",
  ]);
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

export async function getTuning(token: string, signal?: AbortSignal): Promise<TuningResponse> {
  const body = await controlJson<TuningResponse>("/control/tuning", token, { signal });
  return requireFields("/control/tuning", body, ["tuning"]);
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

export function estimateTuning(token: string, config: TuningConfig): Promise<TuningEstimate> {
  return controlJson("/control/tuning/estimate", token, {
    method: "POST",
    body: jsonBody({ config }),
  });
}

export function revertTuning(token: string, version: number): Promise<TuningResponse> {
  return controlJson("/control/tuning/revert", token, {
    method: "POST",
    body: jsonBody({ version }),
  });
}

export async function getProfiles(
  token: string,
  signal?: AbortSignal,
): Promise<VehicleProfilesResponse> {
  const body = await controlJson<VehicleProfilesResponse>("/control/profiles", token, { signal });
  return requireFields("/control/profiles", body, ["profiles"]);
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

/** Edit a profile's identity and vehicle parameters (the machine overlay;
 *  applies to the daemon at its next start). */
export function updateProfile(
  token: string,
  slug: string,
  update: VehicleProfileUpdate,
): Promise<VehicleProfileEditResponse> {
  return controlJson<VehicleProfileEditResponse>(
    `/control/profiles/${encodeURIComponent(slug)}`,
    token,
    { method: "PATCH", body: jsonBody(update) },
  );
}

/** Put `fields` (or, with none, every override) back to the tracked values. */
export function revertProfile(
  token: string,
  slug: string,
  fields?: string[],
): Promise<VehicleProfileEditResponse> {
  return controlJson<VehicleProfileEditResponse>(
    `/control/profiles/${encodeURIComponent(slug)}/revert`,
    token,
    { method: "POST", body: jsonBody({ fields: fields ?? null }) },
  );
}

/** Read the connected vehicle's VIN through the running collector. Never saves it. */
export function readVin(token: string): Promise<{ vin_read: VinReadResult }> {
  return controlJson<{ vin_read: VinReadResult }>("/control/vin/read", token, {
    method: "POST",
  });
}

/** `/control/trends` (Task 4.5) — per-stat session-fingerprint drift over
 *  the frozen reference epoch. Auth-gated like the other `/control/*`
 *  read routes. */
export function getTrends(token: string, signal?: AbortSignal): Promise<TrendsResponse> {
  return controlJson<TrendsResponse>("/control/trends", token, { signal });
}

/** `/control/tires` — tire wear from the trip digests (rolling circumference
 *  trend, estimated tread depth once a gauge reading is on the ledger). */
export async function getTires(token: string, signal?: AbortSignal): Promise<TireStatus> {
  const body = await controlJson<TireStatus>("/control/tires", token, { signal });
  return requireFields("/control/tires", body, ["drives", "caveats"]);
}

/** Record a tire fact on the vehicle ledger: a gauge tread reading (the
 *  baseline), a rotation or replacement (restarts the trend), or a pressure
 *  change (named as a caveat). */
export function recordTireEvent(
  token: string,
  event: {
    occurredAt: string;
    action: TireAction;
    treadDepthMm?: number;
    pressurePsi?: number;
  },
): Promise<{ event: unknown }> {
  const titles: Record<TireAction, string> = {
    tread_measured: `Tread depth gauge reading: ${event.treadDepthMm ?? "?"} mm`,
    rotated: "Tires rotated",
    replaced: "Tires replaced",
    pressure_changed: `Tire pressure set to ${event.pressurePsi ?? "?"} psi`,
  };
  return controlJson("/control/vehicle-ledger", token, {
    method: "POST",
    body: jsonBody({
      occurred_at: event.occurredAt,
      kind: "maintenance",
      title: titles[event.action],
      tires: {
        action: event.action,
        tread_depth_mm: event.treadDepthMm ?? null,
        pressure_psi: event.pressurePsi ?? null,
      },
    }),
  });
}

/** `/control/vehicle-ledger` — every entry, oldest first. */
export function getVehicleLedger(
  token: string,
  signal?: AbortSignal,
): Promise<{ events: VehicleLedgerEvent[]; count: number }> {
  return controlJson("/control/vehicle-ledger", token, { signal });
}

/** Add an entry, or (with `event_id`) replace that entry on disk: a typo or a
 *  wrong reading is gone, not kept under its correction. */
export function saveVehicleEvent(
  token: string,
  draft: VehicleLedgerDraft,
): Promise<{ event: VehicleLedgerEvent }> {
  return controlJson("/control/vehicle-ledger", token, {
    method: "POST",
    body: jsonBody(draft),
  });
}

/** `DELETE /control/vehicle-ledger/{id}` — remove an erroneous entry outright. */
export function deleteVehicleEvent(
  token: string,
  eventId: string,
): Promise<{ deleted: { id: string; title: string } }> {
  return controlJson(`/control/vehicle-ledger/${encodeURIComponent(eventId)}`, token, {
    method: "DELETE",
  });
}

export async function getPowerStatus(
  token: string,
  signal?: AbortSignal,
): Promise<PowerStatusResponse> {
  const body = await controlJson<PowerStatusResponse>("/control/power", token, { signal });
  return requireFields("/control/power", body, ["power"]);
}

export function setKeepAlive(token: string, minutes: number): Promise<PowerKeepaliveResponse> {
  return controlJson<PowerKeepaliveResponse>("/control/power/keepalive", token, {
    method: "POST",
    body: jsonBody({ minutes }),
  });
}

/** What the Pi joined and its addresses, plus the BLE provisioning window (P2.3). */
export async function getNetwork(token: string, signal?: AbortSignal): Promise<NetworkResponse> {
  const body = await controlJson<NetworkResponse>("/control/network", token, { signal });
  return requireFields("/control/network", body, ["network", "provisioning"]);
}

/** Open the BLE provisioning window for `minutes` (`0` closes it). */
export function setProvisioningWindow(
  token: string,
  minutes: number,
): Promise<ProvisioningWindowResponse> {
  return controlJson<ProvisioningWindowResponse>("/control/network/provisioning/window", token, {
    method: "POST",
    body: jsonBody({ minutes }),
  });
}

export async function getFeedback(token: string, signal?: AbortSignal): Promise<FeedbackResponse> {
  const body = await controlJson<FeedbackResponse>("/control/feedback", token, { signal });
  return requireFields("/control/feedback", body, ["feedback", "summary"]);
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

export async function getCustomPids(
  token: string,
  signal?: AbortSignal,
): Promise<CustomPidsResponse> {
  const body = await controlJson<CustomPidsResponse>("/control/custom-pids", token, { signal });
  return requireFields("/control/custom-pids", body, ["custom_pids"]);
}

export function addCustomPid(token: string, pid: CustomPid): Promise<{ custom_pid: CustomPid }> {
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

/** `/control/diagnostics/functions`: the diagnostic functions this vehicle's
 *  profile records (today, reading and clearing a K-line module's codes). */
export async function getDiagnosticFunctions(
  token: string,
  signal?: AbortSignal,
): Promise<DiagnosticFunctionsResponse> {
  const path = "/control/diagnostics/functions";
  const body = await controlJson<DiagnosticFunctionsResponse>(path, token, { signal });
  return requireFields(path, body, ["diagnostic_functions"]);
}

/** Read one module's stored codes. Resolves after the module answered (or
 *  refused); `ok: true` with `codes: []` is a read that found none, while a
 *  failure is `ok: false`, `codes: null` and the reason in `detail`. */
export function readModuleCodes(
  token: string,
  module: string,
): Promise<{ read_codes: ReadCodesResult }> {
  return controlJson("/control/diagnostics/read_codes", token, {
    method: "POST",
    body: jsonBody({ module }),
  });
}

/** Clear one module's stored codes. Resolves after the module answered (or
 *  refused); a refusal is `ok: false` with the module's reason. */
export function clearModuleCodes(
  token: string,
  module: string,
): Promise<{ clear_codes: ClearCodesResult }> {
  return controlJson("/control/diagnostics/clear_codes", token, {
    method: "POST",
    body: jsonBody({ module }),
  });
}
