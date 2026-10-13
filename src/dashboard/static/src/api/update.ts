// Software update from the UI (phone-ui P3.2), one function per route.

import type { UpdateAction, UpdateRequestResponse, UpdateResponse } from "../types/update";
import { controlJson, jsonBody } from "./core";

/** `GET /control/update` — installed version, last check, runner status. */
export function getUpdate(token: string, signal?: AbortSignal) {
  return controlJson<UpdateResponse>("/control/update", token, { signal });
}

/** `POST /control/update` — file a check or an update for the root unit.
 *  409 carries the refusal (capture, ECU backup, already running, no
 *  internet); 503 means the trigger is not installed. */
export function requestUpdate(token: string, action: UpdateAction) {
  return controlJson<UpdateRequestResponse>("/control/update", token, {
    method: "POST",
    body: jsonBody({ action }),
  });
}
