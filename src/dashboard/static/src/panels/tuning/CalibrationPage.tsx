import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { controlJson } from "../../api/core";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { buttonClass, inputClass } from "../../styles";
import type { StatusResponse } from "../../types";

type MemoryState = "verified" | "checked earlier" | "carried" | "unverifiable";
interface MemoryChannel {
  name: string;
  address: string;
  window: string | null;
  unit: string;
  verified_tune_revision: string;
  revision_carried_reason: string | null;
  known_answer: { relation: string; did: number; notes?: string | null } | null;
  state: MemoryState;
}
interface CalibrationData {
  running_revision: string | null;
  registry_calibration_id: string | null;
  ecu_calibration_id: string | null;
  ecu_calibration_read_at: string | null;
  calibration_matches_registry: boolean | null;
  calibration_changed: boolean;
  history: Array<{
    revision: string;
    flashed_at: string;
    calibration_id: string | null;
    k: number | null;
    rom_file: string | null;
    rom_sha256: string | null;
    notes: string;
  }>;
}
interface MemoryData {
  running_revision: string | null;
  count: number;
  channels: MemoryChannel[];
}
interface VerifyData {
  verdicts: Array<{
    channel: string;
    outcome: string;
    reason: string;
    samples: number;
    agreements: number;
  }>;
  counts: Record<string, number>;
  recorded: number;
  notes: string[];
}

const stateClass: Record<MemoryState, string> = {
  verified: "text-emerald-400",
  "checked earlier": "text-amber-300",
  carried: "text-sky-300",
  unverifiable: "text-fg-faint",
};

export function CalibrationPage({
  token,
  status,
}: {
  token: string;
  status: StatusResponse | null;
}) {
  const [calibration, setCalibration] = useState<CalibrationData | null>(null);
  const [memory, setMemory] = useState<MemoryData | null>(null);
  const [verification, setVerification] = useState<VerifyData | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [carryReason, setCarryReason] = useState("");

  const refresh = useCallback(async () => {
    if (!token) return;
    const [nextCalibration, nextMemory] = await Promise.all([
      controlJson<CalibrationData>("/control/calibration", token),
      controlJson<MemoryData>("/control/memory-channels", token),
    ]);
    setCalibration(nextCalibration);
    setMemory(nextMemory);
  }, [token]);
  useEffect(() => {
    refresh().catch((reason: unknown) => setError(String(reason)));
  }, [refresh]);

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

  async function verify(record: boolean) {
    setBusy(record ? "Recording verification" : "Re-verifying memory channels");
    setError("");
    try {
      const data = await controlJson<VerifyData>("/control/memory-channels/verify", token, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          record_revision: record ? calibration?.running_revision : null,
          carry_reason: record && hasUnverifiable ? carryReason || null : null,
        }),
      });
      setVerification(data);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  }

  async function recordFlash(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy("Recording flash");
    setError("");
    try {
      await controlJson("/control/calibration", token, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          revision: form.get("revision"),
          flashed_at: form.get("flashed_at"),
          calibration_id: form.get("calibration_id") || null,
          k: form.get("k") ? Number(form.get("k")) : null,
          rom_file: form.get("rom_file") || null,
          rom_sha256: form.get("rom_sha256") || null,
          notes: form.get("notes") || "",
        }),
      });
      event.currentTarget.reset();
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy("");
    }
  }

  const mismatch =
    calibration?.calibration_matches_registry === false || calibration?.calibration_changed;
  return (
    <section className="space-y-4">
      <Panel title="Running calibration" count={calibration?.running_revision ?? "unknown"}>
        {!token && <p>Enter the control token to inspect calibration evidence.</p>}
        {mismatch && (
          <Notice
            notice={{
              kind: "error",
              text: "The ECU reports a calibration the flash history does not contain — record the flash before trusting any RAM address.",
            }}
          />
        )}
        {error && <Notice notice={{ kind: "error", text: error }} />}
        {busy && <Notice notice={{ kind: "busy", text: busy }} />}
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
                <div className="text-xs text-fg-muted">
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
            <section key={window} className="mb-4 overflow-x-auto">
              <h3 className="mb-1 font-semibold">{window}</h3>
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
                          : "No known-answer relation"}
                      </td>
                      <td
                        className={stateClass[channel.state]}
                        title={channel.revision_carried_reason ?? channel.known_answer?.notes ?? ""}
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
          <p className="text-sm text-amber-300">
            Verification is deferred while {status?.capture_profile?.name} is recording.
          </p>
        )}
        {status?.vehicle_state?.state === "engine_off" && (
          <p className="text-xs text-fg-muted">
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
                    ? "text-red-400"
                    : item.outcome === "pass"
                      ? "text-emerald-400"
                      : "text-fg-muted"
                }
              >
                {item.channel}: {item.outcome} — {item.reason}
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <Panel title="Record a flash">
        <form className="grid grid-cols-2 gap-3 max-[700px]:grid-cols-1" onSubmit={recordFlash}>
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
