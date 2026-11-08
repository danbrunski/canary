// Paired devices and approving a waiting pairing request
// (`src/dashboard/routes/pairing.py`, docs/roadmap/archive/phone-ui.md P2.4). All of
// these need a control token; a paired device's own token counts.

import { controlJson, jsonBody, requireFields } from "./core";

export interface PairedDevice {
  id: string;
  name: string;
  /** Unix seconds. */
  paired_at: number;
  /** `key-cycle`, or `approved-by:<device id>` / `approved-by:shared-token`. */
  method: string;
  last_seen: number | null;
}

/** A request waiting to pair, as a trusted caller sees it (never its secret). */
export interface PendingPairingRequest {
  id: string;
  name: string;
  age_s: number;
  expires_in_s: number;
}

/** `GET /control/devices/me`: which token this browser holds. */
export interface DeviceMe {
  kind: "device" | "shared-token" | "loopback";
  shared_token: boolean;
  device: PairedDevice | null;
}

export interface ApprovePairingResult {
  approved: string;
  by: string;
}

const enc = encodeURIComponent;

export async function listPairedDevices(
  token: string,
  signal?: AbortSignal,
): Promise<{ devices: PairedDevice[] }> {
  const body = await controlJson<{ devices: PairedDevice[] }>("/control/devices", token, {
    signal,
  });
  return requireFields("/control/devices", body, ["devices"]);
}

export function getDeviceMe(token: string, signal?: AbortSignal): Promise<DeviceMe> {
  return controlJson("/control/devices/me", token, { signal });
}

export function renamePairedDevice(
  token: string,
  id: string,
  name: string,
): Promise<{ device: PairedDevice }> {
  return controlJson(`/control/devices/${enc(id)}`, token, {
    method: "PATCH",
    body: jsonBody({ name }),
  });
}

export function removePairedDevice(token: string, id: string): Promise<{ removed: PairedDevice }> {
  return controlJson(`/control/devices/${enc(id)}`, token, { method: "DELETE" });
}

export async function listPairingRequests(
  token: string,
  signal?: AbortSignal,
): Promise<{ requests: PendingPairingRequest[] }> {
  const path = "/control/pairing/requests";
  const body = await controlJson<{ requests: PendingPairingRequest[] }>(path, token, { signal });
  return requireFields(path, body, ["requests"]);
}

export function approvePairingRequest(token: string, id: string): Promise<ApprovePairingResult> {
  return controlJson(`/control/pairing/requests/${enc(id)}/approve`, token, { method: "POST" });
}
