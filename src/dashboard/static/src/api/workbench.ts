// Engine Tuning workbench routes, one function per route (R08.4). These used
// to be split between lib/workbenchApi.ts and inline `controlJson` calls in
// CalibrationPage, RomMapsPanel and AnalysisPage.

import type {
  CalibrationResponse,
  CellResidency,
  DeliveryErrorResponse,
  FlashRecordRequest,
  FrameResponse,
  LogsResponse,
  MemoryChannelsResponse,
  PullComparisonResponse,
  Regime,
  RomInspection,
  SessionChoice,
  TimingResponse,
  TrimSurfaceResponse,
  VerifyMemoryRequest,
  VerifyMemoryResponse,
} from "../types/workbench";
import { controlJson, jsonBody, queryString } from "./core";

function choiceParams(choice: SessionChoice) {
  return { sessions: choice.sessions.join(","), revision: choice.revision };
}

/** `GET /control/logs` — the capture browser (W04.1). */
export function getLogs(token: string, includeSessions: boolean, signal?: AbortSignal) {
  return controlJson<LogsResponse>(
    `/control/logs${queryString({ include_sessions: includeSessions })}`,
    token,
    { signal },
  );
}

/** `GET /control/sessions/{id}/frame` — one log as a wide frame (W04.3). */
export function getFrame(
  token: string,
  sessionId: string,
  params: { specs: string[]; grid: string; regime: Regime; offset: number },
  signal?: AbortSignal,
) {
  return controlJson<FrameResponse>(
    `/control/sessions/${encodeURIComponent(sessionId)}/frame${queryString({
      specs: params.specs.join(","),
      grid: params.grid,
      regime: params.regime,
      offset: params.offset,
    })}`,
    token,
    { signal },
  );
}

export function getDeliveryError(
  token: string,
  choice: SessionChoice,
  measured: string,
  minLoad: number | null,
  signal?: AbortSignal,
) {
  return controlJson<DeliveryErrorResponse>(
    `/analysis/delivery-error${queryString({ ...choiceParams(choice), measured, min_load: minLoad })}`,
    token,
    { signal },
  );
}

export function getTrimSurface(token: string, choice: SessionChoice, signal?: AbortSignal) {
  return controlJson<TrimSurfaceResponse>(
    `/analysis/trim-surface${queryString(choiceParams(choice))}`,
    token,
    { signal },
  );
}

export function getTiming(token: string, choice: SessionChoice, signal?: AbortSignal) {
  return controlJson<TimingResponse>(
    `/analysis/timing${queryString(choiceParams(choice))}`,
    token,
    {
      signal,
    },
  );
}

export function getPullComparison(
  token: string,
  revisionA: string,
  revisionB: string,
  floorChannel: string | null,
  signal?: AbortSignal,
) {
  return controlJson<PullComparisonResponse>(
    `/analysis/pull-comparison${queryString({
      revision_a: revisionA,
      revision_b: revisionB,
      floor_channel: floorChannel,
    })}`,
    token,
    { signal },
  );
}

/** `GET /control/calibration` — running revision and flash history. */
export function getCalibration(token: string, signal?: AbortSignal) {
  return controlJson<CalibrationResponse>("/control/calibration", token, { signal });
}

/** `POST /control/calibration` — record a flash in the registry. */
export function recordFlash(token: string, flash: FlashRecordRequest) {
  return controlJson<unknown>("/control/calibration", token, {
    method: "POST",
    body: jsonBody(flash),
  });
}

/** `GET /control/memory-channels` — RAM channels and their evidence state. */
export function getMemoryChannels(token: string, signal?: AbortSignal) {
  return controlJson<MemoryChannelsResponse>("/control/memory-channels", token, { signal });
}

/** `POST /control/memory-channels/verify` — re-read and check each address. */
export function verifyMemoryChannels(token: string, request: VerifyMemoryRequest) {
  return controlJson<VerifyMemoryResponse>("/control/memory-channels/verify", token, {
    method: "POST",
    body: jsonBody(request),
  });
}

/** `POST /control/rom/inspect` — decode a ROM in memory; never stored. */
export function inspectRom(token: string, file: File, signal?: AbortSignal) {
  return controlJson<RomInspection>("/control/rom/inspect", token, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
    signal,
  });
}

/** `GET /control/sessions/{id}/cell-residency` — which cells a log visited (W05.6). */
export function getCellResidency(
  token: string,
  sessionId: string,
  table: string,
  signal?: AbortSignal,
) {
  return controlJson<CellResidency>(
    `/control/sessions/${encodeURIComponent(sessionId)}/cell-residency${queryString({ table })}`,
    token,
    { signal },
  );
}
