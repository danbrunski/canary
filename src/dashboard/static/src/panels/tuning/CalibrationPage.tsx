import { FormEvent, useCallback, useMemo, useState } from "react";
import { TableScroll } from "../../components/TableScroll";

import {
  getCalibration,
  getMemoryChannels,
  recordFlash,
  verifyMemoryChannels,
} from "../../api/workbench";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { useAsyncAction } from "../../hooks/useAsyncAction";
import { useControlResource } from "../../hooks/useControlResource";
import { buttonClass, inputClass } from "../../styles";
import type { StatusResponse } from "../../types";
import type { MemoryChannel, MemoryState, VerifyMemoryResponse } from "../../types/workbench";
import { TokenRequired } from "../../components/TokenRequired";

const stateClass: Record<MemoryState, string> = {
  verified: "text-ok",
  "checked earlier": "text-warn",
  carried: "text-accent-2",
  unverifiable: "text-fg-faint",
};

export function CalibrationPage({
  token,
  status,
}: {
  token: string;
  status: StatusResponse | null;
}) {
  const enabled = Boolean(token);
  const calibrationRes = useControlResource(
    useCallback((signal: AbortSignal) => getCalibration(token, signal), [token]),
    { enabled, intervalMs: null },
  );
  const memoryRes = useControlResource(
    useCallback((signal: AbortSignal) => getMemoryChannels(token, signal), [token]),
    { enabled, intervalMs: null },
  );
  const calibration = calibrationRes.data;
  const memory = memoryRes.data;
  const loadError = calibrationRes.error ?? memoryRes.error;
  const [verification, setVerification] = useState<VerifyMemoryResponse | null>(null);
  const { notice, busy, run } = useAsyncAction();
  const [carryReason, setCarryReason] = useState("");

  const refresh = () => {
    calibrationRes.refresh();
    memoryRes.refresh();
  };

  const groups = useMemo(() => {
    const result = new Map<string, MemoryChannel[]>();
    for (const channel of memory?.channels ?? []) {
      const key = channel.window ?? "individual reads";
      result.set(key, [...(result.get(key) ?? []), channel]);
    }
    return [...result.entries()];
  }, [memory]);
  const hasUnverifiable =
    memory?.channels.some((channel) => channel.state === "unverifiable") ?? false;
  const captureActive = status?.capture_profile?.active ?? false;

  function verify(record: boolean) {
    return run(
      record ? "Recording verification" : "Re-verifying memory channels",
      async () => {
        setVerification(
          await verifyMemoryChannels(token, {
            record_revision: record ? (calibration?.running_revision ?? null) : null,
            carry_reason: record && hasUnverifiable ? carryReason || null : null,
          }),
        );
      },
      { then: refresh, okText: null },
    );
  }

  function submitFlash(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    const text = (name: string) => {
      const value = form.get(name);
      return typeof value === "string" ? value : "";
    };
    return run(
      "Recording flash",
      async () => {
        await recordFlash(token, {
          revision: text("revision"),
          flashed_at: text("flashed_at"),
          calibration_id: text("calibration_id") || null,
          k: text("k") ? Number(text("k")) : null,
          rom_file: text("rom_file") || null,
          rom_sha256: text("rom_sha256") || null,
          notes: text("notes"),
        });
        formElement.reset();
      },
      { then: refresh, okText: null },
    );
  }

  const mismatch =
    calibration?.calibration_matches_registry === false || calibration?.calibration_changed;
  return (
    <section className="space-y-4">
      <Panel title="Running calibration" count={calibration?.running_revision ?? "unknown"}>
        {!token && <TokenRequired purpose="inspect calibration evidence" />}
        {mismatch && (
          <Notice
            notice={{
              kind: "error",
              text: "The ECU reports a calibration the flash history does not contain — record the flash before trusting any RAM address.",
            }}
          />
        )}
        {loadError && <Notice notice={{ kind: "error", text: loadError }} />}
        {busy ? <Notice notice={{ kind: "busy", text: busy }} /> : <Notice notice={notice} />}
        <div className="grid gap-1 text-sm">
          <span>ECM: {calibration?.ecu_calibration_id ?? "not read yet"}</span>
          <span>
            Registry: {calibration?.registry_calibration_id ?? "no flash history for this vehicle"}
          </span>
          <span>Read at: {calibration?.ecu_calibration_read_at ?? "not reported"}</span>
        </div>
      </Panel>

      <Panel title="Flash history" count={calibration?.history.length ?? 0}>
        {!calibration?.history.length ? (
          <p>No flash history for this vehicle.</p>
        ) : (
          <ol className="space-y-3">
            {calibration.history.map((item) => (
              <li key={item.revision} className="rounded border border-border p-3">
                <div className="font-semibold">
                  {item.revision}
                  {item.revision === calibration.running_revision ? " · running" : ""}
                </div>
                <div className="text-xs text-fg-dim">
                  {new Date(item.flashed_at).toLocaleString()} · K {item.k ?? "unknown"} ·{" "}
                  {item.rom_file ?? "ROM file unrecorded"}
                </div>
                {item.notes && <p className="mt-1 whitespace-pre-wrap text-sm">{item.notes}</p>}
              </li>
            ))}
          </ol>
        )}
      </Panel>

      <Panel title="RAM address evidence" count={memory?.count ?? 0}>
        {!memory?.channels.length ? (
          <p>No RAM channels for this vehicle.</p>
        ) : (
          groups.map(([window, channels]) => (
            <section key={window} className="mb-4">
              <h3 className="mb-1 font-semibold">{window}</h3>
              <TableScroll>
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr>
                      <th>Channel</th>
                      <th>Address</th>
                      <th>Evidence</th>
                      <th>State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {channels.map((channel) => (
                      <tr key={channel.name} className="border-t border-border">
                        <td className="py-2">{channel.name}</td>
                        <td className="font-mono">{channel.address}</td>
                        <td>
                          {channel.known_answer
                            ? `${channel.known_answer.relation} vs 0x${channel.known_answer.did.toString(16).toUpperCase()}`
                            : channel.relations?.length
                              ? channel.relations
                                  .map((relation) =>
                                    relation.owner === channel.name
                                      ? relation.kind
                                      : `${relation.kind} (via ${relation.owner})`,
                                  )
                                  .join(", ")
                              : "No known-answer relation"}
                        </td>
                        <td
                          className={stateClass[channel.state]}
                          title={
                            channel.revision_carried_reason ?? channel.known_answer?.notes ?? ""
                          }
                        >
                          {channel.state}
                          {channel.revision_carried_reason
                            ? ` — ${channel.revision_carried_reason}`
                            : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableScroll>
            </section>
          ))
        )}
        <div className="flex flex-wrap items-end gap-2">
          {hasUnverifiable && (
            <label className="flex min-w-72 flex-1 flex-col gap-1 text-sm">
              Reason to carry unverifiable window members
              <input
                className={inputClass}
                value={carryReason}
                onChange={(event) => setCarryReason(event.target.value)}
              />
            </label>
          )}
          <button
            className={buttonClass}
            disabled={!token || !!busy || captureActive}
            onClick={() => void verify(false)}
          >
            Re-verify
          </button>
          <button
            className={buttonClass}
            disabled={
              !token ||
              !!busy ||
              captureActive ||
              !calibration?.running_revision ||
              (hasUnverifiable && !carryReason.trim())
            }
            onClick={() => void verify(true)}
          >
            Record for {calibration?.running_revision ?? "running revision"}
          </button>
        </div>
        {captureActive && (
          <p className="text-sm text-warn">
            Verification is deferred while {status?.capture_profile?.name} is recording.
          </p>
        )}
        {status?.vehicle_state?.state === "engine_off" && (
          <p className="text-xs text-fg-dim">
            Engine-off verification is allowed, but fast-moving relations cannot distinguish a moved
            address while nothing is moving.
          </p>
        )}
        {verification && (
          <ul className="mt-3 space-y-1 text-sm">
            {verification.verdicts.map((item) => (
              <li
                key={item.channel}
                className={
                  item.outcome === "fail"
                    ? "text-bad"
                    : item.outcome === "pass"
                      ? "text-ok"
                      : "text-fg-dim"
                }
              >
                {item.channel}: {item.outcome} — {item.reason}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Record a flash">
        <form className="grid grid-cols-2 gap-3 max-[700px]:grid-cols-1" onSubmit={submitFlash}>
          <input className={inputClass} name="revision" placeholder="Revision name" required />
          <input className={inputClass} name="flashed_at" type="datetime-local" required />
          <input className={inputClass} name="calibration_id" placeholder="ECU calibration id" />
          <input
            className={inputClass}
            name="k"
            type="number"
            step="any"
            placeholder="Injector K"
          />
          <input className={inputClass} name="rom_file" placeholder="ROM filename" />
          <input
            className={inputClass}
            name="rom_sha256"
            pattern="[0-9a-f]{64}"
            placeholder="ROM SHA-256"
          />
          <textarea
            className={inputClass}
            name="notes"
            placeholder="Notes, including approximate-boundary caveats"
          />
          <button className={buttonClass} disabled={!token || !!busy}>
            Record flash
          </button>
        </form>
      </Panel>
    </section>
  );
}
