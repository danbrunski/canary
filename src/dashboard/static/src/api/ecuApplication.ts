// The ECM application twin's coverage manifest (E05.1). MCP
// `get_ecu_application_model` passes this route through.

import type { ApplicationModelReport } from "../types/ecuApplication";
import { controlJson, requireFields } from "./core";

/** `GET /control/ecu/application-model` — qualified outputs, the traced path
 *  by twin role, blockers, regions and uncovered logic. 404 when the vehicle
 *  declares no application model. */
export async function getApplicationModel(token: string, signal?: AbortSignal) {
  const body = await controlJson<ApplicationModelReport>("/control/ecu/application-model", token, {
    signal,
  });
  return requireFields("/control/ecu/application-model", body, [
    "model",
    "program",
    "outputs",
    "uncovered",
  ]);
}
