// The background-job routes (`/control/jobs`), one function per route.

import type { JobLogsResponse, JobResponse, JobResultResponse, JobsResponse } from "../types";
import { controlJson, jsonBody } from "./core";

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

export function getJob(token: string, jobId: string, signal?: AbortSignal): Promise<JobResponse> {
  return controlJson<JobResponse>(`/control/jobs/${encodeURIComponent(jobId)}`, token, { signal });
}

export function getJobLogs(token: string, jobId: string): Promise<JobLogsResponse> {
  return controlJson<JobLogsResponse>(`/control/jobs/${encodeURIComponent(jobId)}/logs`, token);
}

/** `GET /control/jobs/{id}/result` (Task 4.1) — the structured report a
 *  `replay_window` job writes on success; `result` reads `null` until the
 *  job's report file lands (still running/queued). 404s for any other job
 *  kind, same as `describeError` already renders. */
export function getJobResult(
  token: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<JobResultResponse> {
  return controlJson<JobResultResponse>(
    `/control/jobs/${encodeURIComponent(jobId)}/result`,
    token,
    {
      signal,
    },
  );
}
