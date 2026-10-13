import { useCallback, useEffect, useRef, useState } from "react";

import { getNetwork, setProvisioningWindow } from "../api/http";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useControlResource } from "../hooks/useControlResource";
import {
  buttonClass,
  cn,
  definitionListClass,
  definitionRowClass,
  definitionTermClass,
  definitionValueClass,
  inputClass,
  placeholderClass,
} from "../styles";
import type { StatusResponse } from "../types";
import type { NetworkResponse, NetworkSummary } from "../types/network";
import { toneClass } from "../util/format";
import { formatDuration } from "../util/time";

interface NetworkPanelProps {
  token: string;
  /** `/status.network` — public, so a phone without the token still learns
   *  the address to bookmark. */
  status: StatusResponse | null;
}

/** The window a phone gets to change WiFi from this page, in minutes. */
export const WINDOW_MINUTES = 5;

const MODE_LABELS: Record<string, string> = {
  client: "joined",
  ap: "access point",
  disconnected: "not connected",
  "no-wifi": "no WiFi adapter",
  unknown: "unknown",
};

/** The server's remaining seconds, stamped with when they arrived, so the
 *  countdown ticks between polls. */
type Stamped = NetworkResponse & { receivedAt: number };

function useCountdown(data: Stamped | null): number {
  const open = (data?.provisioning.window_remaining_s ?? 0) > 0;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!open) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [open]);
  if (!data) return 0;
  const elapsed = Math.max(0, now - data.receivedAt) / 1000;
  return Math.max(0, data.provisioning.window_remaining_s - elapsed);
}

function AddressRows({ network }: { network: NetworkSummary }) {
  const names = Object.keys(network.addresses).sort();
  if (names.length === 0) {
    return (
      <div className={definitionRowClass}>
        <dt className={definitionTermClass}>Addresses</dt>
        <dd className={definitionValueClass}>none</dd>
      </div>
    );
  }
  return (
    <>
      {names.map((name) => (
        <div key={name} className={definitionRowClass}>
          <dt className={definitionTermClass}>
            {name}
            {name === network.wifi_interface ? " (WiFi)" : ""}
          </dt>
          <dd className={cn(definitionValueClass, "min-w-0 text-right break-all")}>
            {network.addresses[name].map((address) => (
              <div key={address}>{address}</div>
            ))}
          </dd>
        </div>
      ))}
    </>
  );
}

function DashboardLink({ url }: { url: string }) {
  const field = useRef<HTMLInputElement>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const onCopy = async () => {
    // The Clipboard API needs a secure context, which http://<ip> is not on a
    // phone: fall back to selecting the text for a long-press copy.
    try {
      if (!navigator.clipboard) throw new Error("no clipboard");
      await navigator.clipboard.writeText(url);
      setCopied("Copied.");
    } catch {
      field.current?.select();
      setCopied("Selected — long-press to copy.");
    }
  };

  return (
    <div className="mt-3 flex flex-col gap-1.5">
      <span className={definitionTermClass}>Dashboard address</span>
      <a href={url} className="font-num text-[1.05rem] break-all text-accent underline">
        {url}
      </a>
      <div className="flex flex-wrap items-center gap-2">
        <input
          ref={field}
          readOnly
          value={url}
          aria-label="Dashboard address"
          className={cn(inputClass, "min-w-0 flex-1")}
          onFocus={(e) => e.currentTarget.select()}
        />
        <button type="button" className={cn(buttonClass, "text-xs")} onClick={onCopy}>
          Copy
        </button>
      </div>
      {copied && <span className={cn(placeholderClass, "text-[0.72rem]")}>{copied}</span>}
      <p className={cn(placeholderClass, "text-[0.78rem]")}>
        Bookmark this — Android cannot open canary.local.
      </p>
    </div>
  );
}

function NetworkFacts({ network }: { network: NetworkSummary }) {
  return (
    <>
      {network.error && (
        <p className={cn("mb-2 text-[0.82rem]", toneClass("warn"))}>{network.error}</p>
      )}
      <dl className={definitionListClass}>
        <div className={definitionRowClass}>
          <dt className={definitionTermClass}>WiFi</dt>
          <dd className={cn(definitionValueClass, "min-w-0 text-right break-all")}>
            {network.ssid ?? MODE_LABELS[network.mode ?? "unknown"] ?? network.mode}
          </dd>
        </div>
        <div className={definitionRowClass}>
          <dt className={definitionTermClass}>Mode</dt>
          <dd className={definitionValueClass}>
            {MODE_LABELS[network.mode ?? "unknown"] ?? network.mode}
          </dd>
        </div>
        {network.hostname && (
          <div className={definitionRowClass}>
            <dt className={definitionTermClass}>Hostname</dt>
            <dd className={definitionValueClass}>{network.hostname}</dd>
          </div>
        )}
        <AddressRows network={network} />
      </dl>
      {network.dashboard_url && <DashboardLink url={network.dashboard_url} />}
    </>
  );
}

/** System → Network (phone-ui P2.3): what the Pi joined, the address to
 *  bookmark, and the BLE provisioning window. */
export function NetworkPanel({ token, status }: NetworkPanelProps) {
  const fetcher = useCallback(
    async (signal: AbortSignal): Promise<Stamped> => ({
      ...(await getNetwork(token, signal)),
      receivedAt: Date.now(),
    }),
    [token],
  );
  const { data, error, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 5000,
  });
  const remaining = useCountdown(data);
  const { notice, busy, run } = useAsyncAction();
  const network = data?.network ?? status?.network ?? null;

  const setWindow = (minutes: number, okText: string) =>
    run(
      minutes > 0 ? "open window" : "close window",
      async () => {
        await setProvisioningWindow(token, minutes);
        refresh();
      },
      { okText },
    );

  return (
    <Panel title="Network">
      <Notice notice={notice} className="mb-2" />
      {network ? (
        <NetworkFacts network={network} />
      ) : (
        <p className={placeholderClass}>
          {token ? (error ?? "loading network…") : "No network reading yet."}
        </p>
      )}

      <section className="mt-4 border-t border-border pt-3">
        <h3 className={cn(definitionTermClass, "m-0 mb-2")}>Change WiFi from a phone</h3>
        {token ? (
          <>
            <p className={cn("m-0 mb-2 text-[0.88rem]", remaining > 0 && toneClass("ok"))}>
              {remaining > 0
                ? `Open — ${formatDuration(remaining, { collapseZero: true })} left`
                : "Closed"}
            </p>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className={buttonClass}
                disabled={busy !== null}
                onClick={() =>
                  setWindow(WINDOW_MINUTES, "A phone in Bluetooth range may change WiFi now.")
                }
              >
                Let a phone change WiFi ({WINDOW_MINUTES} min)
              </button>
              {remaining > 0 && (
                <button
                  type="button"
                  className={buttonClass}
                  disabled={busy !== null}
                  onClick={() => setWindow(0, "Window closed.")}
                >
                  Close now
                </button>
              )}
            </div>
          </>
        ) : (
          <p className={placeholderClass}>Enter the control token to open the window.</p>
        )}
        <p className={cn(placeholderClass, "mt-2 text-[0.78rem]")}>
          While it is open, a phone in Bluetooth range can give the Pi a new WiFi network (Android
          Chrome on improv-wifi.com). Turning the key ACC → ON → ACC with no pairing request waiting
          also opens it for 5 minutes.
        </p>
      </section>
    </Panel>
  );
}
