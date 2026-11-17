// The Pi's network routes (`/control/network`): what it joined, the BLE provisioning window, saved Wi-Fi.

import type {
  NetworkResponse,
  ProvisioningWindowResponse,
  WifiAddRequest,
  WifiAddResponse,
  WifiResponse,
} from "../types/network";
import { controlJson, jsonBody, requireFields } from "./core";

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

/** Saved WiFi networks and the last scan (N5). 503 means the Pi's WiFi control
 *  service is not reachable; the detail names why. */
export async function getWifi(token: string, signal?: AbortSignal): Promise<WifiResponse> {
  const body = await controlJson<WifiResponse>("/control/network/wifi", token, { signal });
  return requireFields("/control/network/wifi", body, ["wifi"]);
}

/** Rescan, then the same payload (a few seconds). */
export function scanWifi(token: string): Promise<WifiResponse> {
  return controlJson<WifiResponse>("/control/network/wifi/scan", token, { method: "POST" });
}

/** Save a network, and with `join` switch to it (up to ~45 s; the Pi rolls
 *  back on failure with a 409). The passphrase travels in this body only. */
export function addWifi(token: string, request: WifiAddRequest): Promise<WifiAddResponse> {
  return controlJson<WifiAddResponse>("/control/network/wifi", token, {
    method: "POST",
    body: jsonBody(request),
  });
}

/** Switch to a saved network now (up to ~30 s). */
export function connectWifi(token: string, id: string): Promise<WifiResponse> {
  return controlJson<WifiResponse>(
    `/control/network/wifi/${encodeURIComponent(id)}/connect`,
    token,
    {
      method: "POST",
    },
  );
}

/** `ids` is every saved id, highest priority first. */
export function orderWifi(token: string, ids: string[]): Promise<WifiResponse> {
  return controlJson<WifiResponse>("/control/network/wifi/order", token, {
    method: "PUT",
    body: jsonBody({ ids }),
  });
}

export function forgetWifi(token: string, id: string): Promise<WifiResponse> {
  return controlJson<WifiResponse>(`/control/network/wifi/${encodeURIComponent(id)}`, token, {
    method: "DELETE",
  });
}
