import { useCallback, useState } from "react";

import {
  approvePairingRequest,
  getDeviceMe,
  listPairedDevices,
  listPairingRequests,
  removePairedDevice,
  renamePairedDevice,
  type DeviceMe,
  type PairedDevice,
  type PendingPairingRequest,
} from "../api/pairedDevices";
import { Notice } from "../components/Notice";
import { Panel } from "../components/Panel";
import { ResourceGate } from "../components/ResourceGate";
import { useAsyncAction } from "../hooks/useAsyncAction";
import { useControlResource } from "../hooks/useControlResource";
import { formatWhen, methodLabel } from "../lib/pairedDevices";
import {
  badgeClass,
  buttonClass,
  cn,
  dangerButtonClass,
  inputClass,
  placeholderClass,
} from "../styles";

interface PairedDevicesPanelProps {
  token: string;
}

interface Snapshot {
  devices: PairedDevice[];
  me: DeviceMe;
  requests: PendingPairingRequest[];
}

async function fetchSnapshot(token: string, signal: AbortSignal): Promise<Snapshot> {
  const [devices, me, requests] = await Promise.all([
    listPairedDevices(token, signal),
    getDeviceMe(token, signal),
    listPairingRequests(token, signal),
  ]);
  return { devices: devices.devices, me, requests: requests.requests };
}

const smallButton = cn(buttonClass, "text-xs");
const sectionTitle = "mb-2 mt-0 text-[0.72rem] uppercase tracking-[0.08em] text-fg-faint";
const rowClass = "flex flex-col gap-1.5 border-t border-border-soft py-2.5 first:border-t-0";

type Act = (label: string, action: () => Promise<unknown>, okText: string) => void;

function WaitingList({
  requests,
  busy,
  act,
  token,
}: {
  requests: PendingPairingRequest[];
  busy: boolean;
  act: Act;
  token: string;
}) {
  return (
    <section className="mb-4">
      <h3 className={sectionTitle}>Waiting to pair</h3>
      {requests.length === 0 ? (
        <p className={cn(placeholderClass, "text-[0.86rem]")}>
          No device is waiting. On the new device, open the dashboard and tap “Pair this device”.
        </p>
      ) : (
        <ul className="m-0 list-none p-0">
          {requests.map((r) => (
            <li key={r.id} className={cn(rowClass, "flex-row flex-wrap items-center")}>
              <span className="min-w-0 flex-1 break-words font-semibold">{r.name}</span>
              <span className="text-[0.78rem] text-fg-faint">
                asked {Math.round(r.age_s)} s ago
              </span>
              <button
                type="button"
                className={smallButton}
                disabled={busy}
                aria-label={`Approve ${r.name}`}
                onClick={() =>
                  act(
                    "approve",
                    () => approvePairingRequest(token, r.id),
                    `Approved ${r.name}. It finishes pairing on its own.`,
                  )
                }
              >
                Approve
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function DeviceRow({
  device,
  devices,
  isMe,
  busy,
  act,
  token,
}: {
  device: PairedDevice;
  devices: PairedDevice[];
  isMe: boolean;
  busy: boolean;
  act: Act;
  token: string;
}) {
  const [mode, setMode] = useState<"view" | "rename" | "remove">("view");
  const [name, setName] = useState(device.name);
  const clean = name.trim();

  return (
    <li className={rowClass}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="min-w-0 break-words font-semibold">{device.name}</span>
        {isMe && <span className={cn(badgeClass, "text-accent")}>this device</span>}
      </div>
      <div className="text-[0.78rem] text-fg-dim">
        {methodLabel(device.method, devices)} · paired {formatWhen(device.paired_at)} · last seen{" "}
        {formatWhen(device.last_seen)}
      </div>
      {mode === "view" && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={smallButton}
            disabled={busy}
            onClick={() => {
              setName(device.name);
              setMode("rename");
            }}
          >
            Rename
          </button>
          <button
            type="button"
            className={cn(dangerButtonClass, "text-xs")}
            disabled={busy}
            onClick={() => setMode("remove")}
          >
            Remove
          </button>
        </div>
      )}
      {mode === "rename" && (
        <form
          className="flex flex-wrap items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (!clean) return;
            setMode("view");
            act(
              "rename",
              () => renamePairedDevice(token, device.id, clean),
              `Renamed to ${clean}.`,
            );
          }}
        >
          <input
            className={cn(inputClass, "min-w-0 flex-1 basis-40")}
            value={name}
            maxLength={64}
            aria-label={`New name for ${device.name}`}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" className={smallButton} disabled={busy || !clean}>
            Save
          </button>
          <button type="button" className={smallButton} onClick={() => setMode("view")}>
            Cancel
          </button>
        </form>
      )}
      {mode === "remove" && (
        <div className="flex flex-wrap items-center gap-2" role="alert">
          <span className="min-w-0 flex-1 basis-48 text-[0.86rem] text-bad">
            {isMe
              ? "This is the device you are using. It loses access until it pairs again."
              : `Remove ${device.name}? It loses access until it pairs again.`}
          </span>
          <button
            type="button"
            className={cn(dangerButtonClass, "text-xs")}
            disabled={busy}
            onClick={() => {
              setMode("view");
              act("remove", () => removePairedDevice(token, device.id), `Removed ${device.name}.`);
            }}
          >
            Yes, remove
          </button>
          <button type="button" className={smallButton} onClick={() => setMode("view")}>
            Keep
          </button>
        </div>
      )}
    </li>
  );
}

/** System → Paired devices: who holds a token of their own, and who is
 *  waiting for one (docs/roadmap/phone-ui.md P2.4). */
export function PairedDevicesPanel({ token }: PairedDevicesPanelProps) {
  const fetcher = useCallback((signal: AbortSignal) => fetchSnapshot(token, signal), [token]);
  const { data, error, loading, refresh } = useControlResource(fetcher, {
    enabled: Boolean(token),
    intervalMs: 4000,
  });
  const { notice, busy, run } = useAsyncAction();
  const act: Act = (label, action, okText) =>
    void run(
      label,
      async () => {
        await action();
        refresh();
      },
      { okText },
    );

  return (
    <Panel title="Paired devices" count={data ? data.devices.length : undefined}>
      <ResourceGate
        enabled={Boolean(token)}
        enabledMessage="Pair this device, or enter the control token, to manage paired devices."
        loading={loading}
        loadingMessage="loading paired devices…"
        error={error}
        data={data}
      >
        {(snap) => (
          <>
            <Notice notice={notice} className="mb-3" />
            <WaitingList requests={snap.requests} busy={busy !== null} act={act} token={token} />
            <section>
              <h3 className={sectionTitle}>Paired</h3>
              {snap.me.shared_token && (
                <p className={cn(placeholderClass, "mb-2 text-[0.86rem]")}>
                  This browser uses the shared control token, not a paired device’s own.
                </p>
              )}
              {snap.devices.length === 0 ? (
                <p className={cn(placeholderClass, "text-[0.86rem]")}>No device is paired yet.</p>
              ) : (
                <ul className="m-0 list-none p-0">
                  {snap.devices.map((device) => (
                    <DeviceRow
                      key={device.id}
                      device={device}
                      devices={snap.devices}
                      isMe={snap.me.device?.id === device.id}
                      busy={busy !== null}
                      act={act}
                      token={token}
                    />
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </ResourceGate>
    </Panel>
  );
}
