// Engine Tuning workbench routes, one function per route (R08.4). These used
// to be split between lib/workbenchApi.ts and inline `controlJson` calls in
// CalibrationPage, RomMapsPanel and AnalysisPage.

import type {
  CalibrationResponse,
  CellResidency,
  DeliveryErrorResponse,
  DynoRequest,
  DynoResponse,
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
import type {
  RomBackupState,
  RomCompareResponse,
  RomDescription,
  RomLibraryEntry,
  RomLibraryResponse,
  RomTablesResponse,
  RomUploadResponse,
} from "../types/romLibrary";
import { controlBlob, controlJson, jsonBody, queryString, requireFields } from "./core";

function choiceParams(choice: SessionChoice) {
  return { sessions: choice.sessions.join(","), revision: choice.revision };
}

/** `GET /control/logs` — the capture browser (W04.1). */
export async function getLogs(token: string, includeSessions: boolean, signal?: AbortSignal) {
  const path = `/control/logs${queryString({ include_sessions: includeSessions })}`;
  const body = await controlJson<LogsResponse>(path, token, { signal });
  return requireFields(path, body, ["logs"]);
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

/** `GET /analysis/dyno` — wheel power and torque per session and gear (H07.9).
 *  `request` is what the operator states about the runs; nothing in it is
 *  defaulted, and a run missing any of it is refused by name. */
export function getDyno(
  token: string,
  choice: SessionChoice,
  request: DynoRequest,
  signal?: AbortSignal,
) {
  return controlJson<DynoResponse>(
    `/analysis/dyno${queryString({
      ...choiceParams(choice),
      road: request.road,
      payload_kg: request.payload_kg,
      induction: request.induction,
      ambient_temp_c: request.ambient_temp_c,
      allow_single_direction: request.allow_single_direction || null,
    })}`,
    token,
    { signal },
  );
}

/** `GET /control/calibration` — running revision and flash history. */
export async function getCalibration(token: string, signal?: AbortSignal) {
  const body = await controlJson<CalibrationResponse>("/control/calibration", token, { signal });
  return requireFields("/control/calibration", body, ["history"]);
}

/** `POST /control/calibration` — record a flash in the registry. */
export function recordFlash(token: string, flash: FlashRecordRequest) {
  return controlJson<unknown>("/control/calibration", token, {
    method: "POST",
    body: jsonBody(flash),
  });
}

/** `GET /control/memory-channels` — RAM channels and their evidence state. */
export async function getMemoryChannels(token: string, signal?: AbortSignal) {
  const path = "/control/memory-channels";
  const body = await controlJson<MemoryChannelsResponse>(path, token, { signal });
  return requireFields(path, body, ["channels"]);
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

// ROM library (W08.3–W08.5) and ECU backup (W08.7). Shapes: types/romLibrary.

function librarySha(sha: string) {
  return `/control/rom/library/${encodeURIComponent(sha)}`;
}

/** `GET /control/rom/library` — stored images, newest first. */
export function getRomLibrary(token: string, signal?: AbortSignal) {
  return controlJson<RomLibraryResponse>("/control/rom/library", token, { signal });
}

/** `POST /control/rom/library` — store a plain image or an `.OsirisROM`. The
 *  server detects the format and refuses anything else by name. */
export function uploadRomImage(token: string, file: File, signal?: AbortSignal) {
  return controlJson<RomUploadResponse>(
    `/control/rom/library${queryString({ filename: file.name })}`,
    token,
    {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
      signal,
    },
  );
}

/** `GET /control/rom/library/{sha}` — identity, block statistics, map. */
export function getRomDescription(token: string, sha: string, signal?: AbortSignal) {
  return controlJson<RomDescription>(librarySha(sha), token, { signal });
}

/** `GET /control/rom/library/{sha}/tables` — every table, every slot. */
export function getRomTables(token: string, sha: string, signal?: AbortSignal) {
  return controlJson<RomTablesResponse>(`${librarySha(sha)}/tables`, token, { signal });
}

/** `PATCH /control/rom/library/{sha}` — the label is the only mutable field;
 *  null clears it. */
export function renameRomImage(token: string, sha: string, label: string | null) {
  return controlJson<RomLibraryEntry>(librarySha(sha), token, {
    method: "PATCH",
    body: jsonBody({ label }),
  });
}

/** `DELETE /control/rom/library/{sha}`. */
export function deleteRomImage(token: string, sha: string) {
  return controlJson<unknown>(librarySha(sha), token, { method: "DELETE" });
}

/** `GET /control/rom/library/{sha}/download` — the plain image. */
export function downloadRomImage(token: string, sha: string) {
  return controlBlob(`${librarySha(sha)}/download`, token, {
    headers: { Accept: "application/octet-stream" },
  });
}

/** `GET /control/rom/compare?a=&b=` — changed ranges from `a` to `b`. */
export function compareRomImages(token: string, a: string, b: string, signal?: AbortSignal) {
  return controlJson<RomCompareResponse>(`/control/rom/compare${queryString({ a, b })}`, token, {
    signal,
  });
}

/** `GET /control/rom/backup` — the ECU backup job's state. */
export function getRomBackup(token: string, signal?: AbortSignal) {
  return controlJson<RomBackupState>("/control/rom/backup", token, { signal });
}

/** `POST /control/rom/backup` — start reading the ECU's flash. */
export function startRomBackup(token: string) {
  return controlJson<RomBackupState>("/control/rom/backup", token, {
    method: "POST",
    body: jsonBody({}),
  });
}

/** `POST /control/rom/backup/cancel`. */
export function cancelRomBackup(token: string) {
  return controlJson<RomBackupState>("/control/rom/backup/cancel", token, {
    method: "POST",
    body: jsonBody({}),
  });
}
