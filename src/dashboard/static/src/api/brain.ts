// The second brain's routes (E04.5), one function per route. The MCP brain
// tools return the same bodies (`src/mcp_server/tools/brain.py`).

import type {
  BrainJob,
  BrainJobsResponse,
  BrainStatus,
  InvestigationCardResponse,
  MemoryResponse,
  TwinSessionResponse,
} from "../types/brain";
import { controlJson, jsonBody, queryString } from "./core";

const enc = encodeURIComponent;

/** `GET /control/brain/status` — identity, invalidations, transient state. */
export function getBrainStatus(token: string, signal?: AbortSignal) {
  return controlJson<BrainStatus>("/control/brain/status", token, { signal });
}

/** `GET /control/brain/memory` — `MemoryQuery` filters as query parameters. */
export function queryMemory(
  token: string,
  params: { kind?: string; text?: string; session?: string; history?: boolean },
  signal?: AbortSignal,
) {
  return controlJson<MemoryResponse>(`/control/brain/memory${queryString(params)}`, token, {
    signal,
  });
}

/** `GET /control/brain/investigations/{id}/card` — the deterministic card. */
export function getInvestigationCard(token: string, id: string, signal?: AbortSignal) {
  return controlJson<InvestigationCardResponse>(
    `/control/brain/investigations/${enc(id)}/card`,
    token,
    { signal },
  );
}

/** `GET /control/brain/twin/sessions/{session}` — the twin's evaluation. */
export function getTwinSession(token: string, session: string, signal?: AbortSignal) {
  return controlJson<TwinSessionResponse>(`/control/brain/twin/sessions/${enc(session)}`, token, {
    signal,
  });
}

/** `GET /control/brain/jobs` — idle-time jobs and their states. */
export function getBrainJobs(token: string, signal?: AbortSignal) {
  return controlJson<BrainJobsResponse>("/control/brain/jobs", token, { signal });
}

/** `POST /control/brain/jobs` — queue work for the next idle window. */
export function submitBrainJob(
  token: string,
  kind: BrainJob["kind"],
  params: Record<string, unknown> = {},
) {
  return controlJson<{ job: BrainJob; created: boolean }>("/control/brain/jobs", token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: jsonBody({ kind, params }),
  });
}
