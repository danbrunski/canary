// The tuning-config routes (`/control/tuning`): read, save, estimate, revert.

import type { TuningConfig, TuningEstimate, TuningResponse } from "../types";
import { controlJson, jsonBody, requireFields } from "./core";

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
