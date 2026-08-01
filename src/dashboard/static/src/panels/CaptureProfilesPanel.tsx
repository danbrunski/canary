import { useCallback, useEffect, useMemo, useState } from "react";

import {
  deleteCaptureProfile,
  getCaptureProfiles,
  getDids,
  getObdCoverage,
  saveCaptureProfile,
  startProfileCapture,
  stopProfileCapture,
} from "../api/http";
import { Notice } from "../components/Notice";
import { useAsyncAction } from "../hooks/useAsyncAction";
import {
  buttonClass,
  dangerButtonClass,
  inputClass,
  panelClass,
  panelHeaderClass,
  panelTitleClass,
} from "../styles";
import type {
  CaptureProfile,
  CaptureProfileWarning,
  StatusResponse,
} from "../types";

/** `2026-07-31-1830` — a lowercase slug the backend name validator accepts,
 *  local time so it matches when the operator remembers running it. */
function timestampSlug(now = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, "0");
  return [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
    `${pad(now.getHours())}${pad(now.getMinutes())}`,
  ].join("-");
}

const blankProfile = (): CaptureProfile => {
  const name = timestampSlug();
  return {
    name,
    description: "",
    mode01: {},
    mode22: [],
    mode01_enabled: true,
    mode22_enabled: true,
    did_interval_seconds: null,
    bus_budget_override: null,
    broadcast_decode: true,
    corpus_eligible: false,
    storage_prefix: `_capture-${name}`,
  };
};

export function CaptureProfilesPanel({
  token,
  status,
}: {
  token: string;
  status: StatusResponse | null;
}) {
  const [profiles, setProfiles] = useState<CaptureProfile[]>([]);
  const [selectedName, setSelectedName] = useState("default");
  const [draft, setDraft] = useState<CaptureProfile>(blankProfile());
  const [originalName, setOriginalName] = useState<string | undefined>();
  const [pids, setPids] = useState<string[]>([]);
  const [dids, setDids] = useState<string[]>([]);
  const [warnings, setWarnings] = useState<CaptureProfileWarning[]>([]);
  const [tuneRevision, setTuneRevision] = useState("");
  const { notice, setNotice, busy, run } = useAsyncAction();

  const refresh = useCallback(async () => {
    if (!token) return;
    const [catalog, obd, didMap] = await Promise.all([
      getCaptureProfiles(token),
      getObdCoverage(),
      getDids(),
    ]);
    setProfiles(catalog.profiles);
    setPids(
      obd.rows
        .filter((row) => row.channel && row.status === "decoded")
        .map((row) => row.channel as string)
        .sort(),
    );
    setDids(didMap.dids.map((entry) => entry.name).sort());
  }, [token]);

  useEffect(() => {
    refresh().catch((error: unknown) =>
      setNotice({ kind: "error", text: error instanceof Error ? error.message : String(error) }),
    );
  }, [refresh, setNotice]);

  useEffect(() => {
    const selected = profiles.find((profile) => profile.name === selectedName);
    if (selected) {
      setDraft(structuredClone(selected));
      setOriginalName(selected.name);
      setWarnings([]);
    }
  }, [profiles, selectedName]);

  const active = status?.capture_profile;
  const mode01Set = useMemo(
    () => new Set("AUTO" in draft.mode01 ? pids : Object.keys(draft.mode01)),
    [draft.mode01, pids],
  );
  const mode22Set = useMemo(() => new Set(draft.mode22), [draft.mode22]);

  const togglePid = (channel: string) => {
    const next = "AUTO" in draft.mode01
      ? Object.fromEntries(pids.map((name) => [name, 1]))
      : { ...draft.mode01 };
    if (channel in next) delete next[channel];
    else next[channel] = 1;
    setDraft({ ...draft, mode01: next });
  };
  const toggleDid = (name: string) => {
    setDraft({
      ...draft,
      mode22: mode22Set.has(name)
        ? draft.mode22.filter((item) => item !== name)
        : [...draft.mode22, name],
    });
  };

  const save = (saveAs: boolean) =>
    run(saveAs ? "capture profile save-as" : "capture profile save", async () => {
      const result = await saveCaptureProfile(token, draft, saveAs ? undefined : originalName);
      setWarnings(result.warnings);
      setSelectedName(result.profile.name);
    }, { then: refresh });

  return (
    <section className={`${panelClass} flex flex-col gap-4`}>
      <header className={panelHeaderClass}>
        <h2 className={panelTitleClass}>Capture profiles</h2>
        <span className={active?.active ? "text-warn" : "text-fg-dim"}>
          {active?.active ? `active: ${active.name}` : "default collection"}
        </span>
      </header>
      <Notice notice={notice} />
      <Notice notice={busy ? { kind: "busy", text: `Working: ${busy}` } : null} />

      <div className="flex flex-wrap gap-2">
        <select className={inputClass} value={selectedName} onChange={(event) => setSelectedName(event.target.value)}>
          {profiles.map((profile) => <option key={profile.name}>{profile.name}</option>)}
        </select>
        <button className={buttonClass} type="button" onClick={() => {
          const next = blankProfile();
          setDraft(next);
          setOriginalName(undefined);
          setSelectedName("");
        }}>New</button>
        <input className={inputClass} value={tuneRevision} onChange={(event) => setTuneRevision(event.target.value)} placeholder="ROM / tune revision note" />
        {!active?.active ? (
          <button className={buttonClass} type="button" disabled={!token || draft.name === "default" || !!busy} onClick={() => run("capture start", async () => {
            await startProfileCapture(token, draft.name, tuneRevision || undefined);
          })}>Start</button>
        ) : (
          <button className={buttonClass} type="button" disabled={!token || !!busy} onClick={() => run("capture stop", async () => {
            await stopProfileCapture(token);
          })}>Stop</button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 max-[900px]:grid-cols-1">
        <label className="text-sm text-fg-dim">Name
          <input className={`${inputClass} mt-1 w-full`} value={draft.name} disabled={draft.name === "default"} onChange={(event) => {
            const name = event.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "");
            // Carry an auto-derived prefix along with the rename, so captures
            // don't land under the name the profile used to have. A prefix the
            // operator typed themselves is left alone.
            const derived = draft.storage_prefix === `_capture-${draft.name}`;
            setDraft({
              ...draft,
              name,
              storage_prefix: derived && !draft.corpus_eligible ? `_capture-${name}` : draft.storage_prefix,
            });
          }} />
        </label>
        <label className="text-sm text-fg-dim">Description
          <input className={`${inputClass} mt-1 w-full`} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
        </label>
        <label className="text-sm text-fg-dim">Bus budget (requests/s; blank = default)
          <input className={`${inputClass} mt-1 w-full`} type="number" min="2" max="200" value={draft.bus_budget_override ?? ""} onChange={(event) => setDraft({ ...draft, bus_budget_override: event.target.value ? Number(event.target.value) : null })} />
        </label>
        <label className="text-sm text-fg-dim">Mode 22 window interval (s; blank = daemon default)
          <input className={`${inputClass} mt-1 w-full`} type="number" min="0.1" step="0.1" disabled={!draft.mode22_enabled} value={draft.did_interval_seconds ?? ""} onChange={(event) => setDraft({ ...draft, did_interval_seconds: event.target.value ? Number(event.target.value) : null })} />
        </label>
        <label className="text-sm text-fg-dim">Storage prefix (must be <code>_capture-&lt;slug&gt;</code>)
          <input className={`${inputClass} mt-1 w-full`} value={draft.storage_prefix} disabled={draft.name === "default" || draft.corpus_eligible} onChange={(event) => setDraft({ ...draft, storage_prefix: event.target.value })} />
        </label>
      </div>

      <div className="flex flex-wrap gap-5 text-sm">
        <label><input type="checkbox" checked={draft.broadcast_decode} onChange={(event) => setDraft({ ...draft, broadcast_decode: event.target.checked })} /> decode broadcast CAN</label>
        <label><input type="checkbox" checked={draft.corpus_eligible} disabled={draft.name === "default"} onChange={(event) => setDraft({ ...draft, corpus_eligible: event.target.checked, storage_prefix: event.target.checked ? "" : `_capture-${draft.name}` })} /> eligible for training corpus</label>
      </div>

      <div className="grid grid-cols-2 gap-4 max-[900px]:grid-cols-1">
        <ChannelPicker title="OBD PIDs (Mode 01)" hint="Standard SAE J1979 parameters." items={pids} selected={mode01Set} onToggle={togglePid} weights={draft.mode01} onWeight={(channel, weight) => setDraft({ ...draft, mode01: { ...draft.mode01, [channel]: weight } })} enabled={draft.mode01_enabled} onEnabled={(value) => setDraft({ ...draft, mode01_enabled: value })} />
        <ChannelPicker title="Manufacturer DIDs (Mode 22)" hint="UDS ReadDataByIdentifier, from this vehicle's DID map." items={dids} selected={mode22Set} onToggle={toggleDid} enabled={draft.mode22_enabled} onEnabled={(value) => setDraft({ ...draft, mode22_enabled: value })} />
      </div>

      {draft.mode22_enabled && draft.mode22.length > 0 && <p className="text-sm text-warn">This profile enters the vehicle's configured diagnostic session. On vehicles where Mode 22 silences Mode 01, OBD PID values pause while DIDs own the bus — untick <em>Manufacturer DIDs (Mode 22)</em> to keep Mode 01 continuous for this capture.</p>}
      {warnings.length > 0 && <div className="rounded border border-warn/50 p-2 text-sm text-warn">{warnings.map((warning) => <div key={warning.model}>{warning.model}: missing {warning.missing_channels.join(", ")}</div>)}</div>}
      <div className="flex flex-wrap gap-2">
        <button className={buttonClass} type="button" disabled={!token || !!busy} onClick={() => save(false)}>Save</button>
        <button className={buttonClass} type="button" disabled={!token || !!busy || draft.name === originalName} onClick={() => save(true)}>Save as</button>
        <button className={dangerButtonClass} type="button" disabled={!token || draft.name === "default" || active?.name === draft.name || !!busy} onClick={() => run("capture profile delete", async () => {
          await deleteCaptureProfile(token, draft.name);
          setSelectedName("default");
        }, { then: refresh })}>Delete</button>
      </div>
    </section>
  );
}

function ChannelPicker({
  title, items, selected, onToggle, weights, onWeight, enabled, onEnabled, hint,
}: {
  title: string;
  items: string[];
  selected: Set<string>;
  onToggle: (item: string) => void;
  weights?: Record<string, number>;
  onWeight?: (item: string, weight: number) => void;
  /** Whole-service switch. Disabling greys the list but keeps the ticks, so
   *  re-enabling doesn't mean rebuilding the selection from scratch. */
  enabled: boolean;
  onEnabled: (value: boolean) => void;
  hint?: string;
}) {
  return <fieldset className={`max-h-80 overflow-y-auto rounded border border-border p-2 ${enabled ? "" : "opacity-50"}`}>
    <legend className="px-1 text-sm font-semibold">
      <label className="flex items-center gap-2">
        <input aria-label={`enable ${title}`} type="checkbox" checked={enabled} onChange={(event) => onEnabled(event.target.checked)} />
        {title}
      </label>
    </legend>
    {hint && <p className="mt-0 mb-1 px-1 text-[0.72rem] text-fg-dim">{hint}</p>}
    {items.map((item) => <label key={item} className="flex min-h-8 items-center gap-2 border-b border-border/40 text-sm">
      <input type="checkbox" checked={selected.has(item)} disabled={!enabled} onChange={() => onToggle(item)} />
      <span className="flex-1">{item}</span>
      {selected.has(item) && weights && onWeight && <input aria-label={`${item} rate weight`} className={`${inputClass} w-20`} type="number" min="0.1" step="0.1" disabled={!enabled} value={weights[item] ?? 1} onChange={(event) => onWeight(item, Number(event.target.value))} />}
    </label>)}
  </fieldset>;
}
