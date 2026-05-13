// Thin fetch wrappers around the FastAPI REST endpoints (Task 5.3).
//
// One function per route — kept minimal so a panel that needs an endpoint
// imports the named function. `fetchJson` centralizes error handling so we
// never accidentally render an HTML 500 page as if it were valid JSON.

import type {
  AnomaliesResponse,
  StatusResponse,
  TelemetryLatestResponse,
} from "../types";

class HttpError extends Error {
  status: number;
  body: string;
  constructor(status: number, body: string) {
    super(`HTTP ${status}: ${body.slice(0, 120)}`);
    this.status = status;
    this.body = body;
  }
}

async function fetchJson<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  const text = await response.text();
  if (!response.ok) {
    throw new HttpError(response.status, text);
  }
  if (!text) {
    return {} as T;
  }
  return JSON.parse(text) as T;
}

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

export { HttpError };
