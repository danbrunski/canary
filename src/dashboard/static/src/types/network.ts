/** `GET /control/network` and `/status.network` (phone-ui P2.3) — mirrors
 *  `src.provisioning.reader.NetworkReading.to_dict`. The same facts as the BLE
 *  status characteristic. */
export interface NetworkSummary {
  hostname: string | null;
  /** `unknown` when nmcli failed (see `error`), never a guessed `no-wifi`. */
  mode: "client" | "ap" | "disconnected" | "no-wifi" | "unknown" | null;
  ssid: string | null;
  wifi_interface: string | null;
  /** Interface → addresses (IPv4, global IPv6). */
  addresses: Record<string, string[]>;
  /** By IP, never `canary.local`: Android does not resolve mDNS. */
  dashboard_url: string | null;
  read_at: number;
  age_s: number;
  /** What failed, by name (nmcli missing on a laptop, NetworkManager down). */
  error: string | null;
}

export interface ProvisioningWindow {
  window_remaining_s: number;
  max_minutes: number;
}

export interface NetworkResponse {
  network: NetworkSummary;
  provisioning: ProvisioningWindow;
}

export interface ProvisioningWindowResponse {
  provisioning: ProvisioningWindow;
  expiry: number;
}
