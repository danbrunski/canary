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

/** One saved WiFi client profile (`GET /control/network/wifi`, N5). Never
 *  carries a passphrase. */
export interface WifiNetwork {
  /** The NetworkManager connection UUID. */
  id: string;
  ssid: string;
  priority: number;
  autoconnect: boolean;
  hidden: boolean;
  /** Key management (`wpa-psk`, `sae`, `wpa-eap`, ...); `null` is an open network. */
  security: string | null;
  active: boolean;
  in_range: boolean;
  /** 0-100 while in range. */
  signal: number | null;
  /** `other` was set up outside CANary (the Imager's preconfigured profile, `nmcli`). */
  managed_by: "canary" | "other";
}

/** One access point in the last scan. */
export interface WifiScanEntry {
  ssid: string;
  signal: number;
  /** `""` is an open network; otherwise e.g. `WPA2` or `WPA2 WPA3`. */
  security: string;
  saved: boolean;
}

export interface WifiState {
  /** Highest priority first. */
  networks: WifiNetwork[];
  /** Strongest first. */
  scan: WifiScanEntry[];
  /** Epoch seconds of the last scan. */
  scanned_at: number | null;
  interface: string | null;
}

export interface WifiResponse {
  wifi: WifiState;
}

export interface WifiAddRequest {
  ssid: string;
  /** Omitted or `null` for an open network. */
  password?: string | null;
  hidden?: boolean;
  /** `true` joins now (rolled back on failure); `false` saves for later. */
  join: boolean;
}

export interface WifiAddResponse {
  network: WifiNetwork;
  wifi: WifiState;
}
