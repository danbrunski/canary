import { useCallback, useState } from "react";

import {
  addWifi,
  connectWifi,
  forgetWifi,
  getWifi,
  HttpError,
  NetworkError,
  orderWifi,
  scanWifi,
} from "../api/http";
import { Notice } from "../components/Notice";
import { TokenRequired } from "../components/TokenRequired";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { describeError, useControlResource } from "../hooks/useControlResource";
import { buttonClass, cn, definitionTermClass, inputClass, placeholderClass } from "../styles";
import type { NetworkSummary, WifiNetwork, WifiScanEntry, WifiState } from "../types/network";
import { toneClass } from "../util/format";

/** Where the Pi is always reachable once it has left the WiFi this page is on. */
const CABLE_ADDRESS = "192.168.137.2";

/** The panel shows the daemon's own sentence for these, never the generic
 *  "feature not wired" text: 503 says the WiFi service is not reachable and
 *  why, and a join that failed says what NetworkManager answered. */
function serverDetail(err: HttpError): string {
  try {
    const parsed: unknown = JSON.parse(err.body);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const detail = parsed.detail;
      if (typeof detail === "string") return detail.slice(0, 400);
    }
  } catch {
    /* not JSON: fall through */
  }
  return err.body.slice(0, 400);
}

function describeWifiError(err: unknown): string {
  if (err instanceof HttpError && [404, 409, 422, 503, 504].includes(err.status)) {
    const detail = serverDetail(err);
    if (err.status === 504) return `The Pi's WiFi service timed out: ${detail}`;
    return detail || describeError(err);
  }
  return describeError(err);
}

const SECURITY_LABELS: Record<string, string> = {
  "wpa-psk": "WPA2",
  sae: "WPA3",
  "wpa-eap": "enterprise",
  ieee8021x: "enterprise",
};

function securityLabel(security: string | null): string {
  if (security === null) return "open";
  return SECURITY_LABELS[security] ?? security;
}

function hostMatches(host: string, addresses: string[]): boolean {
  const bare = host.replace(/^\[|\]$/g, "");
  return addresses.includes(bare);
}

/** True when this browser reaches the Pi through its WiFi address, so moving
 *  the Pi to another network (or off this one) drops the page. */
function reachingThroughWifi(network: NetworkSummary | null, host: string): boolean {
  const iface = network?.wifi_interface;
  if (!network || !iface) return false;
  return hostMatches(host, network.addresses[iface] ?? []);
}

function dropWarning(ssid: string, leaving = false): string {
  return (
    `This page will lose its connection; the Pi will ${leaving ? "leave" : "move to"} ${ssid}. ` +
    `Reach it again at its new address or over the cable at ${CABLE_ADDRESS}.`
  );
}

interface WifiNetworksCardProps {
  token: string;
  /** `GET /control/network`: which address is the WiFi one. */
  network: NetworkSummary | null;
  /** The host this page was loaded from; a prop so a test can pick it. */
  host?: string;
}

function SavedRow({
  net,
  index,
  count,
  busy,
  onMove,
  onConnect,
  onForget,
}: {
  net: WifiNetwork;
  index: number;
  count: number;
  busy: boolean;
  onMove: (index: number, delta: -1 | 1) => void;
  onConnect: (net: WifiNetwork) => void;
  onForget: (net: WifiNetwork) => void;
}) {
  const small = cn(buttonClass, "min-h-9 px-2.5 text-xs");
  return (
    <li className="flex flex-col gap-1.5 rounded-[8px] border border-border-soft px-2.5 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="min-w-0 font-semibold break-all">{net.ssid}</span>
        {net.active && <span className={cn("text-xs", toneClass("ok"))}>connected</span>}
        <span className="text-xs text-fg-faint">
          {net.in_range
            ? net.signal !== null
              ? `in range · signal ${Math.round(net.signal)}%`
              : "in range"
            : "not in range"}
        </span>
        <span className="text-xs text-fg-faint">{securityLabel(net.security)}</span>
        {net.hidden && <span className="text-xs text-fg-faint">hidden</span>}
        {!net.autoconnect && <span className="text-xs text-fg-faint">not auto-joined</span>}
      </div>
      {net.managed_by === "other" && (
        <span className="text-xs text-fg-faint">set up outside CANary</span>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className={small}
          aria-label={`move ${net.ssid} up`}
          disabled={busy || index === 0}
          onClick={() => onMove(index, -1)}
        >
          ↑ Up
        </button>
        <button
          type="button"
          className={small}
          aria-label={`move ${net.ssid} down`}
          disabled={busy || index === count - 1}
          onClick={() => onMove(index, 1)}
        >
          ↓ Down
        </button>
        {!net.active && (
          <button
            type="button"
            className={small}
            aria-label={`connect to ${net.ssid}`}
            disabled={busy}
            onClick={() => onConnect(net)}
          >
            Connect
          </button>
        )}
        <button
          type="button"
          className={small}
          aria-label={`forget ${net.ssid}`}
          disabled={busy}
          onClick={() => onForget(net)}
        >
          Forget
        </button>
      </div>
    </li>
  );
}

function ScanList({
  scan,
  scannedAt,
  selected,
  disabled,
  onPick,
}: {
  scan: WifiScanEntry[];
  scannedAt: number | null;
  selected: string;
  disabled: boolean;
  onPick: (entry: WifiScanEntry) => void;
}) {
  if (scan.length === 0) {
    return (
      <p className={cn(placeholderClass, "text-[0.82rem]")}>
        {scannedAt === null ? "Not scanned yet." : "No networks seen in the last scan."}
      </p>
    );
  }
  return (
    <ul className="m-0 flex list-none flex-col gap-1 p-0">
      {scan.map((entry) => (
        <li key={`${entry.ssid}/${entry.security}`}>
          <button
            type="button"
            className={cn(
              buttonClass,
              "flex w-full flex-wrap items-baseline gap-x-2 text-left text-[0.88rem]",
              entry.ssid === selected && "border-accent",
            )}
            aria-pressed={entry.ssid === selected}
            aria-label={`choose ${entry.ssid}`}
            disabled={disabled}
            onClick={() => onPick(entry)}
          >
            <span className="min-w-0 break-all">{entry.ssid}</span>
            <span className="text-xs text-fg-faint">signal {Math.round(entry.signal)}%</span>
            <span className="text-xs text-fg-faint">{entry.security || "open"}</span>
            {entry.saved && <span className={cn("text-xs", toneClass("ok"))}>saved</span>}
          </button>
        </li>
      ))}
    </ul>
  );
}

/** Saved WiFi networks on System → Network (N5.4): the Pi's NetworkManager
 *  profiles in priority order, and a form to add one. All state is the Pi's;
 *  this only asks (`/control/network/wifi`). */
export function WifiNetworksCard({
  token,
  network,
  host = window.location.hostname,
}: WifiNetworksCardProps) {
  const fetcher = useCallback((signal: AbortSignal) => getWifi(token, signal), [token]);
  const { data, error } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 15000,
    describe: describeWifiError,
  });
  // The last answer wins: a mutation's own reply holds until the next poll
  // brings a newer one.
  const [reply, setReply] = useState<{ base: unknown; wifi: WifiState } | null>(null);
  const wifi = reply && reply.base === data ? reply.wifi : (data?.wifi ?? null);
  const setWifi = (next: WifiState) => setReply({ base: data, wifi: next });

  const { notice, setNotice, busy, run } = useAsyncAction();
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [hidden, setHidden] = useState(false);

  const onWifi = reachingThroughWifi(network, host);
  const activeSsid = wifi?.networks.find((n) => n.active)?.ssid ?? null;

  /** Run a mutation, turning its failure into the card's own sentence. */
  const act = (
    label: string,
    fn: () => Promise<string>,
    describe: (err: unknown) => string,
    droppedText?: string,
  ) =>
    run(
      label,
      async () => {
        try {
          setNotice({ kind: "ok", text: await fn() });
        } catch (err) {
          if (droppedText && err instanceof NetworkError) {
            setNotice({ kind: "ok", text: droppedText });
          } else {
            setNotice({ kind: "error", text: describe(err) });
          }
        }
      },
      { okText: null },
    );

  const scanned = wifi?.scan.find((e) => e.ssid === ssid.trim());
  const openChoice = !hidden && scanned !== undefined && scanned.security === "";

  const pick = (entry: WifiScanEntry) => {
    setSsid(entry.ssid);
    setHidden(false);
    setPassword("");
  };

  const rescan = () =>
    act(
      "scan",
      async () => {
        setWifi((await scanWifi(token)).wifi);
        return "Scan finished.";
      },
      describeWifiError,
    );

  const add = (join: boolean) => {
    const name = ssid.trim();
    if (!name) return;
    if (join && onWifi && !window.confirm(dropWarning(name))) return;
    const secret = openChoice || password === "" ? null : password;
    // Never keep the passphrase past the submit, whatever the answer.
    setPassword("");
    setShowPassword(false);
    return act(
      join ? "join" : "save",
      async () => {
        const result = await addWifi(token, { ssid: name, password: secret, hidden, join });
        setWifi(result.wifi);
        setSsid("");
        setHidden(false);
        return join ? `Joined ${name}.` : `Saved ${name} for later.`;
      },
      (err) =>
        err instanceof HttpError && err.status === 409
          ? join
            ? `The Pi could not join ${name}: ${serverDetail(err)}. Nothing was saved.`
            : `The Pi refused to save ${name}: ${serverDetail(err)}.`
          : describeWifiError(err),
      join && onWifi ? droppedNotice(name) : undefined,
    );
  };

  const connect = (net: WifiNetwork) => {
    if (onWifi && !window.confirm(dropWarning(net.ssid))) return;
    return act(
      `connect:${net.id}`,
      async () => {
        setWifi((await connectWifi(token, net.id)).wifi);
        return `Connected to ${net.ssid}.`;
      },
      (err) =>
        err instanceof HttpError && err.status === 409
          ? `The Pi could not switch to ${net.ssid}: ${serverDetail(err)}.`
          : describeWifiError(err),
      onWifi ? droppedNotice(net.ssid) : undefined,
    );
  };

  const forget = (net: WifiNetwork) => {
    const dropping = onWifi && net.active;
    const text = dropping
      ? `Forget ${net.ssid}? ${dropWarning(net.ssid, true)}`
      : `Forget ${net.ssid}? The Pi will no longer join it${net.active ? ", and is connected to it now" : ""}.`;
    if (!window.confirm(text)) return;
    return act(
      `forget:${net.id}`,
      async () => {
        setWifi((await forgetWifi(token, net.id)).wifi);
        return `Forgot ${net.ssid}.`;
      },
      describeWifiError,
      dropping ? droppedNotice(net.ssid) : undefined,
    );
  };

  const move = (index: number, delta: -1 | 1) => {
    if (!wifi) return;
    const ids = wifi.networks.map((n) => n.id);
    const to = index + delta;
    if (to < 0 || to >= ids.length) return;
    [ids[index], ids[to]] = [ids[to], ids[index]];
    return act(
      "order",
      async () => {
        setWifi((await orderWifi(token, ids)).wifi);
        return "Order saved.";
      },
      describeWifiError,
    );
  };

  const working = busy !== null;
  const progress =
    busy === "join"
      ? "Joining… the Pi is connecting and checking it, which can take up to 45 seconds."
      : busy?.startsWith("connect:")
        ? "Switching… this can take up to 30 seconds."
        : busy === "scan"
          ? "Scanning…"
          : null;

  return (
    <section className="mt-4 border-t border-border pt-3" aria-label="Saved WiFi networks">
      <h3 className={cn(definitionTermClass, "m-0 mb-2")}>Saved WiFi networks</h3>
      {!token ? (
        <TokenRequired purpose="manage WiFi networks" />
      ) : (
        <>
          <Notice notice={notice} className="mb-2" />
          {progress && (
            <p role="status" className="m-0 mb-2 text-[0.85rem]">
              {progress}
            </p>
          )}
          {wifi === null ? (
            <p className={placeholderClass}>{error ?? "loading saved networks…"}</p>
          ) : (
            <>
              {error && <p className={cn("mb-2 text-[0.82rem]", toneClass("warn"))}>{error}</p>}
              {wifi.networks.length === 0 ? (
                <p className={placeholderClass}>No saved WiFi networks.</p>
              ) : (
                <ul className="m-0 flex list-none flex-col gap-2 p-0" aria-label="Saved networks">
                  {wifi.networks.map((net, index) => (
                    <SavedRow
                      key={net.id}
                      net={net}
                      index={index}
                      count={wifi.networks.length}
                      busy={working}
                      onMove={move}
                      onConnect={connect}
                      onForget={forget}
                    />
                  ))}
                </ul>
              )}
              <p className={cn(placeholderClass, "mt-1 text-[0.78rem]")}>
                The Pi joins the first one in range. Order decides which wins when several are here.
                {activeSsid ? "" : " It is not connected to a saved network now."}
              </p>

              <h4 className={cn(definitionTermClass, "m-0 mt-4 mb-2")}>Add a network</h4>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <button type="button" className={buttonClass} disabled={working} onClick={rescan}>
                  Rescan
                </button>
                {wifi.scanned_at !== null && (
                  <span className="text-xs text-fg-faint">
                    scanned {new Date(wifi.scanned_at * 1000).toLocaleTimeString()}
                  </span>
                )}
              </div>
              <ScanList
                scan={wifi.scan}
                scannedAt={wifi.scanned_at}
                selected={ssid.trim()}
                disabled={working}
                onPick={pick}
              />
              <form
                className="mt-3 flex flex-col gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                }}
                autoComplete="off"
              >
                <label className="flex flex-col gap-1 text-[0.85rem]">
                  Network name
                  <input
                    className={cn(inputClass, "w-full")}
                    value={ssid}
                    maxLength={32}
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    onChange={(e) => setSsid(e.target.value)}
                    placeholder="pick one above or type a name"
                    disabled={working}
                  />
                </label>
                <label className="flex items-center gap-2 text-[0.85rem]">
                  <input
                    type="checkbox"
                    checked={hidden}
                    disabled={working}
                    onChange={(e) => setHidden(e.target.checked)}
                  />
                  Hidden network
                </label>
                {openChoice ? (
                  <p className={cn(placeholderClass, "text-[0.82rem]")}>
                    Open network: no passphrase.
                  </p>
                ) : (
                  <div className="flex flex-col gap-1 text-[0.85rem]">
                    <label htmlFor="wifi-passphrase">Passphrase</label>
                    <div className="flex items-center gap-2">
                      <input
                        id="wifi-passphrase"
                        className={cn(inputClass, "min-w-0 flex-1")}
                        type={showPassword ? "text" : "password"}
                        value={password}
                        autoComplete="new-password"
                        autoCapitalize="none"
                        autoCorrect="off"
                        spellCheck={false}
                        onChange={(e) => setPassword(e.target.value)}
                        disabled={working}
                      />
                      <button
                        type="button"
                        className={cn(buttonClass, "text-xs")}
                        aria-pressed={showPassword}
                        onClick={() => setShowPassword((v) => !v)}
                      >
                        {showPassword ? "Hide" : "Show"}
                      </button>
                    </div>
                    <span className="text-xs text-fg-faint">Leave empty for an open network.</span>
                  </div>
                )}
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className={buttonClass}
                    disabled={working || ssid.trim() === ""}
                    onClick={() => add(true)}
                  >
                    Join now
                  </button>
                  <button
                    type="button"
                    className={buttonClass}
                    disabled={working || ssid.trim() === ""}
                    onClick={() => add(false)}
                  >
                    Save for later
                  </button>
                </div>
              </form>
            </>
          )}
        </>
      )}
    </section>
  );
}

/** What a dropped request means when the page was on the WiFi being left. */
function droppedNotice(ssid: string): string {
  return (
    `The connection dropped, as expected: the Pi is moving to ${ssid}. ` +
    `Reach it again at its new address or over the cable at ${CABLE_ADDRESS}.`
  );
}
